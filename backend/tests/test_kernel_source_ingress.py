"""Ingress source-truth integration tests (PR70/71 local slice, §13.3).

Full application surface: REST upload/local_filepath/URL-shaped
submissions, agent submissions, and retries run against a real
TaskManager + kernel runtime coordinator with real acquisition, real
PDF probes, and a recording marker service. The probe-to-convert proof:
probe observes revision A's page count, the external file is then
replaced with revision B, and the executed conversion provably parses
A's immutable artifact bytes.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db
from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitService
from app.kernel.source_store import LocalSourceStore
from app.models.job import ConversionJob
from app.services import task_manager as tm_module
from app.services.conversion_service import ConversionService
from app.services.source_acquisition import (
    SOURCE_CONFIG_KEY,
    SourceAcquisitionService,
    set_default_source_acquisition_service,
)
from app.services.task_manager import TaskManager
from tests.conftest import FakeMarkerService

pytestmark = pytest.mark.asyncio


def _digital_pdf_bytes(pages: int = 2, marker_line: str = "revision") -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for page in range(pages):
        for idx in range(20):
            c.drawString(
                72,
                740 - idx * 20,
                f"{marker_line} page {page + 1} line {idx + 1} with useful words.",
            )
        c.showPage()
    c.save()
    return buf.getvalue()


PDF_A = _digital_pdf_bytes(pages=2, marker_line="AlphaRevision")
PDF_B = _digital_pdf_bytes(pages=1, marker_line="BetaRevision")


class RecordingMarkerService(FakeMarkerService):
    """Fake marker backend that records which file the marker route parsed.

    Probed text-clean PDFs may route to the native liteparse engine
    instead; those cases are proven via the extracted result text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parsed_paths: list[str] = []

    def convert_file(self, filepath: str, options: dict[str, Any], device: str | None = None) -> dict[str, Any]:
        self.parsed_paths.append(filepath)
        return {
            "text": f"# Converted\n\nsource bytes: {len(Path(filepath).read_bytes())}",
            "extension": "md",
            "images": [],
            "metadata": {"pages": 1},
        }


def _assert_revision_consumed(env, row, *, expect_marker: str, reject_marker: str) -> None:
    """Prove the executed conversion consumed the expected revision.

    Marker route: the recorded parse path's bytes ARE the proof. Native
    route: the extracted text carries the revision's marker line.
    """
    if env.marker_service.parsed_paths:
        parsed = Path(env.marker_service.parsed_paths[-1])
        content = parsed.read_bytes()
        assert expect_marker.encode() in content and reject_marker.encode() not in content
    else:
        text = row.result_text or ""
        assert expect_marker in text, f"expected {expect_marker!r} in result text"
        assert reject_marker not in text


@pytest_asyncio.fixture
async def ingress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.main import _app_state, app

    roots = tmp_path / "roots"
    docs = roots / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(roots))
    monkeypatch.delenv("MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS", raising=False)

    url = f"sqlite+aiosqlite:///{(tmp_path / 'ingress.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(tm_module, "async_session_factory", factory)

    marker_service = RecordingMarkerService()
    conversion_service = ConversionService(marker_service)
    store = LocalSourceStore(tmp_path / "source_store")
    manager = TaskManager()
    coordinator = manager.start_kernel_runtime(
        conversion_service,
        session_factory=factory,
        commit_service=KernelCommitService(factory),
        source_store=store,
        workspace_id="t",
        owner_id="test-ingress",
        lease_seconds=60.0,
        renew_interval_seconds=0.05,
        dispatch_poll_seconds=0.05,
        watchdog_interval_seconds=0.1,
        max_in_flight=4,
    )
    acquisition = SourceAcquisitionService(
        factory, KernelCommitService(factory), store, workspace_id="t"
    )
    set_default_source_acquisition_service(acquisition)
    coordinator.start()  # dispatch + watchdog loops (lifespan's job)

    original_ms = _app_state.marker_service
    original_cs = _app_state.conversion_service
    original_tm = _app_state.task_manager
    _app_state.marker_service = marker_service  # type: ignore[assignment]
    _app_state.conversion_service = conversion_service  # type: ignore[assignment]
    _app_state.task_manager = manager  # type: ignore[assignment]

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")

    try:
        yield SimpleNamespace(
            client=client,
            factory=factory,
            store=store,
            docs=docs,
            tmp_path=tmp_path,
            marker_service=marker_service,
            coordinator=coordinator,
            manager=manager,
        )
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
        _app_state.marker_service = original_ms
        _app_state.conversion_service = original_cs
        _app_state.task_manager = original_tm
        set_default_source_acquisition_service(None)
        coordinator.stop()
        manager.shutdown()
        await engine.dispose()


async def _wait_status(env, job_id: str, *statuses: str, timeout: float = 30.0):
    import asyncio

    async def _poll():
        while True:
            async with env.factory() as session:
                row = await session.get(ConversionJob, job_id)
            if row and row.status in statuses:
                return row
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _row_config(env, job_id: str) -> dict[str, Any]:
    async with env.factory() as session:
        row = await session.get(ConversionJob, job_id)
        assert row is not None
        return json.loads(row.config_json or "{}")


class TestRestLocalPathIngress:
    async def test_probe_and_execution_consume_one_acquired_revision(self, ingress_env):
        env = ingress_env
        src = env.docs / "report.pdf"
        src.write_bytes(PDF_A)

        resp = await env.client.post(
            "/api/convert/upload",
            params={"local_filepath": str(src), "output_format": "markdown"},
        )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        config = await _row_config(env, job_id)
        block = config[SOURCE_CONFIG_KEY]
        assert block["consistency_class"] == "stable_handle"
        # the probe ran against the acquired revision (2 pages for A)
        assert config["probe_result"]["page_count"] == 2
        artifact = env.store.artifact_path(block["blob_key"], ".pdf")
        assert artifact.read_bytes() == PDF_A
        assert config["durable_filepath"] == str(artifact)

        # Replace the external source with revision B AFTER submission.
        src.write_bytes(PDF_B)

        row = await _wait_status(env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        # probe saw A (page_count 2 above); conversion provably parsed A
        _assert_revision_consumed(
            env, row, expect_marker="AlphaRevision", reject_marker="BetaRevision"
        )

    async def test_external_source_deleting_after_submission_survives(self, ingress_env):
        env = ingress_env
        src = env.docs / "vanish.pdf"
        src.write_bytes(PDF_A)
        resp = await env.client.post(
            "/api/convert/upload",
            params={"local_filepath": str(src), "output_format": "markdown"},
        )
        job_id = resp.json()["job_id"]
        src.unlink()

        row = await _wait_status(env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        _assert_revision_consumed(
            env, row, expect_marker="AlphaRevision", reject_marker="BetaRevision"
        )


class TestRestUploadIngress:
    async def test_upload_binds_committed_revision(self, ingress_env):
        env = ingress_env
        resp = await env.client.post(
            "/api/convert/upload",
            files={"file": ("upload.pdf", io.BytesIO(PDF_A), "application/pdf")},
            params={"output_format": "markdown"},
        )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        config = await _row_config(env, job_id)
        block = config[SOURCE_CONFIG_KEY]
        assert block["blob_key"].startswith("sha256:")
        assert config["probe_result"]["page_count"] == 2
        artifact = env.store.artifact_path(block["blob_key"], ".pdf")
        assert artifact.read_bytes() == PDF_A

        row = await _wait_status(env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        _assert_revision_consumed(
            env, row, expect_marker="AlphaRevision", reject_marker="BetaRevision"
        )


class TestRetrySourceTruth:
    async def test_retry_reuses_committed_revision_after_upload_deleted(self, ingress_env):
        env = ingress_env
        resp = await env.client.post(
            "/api/convert/upload",
            files={"file": ("retry.pdf", io.BytesIO(PDF_A), "application/pdf")},
            params={"output_format": "markdown"},
        )
        job_id = resp.json()["job_id"]
        row = await _wait_status(env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        first_config = await _row_config(env, job_id)

        # The uploaded copy disappears; the committed revision remains.
        from app.core.config import UPLOAD_DIR

        (UPLOAD_DIR / row.filename).unlink(missing_ok=True)

        retry = await env.client.post(f"/api/convert/{job_id}/retry", json={})
        assert retry.status_code == 200, retry.text
        new_job_id = retry.json()["new_job_id"]

        new_config = await _row_config(env, new_job_id)
        assert (
            new_config[SOURCE_CONFIG_KEY]["content_revision_id"]
            == first_config[SOURCE_CONFIG_KEY]["content_revision_id"]
        )
        new_row = await _wait_status(env, new_job_id, "completed", "failed")
        assert new_row.status == "completed", new_row.error_message
        _assert_revision_consumed(
            env, new_row, expect_marker="AlphaRevision", reject_marker="BetaRevision"
        )

    async def test_retry_reuses_committed_revision_despite_external_change(
        self, ingress_env
    ):
        env = ingress_env
        src = env.docs / "changed.pdf"
        src.write_bytes(PDF_A)
        resp = await env.client.post(
            "/api/convert/upload",
            params={"local_filepath": str(src), "output_format": "markdown"},
        )
        job_id = resp.json()["job_id"]
        row = await _wait_status(env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        original_config = await _row_config(env, job_id)

        # The external source mutates, but the committed revision's owned
        # bytes outrank it: retry MUST reuse revision A, not silently
        # convert different bytes under the same job lineage.
        src.write_bytes(PDF_B)
        retry = await env.client.post(f"/api/convert/{job_id}/retry", json={})
        assert retry.status_code == 200, retry.text
        new_job_id = retry.json()["new_job_id"]

        new_config = await _row_config(env, new_job_id)
        assert (
            new_config[SOURCE_CONFIG_KEY]["content_revision_id"]
            == original_config[SOURCE_CONFIG_KEY]["content_revision_id"]
        )
        assert new_config["probe_result"]["page_count"] == 2  # A's probe stands

        new_row = await _wait_status(env, new_job_id, "completed", "failed")
        assert new_row.status == "completed", new_row.error_message
        _assert_revision_consumed(
            env, new_row, expect_marker="AlphaRevision", reject_marker="BetaRevision"
        )

    async def test_retry_reacquires_new_revision_when_artifact_destroyed(
        self, ingress_env
    ):
        env = ingress_env
        src = env.docs / "resurrect.pdf"
        src.write_bytes(PDF_A)
        resp = await env.client.post(
            "/api/convert/upload",
            params={"local_filepath": str(src), "output_format": "markdown"},
        )
        job_id = resp.json()["job_id"]
        row = await _wait_status(env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        original_config = await _row_config(env, job_id)

        # The owned artifact is destroyed AND the external source now
        # holds different bytes: retry re-acquires honestly — a NEW
        # revision describing B, with a fresh probe for B.
        block = original_config[SOURCE_CONFIG_KEY]
        artifact = env.store.artifact_path(block["blob_key"], block["suffix"])
        artifact.chmod(0o644)
        artifact.unlink()
        src.write_bytes(PDF_B)

        retry = await env.client.post(f"/api/convert/{job_id}/retry", json={})
        assert retry.status_code == 200, retry.text
        new_job_id = retry.json()["new_job_id"]

        new_config = await _row_config(env, new_job_id)
        assert (
            new_config[SOURCE_CONFIG_KEY]["content_revision_id"]
            != original_config[SOURCE_CONFIG_KEY]["content_revision_id"]
        )
        assert new_config["probe_result"]["page_count"] == 1  # fresh probe of B

        new_row = await _wait_status(env, new_job_id, "completed", "failed")
        assert new_row.status == "completed", new_row.error_message
        _assert_revision_consumed(
            env, new_row, expect_marker="BetaRevision", reject_marker="AlphaRevision"
        )


class TestAgentIngress:
    async def test_agent_submission_binds_revision(self, ingress_env, monkeypatch):
        import app.agent_api as agent_api

        async def _noop_ready() -> None:
            return None

        monkeypatch.setattr(agent_api, "_ensure_db_ready", _noop_ready)
        monkeypatch.setattr(agent_api, "_db_session_factory", ingress_env.factory)

        src = ingress_env.docs / "agent.pdf"
        src.write_bytes(PDF_A)
        result = await agent_api.submit_conversion_job(
            local_file_path=str(src),
            options=agent_api.AgentConversionOptions(output_format="markdown"),
        )
        job_id = result["job_id"]

        config = await _row_config(ingress_env, job_id)
        block = config[SOURCE_CONFIG_KEY]
        assert block["consistency_class"] == "stable_handle"
        assert config["probe_result"]["page_count"] == 2
        artifact = ingress_env.store.artifact_path(block["blob_key"], ".pdf")
        assert artifact.read_bytes() == PDF_A

        row = await _wait_status(ingress_env, job_id, "completed", "failed")
        assert row.status == "completed", row.error_message
        _assert_revision_consumed(
            ingress_env, row, expect_marker="AlphaRevision", reject_marker="BetaRevision"
        )
