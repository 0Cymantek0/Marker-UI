"""Tests for settings CRUD, encryption, masking, and SSRF protection."""

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.settings import Setting  # noqa: F401
from app.routes.settings import ALLOWED_LLM_HOSTS, validate_llm_url
from app.utils.secrets import decrypt_value, encrypt_value, is_masked, mask_value

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Test DB fixtures (separate from conftest to keep isolation)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def settings_engine():
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
async def settings_session(settings_engine):
    factory = async_sessionmaker(settings_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def settings_client(settings_session):
    async def _override():
        yield settings_session

    app.dependency_overrides[get_db] = _override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


# ===========================================================================
# SSRF - validate_llm_url
# ===========================================================================


class TestValidateLLMUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.openai.com/v1",
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "https://api.anthropic.com/v1/messages",
            "https://generativelanguage.googleapis.com/v1beta/models",
            "https://us-central1-aiplatform.googleapis.com/v1/projects/xyz",
            "http://[::1]:8080",
        ],
    )
    def test_allowed_hosts_pass(self, url: str):
        assert validate_llm_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.com/steal",
            "http://192.168.1.1/scan",
            "http://10.0.0.1/internal",
            "https://attacker.example.com",
            "http://169.254.169.254/metadata",
        ],
    )
    def test_rejected_hosts_raise(self, url: str):
        with pytest.raises(ValueError, match="not in the allowed list"):
            validate_llm_url(url)

    def test_empty_string_passes(self):
        assert validate_llm_url("") == ""

    def test_allowed_llm_hosts_constant(self):
        assert "api.openai.com" in ALLOWED_LLM_HOSTS
        assert "localhost" in ALLOWED_LLM_HOSTS
        assert "127.0.0.1" in ALLOWED_LLM_HOSTS


# ===========================================================================
# Settings CRUD
# ===========================================================================


class TestSettingsCRUD:
    @pytest.mark.asyncio
    async def test_create_and_read_setting(self,settings_client: AsyncClient):
        # Create
        resp = await settings_client.put(
            "/api/settings/",
            json={"key": "model_name", "value": "gpt-4o", "category": "llm"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["key"] == "model_name"
        assert body["value"] == "gpt-4o"
        assert body["category"] == "llm"

        # Read single
        resp = await settings_client.get("/api/settings/key/model_name")
        assert resp.status_code == 200
        assert resp.json()["value"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_list_settings(self, settings_client: AsyncClient):
        await settings_client.put(
            "/api/settings/",
            json={"key": "k1", "value": "v1", "category": "cat_a"},
        )
        await settings_client.put(
            "/api/settings/",
            json={"key": "k2", "value": "v2", "category": "cat_b"},
        )
        resp = await settings_client.get("/api/settings/")
        assert resp.status_code == 200
        data = resp.json()
        assert "cat_a" in data
        assert "cat_b" in data

    @pytest.mark.asyncio
    async def test_update_existing_setting(self, settings_client: AsyncClient):
        await settings_client.put(
            "/api/settings/",
            json={"key": "my_key", "value": "old", "category": "general"},
        )
        resp = await settings_client.put(
            "/api/settings/",
            json={"key": "my_key", "value": "new", "category": "general"},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "new"

    @pytest.mark.asyncio
    async def test_delete_setting(self, settings_client: AsyncClient):
        await settings_client.put(
            "/api/settings/",
            json={"key": "to_delete", "value": "gone", "category": "general"},
        )
        resp = await settings_client.delete("/api/settings/key/to_delete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Confirm gone
        resp = await settings_client.get("/api/settings/key/to_delete")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_nonexistent_setting(self, settings_client: AsyncClient):
        resp = await settings_client.get("/api/settings/key/nonexistent")
        assert resp.status_code == 404


# ===========================================================================
# Encryption & Masking in settings API
# ===========================================================================


class TestSettingsEncryption:
    @pytest.mark.asyncio
    async def test_sensitive_key_is_encrypted_on_save(
        self, settings_client: AsyncClient, settings_session: AsyncSession
    ):
        plain = "sk-test-key-1234567890"
        resp = await settings_client.put(
            "/api/settings/",
            json={"key": "openai_api_key", "value": plain, "category": "llm"},
        )
        assert resp.status_code == 200

        # Response should show masked value
        body = resp.json()
        assert body["key"] == "openai_api_key"
        # Masked value should contain asterisks
        assert "*" in body["value"]
        assert body["value"] != plain

        # Raw DB value should be encrypted (not plaintext, not masked)
        from sqlalchemy import select

        result = await settings_session.execute(
            select(Setting).where(Setting.key == "openai_api_key")
        )
        row = result.scalar_one()
        assert row.value != plain  # encrypted
        assert decrypt_value(row.value) == plain  # can be decrypted back

    @pytest.mark.asyncio
    async def test_non_sensitive_key_not_encrypted(
        self, settings_client: AsyncClient, settings_session: AsyncSession
    ):
        resp = await settings_client.put(
            "/api/settings/",
            json={"key": "model_name", "value": "gpt-4o", "category": "llm"},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "gpt-4o"  # returned as-is

    @pytest.mark.asyncio
    async def test_masked_value_preserved_on_resave(
        self, settings_client: AsyncClient, settings_session: AsyncSession
    ):
        plain = "sk-original-key-12345"
        await settings_client.put(
            "/api/settings/",
            json={"key": "claude_api_key", "value": plain, "category": "llm"},
        )

        from sqlalchemy import select

        await settings_session.commit()
        result = await settings_session.execute(
            select(Setting).where(Setting.key == "claude_api_key")
        )
        encrypted_val = result.scalar_one().value

        masked = mask_value(plain)
        resp = await settings_client.put(
            "/api/settings/",
            json={"key": "claude_api_key", "value": masked, "category": "llm"},
        )
        assert resp.status_code == 200

        await settings_session.commit()
        settings_session.expire_all()
        result = await settings_session.execute(
            select(Setting).where(Setting.key == "claude_api_key")
        )
        row = result.scalar_one()
        assert row.value == encrypted_val

    @pytest.mark.asyncio
    async def test_batch_upsert_encrypts_sensitive_keys(
        self, settings_client: AsyncClient, settings_session: AsyncSession
    ):
        resp = await settings_client.put(
            "/api/settings/batch",
            json={
                "settings": [
                    {"key": "openai_api_key", "value": "sk-batch-test", "category": "llm"},
                    {"key": "timeout", "value": "30", "category": "llm"},
                ]
            },
        )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2

        # openai_api_key should be masked in response
        openai_item = next(i for i in items if i["key"] == "openai_api_key")
        assert "*" in openai_item["value"]

        # timeout should be plaintext
        timeout_item = next(i for i in items if i["key"] == "timeout")
        assert timeout_item["value"] == "30"


# ===========================================================================
# LLM config endpoint
# ===========================================================================


class TestLLMConfigEndpoint:
    @pytest.mark.asyncio
    async def test_get_llm_config_defaults(self, settings_client: AsyncClient):
        resp = await settings_client.get("/api/settings/llm/config")
        assert resp.status_code == 200
        body = resp.json()
        # Should return defaults when nothing saved
        assert body["llm_service"] == "no_llm"
        assert body["timeout"] == 60

    @pytest.mark.asyncio
    async def test_save_and_read_llm_config(self, settings_client: AsyncClient):
        # Save config with an API key
        resp = await settings_client.put(
            "/api/settings/llm/config",
            json={
                "llm_service": "openai",
                "openai_api_key": "sk-llm-test-key-abcdef",
                "openai_model": "gpt-4o-mini",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm_service"] == "openai"
        # API key should be masked in response
        assert "*" in body["openai_api_key"]

        # Read it back - should still be masked
        resp = await settings_client.get("/api/settings/llm/config")
        body = resp.json()
        assert body["llm_service"] == "openai"
        assert "*" in body["openai_api_key"]
        assert body["openai_model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_masked_api_key_preserved_on_resave(self, settings_client: AsyncClient):
        # Save initial config
        await settings_client.put(
            "/api/settings/llm/config",
            json={
                "llm_service": "gemini",
                "gemini_api_key": "AIza-sy-original-key-data",
            },
        )

        # Read back (masked)
        resp = await settings_client.get("/api/settings/llm/config")
        masked_key = resp.json()["gemini_api_key"]
        assert is_masked(masked_key)

        # Re-save with masked value (user changed something else)
        resp = await settings_client.put(
            "/api/settings/llm/config",
            json={
                "llm_service": "gemini",
                "gemini_api_key": masked_key,
                "gemini_model_name": "gemini-2.0-flash",
            },
        )
        assert resp.status_code == 200

        # The masked key should remain masked in response
        assert is_masked(resp.json()["gemini_api_key"])


# ===========================================================================
# LLM Connection Testing Interceptor Integration
# ===========================================================================

class TestLLMConnectionEndpoint:
    @pytest.mark.asyncio
    async def test_connection_with_masked_key(self, settings_client: AsyncClient, settings_session: AsyncSession):
        # 1. Save key to database first so it is in settings table
        # Clear existing
        from sqlalchemy import delete
        await settings_session.execute(delete(Setting).where(Setting.key == "gemini_api_key"))
        settings_session.add(Setting(key="gemini_api_key", value=encrypt_value("real-gemini-super-secret-key"), category="llm"))
        await settings_session.commit()

        # Update cache in api_manager
        from app.core.api_manager import update_secret_cache, setup_api_manager_monkeypatch
        update_secret_cache("gemini_api_key", "real-gemini-super-secret-key")
        setup_api_manager_monkeypatch()

        # 2. Mock the underlying send method to verify placeholder replacement
        import httpx
        from app.core.api_manager import _orig_async_client_send

        mock_response = httpx.Response(200, json={"models": []})
        
        async def mock_send(client_self, request, *args, **kwargs):
            if "testserver" in str(request.url):
                return await _orig_async_client_send(client_self, request, *args, **kwargs)
            # Assert that the request headers contain the real decrypted key, NOT the placeholder or masked key
            assert request.headers["x-goog-api-key"] == "real-gemini-super-secret-key"
            return mock_response

        with patch("app.core.api_manager._orig_async_client_send", new=mock_send):
            from app.utils.secrets import mask_value
            resp = await settings_client.post(
                "/api/settings/llm/test",
                json={
                    "llm_service": "gemini",
                    "gemini_api_key": mask_value("real-gemini-super-secret-key"),
                }
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True

    @pytest.mark.asyncio
    async def test_connection_with_new_plaintext_key(self, settings_client: AsyncClient):
        from app.core.api_manager import setup_api_manager_monkeypatch
        setup_api_manager_monkeypatch()

        import httpx
        from app.core.api_manager import _orig_async_client_send

        mock_response = httpx.Response(200, json={"models": []})
        
        async def mock_send(client_self, request, *args, **kwargs):
            if "testserver" in str(request.url):
                return await _orig_async_client_send(client_self, request, *args, **kwargs)
            # Assert that the request headers contain the new plaintext key
            assert request.headers["x-goog-api-key"] == "new-gemini-plaintext-key"
            return mock_response

        with patch("app.core.api_manager._orig_async_client_send", new=mock_send):
            resp = await settings_client.post(
                "/api/settings/llm/test",
                json={
                    "llm_service": "gemini",
                    "gemini_api_key": "new-gemini-plaintext-key",
                }
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            
            # Verify that the cache was updated with the new plaintext key
            from app.core.api_manager import get_secret
            assert get_secret("gemini_api_key") == "new-gemini-plaintext-key"


# ===========================================================================
# Vision-capable models & vlm_model setting
# ===========================================================================


class TestModelConfigVisionCapable:
    def test_model_config_has_vision_capable_field_default_false(self):
        from app.models.schemas import ModelConfig

        m = ModelConfig(model_id="gpt-5.5")
        assert m.vision_capable is False

    def test_model_config_vision_capable_can_be_set_true(self):
        from app.models.schemas import ModelConfig

        m = ModelConfig(model_id="gpt-5.5", vision_capable=True)
        assert m.vision_capable is True

    @pytest.mark.asyncio
    async def test_get_providers_returns_vision_capable_per_model(
        self, settings_client: AsyncClient
    ):
        resp = await settings_client.get("/api/settings/llm/providers")
        assert resp.status_code == 200
        providers = resp.json()
        assert len(providers) > 0
        for p in providers:
            assert p.get("models"), f"provider {p.get('id')} should seed with models"
            for m in p["models"]:
                assert "vision_capable" in m, (
                    f"model {m.get('model_id')} missing vision_capable key"
                )
                assert m["vision_capable"] is False

    @pytest.mark.asyncio
    async def test_model_config_vision_capable_persists_through_round_trip(
        self, settings_client: AsyncClient
    ):
        # Get initial providers (triggers init on first call)
        resp = await settings_client.get("/api/settings/llm/providers")
        assert resp.status_code == 200
        providers = resp.json()
        assert len(providers) > 0
        assert len(providers[0]["models"]) > 0

        # Mark first model as vision-capable
        providers[0]["models"][0]["vision_capable"] = True

        # PUT back
        resp = await settings_client.put(
            "/api/settings/llm/providers", json=providers
        )
        assert resp.status_code == 200

        # GET again, verify the flag round-tripped
        resp = await settings_client.get("/api/settings/llm/providers")
        assert resp.status_code == 200
        providers_after = resp.json()
        first_model = providers_after[0]["models"][0]
        assert first_model["vision_capable"] is True, (
            "vision_capable=True did not round-trip through providers endpoint"
        )

    @pytest.mark.asyncio
    async def test_init_providers_defaults_vision_capable_false(
        self, settings_session: AsyncSession
    ):
        from app.routes.settings import init_llm_providers_if_missing
        from sqlalchemy import select

        await init_llm_providers_if_missing(settings_session)

        # Read the raw JSON persisted by the init helper
        result = await settings_session.execute(
            select(Setting).where(Setting.key == "llm_providers")
        )
        row = result.scalar_one()
        providers = json.loads(row.value)
        assert len(providers) > 0
        for p in providers:
            for m in p.get("models", []):
                assert m.get("vision_capable") is False, (
                    f"model {m.get('model_id')} in provider {p.get('id')} "
                    "should seed with vision_capable=False"
                )

    @pytest.mark.asyncio
    async def test_vlm_model_setting_round_trip(self, settings_client: AsyncClient):
        # PUT
        resp = await settings_client.put(
            "/api/settings/",
            json={
                "key": "vlm_model",
                "value": "gemma4:12b",
                "category": "vlm",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["key"] == "vlm_model"
        assert body["value"] == "gemma4:12b"
        assert body["category"] == "vlm"

        # GET via grouped list
        resp = await settings_client.get("/api/settings/")
        assert resp.status_code == 200
        grouped = resp.json()
        assert "vlm" in grouped
        vlm_entries = grouped["vlm"]
        match = next((s for s in vlm_entries if s["key"] == "vlm_model"), None)
        assert match is not None
        assert match["value"] == "gemma4:12b"

        # GET via single-key
        resp = await settings_client.get("/api/settings/key/vlm_model")
        assert resp.status_code == 200
        assert resp.json()["value"] == "gemma4:12b"

