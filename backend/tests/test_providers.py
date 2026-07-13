"""Tests for dynamic LLM providers, API key fallback rotation, and model overrides."""

from contextlib import asynccontextmanager
import json
from unittest.mock import patch

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.settings import Setting
from app.utils.secrets import decrypt_value, encrypt_value, is_masked
from app.core.api_manager import (
    load_secrets_from_db,
    setup_api_manager_monkeypatch,
    _active_key_index,
    _provider_keys,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def test_engine():
    eng = create_async_engine(
        TEST_DB_URL, echo=False, future=True, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def test_session(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def patch_session_factory(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    
    @asynccontextmanager
    async def mock_session_factory():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    with patch("app.database.async_session_factory", new=mock_session_factory):
        yield factory


@pytest_asyncio.fixture
async def test_client(test_session):
    async def _override():
        yield test_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


class TestLLMProviders:
    @pytest.mark.asyncio
    async def test_provider_endpoints(self, test_client: AsyncClient, test_session: AsyncSession):
        # 1. Fetch initial providers (should trigger migration automatically)
        resp = await test_client.get("/api/settings/llm/providers")
        assert resp.status_code == 200
        providers = resp.json()
        assert len(providers) >= 6
        gemini = next(p for p in providers if p["id"] == "gemini")
        assert gemini["label"] == "Gemini"

        # 2. Add custom provider
        new_provider = {
            "id": "together-deepseek",
            "type": "custom_openai",
            "label": "Together DeepSeek",
            "api_key": "sk-together-12345",
            "fallback_api_keys": ["sk-together-fallback-1", "sk-together-fallback-2"],
            "base_url": "https://api.together.xyz/v1",
            "concurrency": 3,
            "models": [
                {"model_id": "deepseek-coder", "timeout": 30}
            ]
        }
        updated_list = providers + [new_provider]

        resp = await test_client.put("/api/settings/llm/providers", json=updated_list)
        assert resp.status_code == 200
        body = resp.json()

        custom_prov = next(p for p in body if p["id"] == "together-deepseek")
        assert custom_prov["label"] == "Together DeepSeek"
        assert custom_prov["concurrency"] == 3
        assert is_masked(custom_prov["api_key"])
        assert len(custom_prov["fallback_api_keys"]) == 2
        assert is_masked(custom_prov["fallback_api_keys"][0])

        # 3. Verify in DB it is encrypted
        from sqlalchemy import select
        stmt = select(Setting).where(Setting.key == "llm_providers")
        row = (await test_session.execute(stmt)).scalar_one()
        raw_providers = json.loads(row.value)
        db_custom = next(p for p in raw_providers if p["id"] == "together-deepseek")
        assert db_custom["api_key"] != "sk-together-12345"
        assert decrypt_value(db_custom["api_key"]) == "sk-together-12345"
        assert decrypt_value(db_custom["fallback_api_keys"][0]) == "sk-together-fallback-1"
        assert db_custom["concurrency"] == 3

        # 4. Re-fetch through the API and confirm concurrency persists round-trip
        resp = await test_client.get("/api/settings/llm/providers")
        refetched = next(p for p in resp.json() if p["id"] == "together-deepseek")
        assert refetched["concurrency"] == 3

    @pytest.mark.asyncio
    async def test_active_llm_endpoints(self, test_client: AsyncClient):
        # 1. Get active (should default to gemini or none depending on legacy)
        resp = await test_client.get("/api/settings/llm/active")
        assert resp.status_code == 200
        active = resp.json()
        assert "provider_id" in active

        # 2. Update active
        resp = await test_client.put("/api/settings/llm/active", json={"provider_id": "openai", "model_id": "gpt-4o"})
        assert resp.status_code == 200
        assert resp.json()["provider_id"] == "openai"
        assert resp.json()["model_id"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_api_manager_key_rotation(self, test_client: AsyncClient, test_session: AsyncSession):
        # 1. Save provider with fallbacks
        providers = [
            {
                "id": "test-rotation",
                "type": "openai",
                "label": "Test Rotation",
                "api_key": encrypt_value("key-primary"),
                "fallback_api_keys": [encrypt_value("key-fallback-1"), encrypt_value("key-fallback-2")],
                "base_url": "https://api.openai.com/v1",
                "models": [{"model_id": "gpt-4"}]
            }
        ]
        test_session.add(Setting(key="llm_providers", value=json.dumps(providers), category="llm"))
        await test_session.commit()

        # Update cache in api_manager
        await load_secrets_from_db()
        setup_api_manager_monkeypatch()

        # Assert key configurations are cached correctly
        assert _provider_keys["test-rotation"] == ["key-primary", "key-fallback-1", "key-fallback-2"]
        assert _active_key_index["test-rotation"] == 0

        # Mock outbound HTTP requests to fail for primary key, but succeed for fallback-1
        calls = []
        from app.core.api_manager import _orig_async_client_send as real_orig_send
        async def mock_send(client_self, request, *args, **kwargs):
            if "testserver" in str(request.url):
                return await real_orig_send(client_self, request, *args, **kwargs)

            # Check header
            auth_header = request.headers.get("Authorization", "")
            calls.append(auth_header)

            if "key-primary" in auth_header:
                # Primary fails with rate limit
                return httpx.Response(429, json={"error": "Rate limit exceeded"}, request=request)
            elif "key-fallback-1" in auth_header:
                # Fallback succeeds
                return httpx.Response(200, json={"data": [{"id": "gpt-4"}]}, request=request)
            return httpx.Response(500, request=request)

        # No-op the backoff sleeps so the test does not wait real seconds, and
        # disable the LLM response cache so the 429s are not short-circuited.
        import app.core.api_manager as am
        import app.core.llm_cache as llm_cache

        async def _noop_async(_s: float) -> None:
            return None

        with patch("app.core.api_manager._orig_async_client_send", new=mock_send), \
             patch.object(am, "_sleep_async", _noop_async), \
             patch.object(llm_cache, "_CACHE_ENABLED", False):
            # Query models for this provider
            resp = await test_client.post(
                "/api/settings/llm/providers/fetch-models",
                json={
                    "provider_id": "test-rotation",
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "secret:provider_test-rotation_key_0_api_key"
                }
            )
            assert resp.status_code == 200
            assert resp.json() == ["gpt-4"]

            # Call sequence: primary (429) -> backoff retries on primary (all 429)
            # -> rotation to fallback-1 (200). The LAST call must be the fallback.
            assert "Bearer key-primary" in calls[0]
            assert "Bearer key-fallback-1" in calls[-1]

            # Check cached active key index is updated to 1
            assert _active_key_index["test-rotation"] == 1

    @pytest.mark.asyncio
    async def test_custom_anthropic_provider_and_persistence(self, test_client: AsyncClient, test_session: AsyncSession):
        # 1. Clear database providers first to test merging default providers on startup
        from sqlalchemy import delete
        await test_session.execute(delete(Setting).where(Setting.key == "llm_providers"))
        await test_session.commit()

        # 2. Querying providers should trigger auto-merging of default providers
        resp = await test_client.get("/api/settings/llm/providers")
        assert resp.status_code == 200
        providers = resp.json()
        assert len(providers) >= 6
        assert any(p["id"] == "claude" for p in providers)

        # 3. Add custom Anthropic provider
        custom_anthropic = {
            "id": "custom-claude-endpoint",
            "type": "custom_anthropic",
            "label": "My Custom Claude",
            "api_key": "sk-anthropic-custom-key-123",
            "fallback_api_keys": ["sk-anthropic-fallback-key"],
            "base_url": "https://custom.anthropic.api/v1",
            "models": [{"model_id": "claude-custom-1"}]
        }
        updated_list = providers + [custom_anthropic]
        resp = await test_client.put("/api/settings/llm/providers", json=updated_list)
        assert resp.status_code == 200
        body = resp.json()
        assert any(p["id"] == "custom-claude-endpoint" for p in body)

        # 4. Clear/Restart simulation: call init_llm_providers_if_missing again to verify custom provider is retained
        from app.routes.settings import init_llm_providers_if_missing
        await init_llm_providers_if_missing(test_session)

        # Query again, custom provider should still exist! (i.e. settings retain across restarts)
        resp = await test_client.get("/api/settings/llm/providers")
        assert resp.status_code == 200
        providers_after_restart = resp.json()
        assert any(p["id"] == "custom-claude-endpoint" for p in providers_after_restart)

    @pytest.mark.asyncio
    async def test_masked_key_not_double_encrypted_on_resave(
        self, test_client: AsyncClient, test_session: AsyncSession
    ):
        """Re-saving a provider whose key comes back MASKED must preserve the
        stored ciphertext verbatim - never decrypt-then-re-encrypt.

        Regression: the old save path round-tripped every existing key through
        decrypt_value -> encrypt_value. When decrypt_value fail-softs (key
        mismatch) it returns the ciphertext unchanged, so re-encrypting wrapped
        it a second time. The double-wrapped token then decrypts (once) to
        another Fernet token instead of the real secret -> provider HTTP 400.
        """
        from sqlalchemy import select, delete

        await test_session.execute(delete(Setting).where(Setting.key == "llm_providers"))
        await test_session.commit()

        real_key = "AIzaSyARealLookingGeminiKey-1234567890x"
        provider = {
            "id": "mask-test",
            "type": "gemini",
            "label": "Mask Test",
            "api_key": real_key,
            "fallback_api_keys": [],
            "base_url": "https://generativelanguage.googleapis.com",
            "models": [{"model_id": "gemini-3-flash-preview"}],
        }

        # 1. First save: real plaintext -> encrypted once at rest.
        resp = await test_client.put("/api/settings/llm/providers", json=[provider])
        assert resp.status_code == 200

        async def _stored_cipher() -> str:
            row = (
                await test_session.execute(
                    select(Setting).where(Setting.key == "llm_providers")
                )
            ).scalar_one()
            stored = next(p for p in json.loads(row.value) if p["id"] == "mask-test")
            return stored["api_key"]

        cipher_after_first = await _stored_cipher()
        # Encrypted (not plaintext) and decrypts to the original.
        assert cipher_after_first != real_key
        assert decrypt_value(cipher_after_first) == real_key

        # 2. UI re-fetches: key is returned MASKED.
        resp = await test_client.get("/api/settings/llm/providers")
        masked = next(p for p in resp.json() if p["id"] == "mask-test")["api_key"]
        assert is_masked(masked)

        # 3. Re-save with the masked value (e.g. user changed only the label).
        resaved = dict(provider, api_key=masked, label="Renamed")
        resp = await test_client.put("/api/settings/llm/providers", json=[resaved])
        assert resp.status_code == 200

        cipher_after_resave = await _stored_cipher()
        # Ciphertext is byte-identical: not re-encrypted, not double-wrapped.
        assert cipher_after_resave == cipher_after_first
        # Still decrypts to the real key in ONE pass (no nested token).
        decrypted = decrypt_value(cipher_after_resave)
        assert decrypted == real_key
        assert not decrypted.startswith("gAAAAA")

    @pytest.mark.asyncio
    async def test_live_override_swaps_model_and_concurrency(
        self, test_client: AsyncClient, test_session: AsyncSession
    ):
        """POST /llm/live-override installs a live model rewrite + concurrency
        cap, persists the new active model, and writes concurrency back to the
        provider record."""
        import app.core.api_manager as am
        from sqlalchemy import select, delete

        await test_session.execute(delete(Setting).where(Setting.key == "llm_providers"))
        await test_session.execute(delete(Setting).where(Setting.key == "llm_global_active"))
        await test_session.commit()

        provider = {
            "id": "gemini",
            "type": "gemini",
            "label": "Gemini",
            "api_key": "AIzaTestKey-0987654321",
            "fallback_api_keys": [],
            "base_url": "https://generativelanguage.googleapis.com",
            "concurrency": 4,
            "models": [
                {"model_id": "gemini-3-flash-preview"},
                {"model_id": "gemini-2.0-flash"},
            ],
        }
        await test_client.put("/api/settings/llm/providers", json=[provider])
        await test_client.put(
            "/api/settings/llm/active",
            json={"provider_id": "gemini", "model_id": "gemini-3-flash-preview"},
        )

        try:
            resp = await test_client.post(
                "/api/settings/llm/live-override",
                json={
                    "provider_id": "gemini",
                    "old_model": "gemini-3-flash-preview",
                    "new_model": "gemini-2.0-flash",
                    "concurrency": 2,
                    "persist": True,
                },
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["status"] == "applied"
            assert payload["model_swap"] == {
                "from": "gemini-3-flash-preview",
                "to": "gemini-2.0-flash",
            }
            assert payload["concurrency"] == 2

            # Live override registered in the api_manager.
            with am._override_lock:
                assert am._model_overrides["gemini"] == (
                    "gemini-3-flash-preview",
                    "gemini-2.0-flash",
                )
            with am._concurrency_lock:
                assert am._provider_concurrency["gemini"] == 2

            # Active model persisted for future jobs.
            active = await test_client.get("/api/settings/llm/active")
            assert active.json()["model_id"] == "gemini-2.0-flash"

            # Concurrency persisted into the provider record.
            row = (
                await test_session.execute(
                    select(Setting).where(Setting.key == "llm_providers")
                )
            ).scalar_one()
            stored = next(p for p in json.loads(row.value) if p["id"] == "gemini")
            assert stored["concurrency"] == 2
        finally:
            am.clear_model_override("gemini")
            am.set_provider_concurrency("gemini", None)

