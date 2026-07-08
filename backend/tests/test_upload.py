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
        [".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp", ".gif", ".tsv", ".xls", ".msg", ".wav", ".mp4"],
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

    @pytest.mark.asyncio
    async def test_max_upload_size_env_override_takes_effect(self, monkeypatch: pytest.MonkeyPatch):
        """UCM-004.3: MARKER_MAX_UPLOAD_SIZE_MB must drive the enforced limit at import time."""
        import importlib
        import sys

        import app.core.config as cfg_mod

        monkeypatch.setenv("MARKER_MAX_UPLOAD_SIZE_MB", "1")
        for name, mod in list(sys.modules.items()):
            if name in ("app.core.config", "backend.app.core.config"):
                importlib.reload(mod)
        try:
            # 1 MB expressed in bytes
            assert cfg_mod.MAX_UPLOAD_SIZE == 1 * 1024 * 1024

            # convert.py must read this value from config (single source of truth).
            import app.routes.convert as convert_mod
            for name, mod in list(sys.modules.items()):
                if name in ("app.routes.convert", "backend.app.routes.convert"):
                    importlib.reload(mod)
            assert convert_mod.MAX_UPLOAD_SIZE == cfg_mod.MAX_UPLOAD_SIZE
        finally:
            monkeypatch.delenv("MARKER_MAX_UPLOAD_SIZE_MB", raising=False)
            for name, mod in list(sys.modules.items()):
                if name in (
                    "app.core.config",
                    "backend.app.core.config",
                    "app.routes.convert",
                    "backend.app.routes.convert",
                ):
                    importlib.reload(mod)


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

        async def fake_download(source_url, destination, db):
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
    async def test_local_path_requires_workspace_roots(self, upload_client: AsyncClient, tmp_path):
        temp_file = tmp_path / "valid.pdf"
        temp_file.write_bytes(b"%PDF-1.4 fake")

        resp = await upload_client.post(
            "/api/convert/upload",
            params={"local_filepath": str(temp_file.resolve())},
        )

        assert resp.status_code == 400
        assert "MARKER_WORKSPACE_ROOTS" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_valid_local_path_accepted(self, upload_client: AsyncClient, tmp_path, monkeypatch):
        # Create a temp file on disk
        temp_file = tmp_path / "valid.pdf"
        temp_file.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(tmp_path))

        resp = await upload_client.post(
            "/api/convert/upload",
            params={"local_filepath": str(temp_file.resolve())}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["filename"] == "valid.pdf"

    @pytest.mark.asyncio
    async def test_output_dir_requires_output_root(self, upload_client: AsyncClient, tmp_path):
        data = io.BytesIO(b"%PDF-1.4 fake")
        files = {"file": ("document.pdf", data, "application/pdf")}

        resp = await upload_client.post(
            "/api/convert/upload",
            files=files,
            params={"output_dir": str(tmp_path / "out")},
        )

        assert resp.status_code == 400
        assert "MARKER_OUTPUT_ROOT" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_output_dir_under_output_root_accepted(self, upload_client: AsyncClient, tmp_path, monkeypatch):
        output_root = tmp_path / "outputs"
        output_root.mkdir()
        data = io.BytesIO(b"%PDF-1.4 fake")
        files = {"file": ("document.pdf", data, "application/pdf")}
        monkeypatch.setenv("MARKER_OUTPUT_ROOT", str(output_root))

        resp = await upload_client.post(
            "/api/convert/upload",
            files=files,
            params={"output_dir": str(output_root / "nested")},
        )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# MUI-003: commit-before-submit race condition
#
# The REST upload route must commit the ConversionJob row BEFORE calling
# task_manager.submit_job(). Otherwise a fast CPU/native converter can finish
# in the worker thread and call _finalize_job, which opens a fresh DB session
# that cannot see the uncommitted row, silently skipping finalization and
# leaving the job stuck at "pending" forever.
#
# This test instruments submit_job to verify, from a brand-new session, that
# the job row is already committed at the moment submit fires.
# ---------------------------------------------------------------------------


class TestUploadCommitsBeforeSubmit:
    @pytest.mark.asyncio
    async def test_job_row_committed_to_disk_when_submit_fires(self, tmp_path):
        """At the moment task_manager.submit_job runs, the job row must already
        be committed — provable by reading the DB file through a completely
        independent stdlib sqlite3 connection.

        submit_job is synchronous, so the verifier is synchronous too. A raw
        sqlite3 connection to the on-disk DB sees only committed rows, so if the
        request transaction hasn't committed yet the row is invisible and the
        race is caught.
        """
        import sqlite3

        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        from app.database import Base, get_db
        from app.main import app
        from app.models.job import ConversionJob  # noqa: F401

        db_path = tmp_path / "commit_race.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
            future=True,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        captured: dict[str, object] = {}

        def _verifying_submit(job_id, *args, **kwargs):
            # Independent connection to the DB file — sees only committed rows.
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT status FROM conversion_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            finally:
                conn.close()
            captured["status_at_submit"] = row[0] if row else None

        mock_task_mgr = MagicMock()
        mock_task_mgr.submit_job = _verifying_submit
        mock_task_mgr.enqueue_durable_job = AsyncMock(return_value=False)

        async def _override():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override

        try:
            with patch("app.main._app_state") as mock_state:
                mock_state.marker_service = MagicMock()
                mock_state.conversion_service = MagicMock()
                mock_state.task_manager = mock_task_mgr

                with patch(
                    "app.services.marker_service.build_marker_options",
                    return_value={},
                ), patch(
                    "app.routes.convert._load_active_audio_defaults",
                    new_callable=AsyncMock,
                    return_value={},
                ), patch(
                    "app.routes.convert._resolve_audio_vocabulary_packs",
                    new_callable=AsyncMock,
                    return_value=[],
                ), patch(
                    "app.routes.convert._load_llm_config",
                    new_callable=AsyncMock,
                    return_value={"providers": [], "active": {}},
                ):
                    transport = ASGITransport(app=app)
                    async with AsyncClient(
                        transport=transport, base_url="http://testserver"
                    ) as c:
                        data = io.BytesIO(b"hello world text data")
                        files = {"file": ("data.txt", data, "text/plain")}
                        resp = await c.post("/api/convert/upload", files=files)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

        assert resp.status_code == 200, resp.text
        # The job row MUST be committed to disk when submit_job fires.
        assert captured.get("status_at_submit") == "pending", (
            "Job row was NOT committed before task_manager.submit_job() — "
            "the commit-before-submit race (MUI-003) is present. "
            f"Got status_at_submit={captured.get('status_at_submit')!r}"
        )
