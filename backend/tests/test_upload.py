"""Tests for upload endpoint - extension allowlist, size limit, streaming."""

import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app
from app.models.job import ConversionJob  # noqa: F401
from app.models.settings import Setting  # noqa: F401
from app.routes.convert import _assert_safe_source_url

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def upload_engine():
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
async def upload_session(upload_engine):
    factory = async_sessionmaker(upload_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def upload_client(upload_session):
    async def _override():
        yield upload_session

    app.dependency_overrides[get_db] = _override

    mock_task_mgr = MagicMock()
    mock_task_mgr.submit_job = MagicMock()
    
    with patch("app.main._app_state") as mock_state:
        mock_state.marker_service = MagicMock()
        mock_state.task_manager = mock_task_mgr

        with patch("app.services.marker_service.build_marker_options", return_value={}):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as c:
                yield c

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Allowed extensions
# ---------------------------------------------------------------------------


class TestUploadExtensionAllowlist:
    @pytest.mark.parametrize(
        "ext",
        [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp", ".tsv", ".xls", ".msg", ".wav", ".mp4"],
    )
    @pytest.mark.asyncio
    async def test_allowed_extensions_accepted(self, upload_client: AsyncClient, ext: str):
        data = io.BytesIO(b"%PDF-1.4 fake content")
        files = {"file": (f"test{ext}", data, "application/octet-stream")}
        resp = await upload_client.post("/api/convert/upload", files=files)
        assert resp.status_code == 200
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "pending"
        assert body["filename"] == f"test{ext}"

    @pytest.mark.parametrize("ext", [".exe", ".sh", ".py", ".bat", ".cmd", ".js"])
    @pytest.mark.asyncio
    async def test_disallowed_extensions_rejected(self, upload_client: AsyncClient, ext: str):
        data = io.BytesIO(b"malicious payload")
        files = {"file": (f"evil{ext}", data, "application/octet-stream")}
        resp = await upload_client.post("/api/convert/upload", files=files)
        assert resp.status_code == 400
        assert "Unsupported file type" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------


class TestUploadSizeLimit:
    @pytest.mark.asyncio
    async def test_oversized_file_rejected(self, upload_client: AsyncClient):
        with patch("app.routes.convert.MAX_UPLOAD_SIZE", 100):
            data = io.BytesIO(b"A" * 200)
            files = {"file": ("big.pdf", data, "application/pdf")}
            resp = await upload_client.post("/api/convert/upload", files=files)
            assert resp.status_code == 413
            assert "exceeds maximum size" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Streaming / successful upload
# ---------------------------------------------------------------------------


class TestUploadSuccess:
    @pytest.mark.asyncio
    async def test_successful_upload_returns_conversion_response(self, upload_client: AsyncClient):
        data = io.BytesIO(b"%PDF-1.4 test content")
        files = {"file": ("document.pdf", data, "application/pdf")}
        resp = await upload_client.post("/api/convert/upload", files=files)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["output_format"] == "markdown"
        assert body["filename"] == "document.pdf"

    @pytest.mark.asyncio
    async def test_upload_with_output_format(self, upload_client: AsyncClient):
        data = io.BytesIO(b"image data")
        files = {"file": ("photo.png", data, "image/png")}
        resp = await upload_client.post(
            "/api/convert/upload",
            files=files,
            params={"output_format": "json"},
        )
        assert resp.status_code == 200
        assert resp.json()["output_format"] == "json"

    @pytest.mark.asyncio
    async def test_upload_persists_audio_enhancement_config(
        self,
        upload_client: AsyncClient,
        upload_session: AsyncSession,
    ):
        data = io.BytesIO(b"RIFF fake wav")
        files = {"file": ("voice.wav", data, "audio/wav")}
        resp = await upload_client.post(
            "/api/convert/upload",
            files=files,
            params={
                "audio_output_mode": "meeting_notes",
                "audio_model": "base.en",
                "audio_vocabulary": "Marker, LiteParse",
                "audio_context": "project call",
                "audio_low_confidence_threshold": "0.7",
                "audio_word_timestamps": "true",
            },
        )

        assert resp.status_code == 200
        job = await upload_session.get(ConversionJob, resp.json()["job_id"])
        assert job is not None
        config = json.loads(job.config_json)
        assert config["audio_output_mode"] == "meeting_notes"
        assert config["audio_model"] == "base.en"
        assert config["audio_vocabulary"] == "Marker, LiteParse"
        assert config["audio_context"] == "project call"
        assert config["audio_low_confidence_threshold"] == 0.7
        assert config["audio_word_timestamps"] is True

    @pytest.mark.asyncio
    async def test_upload_from_source_url_uses_downloaded_file(self, upload_client: AsyncClient, tmp_path):
        downloaded = tmp_path / "download.pdf"
        downloaded.write_bytes(b"%PDF-1.4")

        async def fake_download(source_url, destination):
            destination.write_bytes(downloaded.read_bytes())
            return "remote.pdf", ".pdf", "https://example.com/docs/remote.pdf"

        with patch("app.routes.convert._download_source_url", side_effect=fake_download):
            resp = await upload_client.post(
                "/api/convert/upload",
                params={"source_url": "https://example.com/docs/remote.pdf"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["filename"] == "remote.pdf"
        assert body["status"] == "pending"

    @pytest.mark.asyncio
    async def test_upload_rejects_multiple_input_sources(self, upload_client: AsyncClient):
        data = io.BytesIO(b"%PDF-1.4")
        files = {"file": ("document.pdf", data, "application/pdf")}
        resp = await upload_client.post(
            "/api/convert/upload",
            files=files,
            params={"source_url": "https://example.com/document.pdf"},
        )

        assert resp.status_code == 400
        assert "Provide only one input source" in resp.json()["detail"]


def test_source_url_rejects_private_network_resolution():
    with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 80))]):
        with pytest.raises(Exception) as exc:
            _assert_safe_source_url("https://example.com/file.pdf")

    assert "private or local network" in str(exc.value)


# ---------------------------------------------------------------------------
# Local file paths
# ---------------------------------------------------------------------------


class TestUploadLocalFile:
    @pytest.mark.asyncio
    async def test_missing_file_and_path_returns_400(self, upload_client: AsyncClient):
        resp = await upload_client.post("/api/convert/upload")
        assert resp.status_code == 400
        assert "Either an uploaded file, local_filepath, or source_url must be provided." in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_non_absolute_local_path_rejected(self, upload_client: AsyncClient):
        resp = await upload_client.post(
            "/api/convert/upload",
            params={"local_filepath": "relative/path/file.pdf"}
        )
        assert resp.status_code == 400
        assert "must be an absolute path" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_non_existent_local_path_rejected(self, upload_client: AsyncClient):
        import os
        # Generate an absolute path that does not exist
        fake_path = "/non_existent_file_xyz.pdf" if os.name != "nt" else "C:\\non_existent_file_xyz.pdf"
        resp = await upload_client.post(
            "/api/convert/upload",
            params={"local_filepath": fake_path}
        )
        assert resp.status_code == 400
        assert "Local file not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_valid_local_path_accepted(self, upload_client: AsyncClient, tmp_path):
        # Create a temp file on disk
        temp_file = tmp_path / "valid.pdf"
        temp_file.write_bytes(b"%PDF-1.4 fake")
        
        resp = await upload_client.post(
            "/api/convert/upload",
            params={"local_filepath": str(temp_file.resolve())}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["filename"] == "valid.pdf"
