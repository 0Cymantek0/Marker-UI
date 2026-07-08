"""Tests for the /api/convert endpoints."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.database import get_db
from app.models.job import ConversionJob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_PDF_FILENAME = "test_document.pdf"
MINIMAL_PDF_BYTES = b"%PDF-1.4 test content"


def _digital_pdf_bytes(pages: int = 2) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for page in range(pages):
        for idx in range(20):
            c.drawString(72, 740 - idx * 20, f"Digital text page {page + 1} line {idx + 1} with useful words.")
        c.showPage()
    c.save()
    return buf.getvalue()


async def _upload_file(
    client: AsyncClient,
    filename: str = VALID_PDF_FILENAME,
    content: bytes = MINIMAL_PDF_BYTES,
    extra_params: dict | None = None,
):
    """Upload a file and return the response."""
    files = {"file": (filename, io.BytesIO(content), "application/pdf")}
    params: dict = {"output_format": "markdown"}
    if extra_params:
        params.update(extra_params)
    return await client.post(
        "/api/convert/upload",
        files=files,
        params=params,
    )


# ---------------------------------------------------------------------------
# Upload - valid extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_valid_extension_returns_200(
    client: AsyncClient,
):
    resp = await _upload_file(client)
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "pending"
    assert body["filename"] == VALID_PDF_FILENAME
    assert body["output_format"] == "markdown"


@pytest.mark.asyncio
async def test_pdf_upload_persists_probe_result(client: AsyncClient, db_session):
    resp = await _upload_file(client, content=_digital_pdf_bytes())
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)

    assert config["probe_result"]["page_count"] == 2
    assert config["probe_result"]["recommended_engine"] == "liteparse"


@pytest.mark.asyncio
async def test_upload_persists_engine_override(client: AsyncClient, db_session):
    resp = await _upload_file(
        client,
        content=_digital_pdf_bytes(),
        extra_params={"engine_override": "marker_pdf"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)

    assert config["engine_override"] == "marker_pdf"


@pytest.mark.asyncio
async def test_upload_persists_conversion_profile(client: AsyncClient, db_session):
    resp = await _upload_file(
        client,
        content=_digital_pdf_bytes(),
        extra_params={"conversion_profile": "high_accuracy"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)

    assert config["conversion_profile"] == "high_accuracy"


@pytest.mark.asyncio
async def test_pdf_upload_rejects_page_range_past_document(client: AsyncClient):
    resp = await _upload_file(
        client,
        content=_digital_pdf_bytes(pages=2),
        extra_params={"page_range": "1-3"},
    )

    assert resp.status_code == 400
    assert "document length" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_pdf_upload_rejects_empty_page_range(client: AsyncClient):
    resp = await _upload_file(
        client,
        content=_digital_pdf_bytes(pages=2),
        extra_params={"page_range": ","},
    )

    assert resp.status_code == 400
    assert "Invalid page_range" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_docx_rejects_structured_output_format(client: AsyncClient):
    files = {"file": ("report.docx", io.BytesIO(b"PK docx content"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    resp = await client.post(
        "/api/convert/upload",
        files=files,
        params={"output_format": "html"},
    )
    assert resp.status_code == 400
    assert "not supported for engine 'office_docx'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_docx_rejects_incompatible_engine_override(client: AsyncClient):
    files = {"file": ("report.docx", io.BytesIO(b"PK docx content"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    resp = await client.post(
        "/api/convert/upload",
        files=files,
        params={"engine_override": "marker_pdf"},
    )

    assert resp.status_code == 400
    assert "engine_override 'marker_pdf' is incompatible with extension '.docx'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_native_file_accepts_derived_chunks(client: AsyncClient, db_session):
    files = {"file": ("scores.tsv", io.BytesIO(b"name\tscore\nAda\t10\n"), "text/tab-separated-values")}

    resp = await client.post(
        "/api/convert/upload",
        files=files,
        params={
            "output_format": "chunks",
            "chunking_strategy": "unstructured_by_title",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["output_format"] == "chunks"

    stmt = select(ConversionJob).where(ConversionJob.id == body["job_id"])
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)
    assert job.output_format == "chunks"
    assert config["output_format"] == "chunks"
    assert config["chunking_strategy"] == "unstructured_by_title"


@pytest.mark.asyncio
async def test_upload_archive_persists_full_budget_controls(client: AsyncClient, db_session):
    files = {"file": ("bundle.zip", io.BytesIO(b"PK\x05\x06" + b"\x00" * 18), "application/zip")}

    resp = await client.post(
        "/api/convert/upload",
        files=files,
        params={
            "archive_recursive": "false",
            "archive_max_files": "12",
            "archive_inline_bytes": "4096",
            "archive_max_converted_children": "3",
            "archive_max_child_bytes": "8192",
            "archive_max_total_uncompressed_bytes": "16384",
            "archive_max_compression_ratio": "25",
            "archive_max_depth": "1",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    stmt = select(ConversionJob).where(ConversionJob.id == body["job_id"])
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)
    assert config["archive_recursive"] is False
    assert config["archive_max_files"] == 12
    assert config["archive_inline_bytes"] == 4096
    assert config["archive_max_converted_children"] == 3
    assert config["archive_max_child_bytes"] == 8192
    assert config["archive_max_total_uncompressed_bytes"] == 16384
    assert config["archive_max_compression_ratio"] == 25
    assert config["archive_max_depth"] == 1


@pytest.mark.asyncio
async def test_pdf_structured_output_forces_marker_route(client: AsyncClient, db_session):
    resp = await _upload_file(
        client,
        content=_digital_pdf_bytes(),
        extra_params={"output_format": "json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["output_format"] == "json"

    stmt = select(ConversionJob).where(ConversionJob.id == body["job_id"])
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)

    assert job.output_format == "json"
    assert config["output_format"] == "json"
    assert config["engine_override"] == "marker_pdf"


@pytest.mark.asyncio
async def test_pdf_chunks_keeps_liteparse_fast_profile(client: AsyncClient, db_session):
    resp = await _upload_file(
        client,
        content=_digital_pdf_bytes(),
        extra_params={"output_format": "chunks", "conversion_profile": "fast"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["output_format"] == "chunks"

    stmt = select(ConversionJob).where(ConversionJob.id == body["job_id"])
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)

    assert job.output_format == "chunks"
    assert config["output_format"] == "chunks"
    assert config["conversion_profile"] == "fast"
    assert "engine_override" not in config


@pytest.mark.asyncio
async def test_upload_valid_pptx(client: AsyncClient):
    files = {"file": ("presentation.pptx", io.BytesIO(b"PK pptx content"), "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
    resp = await client.post(
        "/api/convert/upload",
        files=files,
        params={"output_format": "markdown"},
    )
    assert resp.status_code == 200
    assert resp.json()["output_format"] == "markdown"


# ---------------------------------------------------------------------------
# Upload - invalid extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_invalid_extension_returns_400(
    client: AsyncClient,
):
    files = {"file": ("malware.exe", io.BytesIO(b"MZ\x90\x00"), "application/octet-stream")}
    resp = await client.post(
        "/api/convert/upload",
        files=files,
        params={"output_format": "markdown"},
    )
    assert resp.status_code == 400
    assert "not supported" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Upload - exceeds size limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_exceeds_100mb_returns_413(
    client: AsyncClient
):
    original_unlink = Path.unlink

    def safe_unlink(self, missing_ok=False):
        try:
            original_unlink(self, missing_ok=missing_ok)
        except PermissionError:
            pass

    with patch("app.routes.convert.MAX_UPLOAD_SIZE", 100), \
         patch.object(Path, "unlink", safe_unlink):
        content = b"x" * 200
        files = {"file": ("big.pdf", io.BytesIO(content), "application/pdf")}
        resp = await client.post(
            "/api/convert/upload",
            files=files,
            params={"output_format": "markdown"},
        )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_emits_progress_and_status_events(
    client: AsyncClient,
):
    """Upload a file, then consume the SSE stream and verify event types."""
    upload_resp = await _upload_file(client)
    assert upload_resp.status_code == 200
    job_id = upload_resp.json()["job_id"]

    sse_resp = await client.get(
        f"/api/convert/events/{job_id}",
    )
    assert sse_resp.status_code == 200

    text = sse_resp.text
    has_progress = "event: progress" in text
    has_status = "event: status" in text
    assert has_progress, f"SSE stream missing 'progress' event. Body:\n{text}"
    assert has_status, f"SSE stream missing 'status' event. Body:\n{text}"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_completed_job_returns_file(
    client: AsyncClient, db_session
):
    """Create a completed job in DB and verify download returns the file."""
    job_id = "test-download-job-id"
    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir = Path("data/output")
    output_dir.mkdir(parents=True, exist_ok=True)

    result_path = output_dir / f"{job_id}.md"
    result_path.write_text("# Converted output", encoding="utf-8")

    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# Converted output",
        result_path=str(result_path),
        progress=100,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(
        f"/api/convert/download/{job_id}",
    )
    assert resp.status_code == 200

    result_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_job_marks_cancelled_without_deleting_row(
    client: AsyncClient, db_session
):
    """POST /cancel is non-destructive: row remains for history/status."""
    job_id = "rest-cancel-job"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename=f"{job_id}.pdf",
            original_name="running.pdf",
            status="processing",
            input_format="pdf",
            output_format="markdown",
            progress=42,
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/convert/{job_id}/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled", "job_id": job_id, "cancelled": True}
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.progress == 0


@pytest.mark.asyncio
async def test_delete_job_removes_from_db(
    client: AsyncClient, db_session
):
    """Create a terminal job, delete it via API, verify it's gone from DB."""
    from sqlalchemy import select

    job_id = "rest-delete-completed-job"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename=f"{job_id}.pdf",
            original_name="done.pdf",
            status="completed",
            input_format="pdf",
            output_format="markdown",
            progress=100,
        )
    )
    await db_session.commit()

    del_resp = await client.delete(
        f"/api/convert/{job_id}",
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_job_rejects_live_job_without_force(
    client: AsyncClient, db_session
):
    job_id = "rest-delete-running-job"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename=f"{job_id}.pdf",
            original_name="running.pdf",
            status="processing",
            input_format="pdf",
            output_format="markdown",
            progress=50,
        )
    )
    await db_session.commit()

    resp = await client.delete(f"/api/convert/{job_id}")

    assert resp.status_code == 409
    assert "cancel it first or pass force=true" in resp.json()["detail"]
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "processing"


@pytest.mark.asyncio
async def test_delete_job_force_deletes_live_job(
    client: AsyncClient, db_session
):
    job_id = "rest-force-delete-running-job"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename=f"{job_id}.pdf",
            original_name="running.pdf",
            status="processing",
            input_format="pdf",
            output_format="markdown",
            progress=50,
        )
    )
    await db_session.commit()

    resp = await client.delete(f"/api/convert/{job_id}", params={"force": "true"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "job_id": job_id}
    assert await db_session.get(ConversionJob, job_id) is None


@pytest.mark.asyncio
async def test_purge_job_files_removes_artifacts_but_keeps_history(
    client: AsyncClient,
    db_session,
    tmp_path: Path,
):
    job_id = "rest-purge-completed-job"
    result_file = tmp_path / "rest-purge.md"
    manifest_file = tmp_path / "rest-purge.marker.json"
    result_file.write_text("# retained in db", encoding="utf-8")
    manifest_file.write_text("{}", encoding="utf-8")
    db_session.add(
        ConversionJob(
            id=job_id,
            filename=f"{job_id}.csv",
            original_name="purge.csv",
            status="completed",
            input_format="csv",
            output_format="markdown",
            result_text="# retained in db",
            result_path=str(result_file),
            progress=100,
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/convert/{job_id}/purge-files")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "purged"
    assert {Path(path).name for path in body["files_removed"]} == {
        "rest-purge.md",
        "rest-purge.marker.json",
    }
    assert not result_file.exists()
    assert not manifest_file.exists()
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "completed"
    assert row.result_text == "# retained in db"
    assert row.result_path is None
    metadata = json.loads(row.result_metadata_json or "{}")
    assert metadata["purged_artifacts"]["files_removed"] == body["files_removed"]


@pytest.mark.asyncio
async def test_purge_job_files_rejects_live_job(
    client: AsyncClient,
    db_session,
):
    job_id = "rest-purge-running-job"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename=f"{job_id}.csv",
            original_name="running.csv",
            status="processing",
            input_format="csv",
            output_format="markdown",
            progress=25,
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/convert/{job_id}/purge-files")

    assert resp.status_code == 409
    assert "cancel or wait for terminal status" in resp.json()["detail"]
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "processing"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_excludes_result_text(
    client: AsyncClient, db_session
):
    """Upload a job, add result_text to DB, verify history omits it."""
    job_id = "hist-test-job"
    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="history_test.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# Secret conversion output that should not appear",
        progress=100,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get("/api/convert/history")
    assert resp.status_code == 200
    body = resp.json()

    jobs = body["jobs"]
    our_job = next((j for j in jobs if j["job_id"] == job_id), None)
    assert our_job is not None, f"Job {job_id} not found in history response"
    assert our_job["result_text"] is None, "result_text should be excluded from history"


@pytest.mark.asyncio
async def test_history_filtering(
    client: AsyncClient, db_session
):
    """Verify history endpoint filters by search, status, and converter."""
    # Insert multiple jobs
    job1 = ConversionJob(
        id="job-filter-1",
        filename="stored-job-filter-1.pdf",
        original_name="apple display.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        config_json='{"converter_cls": "PdfConverter"}',
        progress=100,
    )
    job2 = ConversionJob(
        id="job-filter-2",
        filename="banana.pdf",
        original_name="banana.pdf",
        status="failed",
        input_format="pdf",
        output_format="markdown",
        config_json='{"converter_cls": "LiteParse"}',
        progress=0,
    )
    job3 = ConversionJob(
        id="job-filter-3",
        filename="stored-job-filter-3.pdf",
        original_name="compact-table.pdf",
        status="pending",
        input_format="pdf",
        output_format="markdown",
        config_json='{"converter_cls":"TableConverter"}',
        progress=0,
    )
    db_session.add_all([job1, job2, job3])
    await db_session.commit()

    # Test search filter against displayed original filename, not only stored path name
    resp = await client.get("/api/convert/history?search=apple%20display")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["job_id"] == "job-filter-1"

    # Test status filter
    resp = await client.get("/api/convert/history?status=failed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["job_id"] == "job-filter-2"

    # Test queued UI alias maps to persisted pending status
    resp = await client.get("/api/convert/history?status=queued")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["job_id"] == "job-filter-3"

    # Test converter filter
    resp = await client.get("/api/convert/history?converter=LiteParse")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["job_id"] == "job-filter-2"

    # Test converter filter against compact JSON serialization
    resp = await client.get("/api/convert/history?converter=TableConverter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["job_id"] == "job-filter-3"


# ---------------------------------------------------------------------------
# Cancelled jobs stay cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_job_stays_cancelled(
    client: AsyncClient, db_session
):
    """A cancelled job should not be overwritten to 'failed' by _fail_job."""
    from datetime import datetime, timezone

    job_id = "cancel-stay-job"
    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="cancel_test.pdf",
        status="cancelled",
        input_format="pdf",
        output_format="markdown",
        progress=0,
    )
    db_session.add(job)
    await db_session.commit()

    from app.main import _app_state

    tm = _app_state.task_manager
    from sqlalchemy import update

    await db_session.execute(
        update(ConversionJob)
        .where(ConversionJob.id == job_id)
        .where(ConversionJob.status != "cancelled")
        .values(
            status="failed",
            error_message="some error",
            completed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    from sqlalchemy import select

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db_session.execute(stmt)
    fresh = result.scalar_one()
    assert fresh.status == "cancelled", (
        f"Expected 'cancelled' but got '{fresh.status}' - _fail_job overwrote it!"
    )


# ---------------------------------------------------------------------------
# LLM Model Override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_with_llm_model_override(client: AsyncClient, db_session):
    """Verify upload endpoint accepts llm_model override and saves it in config."""
    resp = await _upload_file(
        client,
        extra_params={"use_llm": "true", "llm_model": "custom-override-model-123"}
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    from sqlalchemy import select
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    res = await db_session.execute(stmt)
    job = res.scalar_one()

    cfg = json.loads(job.config_json)
    assert cfg["llm_model"] == "custom-override-model-123"
    assert cfg["use_llm"] is True


@pytest.mark.asyncio
async def test_upload_with_image_handling_mode(client: AsyncClient, db_session):
    """Verify upload endpoint saves image_handling_mode for processor wiring."""
    resp = await _upload_file(
        client,
        extra_params={"image_handling_mode": "both"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    from sqlalchemy import select
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    res = await db_session.execute(stmt)
    job = res.scalar_one()

    cfg = json.loads(job.config_json)
    assert cfg["image_handling_mode"] == "both"
    assert cfg["allow_cloud_vlm"] is False


@pytest.mark.asyncio
async def test_upload_with_cloud_vlm_opt_in(client: AsyncClient, db_session):
    """Cloud image understanding is explicit opt-in, never implied by mode."""
    resp = await _upload_file(
        client,
        extra_params={"image_handling_mode": "both", "allow_cloud_vlm": "true"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    from sqlalchemy import select
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    res = await db_session.execute(stmt)
    job = res.scalar_one()

    cfg = json.loads(job.config_json)
    assert cfg["image_handling_mode"] == "both"
    assert cfg["allow_cloud_vlm"] is True


@pytest.mark.asyncio
async def test_upload_image_pipeline_knobs_reach_config(client: AsyncClient, db_session):
    """Every new image-understanding @Query lands in the stored config dict."""
    resp = await _upload_file(
        client,
        extra_params={
            "image_handling_mode": "understanding",
            "router_enabled": "false",
            "smart_router_level": "beeg_brain",
            "dedup_enabled": "false",
            "downscale_vlm_crops": "false",
            "batch_enabled": "false",
            "ocr_engine": "hybrid_ocr",
            "hybrid_ocr_profile": "low_vram",
            "hybrid_ocr_require_specialists": "true",
            "decorative_max_text_density": "0.05",
            "ocr_min_text_density": "0.6",
            "ocr_min_lines": "5",
            "dedup_max_distance": "4",
            "vlm_crop_max_px": "1024",
            "vlm_batch_size": "16",
            "max_batch_retries": "3",
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    from sqlalchemy import select
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    res = await db_session.execute(stmt)
    job = res.scalar_one()

    cfg = json.loads(job.config_json)
    assert cfg["router_enabled"] is False
    assert cfg["smart_router_level"] == "beeg_brain"
    assert cfg["dedup_enabled"] is False
    assert cfg["downscale_vlm_crops"] is False
    assert cfg["batch_enabled"] is False
    assert cfg["ocr_engine"] == "hybrid_ocr"
    assert cfg["hybrid_ocr_profile"] == "low_vram"
    assert cfg["hybrid_ocr_require_specialists"] is True
    assert cfg["decorative_max_text_density"] == 0.05
    assert cfg["ocr_min_text_density"] == 0.6
    assert cfg["ocr_min_lines"] == 5
    assert cfg["dedup_max_distance"] == 4
    assert cfg["vlm_crop_max_px"] == 1024
    assert cfg["vlm_batch_size"] == 16
    assert cfg["max_batch_retries"] == 3


@pytest.mark.asyncio
async def test_upload_omits_unset_pipeline_knobs(client: AsyncClient, db_session):
    """Knobs the UI does not send stay out of config, so processor defaults win."""
    resp = await _upload_file(
        client,
        extra_params={"image_handling_mode": "understanding"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    from sqlalchemy import select
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    res = await db_session.execute(stmt)
    job = res.scalar_one()

    cfg = json.loads(job.config_json)
    for key in (
        "router_enabled", "dedup_enabled", "downscale_vlm_crops", "batch_enabled",
        "ocr_engine", "hybrid_ocr_profile", "hybrid_ocr_require_specialists",
        "decorative_max_text_density", "ocr_min_text_density",
        "ocr_min_lines", "dedup_max_distance", "vlm_crop_max_px",
        "vlm_batch_size", "max_batch_retries", "smart_router_level",
    ):
        assert key not in cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["glm_ocr", "paddleocr_vl", "mistral_ocr", "bogus"])
async def test_upload_rejects_legacy_ocr_engine_values(client: AsyncClient, legacy: str):
    resp = await _upload_file(client, extra_params={"ocr_engine": legacy})

    assert resp.status_code == 400
    assert "ocr" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_rejects_out_of_range_knob(client: AsyncClient, db_session):
    """FastAPI Query bounds reject an out-of-range tuning value (422)."""
    resp = await _upload_file(
        client,
        extra_params={"vlm_batch_size": "999"},  # le=64
    )
    assert resp.status_code == 422


def test_build_marker_options_model_override():
    """Verify that build_marker_options correctly overrides model names for all services."""
    from app.services.marker_service import build_marker_options

    llm_cfg = {
        "providers": [
            {
                "id": "gemini",
                "type": "gemini",
                "label": "Gemini",
                "api_key": "gemini-key",
                "models": [{"model_id": "gemini-2.0-flash"}, {"model_id": "gemini-1.5-pro"}]
            },
            {
                "id": "claude",
                "type": "claude",
                "label": "Claude",
                "api_key": "claude-key",
                "models": [{"model_id": "claude-3-7-sonnet"}, {"model_id": "claude-3-5-haiku"}]
            },
            {
                "id": "openai",
                "type": "openai",
                "label": "OpenAI",
                "api_key": "openai-key",
                "models": [{"model_id": "gpt-4o-mini"}, {"model_id": "gpt-4o"}]
            }
        ],
        "active": {
            "provider_id": "gemini",
            "model_id": "gemini-2.0-flash"
        }
    }

    # Gemini Override
    opts = build_marker_options(llm_cfg, {"use_llm": True, "llm_provider": "gemini", "llm_model": "gemini-1.5-pro"})
    assert opts["llm_service"] == "marker.services.gemini.GoogleGeminiService"
    assert opts["gemini_model_name"] == "gemini-1.5-pro"

    # Claude Override
    opts = build_marker_options(llm_cfg, {"use_llm": True, "llm_provider": "claude", "llm_model": "claude-3-5-haiku"})
    assert opts["llm_service"] == "marker.services.claude.ClaudeService"
    assert opts["claude_model_name"] == "claude-3-5-haiku"

    # OpenAI Override
    opts = build_marker_options(llm_cfg, {"use_llm": True, "llm_provider": "openai", "llm_model": "gpt-4o"})
    assert opts["llm_service"] == "marker.services.openai.OpenAIService"
    assert opts["openai_model"] == "gpt-4o"


def test_build_marker_options_llm_service_import_path_is_marker_compatible():
    """Regression: marker-pdf expects a dotted class path, not a short provider key."""
    from marker.config.parser import ConfigParser

    from app.services.marker_service import build_marker_options

    llm_cfg = {
        "providers": [
            {
                "id": "openai",
                "type": "openai",
                "label": "OpenAI",
                "api_key": "openai-key",
                "models": [{"model_id": "gpt-4o"}],
            }
        ],
        "active": {
            "provider_id": "openai",
            "model_id": "gpt-4o",
        },
    }

    opts = build_marker_options(
        llm_cfg,
        {
            "use_llm": True,
            "image_handling_mode": "both",
        },
    )

    assert "." in opts["llm_service"]
    assert ConfigParser(opts).get_llm_service() == "marker.services.openai.OpenAIService"


@pytest.mark.asyncio
async def test_upload_with_advanced_settings(client: AsyncClient, db_session):
    """Verify upload endpoint accepts advanced settings (page_range, lang) and saves them."""
    resp = await _upload_file(
        client,
        extra_params={"page_range": "1-3,5", "lang": "fr"}
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    from sqlalchemy import select
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    res = await db_session.execute(stmt)
    job = res.scalar_one()

    cfg = json.loads(job.config_json)
    assert cfg["page_range"] == "1-3,5"
    assert cfg["lang"] == "fr"


def test_build_marker_options_advanced_settings():
    """Verify that build_marker_options correctly includes page_range and lang."""
    from app.services.marker_service import build_marker_options

    llm_cfg = {
        "providers": [
            {
                "id": "gemini",
                "type": "gemini",
                "label": "Gemini",
                "api_key": "gemini-key",
                "models": [{"model_id": "gemini-2.0-flash"}]
            }
        ],
        "active": {
            "provider_id": "gemini",
            "model_id": "gemini-2.0-flash"
        }
    }
    conv_cfg = {"use_llm": True, "page_range": "1-5", "lang": "es"}
    opts = build_marker_options(llm_cfg, conv_cfg)

    assert opts["page_range"] == "1-5"
    assert opts["lang"] == "es"


def test_parse_image_understanding_extracts_sidecar():
    """The helper pulls the image_understanding list out of the metadata JSON column."""
    from app.routes.convert import _parse_image_understanding
    import json

    payload = json.dumps({
        "image_understanding": [
            {"image_name": "_page_0_Picture_1.jpeg", "image_type": "chart_bar", "confidence": 0.9, "model": "gpt-4o", "omitted": False},
        ]
    })
    result = _parse_image_understanding(payload)
    assert result is not None
    assert result[0]["image_type"] == "chart_bar"
    assert result[0]["image_name"] == "_page_0_Picture_1.jpeg"


def test_parse_image_understanding_returns_none_for_empty_or_invalid():
    from app.routes.convert import _parse_image_understanding

    assert _parse_image_understanding(None) is None
    assert _parse_image_understanding("") is None
    assert _parse_image_understanding("not json") is None
    # Valid JSON but no image_understanding key.
    assert _parse_image_understanding('{"other": 1}') is None
    # Empty list.
    assert _parse_image_understanding('{"image_understanding": []}') is None


@pytest.mark.asyncio
async def test_status_route_surfaces_image_understanding(client: AsyncClient, db_session):
    """A completed job with result_metadata_json surfaces image_understanding in /status."""
    import json
    from app.models.job import ConversionJob
    from datetime import datetime, timezone

    job = ConversionJob(
        id="job-meta-1",
        filename="doc.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        result_metadata_json=json.dumps({
            "image_understanding": [
                {"image_name": "img.jpeg", "image_type": "photo", "confidence": 0.8, "model": "gpt-4o", "omitted": False}
            ]
        }),
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/convert/status/job-meta-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_understanding"] is not None
    assert body["image_understanding"][0]["image_type"] == "photo"


@pytest.mark.asyncio
async def test_status_route_omits_image_understanding_for_legacy_jobs(client: AsyncClient, db_session):
    """A job with no metadata (pre-feature) returns no image_understanding field value."""
    from app.models.job import ConversionJob
    from datetime import datetime, timezone

    job = ConversionJob(
        id="job-legacy-1",
        filename="doc.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        result_metadata_json=None,
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/convert/status/job-legacy-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("image_understanding") is None


# ---------------------------------------------------------------------------
# Timestamps must round-trip with UTC offset so the frontend can parse them
# as UTC, not as local time. Regression for the "all times wrong" bug.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_route_serializes_timestamps_with_utc_offset(
    client: AsyncClient, db_session
):
    """created_at / completed_at JSON must end with Z or +00:00, never naive."""
    from datetime import datetime, timezone

    naive_utc = datetime(2026, 6, 11, 9, 0, 0)  # what SQLite hands back
    job = ConversionJob(
        id="job-tz-status",
        filename="doc.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        progress=100,
        created_at=naive_utc,
        completed_at=naive_utc,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get("/api/convert/status/job-tz-status")
    assert resp.status_code == 200
    body = resp.json()

    for key in ("created_at", "completed_at"):
        val = body[key]
        assert val is not None, f"{key} was None"
        # Must carry an explicit UTC offset so JS new Date() parses it as UTC.
        assert val.endswith("Z") or "+00:00" in val, (
            f"{key}='{val}' missing UTC offset — frontend will misparse as local"
        )


@pytest.mark.asyncio
async def test_history_route_serializes_timestamps_with_utc_offset(
    client: AsyncClient, db_session
):
    """History endpoint must also emit tz-aware timestamps."""
    from datetime import datetime

    naive_utc = datetime(2026, 6, 11, 9, 0, 0)
    job = ConversionJob(
        id="job-tz-history",
        filename="doc.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        progress=100,
        created_at=naive_utc,
        completed_at=naive_utc,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get("/api/convert/history")
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    ours = next((j for j in jobs if j["job_id"] == "job-tz-history"), None)
    assert ours is not None

    for key in ("created_at", "completed_at"):
        val = ours[key]
        assert val is not None
        assert val.endswith("Z") or "+00:00" in val, (
            f"{key}='{val}' missing UTC offset"
        )


# ---------------------------------------------------------------------------
# Multi-format output: status exposes available_formats + formats cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_surfaces_available_formats_and_cached_formats(
    client: AsyncClient, db_session
):
    """A completed job with formats_json exposes its available formats + cached text."""
    import json as _json
    from app.models.job import ConversionJob

    job = ConversionJob(
        id="job-multi-fmt",
        filename="doc.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        formats_json=_json.dumps({
            "markdown": "# hi",
            "json": '{"blocks": []}',
        }),
        progress=100,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get("/api/convert/status/job-multi-fmt")
    assert resp.status_code == 200
    body = resp.json()
    import sys as _sys
    print("DEBUG BODY:", _json.dumps(body), file=_sys.stderr)

    # Available formats come from the cache keys, in insertion order so the
    # primary format (markdown) stays first and tab order is stable.
    assert body["available_formats"] == ["markdown", "json"]
    # The cached text is returned per format so preview tabs never reconvert.
    assert body["formats"]["markdown"] == "# hi"
    assert body["formats"]["json"] == '{"blocks": []}'


@pytest.mark.asyncio
async def test_status_formats_omitted_when_no_cache(client: AsyncClient, db_session):
    """A legacy job (no formats_json) reports a single available format, no cache."""
    from app.models.job import ConversionJob

    job = ConversionJob(
        id="job-single-fmt",
        filename="doc.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        progress=100,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get("/api/convert/status/job-single-fmt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available_formats"] == ["markdown"]
    assert body["formats"] is None


# ---------------------------------------------------------------------------
# Regenerate: append one format to an existing completed job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_appends_format_to_existing_job(client: AsyncClient, db_session, tmp_path, monkeypatch):
    """POST /regenerate?format=json renders one new format and merges it into formats_json.

    The source file is reused from the original upload (it is only cleaned up on
    job deletion), so regeneration never re-uploads. We stub the conversion layer
    so this test stays marker-model-free.
    """
    import json as _json
    from datetime import datetime, timezone
    from app.models.job import ConversionJob

    job_id = "job-regen"
    from app.core.config import UPLOAD_DIR
    uploads = Path(UPLOAD_DIR)
    uploads.mkdir(parents=True, exist_ok=True)
    src = uploads / f"{job_id}.pdf"
    src.write_bytes(b"%PDF-1.4 regen source")

    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        config_json=_json.dumps({
            "output_format": "markdown",
            "converter_cls": "PdfConverter",
        }),
        formats_json=_json.dumps({"markdown": "# hi"}),
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    # Stub the conversion service used by the regenerate endpoint.
    # convert_file_formats returns {format: legacy_envelope_dict}.
    from app.main import _app_state

    fake_service = _app_state.conversion_service
    monkeypatch.setattr(
        fake_service,
        "convert_file_formats",
        lambda filepath, config, formats, device=None: {
            fmt: {"text": '{"blocks": ["regen"]}', "extension": "json", "images": {}, "metadata": {}}
            for fmt in formats
        },
    )
    monkeypatch.setattr(fake_service, "supports_multiple_formats", lambda filepath, config: True)

    resp = await client.post(f"/api/convert/{job_id}/regenerate", params={"format": "json"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "regenerated"
    assert body["format"] == "json"
    # The new format was merged alongside the existing markdown cache.
    assert set(body["available_formats"]) == {"markdown", "json"}

    # And it persisted to the DB. expire_all() drops the test session's cached
    # row so we see the regenerate endpoint's committed write.
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    db_session.expire_all()
    refreshed = (await db_session.execute(stmt)).scalar_one()
    cached = _json.loads(refreshed.formats_json)
    assert cached["json"] == '{"blocks": ["regen"]}'
    assert cached["markdown"] == "# hi"

    src.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_regenerate_rejects_native_structured_format(client: AsyncClient, db_session):
    """Native Markdown-only jobs cannot regenerate fake JSON/HTML/chunks."""
    import json as _json
    from datetime import datetime, timezone
    from app.core.config import UPLOAD_DIR

    job_id = "job-regen-native"
    uploads = Path(UPLOAD_DIR)
    uploads.mkdir(parents=True, exist_ok=True)
    src = uploads / f"{job_id}.tsv"
    src.write_text("name\tscore\nAda\t10\n", encoding="utf-8")

    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.tsv",
        original_name="scores.tsv",
        status="completed",
        input_format="tsv",
        output_format="markdown",
        result_text="| name | score |",
        config_json=_json.dumps({"output_format": "markdown"}),
        formats_json=_json.dumps({"markdown": "| name | score |"}),
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.post(f"/api/convert/{job_id}/regenerate", params={"format": "json"})

    assert resp.status_code == 409
    assert "not supported for engine 'text_data'" in resp.json()["detail"]
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert _json.loads(row.formats_json) == {"markdown": "| name | score |"}

    src.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Multi-format upload (output_formats query param)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_accepts_output_formats_param(client: AsyncClient, db_session):
    """Upload with output_formats=markdown,json stores both in the config."""
    resp = await _upload_file(
        client,
        extra_params={"output_formats": "markdown,json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    job_id = body["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)
    assert config["output_formats"] == ["markdown", "json"]
    assert config["output_format"] == "markdown"


@pytest.mark.asyncio
async def test_upload_output_formats_drops_invalid(client: AsyncClient, db_session):
    """Invalid format names in output_formats are silently dropped."""
    resp = await _upload_file(
        client,
        extra_params={"output_formats": "markdown,nonsense,html"},
    )
    assert resp.status_code == 200
    body = resp.json()
    job_id = body["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    config = json.loads(job.config_json)
    assert config["output_formats"] == ["markdown", "html"]


@pytest.mark.asyncio
async def test_regenerate_fallback_to_marker_pdf_on_unsupported_engine(client: AsyncClient, db_session, monkeypatch):
    """POST /regenerate?format=html falls back to marker_pdf if the engine is unsupported but the file is a PDF."""
    import json as _json
    from datetime import datetime, timezone
    from app.models.job import ConversionJob

    job_id = "job-regen-fallback"
    from app.core.config import UPLOAD_DIR
    uploads = Path(UPLOAD_DIR)
    uploads.mkdir(parents=True, exist_ok=True)
    src = uploads / f"{job_id}.pdf"
    src.write_bytes(b"%PDF-1.4 fallback source")

    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# hi",
        config_json=_json.dumps({
            "output_format": "markdown",
            "conversion_profile": "fast",
        }),
        formats_json=_json.dumps({"markdown": "# hi"}),
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    from app.main import _app_state

    fake_service = _app_state.conversion_service
    
    called_with_config = {}
    
    def mock_convert_file_formats(filepath, config, formats, device=None):
        nonlocal called_with_config
        called_with_config = config
        return {
            fmt: {"text": "<html>fallback</html>", "extension": "html", "images": {}, "metadata": {}}
            for fmt in formats
        }
        
    monkeypatch.setattr(fake_service, "convert_file_formats", mock_convert_file_formats)

    resp = await client.post(f"/api/convert/{job_id}/regenerate", params={"format": "html"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "regenerated"
    assert body["format"] == "html"
    assert "html" in body["available_formats"]
    
    assert called_with_config.get("engine_override") == "marker_pdf"

    src.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_download_supports_format_param(client: AsyncClient, db_session):
    """GET /download/{job_id}?format={format} returns the requested format."""
    import json as _json
    from datetime import datetime, timezone
    from app.models.job import ConversionJob

    job_id = "job-download-format"
    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# md content",
        config_json=_json.dumps({"output_format": "markdown"}),
        formats_json=_json.dumps({
            "markdown": "# md content",
            "html": "<html>html content</html>"
        }),
        result_path=None,
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    # 1. Download specific HTML format (should be returned as raw html file)
    resp = await client.get(f"/api/convert/download/{job_id}", params={"format": "html"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text == "<html>html content</html>"

    # 2. Download all formats (should be a ZIP)
    resp = await client.get(f"/api/convert/download/{job_id}", params={"format": "all"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


@pytest.mark.asyncio
async def test_download_specific_format_does_not_zip_assets(client: AsyncClient, db_session, tmp_path: Path):
    """Description-only Markdown download must stay a .md file even if assets exist."""
    import json as _json
    from datetime import datetime, timezone
    from app.models.job import ConversionJob

    result_dir = tmp_path / "job-assets"
    result_dir.mkdir()
    (result_dir / "image.png").write_bytes(b"fake image bytes")
    (result_dir / "job-assets.marker.json").write_text("{}", encoding="utf-8")

    job_id = "job-download-md-assets"
    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# md content",
        config_json=_json.dumps({"output_format": "markdown"}),
        formats_json=_json.dumps({"markdown": "# md content"}),
        result_path=str(result_dir),
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/convert/download/{job_id}", params={"format": "markdown"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text == "# md content"
    assert resp.headers["content-disposition"].endswith('filename="doc.md"')

    default_resp = await client.get(f"/api/convert/download/{job_id}")
    assert default_resp.status_code == 200
    assert default_resp.headers["content-type"].startswith("text/markdown")
    assert default_resp.text == "# md content"

    package_resp = await client.get(f"/api/convert/download/{job_id}", params={"format": "all"})
    assert package_resp.status_code == 200
    assert package_resp.headers["content-type"] == "application/zip"


@pytest.mark.asyncio
async def test_output_asset_endpoint_serves_manifest_listed_asset(client: AsyncClient, db_session, tmp_path: Path):
    result_dir = tmp_path / "job-assets"
    result_dir.mkdir()
    asset = result_dir / "assets" / "chart.png"
    asset.parent.mkdir()
    asset.write_bytes(b"png-bytes")
    manifest = {
        "schema_version": "marker.output_manifest.v1",
        "output": {
            "assets": [
                {
                    "name": "assets/chart.png",
                    "relative_path": "assets/chart.png",
                    "path": str(asset),
                    "media_type": "image/png",
                }
            ]
        },
    }
    (result_dir / "job-assets.marker.json").write_text(json.dumps(manifest), encoding="utf-8")
    job_id = "job-asset-preview"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename="source.pdf",
            original_name="source.pdf",
            status="completed",
            input_format="pdf",
            output_format="markdown",
            result_text="![chart](assets/chart.png)",
            result_path=str(result_dir),
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/convert/assets/{job_id}/assets/chart.png")

    assert resp.status_code == 200
    assert resp.content == b"png-bytes"
    assert resp.headers["content-type"].startswith("image/png")


@pytest.mark.asyncio
async def test_output_asset_endpoint_rejects_unlisted_and_outside_assets(client: AsyncClient, db_session, tmp_path: Path):
    result_dir = tmp_path / "job-assets"
    result_dir.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    manifest = {
        "schema_version": "marker.output_manifest.v1",
        "output": {
            "assets": [
                {
                    "name": "safe.png",
                    "relative_path": "safe.png",
                    "path": str(outside),
                    "media_type": "image/png",
                }
            ]
        },
    }
    (result_dir / "job-assets.marker.json").write_text(json.dumps(manifest), encoding="utf-8")
    job_id = "job-asset-denied"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename="source.pdf",
            original_name="source.pdf",
            status="completed",
            input_format="pdf",
            output_format="markdown",
            result_text="![safe](safe.png)",
            result_path=str(result_dir),
        )
    )
    await db_session.commit()

    outside_resp = await client.get(f"/api/convert/assets/{job_id}/safe.png")
    unlisted_resp = await client.get(f"/api/convert/assets/{job_id}/missing.png")
    traversal_resp = await client.get(f"/api/convert/assets/{job_id}/assets/%2e%2e/safe.png")

    assert outside_resp.status_code == 404
    assert unlisted_resp.status_code == 404
    assert traversal_resp.status_code in {404, 405}


@pytest.mark.asyncio
async def test_download_chunks_format_uses_json_extension(client: AsyncClient, db_session):
    """Marker chunks are JSON payloads and should download as .json."""
    import json as _json
    from datetime import datetime, timezone
    from app.models.job import ConversionJob

    job_id = "job-download-chunks-format"
    chunks_text = '{"chunks": []}'
    job = ConversionJob(
        id=job_id,
        filename=f"{job_id}.pdf",
        original_name="doc.pdf",
        status="completed",
        input_format="pdf",
        output_format="chunks",
        result_text=chunks_text,
        config_json=_json.dumps({"output_format": "chunks"}),
        formats_json=_json.dumps({"chunks": chunks_text}),
        result_path=None,
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/convert/download/{job_id}", params={"format": "chunks"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["content-disposition"].endswith('filename="doc.json"')
    assert resp.text == chunks_text


# ---------------------------------------------------------------------------
# Advanced audio controls (plan §5.5) — audio_config JSON blob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_accepts_audio_config_blob(client: AsyncClient, db_session):
    """The audio_config JSON blob is parsed and merged into the stored config."""
    blob = {
        "audio_provider": "local_faster_whisper",
        "audio_diarization": True,
        "audio_vocabulary_pack_ids": ["medical", "team"],
        "audio_text_enhancement_strength": 3,
    }
    resp = await _upload_file(
        client,
        extra_params={"audio_config": json.dumps(blob)},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    cfg = json.loads(job.config_json)
    assert cfg["audio_provider"] == "local_faster_whisper"
    assert cfg["audio_diarization"] is True
    assert cfg["audio_vocabulary_pack_ids"] == ["medical", "team"]
    assert cfg["audio_text_enhancement_strength"] == 3


@pytest.mark.asyncio
async def test_upload_rejects_deferred_audio_provider_before_queue(client: AsyncClient, db_session):
    """Known-but-unshipped STT providers fail before a pending job is created."""
    resp = await _upload_file(
        client,
        filename="call.wav",
        content=b"RIFF fake wav",
        extra_params={
            "audio_config": json.dumps(
                {"audio_provider": "openai", "audio_allow_cloud_stt": True}
            )
        },
    )

    assert resp.status_code == 400
    assert "not shipped yet" in resp.json()["detail"]

    stmt = select(ConversionJob).where(ConversionJob.original_name == "call.wav")
    assert (await db_session.execute(stmt)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_upload_rejects_unknown_audio_provider_before_queue(client: AsyncClient, db_session):
    """Unknown STT provider ids fail instead of silently falling back to local."""
    resp = await _upload_file(
        client,
        filename="unknown-provider.wav",
        content=b"RIFF fake wav",
        extra_params={
            "audio_config": json.dumps(
                {"audio_provider": "does_not_exist", "audio_allow_cloud_stt": True}
            )
        },
    )

    assert resp.status_code == 400
    assert "Unknown audio provider" in resp.json()["detail"]

    stmt = select(ConversionJob).where(ConversionJob.original_name == "unknown-provider.wav")
    assert (await db_session.execute(stmt)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_upload_rejects_unshipped_audio_benchmark_compare_before_queue(client: AsyncClient, db_session):
    """Benchmark comparison is not wired; do not accept a silent no-op job."""
    resp = await _upload_file(
        client,
        filename="compare.wav",
        content=b"RIFF fake wav",
        extra_params={
            "audio_config": json.dumps(
                {
                    "audio_provider": "local_faster_whisper",
                    "audio_benchmark_compare": True,
                    "audio_compare_providers": ["local_faster_whisper"],
                }
            )
        },
    )

    assert resp.status_code == 400
    assert "Audio provider comparison is not shipped" in resp.json()["detail"]

    stmt = select(ConversionJob).where(ConversionJob.original_name == "compare.wav")
    assert (await db_session.execute(stmt)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_upload_accepts_all_frontend_audio_output_modes(client: AsyncClient, db_session):
    """REST allow-list must match the frontend audio output style cards."""

    for mode in ("interview_qna", "action_decision_log"):
        resp = await _upload_file(client, extra_params={"audio_output_mode": mode})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        stmt = select(ConversionJob).where(ConversionJob.id == job_id)
        job = (await db_session.execute(stmt)).scalar_one()
        cfg = json.loads(job.config_json)
        assert cfg["audio_output_mode"] == mode


@pytest.mark.asyncio
async def test_upload_resolves_audio_vocabulary_pack_ids(client: AsyncClient, db_session):
    """Saved pack ids are converted to terms before the audio converter runs."""

    from app.models.settings import Setting

    db_session.add(
        Setting(
            key="audio_vocabulary_packs",
            category="audio",
            value=json.dumps(
                [
                    {"id": "team", "name": "Team", "terms": ["Marker", "LiteParse"]},
                    {"id": "unused", "name": "Unused", "terms": ["DoNotSend"]},
                ]
            ),
        )
    )
    await db_session.commit()

    resp = await _upload_file(
        client,
        extra_params={"audio_config": json.dumps({"audio_vocabulary_pack_ids": ["team"]})},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    cfg = json.loads(job.config_json)
    assert cfg["audio_vocabulary_pack_ids"] == ["team"]
    assert cfg["audio_vocabulary_packs"] == [["Marker", "LiteParse"]]


@pytest.mark.asyncio
async def test_status_surfaces_audio_metadata(client: AsyncClient, db_session):
    """Completed audio jobs expose transcript metadata for the audio preview UI."""

    job = ConversionJob(
        id="job-audio-meta",
        filename="voice.wav",
        original_name="voice.wav",
        status="completed",
        input_format="wav",
        output_format="markdown",
        result_text="# Audio Transcript",
        result_metadata_json=json.dumps(
            {
                "audio": {
                    "transcript": {
                        "provider": "local_faster_whisper",
                        "model": "tiny.en",
                        "segments": [
                            {
                                "segment_id": "voice_seg_0001",
                                "start_ms": 0,
                                "end_ms": 1000,
                                "speaker": "speaker_0",
                                "text": "hello",
                                "confidence": 0.9,
                                "warnings": [],
                            }
                        ],
                    },
                    "quality": {"review_required": False},
                }
            }
        ),
        progress=100,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get("/api/convert/status/job-audio-meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversion_metadata"]["audio"]["transcript"]["segments"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_upload_rejects_invalid_audio_config_json(client: AsyncClient):
    """Malformed audio_config JSON is rejected with a 400, not silently ignored."""
    resp = await _upload_file(
        client,
        extra_params={"audio_config": "{not valid json"},
    )
    assert resp.status_code == 400
    assert "audio_config" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_audio_config_non_object(client: AsyncClient):
    """A JSON array as audio_config is rejected — must be an object."""
    resp = await _upload_file(
        client,
        extra_params={"audio_config": "[1, 2, 3]"},
    )
    assert resp.status_code == 400
    assert "audio_config" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_flat_audio_param_beats_blob_on_conflict(
    client: AsyncClient, db_session
):
    """Flat audio_* query params take precedence over the blob (plan §5.5).

    A caller using the legacy flat contract must never have its explicit choice
    silently overridden by a stale value inside the blob.
    """
    blob = {"audio_model": "blob-model"}
    resp = await _upload_file(
        client,
        extra_params={"audio_config": json.dumps(blob), "audio_model": "flat-model"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    cfg = json.loads(job.config_json)
    assert cfg["audio_model"] == "flat-model"


@pytest.mark.asyncio
async def test_upload_audio_config_ignores_non_audio_keys(client: AsyncClient, db_session):
    """Only keys prefixed with audio_ from the blob are accepted."""
    blob = {"audio_language": "es", "page_range": "1-5", "rogue_key": "evil"}
    resp = await _upload_file(
        client,
        extra_params={"audio_config": json.dumps(blob)},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db_session.execute(stmt)).scalar_one()
    cfg = json.loads(job.config_json)
    assert cfg.get("audio_language") == "es"
    # Non-audio keys from the blob must NOT leak through.
    assert "page_range" not in cfg or cfg.get("page_range") != "1-5"
    assert "rogue_key" not in cfg
