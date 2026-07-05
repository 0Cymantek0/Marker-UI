"""Tests for POST /api/convert/{job_id}/retry: cross-provider re-run endpoint."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.job import ConversionJob
from app.models.settings import Setting
from app.routes import settings as settings_route
from app.services.audit import record_audit_event

# Encryption key must be set before importing app.crypto
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS1mb3ItdW5pdHRlc3Q=")

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


@pytest_asyncio.fixture
async def seeded_providers(test_session):
    """Insert two LLM providers (gemini, openai) so retry validation can pass."""
    providers = [
        {
            "id": "gemini",
            "type": "gemini",
            "name": "Google Gemini",
            "api_key": "",
            "base_url": "https://generativelanguage.googleapis.com",
            "models": [{"model_id": "gemini-flash", "name": "Gemini Flash"}],
        },
        {
            "id": "openai",
            "type": "openai",
            "name": "OpenAI",
            "api_key": "",
            "base_url": "https://api.openai.com",
            "models": [{"model_id": "gpt-4", "name": "GPT-4"}],
        },
    ]
    test_session.add(Setting(key="llm_providers", value=json.dumps(providers)))
    test_session.add(
        Setting(key="llm_global_active", value=json.dumps({"provider_id": "gemini", "model_id": "gemini-flash"}))
    )
    await test_session.flush()
    # Repopulate the api_manager in-memory host map so _identify_provider works.
    await settings_route.init_llm_providers_if_missing(test_session)
    yield


@pytest_asyncio.fixture
async def terminal_job(test_session, tmp_path):
    """A completed job with a stored source file on disk."""
    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF-1.4\n%test\n")
    cfg = {
        "output_format": "markdown",
        "original_name": "input.pdf",
        "local_filepath": str(source),
        "use_llm": True,
        "llm_provider": "gemini",
        "llm_model": "gemini-flash",
    }
    job = ConversionJob(
        id="job-terminal-1",
        filename="input.pdf",
        original_name="input.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        config_json=json.dumps(cfg),
    )
    test_session.add(job)
    await test_session.flush()
    yield job


# Stubs so submit_job does not actually start a converter process.
class _FakeTaskManager:
    def submit_job(self, *args, **kwargs):
        return None

    async def enqueue_durable_job(self, db, **kwargs):
        return None


@pytest_asyncio.fixture
async def patched_task_manager():
    """Replace the real task_manager so retry_job does not spawn a worker."""
    from app.main import _app_state

    real = _app_state.task_manager
    _app_state.task_manager = _FakeTaskManager()
    yield
    _app_state.task_manager = real


@pytest.mark.asyncio
async def test_retry_creates_new_job_with_overrides(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, terminal_job, patched_task_manager
):
    """A retry with a different provider creates a new pending job whose config
    carries the override, while the original job row is untouched."""
    resp = await test_client.post(
        f"/api/convert/{terminal_job.id}/retry",
        json={"llm_provider": "openai", "llm_model": "gpt-4"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source_job_id"] == terminal_job.id
    assert data["new_job_id"] != terminal_job.id
    assert data["status"] == "pending"

    new_job = (
        await test_session.execute(
            select(ConversionJob).where(ConversionJob.id == data["new_job_id"])
        )
    ).scalar_one()
    assert new_job.status == "pending"
    new_cfg = json.loads(new_job.config_json)
    assert new_cfg["llm_provider"] == "openai"
    assert new_cfg["llm_model"] == "gpt-4"
    assert new_cfg["retried_from"] == terminal_job.id

    # Original untouched.
    await test_session.refresh(terminal_job)
    assert terminal_job.status == "completed"
    orig_cfg = json.loads(terminal_job.config_json)
    assert orig_cfg["llm_provider"] == "gemini"


@pytest.mark.asyncio
async def test_retry_rejects_non_terminal_job(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, patched_task_manager
):
    """A pending/processing job cannot be retried (would race the active run)."""
    source = Path(tempfile.mkdtemp()) / "input.pdf"
    source.write_bytes(b"%PDF-1.4\n%test\n")
    cfg = {"output_format": "markdown", "original_name": "input.pdf", "local_filepath": str(source)}
    job = ConversionJob(
        id="job-running-1",
        filename="input.pdf",
        original_name="input.pdf",
        status="processing",
        input_format="pdf",
        output_format="markdown",
        config_json=json.dumps(cfg),
    )
    test_session.add(job)
    await test_session.flush()

    resp = await test_client.post(f"/api/convert/{job.id}/retry", json={})
    assert resp.status_code == 409
    assert "still running" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_retry_unknown_provider_rejected(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, terminal_job, patched_task_manager
):
    """An unknown provider id is rejected with 400 before any job is created."""
    resp = await test_client.post(
        f"/api/convert/{terminal_job.id}/retry",
        json={"llm_provider": "no-such-provider"},
    )
    assert resp.status_code == 400
    assert "no-such-provider" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_unknown_model_rejected(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, terminal_job, patched_task_manager
):
    """A model not configured for the requested provider is rejected."""
    resp = await test_client.post(
        f"/api/convert/{terminal_job.id}/retry",
        json={"llm_provider": "gemini", "llm_model": "no-such-model"},
    )
    assert resp.status_code == 400
    assert "no-such-model" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_retry_missing_source_file_returns_409(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, patched_task_manager, tmp_path
):
    """A terminal job whose source file was deleted returns 409."""
    cfg = {
        "output_format": "markdown",
        "original_name": "gone.pdf",
        "local_filepath": str(tmp_path / "does-not-exist.pdf"),
    }
    job = ConversionJob(
        id="job-no-source",
        filename="gone.pdf",
        original_name="gone.pdf",
        status="failed",
        input_format="pdf",
        output_format="markdown",
        config_json=json.dumps(cfg),
    )
    test_session.add(job)
    await test_session.flush()

    resp = await test_client.post(f"/api/convert/{job.id}/retry", json={})
    assert resp.status_code == 409
    assert "source file" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_retry_unknown_job_returns_404(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, patched_task_manager
):
    """A retry on a non-existent job id returns 404."""
    resp = await test_client.post("/api/convert/does-not-exist/retry", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_without_overrides_reuses_config(
    test_client: AsyncClient, test_session: AsyncSession, seeded_providers, terminal_job, patched_task_manager
):
    """A retry with an empty body reuses the original job's config unchanged."""
    resp = await test_client.post(f"/api/convert/{terminal_job.id}/retry", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    new_job = (
        await test_session.execute(
            select(ConversionJob).where(ConversionJob.id == data["new_job_id"])
        )
    ).scalar_one()
    new_cfg = json.loads(new_job.config_json)
    assert new_cfg["llm_provider"] == "gemini"
    assert new_cfg["llm_model"] == "gemini-flash"
