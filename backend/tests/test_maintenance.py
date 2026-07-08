"""Tests for system maintenance endpoints (/self-heal and /reset)."""

import json
from pathlib import Path
from unittest.mock import patch
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.settings import Setting
from app.services.model_tracker import tracker

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


@pytest.fixture
def mock_dirs(tmp_path):
    upload_dir = tmp_path / "uploads"
    output_dir = tmp_path / "output"
    upload_dir.mkdir()
    output_dir.mkdir()
    
    with patch("app.core.config.UPLOAD_DIR", upload_dir), \
         patch("app.core.config.OUTPUT_DIR", output_dir):
        yield upload_dir, output_dir


@pytest.fixture
def mock_checkpoints(tmp_path):
    # Create temp folders for checkpoints
    cp_paths = {}
    checkpoints = ["layout_cp", "rec_cp", "table_cp", "det_cp", "ocr_cp"]
    for cp in checkpoints:
        path = tmp_path / cp
        path.mkdir()
        cp_paths[cp] = path
        
    def get_local_path_mock(cp):
        return str(tmp_path / cp)
        
    with patch("surya.settings.settings.LAYOUT_MODEL_CHECKPOINT", "layout_cp"), \
         patch("surya.settings.settings.RECOGNITION_MODEL_CHECKPOINT", "rec_cp"), \
         patch("surya.settings.settings.TABLE_REC_MODEL_CHECKPOINT", "table_cp"), \
         patch("surya.settings.settings.DETECTOR_MODEL_CHECKPOINT", "det_cp"), \
         patch("surya.settings.settings.OCR_ERROR_MODEL_CHECKPOINT", "ocr_cp"), \
         patch("surya.common.s3.S3DownloaderMixin.get_local_path", side_effect=get_local_path_mock):
        yield cp_paths


def make_healthy(cp_path: Path):
    manifest = {"files": ["model.safetensors", "config.json"]}
    with open(cp_path / "manifest.json", "w") as f:
        json.dump(manifest, f)
    for f_name in manifest["files"]:
        with open(cp_path / f_name, "w") as f:
            f.write("some data")


# ===========================================================================
# /self-heal tests
# ===========================================================================

@pytest.mark.asyncio
async def test_self_heal_all_healthy(test_client: AsyncClient, mock_checkpoints):
    # Mark all checkpoints as healthy
    for path in mock_checkpoints.values():
        make_healthy(path)

    # Initialize tracker as initialized
    tracker.set_initialized(True)

    with patch("app.services.model_tracker.trigger_retry") as mock_retry:
        resp = await test_client.post("/api/models/self-heal")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["healed_count"] == 0
        assert data["issues"] == []
        mock_retry.assert_called_once()
        # Verify tracker was reset (so tracker.initialized is False)
        assert tracker.get_status_dict()["initialized"] is False


@pytest.mark.asyncio
async def test_self_heal_some_corrupt(test_client: AsyncClient, mock_checkpoints):
    # layout: missing manifest
    # rec: missing file
    # others: healthy
    make_healthy(mock_checkpoints["rec_cp"])
    # Delete a file in rec to make it corrupt
    (mock_checkpoints["rec_cp"] / "config.json").unlink()

    make_healthy(mock_checkpoints["table_cp"])
    make_healthy(mock_checkpoints["det_cp"])
    make_healthy(mock_checkpoints["ocr_cp"])

    # Initialize tracker
    tracker.set_initialized(True)

    with patch("app.services.model_tracker.trigger_retry") as mock_retry:
        resp = await test_client.post("/api/models/self-heal")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["healed_count"] == 2  # layout & rec
        assert len(data["issues"]) == 2
        assert any("layout" in issue for issue in data["issues"])
        assert any("text_recognition" in issue for issue in data["issues"])
        mock_retry.assert_called_once()
        assert tracker.get_status_dict()["initialized"] is False
        
        # Verify corrupt directories were deleted
        assert not mock_checkpoints["layout_cp"].exists()
        assert not mock_checkpoints["rec_cp"].exists()
        # Healthy ones remain
        assert mock_checkpoints["table_cp"].exists()


# ===========================================================================
# /reset tests
# ===========================================================================

@pytest.mark.asyncio
async def test_reset_without_user_data(test_client: AsyncClient, mock_checkpoints, mock_dirs, test_session: AsyncSession):
    # Set up models folders
    for path in mock_checkpoints.values():
        make_healthy(path)
        
    # Write some user data & files
    upload_dir, output_dir = mock_dirs
    with open(upload_dir / "user_uploaded.pdf", "w") as f:
        f.write("user data")
    with open(output_dir / "converted_output.md", "w") as f:
        f.write("output markdown")

    # Add a setting in the DB
    test_session.add(Setting(key="some_user_setting", value="hello", category="general"))
    await test_session.commit()

    tracker.set_initialized(True)

    with patch("app.services.model_tracker.trigger_retry") as mock_retry:
        resp = await test_client.post("/api/models/reset?delete_user_data=false")
        assert resp.status_code == 200
        mock_retry.assert_called_once()
    data = resp.json()
    assert data["success"] is True
    assert len(data["deleted_models"]) == 5
    assert data["user_data_reset"] is False

    # Models folders are deleted
    for path in mock_checkpoints.values():
        assert not path.exists()

    # User uploads and outputs are NOT deleted
    assert (upload_dir / "user_uploaded.pdf").exists()
    assert (output_dir / "converted_output.md").exists()

    # DB settings are NOT cleared
    stmt = select(Setting).where(Setting.key == "some_user_setting")
    res = await test_session.execute(stmt)
    assert res.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_reset_with_user_data(test_client: AsyncClient, mock_checkpoints, mock_dirs, test_session: AsyncSession):
    # Set up models folders
    for path in mock_checkpoints.values():
        make_healthy(path)
        
    # Write some user data & files
    upload_dir, output_dir = mock_dirs
    with open(upload_dir / "user_uploaded.pdf", "w") as f:
        f.write("user data")
    with open(output_dir / "converted_output.md", "w") as f:
        f.write("output markdown")

    # Add a setting in the DB
    test_session.add(Setting(key="some_user_setting", value="hello", category="general"))
    await test_session.commit()

    tracker.set_initialized(True)

    # We need to mock load_secrets_from_db because it would run async_session_factory
    # which is also patched, so it should run fine, but mocking it is safer.
    with patch("app.core.api_manager.load_secrets_from_db") as mock_secrets, \
         patch("app.services.model_tracker.trigger_retry") as mock_retry:
        resp = await test_client.post("/api/models/reset?delete_user_data=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["deleted_models"]) == 5
        assert data["user_data_reset"] is True
        mock_secrets.assert_called_once()
        mock_retry.assert_called_once()


    # Models folders are deleted
    for path in mock_checkpoints.values():
        assert not path.exists()

    # User uploads and outputs are deleted
    assert not (upload_dir / "user_uploaded.pdf").exists()
    assert not (output_dir / "converted_output.md").exists()

    # DB settings table was dropped and re-created, so 'some_user_setting' is gone
    # but default providers settings are reloaded
    test_session.expire_all()
    stmt = select(Setting).where(Setting.key == "some_user_setting")
    res = await test_session.execute(stmt)
    assert res.scalar_one_or_none() is None

    # Verify standard LLM settings exist (from init_llm_providers_if_missing)
    stmt = select(Setting).where(Setting.key == "llm_providers")
    res = await test_session.execute(stmt)
    assert res.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_tracker_status_during_download_after_reset(test_client: AsyncClient, mock_checkpoints, mock_dirs, test_session: AsyncSession):
    # Set up models folders to be empty/not healthy so they need download
    for path in mock_checkpoints.values():
        if (path / "manifest.json").exists():
            (path / "manifest.json").unlink()
            
    # Reset tracker
    tracker.reset()
    
    # Verify check_models_downloaded is False
    from app.services.model_tracker import check_models_downloaded
    assert check_models_downloaded() is False
    
    # Simulate loading starts but models not downloaded
    tracker.set_loading(True)
    
    # Status should still be loading because any_downloading is False and check_models_downloaded is False
    # Wait, check if the status check prioritizes loading/downloading properly
    # If loading is True but any_downloading is False, get_status_dict overall status is loading.
    # But wait, in initialize(), we only call set_loading(True) if check_models_downloaded() is True!
    # So in a real scenario, tracker.set_loading(True) wouldn't even be called!
    # But if it is set_loading(True) and layout model starts downloading:
    tracker.start_model_download("layout")
    
    # Status should now be downloading since layout is downloading
    assert tracker.get_status_dict()["overall"]["status"] == "downloading"

