"""Tests for the GPU acceleration settings endpoints."""

import pytest
from httpx import AsyncClient
from unittest.mock import MagicMock, PropertyMock, patch
from app.services.gpu_service import gpu_service
from app.models.settings import Setting
from sqlalchemy import select

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

pytest_plugins = ("tests.test_settings",)


@pytest.mark.asyncio
async def test_get_gpu_status(settings_client: AsyncClient):
    """Verify that GET /gpu/status returns correct service state."""
    mock_state = {
        "status": "not_installed",
        "progress": 0,
        "logs": ["Line 1", "Line 2"],
        "error_message": None,
        "cuda_available": False,
    }
    with patch("app.services.gpu_service.GPUService.status_dict", new_callable=PropertyMock) as mock_status:
        mock_status.return_value = mock_state
        resp = await settings_client.get("/api/settings/gpu/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "not_installed"
        assert body["progress"] == 0
        assert body["logs"] == ["Line 1", "Line 2"]
        assert body["cuda_available"] is False


@pytest.mark.asyncio
async def test_install_gpu(settings_client: AsyncClient):
    """Verify that POST /gpu/install triggers background installation."""
    mock_state = {
        "status": "installing",
        "progress": 10,
        "logs": ["Starting..."],
        "error_message": None,
        "cuda_available": False,
    }
    with patch.object(gpu_service, "start_install") as mock_start, \
         patch("app.services.gpu_service.GPUService.status_dict", new_callable=PropertyMock) as mock_status:
        mock_status.return_value = mock_state
        resp = await settings_client.post("/api/settings/gpu/install")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "installing"
        assert body["progress"] == 10
        mock_start.assert_called_once()


@pytest.mark.asyncio
async def test_toggle_gpu(settings_client: AsyncClient, settings_session):
    """Verify that POST /gpu/toggle correctly updates the setting DB record."""
    # Toggle on
    resp = await settings_client.post(
        "/api/settings/gpu/toggle",
        json={"enabled": True}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    await settings_session.commit()
    stmt = select(Setting).where(Setting.key == "gpu_acceleration_enabled")
    res = await settings_session.execute(stmt)
    row = res.scalar_one()
    assert row.value == "true"
    assert row.category == "gpu"

    # Toggle off
    resp = await settings_client.post(
        "/api/settings/gpu/toggle",
        json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    await settings_session.commit()
    settings_session.expire_all()
    res = await settings_session.execute(stmt)
    row = res.scalar_one()
    assert row.value == "false"


@pytest.mark.asyncio
async def test_startup_gpu_install_trigger(settings_session):
    """Verify that lifespan startup triggers GPU installation if enabled but not ready."""
    settings_session.add(Setting(key="gpu_acceleration_enabled", value="true", category="gpu"))
    await settings_session.commit()

    with patch("app.database.async_session_factory") as mock_factory, \
         patch("app.services.gpu_service.GPUService.status_dict", new_callable=PropertyMock) as mock_status, \
         patch("app.services.gpu_service.gpu_service.start_install") as mock_start, \
         patch("app.main._load_models_background"), \
         patch("app.main.create_tables"), \
         patch("app.core.api_manager.load_secrets_from_db"):

        class AsyncContextMock:
            async def __aenter__(self):
                return settings_session
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        mock_factory.return_value = AsyncContextMock()
        mock_status.return_value = {"cuda_available": False}

        from app.main import lifespan
        from fastapi import FastAPI

        app = FastAPI()
        async with lifespan(app):
            pass

        mock_start.assert_called_once()


def test_marker_service_waits_for_gpu_install():
    """Verify that MarkerService.initialize() blocks/waits while GPU status is 'installing'."""
    from app.services.marker_service import MarkerService
    import sys

    marker_service = MagicMock()
    marker_service._initialized = False

    # Standard Mocking for models
    mock_models = MagicMock()
    mock_models.create_model_dict.return_value = {"fake": "model"}

    with patch.dict(sys.modules, {"marker": MagicMock(), "marker.models": mock_models}), \
         patch("app.services.gpu_service.GPUService.status_dict", new_callable=PropertyMock) as mock_status, \
         patch("app.services.marker_service._import_marker") as mock_import, \
         patch("time.sleep") as mock_sleep:

        mock_status.side_effect = [
            {"status": "installing"},
            {"status": "ready"}
        ]

        # Use the actual initialize implementation from class, bound to our mocked marker_service
        MarkerService.initialize(marker_service)

        # Check that we waited (time.sleep called) and then imported
        mock_sleep.assert_called_once_with(5)
        mock_import.assert_called_once()
        assert marker_service._initialized is True


@pytest.mark.asyncio
async def test_gpu_install_uses_cu126_index(settings_client: AsyncClient, settings_session):
    """GPU install command must use cu126 wheel index (cu121 tops out at torch 2.5.1,
    which is incompatible with marker-pdf's torch>=2.7.0 requirement)."""
    mock_popen = MagicMock()
    mock_popen.wait.return_value = 0
    mock_popen.stdout = []

    mock_run = MagicMock()
    mock_run.return_value = MagicMock(stdout="True\n", stderr="")

    with patch("subprocess.Popen", return_value=mock_popen) as mock_popen_ctor, \
         patch("subprocess.run", return_value=mock_run), \
         patch("app.services.gpu_service.GPUService._check_cuda_available", return_value=True):

        gpu_service.start_install()
        if gpu_service._thread:
            gpu_service._thread.join(timeout=5.0)

        assert mock_popen_ctor.called
        install_cmd = mock_popen_ctor.call_args[0][0]

        # Must include both torch and torchvision (paired CUDA wheels)
        assert "torch" in install_cmd
        assert "torchvision" in install_cmd

        # Must use cu126 index — cu121 caps at torch 2.5.1, breaks marker-pdf>=2.7.0
        assert "https://download.pytorch.org/whl/cu126" in install_cmd
        assert "cu121" not in " ".join(install_cmd)


@pytest.mark.asyncio
async def test_gpu_install_failure_disables_setting(settings_client: AsyncClient, settings_session):
    """Verify that if GPU installation fails or CUDA verification fails, the database setting is disabled."""
    # Ensure starting clean and set setting to true
    settings_session.add(Setting(key="gpu_acceleration_enabled", value="true", category="gpu"))
    await settings_session.commit()

    mock_popen = MagicMock()
    mock_popen.wait.return_value = 0  # pip succeeds
    mock_popen.stdout = []

    mock_run = MagicMock()
    mock_run.return_value = MagicMock(stdout="False\n", stderr="Verification failed")  # verification fails

    class AsyncContextMock:
        async def __aenter__(self):
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
            self.session = async_sessionmaker(settings_session.bind, class_=AsyncSession, expire_on_commit=False)()
            return self.session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            await self.session.close()

    with patch("subprocess.Popen", return_value=mock_popen), \
         patch("subprocess.run", return_value=mock_run), \
         patch("app.services.gpu_service.async_session_factory", return_value=AsyncContextMock()), \
         patch("app.services.gpu_service.GPUService._check_cuda_available", return_value=False):

        gpu_service.start_install()

        # Wait for the background thread to finish
        if gpu_service._thread:
            gpu_service._thread.join(timeout=5.0)

        # Allow threading to execute the _disable_gpu_setting_in_db thread
        import asyncio
        await asyncio.sleep(0.5)

        # Verify that setting was changed to false in DB
        async with async_sessionmaker(settings_session.bind, class_=AsyncSession, expire_on_commit=False)() as verify_session:
            stmt = select(Setting).where(Setting.key == "gpu_acceleration_enabled")
            res = await verify_session.execute(stmt)
            row = res.scalar_one()
            assert row.value == "false"
