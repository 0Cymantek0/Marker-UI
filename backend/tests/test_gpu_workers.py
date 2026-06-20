"""Tests for the multi-GPU worker scaling settings (mode/count + resolved)."""

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.settings import Setting  # noqa: F401
from app.models.schemas import GPUWorkerMode
from app.routes.settings import get_effective_worker_count

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def workers_engine():
    eng = create_async_engine(TEST_DB_URL, echo=False, future=True, connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def workers_session(workers_engine):
    factory = async_sessionmaker(workers_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def workers_client(workers_session):
    async def _override():
        yield workers_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pure resolution logic
# ---------------------------------------------------------------------------


class TestEffectiveWorkerCount:
    def test_auto_uses_detected(self):
        assert get_effective_worker_count(GPUWorkerMode.auto, None, 4) == 4

    def test_manual_clamps_to_detected(self):
        assert get_effective_worker_count(GPUWorkerMode.manual, 8, 3) == 3

    def test_cpu_only_is_one(self):
        assert get_effective_worker_count(GPUWorkerMode.auto, None, 0) == 1


# ---------------------------------------------------------------------------
# Resolved + PUT round-trip endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_defaults_to_auto(workers_client):
    with patch("app.core.gpu.detect_gpus", return_value=2):
        resp = await workers_client.get("/api/settings/gpu-workers/resolved")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "auto"
    assert body["detected"] == 2
    assert body["effective"] == 2


@pytest.mark.asyncio
async def test_put_manual_persists_and_resolves(workers_client, workers_session):
    with patch("app.core.gpu.detect_gpus", return_value=3):
        resp = await workers_client.put(
            "/api/settings/gpu-workers",
            json={"mode": "manual", "manual_count": 2},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "manual"
    assert body["effective"] == 2
    assert body["restart_required"] is True

    # Persisted to the settings store.
    await workers_session.commit()
    from sqlalchemy import select
    mode_row = (
        await workers_session.execute(select(Setting).where(Setting.key == "gpu_worker_mode"))
    ).scalar_one()
    count_row = (
        await workers_session.execute(select(Setting).where(Setting.key == "gpu_worker_count"))
    ).scalar_one()
    assert mode_row.value == "manual"
    assert count_row.value == "2"
    assert mode_row.category == "gpu"


@pytest.mark.asyncio
async def test_put_manual_clamps_oversized_count(workers_client):
    with patch("app.core.gpu.detect_gpus", return_value=2):
        resp = await workers_client.put(
            "/api/settings/gpu-workers",
            json={"mode": "manual", "manual_count": 99},
        )
    assert resp.status_code == 200
    # Clamped to the detected GPU count, never more workers than GPUs.
    assert resp.json()["effective"] == 2


@pytest.mark.asyncio
async def test_resolved_reflects_saved_manual_setting(workers_client, workers_session):
    workers_session.add(Setting(key="gpu_worker_mode", value="manual", category="gpu"))
    workers_session.add(Setting(key="gpu_worker_count", value="1", category="gpu"))
    await workers_session.commit()

    with patch("app.core.gpu.detect_gpus", return_value=4):
        resp = await workers_client.get("/api/settings/gpu-workers/resolved")
    body = resp.json()
    assert body["mode"] == "manual"
    assert body["effective"] == 1


@pytest.mark.asyncio
async def test_put_auto_ignores_manual_count(workers_client):
    with patch("app.core.gpu.detect_gpus", return_value=4):
        resp = await workers_client.put(
            "/api/settings/gpu-workers",
            json={"mode": "auto", "manual_count": 1},
        )
    assert resp.json()["effective"] == 4
