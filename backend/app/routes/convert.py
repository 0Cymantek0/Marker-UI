"""Conversion endpoints - upload, status, download, history."""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
import zipfile
import asyncio
import ipaddress
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

import aiofiles
import httpx

from app.core.config import MAX_UPLOAD_SIZE, OUTPUT_DIR, UPLOAD_DIR
from app.conversion.probe import PdfProbeResult, plan_pdf_routing_segments, probe_pdf
from app.database import get_db
from app.models.job import ConversionJob
from app.models.schemas import ConversionResponse, JobStatusResponse, HistoryResponse, ConvertPlanRequest, ConverterPlanResponse

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".msg", ".xlsx", ".xls", ".epub", ".html",
    ".htm", ".csv", ".tsv", ".json", ".jsonl", ".txt", ".md", ".rst",
    ".log", ".xml", ".rss", ".atom", ".ipynb", ".zip",
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac",
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp"
}
MAX_PAGE_RANGE_PAGES = 500
HARD_MAX_PAGE_RANGE_PAGES = 2000
MAX_URL_REDIRECTS = 5

# MAX_UPLOAD_SIZE is imported from app.core.config so the upload + source_url
# download paths share a single source of truth driven by
# MARKER_MAX_UPLOAD_SIZE_MB.

router = APIRouter(prefix="/api/convert", tags=["convert"])

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-outlook": ".msg",
    "application/zip": ".zip",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/json": ".json",
    "application/x-ndjson": ".jsonl",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/aac": ".aac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
}


def _count_requested_pages(page_range: str) -> int:
    count = 0
    for part in page_range.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            if start <= 0 or end <= 0 or end < start:
                raise ValueError
            count += end - start + 1
        else:
            page = int(token)
            if page <= 0:
                raise ValueError
            count += 1
    return count


def _safe_source_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _assert_safe_source_url(raw_url: str) -> None:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="source_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="source_url must not contain credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail="source_url host could not be resolved") from exc
    for family, _socktype, _proto, _canonname, sockaddr in addresses:
        host = sockaddr[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="source_url resolved to an invalid address") from exc
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HTTPException(status_code=400, detail="source_url resolves to a private or local network address")


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() in {"filename", "filename*"}:
            filename = raw.strip().strip('"')
            if "''" in filename:
                filename = filename.split("''", 1)[1]
            return Path(filename.replace("\\", "/")).name
    return None


def _extension_for_download(url: str, headers: httpx.Headers) -> tuple[str, str]:
    content_type = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    header_name = _filename_from_content_disposition(headers.get("content-disposition"))
    path_name = Path(urlparse(url).path).name
    filename = header_name or path_name or "download"
    ext_from_type = _CONTENT_TYPE_EXTENSIONS.get(content_type)
    ext_from_name = Path(filename).suffix.lower()
    suffix = ext_from_type or ext_from_name
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported downloaded content type or extension '{content_type or suffix}'",
        )
    stem = Path(filename).stem or "download"
    safe_stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)[:80] or "download"
    return f"{safe_stem}{suffix}", suffix


async def _download_source_url(raw_url: str, destination: Path) -> tuple[str, str, str]:
    current_url = raw_url
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for _ in range(MAX_URL_REDIRECTS + 1):
            _assert_safe_source_url(current_url)
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise HTTPException(status_code=400, detail="source_url redirect missing Location header")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code >= 400:
                    raise HTTPException(status_code=400, detail=f"source_url returned HTTP {response.status_code}")
                original_name, suffix = _extension_for_download(current_url, response.headers)
                total = 0
                async with aiofiles.open(destination, "wb") as f:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_SIZE:
                            destination.unlink(missing_ok=True)
                            raise HTTPException(
                                status_code=413,
                                detail=f"Downloaded file exceeds maximum size of {MAX_UPLOAD_SIZE} bytes.",
                            )
                        await f.write(chunk)
                return original_name, suffix, _safe_source_url(current_url)
    raise HTTPException(status_code=400, detail="source_url exceeded redirect limit")


def _validate_page_range(page_range: str, page_count: int) -> None:
    try:
        requested = _count_requested_pages(page_range)
        highest = 0
        for part in page_range.split(","):
            token = part.strip()
            if not token:
                continue
            highest = max(highest, int(token.split("-")[-1]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid page_range format") from exc
    if requested <= 0:
        raise HTTPException(status_code=400, detail="Invalid page_range format")
    if highest > page_count:
        raise HTTPException(
            status_code=400,
            detail=f"page_range exceeds document length ({page_count} pages)",
        )
    if requested > HARD_MAX_PAGE_RANGE_PAGES:
        raise HTTPException(status_code=400, detail="page_range exceeds hard cap of 2000 pages")
    if requested > MAX_PAGE_RANGE_PAGES:
        raise HTTPException(status_code=400, detail="page_range exceeds cap of 500 pages")


def _parse_image_understanding(metadata_json: str | None) -> list[dict] | None:
    """Extract the per-image sidecar list from a job's metadata column.

    Returns None when there is no metadata or no understanding entries, so the
    JSON response omits the field entirely for legacy jobs (graceful degrade).
    """
    if not metadata_json:
        return None
    try:
        parsed = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    entries = parsed.get("image_understanding") if isinstance(parsed, dict) else None
    return entries or None


def _parse_conversion_metadata(metadata_json: str | None) -> dict[str, Any] | None:
    if not metadata_json:
        return None
    try:
        parsed = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    metadata = {
        key: parsed[key]
        for key in ("engine", "probe_result", "mixed_engine_segments", "assets", "manifest_path")
        if key in parsed and parsed[key]
    }
    return metadata or None


def _planned_mixed_segments(probe_data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(probe_data, dict) or not probe_data.get("page_results"):
        return None
    probe = PdfProbeResult.from_mapping(probe_data)
    segments = plan_pdf_routing_segments(probe)
    if len(segments) <= 1:
        return None
    return [
        {
            "pages": list(segment.pages),
            "page_range": _pages_to_range(segment.pages),
            "requested_engine": (
                "liteparse_pdf" if segment.engine == "liteparse" else "marker_pdf"
            ),
            "actual_engine": (
                "liteparse_pdf" if segment.engine == "liteparse" else "marker_pdf"
            ),
            "reasons": list(segment.reasons),
            "fallback_reason": None,
        }
        for segment in segments
    ]


def _pages_to_range(pages: list[int]) -> str:
    if not pages:
        return ""
    ranges: list[str] = []
    start = prev = sorted(pages)[0]
    for page in sorted(pages)[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = page
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


async def _load_llm_config(db: AsyncSession) -> dict[str, Any]:
    """Load stored LLM providers and active LLM configuration."""
    from app.models.settings import Setting
    from app.routes.settings import init_llm_providers_if_missing
    import json

    stmt = select(Setting).where(Setting.key == "llm_providers")
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        await init_llm_providers_if_missing(db)
        row = (await db.execute(stmt)).scalar_one_or_none()

    providers = json.loads(row.value) if row else []

    stmt = select(Setting).where(Setting.key == "llm_global_active")
    active_row = (await db.execute(stmt)).scalar_one_or_none()
    active = json.loads(active_row.value) if active_row else {"provider_id": "none", "model_id": ""}

    return {
        "providers": providers,
        "active": active
    }


# ------------------------------------------------------------------
# Upload & start conversion
# ------------------------------------------------------------------


@router.post("/upload", response_model=ConversionResponse)
async def upload_file(
    file: Optional[UploadFile] = File(None),
    local_filepath: Optional[str] = Query(None, description="Optional local absolute file path on the server"),
    source_url: Optional[str] = Query(None, description="Optional public http(s) document URL"),
    output_dir: Optional[str] = Query(None, description="Optional custom output directory path"),
    output_format: str = Query("markdown", description="Output format: markdown, json, html, chunks"),
    converter: Optional[str] = Query(None, description="Converter class: PdfConverter, TableConverter, OCRConverter"),
    engine_override: Optional[str] = Query(None, description="Optional explicit conversion engine override"),
    conversion_profile: Optional[str] = Query(None, description="Conversion profile: auto, fast, high_accuracy"),
    use_llm: bool = Query(False, description="Enable LLM-assisted conversion"),
    llm_provider: Optional[str] = Query(None, description="LLM provider ID override"),
    llm_model: Optional[str] = Query(None, description="LLM model name override"),
    image_handling_mode: str = Query(
        "extraction",
        description="Image handling: extraction, understanding, or both",
    ),
    allow_cloud_vlm: bool = Query(
        False,
        description="Allow image-understanding crops to be sent to the configured cloud VLM provider",
    ),
    force_ocr: bool = Query(False, description="Force OCR on all pages"),
    paginate_output: bool = Query(False, description="Add page separators in output"),
    disable_image_extraction: bool = Query(False, description="Skip extracting images"),
    page_range: Optional[str] = Query(None, description="Page range e.g. '1-5,8,10-12'"),
    lang: Optional[str] = Query(None, description="Document language hint"),
    audio_output_mode: Optional[str] = Query(None, description="Audio output: transcript, meeting_notes, lecture_notes, or enhanced"),
    audio_model: Optional[str] = Query(None, description="Local STT model name for faster-whisper"),
    audio_vocabulary: Optional[str] = Query(None, description="Comma/newline-separated vocabulary hints for audio transcription"),
    audio_context: Optional[str] = Query(None, description="Context used only to organize audio batch output"),
    audio_low_confidence_threshold: Optional[float] = Query(None, ge=0.0, le=1.0, description="Segment confidence threshold for audio warnings"),
    audio_word_timestamps: bool = Query(False, description="Request word-level timestamps from the STT engine when supported"),
    disable_multiprocessing: bool = Query(False, description="Run single-threaded"),
    strip_existing_ocr: bool = Query(False, description="Strip existing OCR text"),
    redo_inline_math: bool = Query(False, description="Re-render inline math"),
    debug: bool = Query(False, description="Enable debug output"),
    # --- Image-understanding pipeline knobs (mirror ImageUnderstandingConfig).
    #     Optional[...] = None so only knobs the UI actually sends override the
    #     backend schema defaults; others fall through to the processor defaults.
    router_enabled: Optional[bool] = Query(None, description="Master switch for the Tier-0 router (off = legacy path)"),
    smart_router_level: Optional[str] = Query(None, description="Tier-0 routing brain: disabled | smart | beeg_brain"),
    dedup_enabled: Optional[bool] = Query(None, description="Collapse repeated identical images to one extraction"),
    downscale_vlm_crops: Optional[bool] = Query(None, description="Downscale crops before VLM send (cost lever)"),
    batch_enabled: Optional[bool] = Query(None, description="Batch route+extract calls instead of serial per-image"),
    ocr_engine: Optional[str] = Query(None, description="Local OCR engine (only 'surya' ships; others fall back)"),
    decorative_max_text_density: Optional[float] = Query(None, ge=0.0, le=1.0, description="Text-density at/below which an image is decorative"),
    ocr_min_text_density: Optional[float] = Query(None, ge=0.0, le=1.0, description="Text-density at/above which an image routes to local OCR"),
    ocr_min_lines: Optional[int] = Query(None, ge=1, description="Min detected text lines to consider the OCR route"),
    dedup_max_distance: Optional[int] = Query(None, ge=0, le=64, description="Max aHash Hamming distance treated as duplicate (0 = exact)"),
    vlm_crop_max_px: Optional[int] = Query(None, ge=64, le=4096, description="Longest-side pixel cap applied to a crop before VLM send"),
    vlm_batch_size: Optional[int] = Query(None, ge=1, le=64, description="Images per batched VLM call"),
    max_batch_retries: Optional[int] = Query(None, ge=0, le=5, description="Max extra batch calls to recover missing/garbled indices"),
    db: AsyncSession = Depends(get_db),
) -> ConversionResponse:
    """Accept a document upload or local file path, create a job, and start conversion."""
    source_count = sum(1 for item in (file, local_filepath, source_url) if item)
    if source_count == 0:
        raise HTTPException(
            status_code=400,
            detail="Either an uploaded file, local_filepath, or source_url must be provided.",
        )
    if source_count > 1:
        raise HTTPException(status_code=400, detail="Provide only one input source.")

    original_name = ""
    suffix = ""
    stored_path = ""
    is_local = False
    source_url_safe: str | None = None
    job_id = str(uuid.uuid4())

    if local_filepath:
        path = Path(local_filepath)
        if not path.is_absolute():
            raise HTTPException(
                status_code=400,
                detail="The local_filepath must be an absolute path.",
            )
        if not path.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"Local file not found: {local_filepath}",
            )
        original_name = path.name
        suffix = path.suffix.lower()
        stored_path = str(path)
        is_local = True
    elif source_url:
        stored_path_obj = UPLOAD_DIR / f"{job_id}.download"
        original_name, suffix, source_url_safe = await _download_source_url(source_url, stored_path_obj)
        final_path = UPLOAD_DIR / f"{job_id}{suffix}"
        stored_path_obj.replace(final_path)
        stored_path = str(final_path)
        is_local = False
    else:
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="No file or filename provided")
        original_name = file.filename
        suffix = Path(file.filename).suffix.lower()
        is_local = False

    # Validate file extension
    if suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}' (not supported). Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    input_format = suffix.lstrip(".")

    stored_name = f"{job_id}{suffix}"

    if not is_local and file:
        stored_path_obj = UPLOAD_DIR / stored_name
        stored_path = str(stored_path_obj)
        # Stream upload to disk with size limit
        limit_exceeded = False
        try:
            total_size = 0
            async with aiofiles.open(stored_path_obj, "wb") as f:
                while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                    total_size += len(chunk)
                    if total_size > MAX_UPLOAD_SIZE:
                        limit_exceeded = True
                        break
                    await f.write(chunk)
        except Exception as exc:
            stored_path_obj.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

        if limit_exceeded:
            stored_path_obj.unlink(missing_ok=True)
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds maximum size (too large) of {MAX_UPLOAD_SIZE} bytes.",
            )

    # Build conversion config from query params
    config: dict[str, Any] = {
        "output_format": output_format,
        "original_name": original_name,
    }
    if converter:
        config["converter_cls"] = converter
    if engine_override:
        config["engine_override"] = engine_override
    if conversion_profile:
        config["conversion_profile"] = conversion_profile
    if use_llm:
        config["use_llm"] = True
    if llm_provider:
        config["llm_provider"] = llm_provider
    if llm_model:
        config["llm_model"] = llm_model
    if image_handling_mode in ("extraction", "understanding", "both"):
        config["image_handling_mode"] = image_handling_mode
    config["allow_cloud_vlm"] = allow_cloud_vlm
    if force_ocr:
        config["force_ocr"] = True
    if paginate_output:
        config["paginate_output"] = True
    if disable_image_extraction:
        config["disable_image_extraction"] = True
    if page_range:
        config["page_range"] = page_range
    if lang:
        config["lang"] = lang
    if audio_output_mode in {"transcript", "enhanced", "notes", "meeting_notes", "lecture_notes"}:
        config["audio_output_mode"] = audio_output_mode
    if audio_model:
        config["audio_model"] = audio_model
    if audio_vocabulary:
        config["audio_vocabulary"] = audio_vocabulary
    if audio_context:
        config["audio_context"] = audio_context
    if audio_low_confidence_threshold is not None:
        config["audio_low_confidence_threshold"] = audio_low_confidence_threshold
    if audio_word_timestamps:
        config["audio_word_timestamps"] = True
    if disable_multiprocessing:
        config["disable_multiprocessing"] = True
    if strip_existing_ocr:
        config["strip_existing_ocr"] = True
    if redo_inline_math:
        config["redo_inline_math"] = True
    if debug:
        config["debug"] = True
    # --- Image-understanding pipeline knobs. None = not sent by the UI, so the
    #     processor keeps its schema default. Anything non-None is an explicit
    #     user override and flows through IMAGE_UNDERSTANDING_CONFIG_KEYS.
    if router_enabled is not None:
        config["router_enabled"] = router_enabled
    if smart_router_level in ("disabled", "smart", "beeg_brain"):
        config["smart_router_level"] = smart_router_level
    if dedup_enabled is not None:
        config["dedup_enabled"] = dedup_enabled
    if downscale_vlm_crops is not None:
        config["downscale_vlm_crops"] = downscale_vlm_crops
    if batch_enabled is not None:
        config["batch_enabled"] = batch_enabled
    if ocr_engine in ("surya", "glm_ocr", "paddleocr_vl", "mistral_ocr"):
        config["ocr_engine"] = ocr_engine
    if decorative_max_text_density is not None:
        config["decorative_max_text_density"] = decorative_max_text_density
    if ocr_min_text_density is not None:
        config["ocr_min_text_density"] = ocr_min_text_density
    if ocr_min_lines is not None:
        config["ocr_min_lines"] = ocr_min_lines
    if dedup_max_distance is not None:
        config["dedup_max_distance"] = dedup_max_distance
    if vlm_crop_max_px is not None:
        config["vlm_crop_max_px"] = vlm_crop_max_px
    if vlm_batch_size is not None:
        config["vlm_batch_size"] = vlm_batch_size
    if max_batch_retries is not None:
        config["max_batch_retries"] = max_batch_retries
    if local_filepath:
        config["local_filepath"] = local_filepath
    if source_url_safe:
        config["source_url"] = source_url_safe
    if output_dir:
        config["output_dir"] = output_dir

    if suffix == ".pdf":
        probe_result = await asyncio.to_thread(probe_pdf, stored_path)
        config["probe_result"] = probe_result.to_dict()
        if page_range and probe_result.page_count > 0:
            _validate_page_range(page_range, probe_result.page_count)

    # DB record
    job = ConversionJob(
        id=job_id,
        filename=stored_name if not is_local else original_name,
        original_name=original_name,
        status="pending",
        input_format=input_format,
        output_format=output_format,
        config_json=json.dumps(config),
    )
    db.add(job)
    await db.flush()

    from app.main import _app_state

    marker_service = _app_state.marker_service
    conversion_service = _app_state.conversion_service
    task_manager = _app_state.task_manager

    llm_config = await _load_llm_config(db)

    from app.services.marker_service import build_marker_options
    options = build_marker_options(llm_config, config)

    task_manager.submit_job(job_id, stored_path, options, conversion_service)

    return ConversionResponse(
        job_id=job_id,
        status="pending",
        filename=original_name,
        output_format=output_format,
    )


@router.post("/plan", response_model=ConverterPlanResponse)
async def plan_conversion(
    req: ConvertPlanRequest,
) -> ConverterPlanResponse:
    """Predict the conversion plan for a file before uploading."""
    from app.main import _app_state

    config: dict[str, Any] = {}
    preliminary = True
    if req.engine_override:
        config["engine_override"] = req.engine_override
    if req.conversion_profile:
        config["conversion_profile"] = req.conversion_profile
    if req.image_handling_mode:
        config["image_handling_mode"] = req.image_handling_mode
    if req.converter_cls:
        config["converter_cls"] = req.converter_cls
    if req.force_ocr:
        config["force_ocr"] = True
    if req.local_filepath:
        path = Path(req.local_filepath)
        if path.is_absolute() and path.is_file() and path.suffix.lower() == ".pdf":
            probe_result = await asyncio.to_thread(probe_pdf, path)
            config["probe_result"] = probe_result.to_dict()
            preliminary = False

    plan = (
        _app_state.conversion_service.plan(req.local_filepath, config)
        if req.local_filepath and not preliminary
        else _app_state.conversion_service.plan_by_metadata(req.filename, req.size, config)
    )
    return ConverterPlanResponse(
        engine=plan.engine,
        label=plan.label,
        confidence=plan.confidence,
        reasons=plan.reasons,
        needs_marker_models=plan.needs_marker_models,
        needs_gpu=plan.needs_gpu,
        execution_backend=plan.execution_backend,
        needs_cloud=plan.needs_cloud,
        optional_dependencies=plan.optional_dependencies,
        fallback_chain=plan.fallback_chain,
        warnings=plan.warnings,
        preliminary=preliminary,
        probe_result=config.get("probe_result"),
        mixed_engine_segments=(
            _planned_mixed_segments(config.get("probe_result"))
            if plan.engine == "mixed_pdf"
            else None
        ),
    )


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------


@router.get("/status/{job_id}", response_model=JobStatusResponse)
async def get_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    """Return current job status."""
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job.status
    progress = job.progress
    message = None
    logs = None
    elapsed = None
    eta = None

    # Merge in-memory progress from task manager if still processing
    if job.status not in ("completed", "failed", "cancelled"):
        from app.main import _app_state

        live = _app_state.task_manager.get_status(job_id)
        if live.get("status") in ("processing", "completed", "failed", "cancelled"):
            status = live["status"]
            progress = max(progress, live.get("progress", 0))
            message = live.get("message")
            logs = live.get("logs")
            elapsed = live.get("elapsed")
            eta = live.get("eta")

    # Parse config to extract converter
    converter = "PdfConverter"
    if job.config_json:
        try:
            import json
            cfg = json.loads(job.config_json)
            converter = cfg.get("converter_cls", "PdfConverter")
        except Exception:
            pass

    return JobStatusResponse(
        job_id=job.id,
        status=status,
        progress=progress,
        error_message=job.error_message,
        result_text=job.result_text,
        image_understanding=_parse_image_understanding(job.result_metadata_json),
        conversion_metadata=_parse_conversion_metadata(job.result_metadata_json),
        created_at=job.created_at,
        completed_at=job.completed_at,
        filename=job.original_name,
        output_format=job.output_format,
        converter=converter,
        message=message,
        logs=logs,
        elapsed=elapsed,
        eta=eta,
    )


# ------------------------------------------------------------------
# SSE events
# ------------------------------------------------------------------


@router.get("/events/{job_id}")
async def job_events(request: Request, job_id: str):
    from sse_starlette.sse import EventSourceResponse

    from app.main import _app_state

    return EventSourceResponse(_app_state.task_manager.job_events(request, job_id))


# ------------------------------------------------------------------
# Download
# ------------------------------------------------------------------


@router.get("/download/{job_id}")
async def download_result(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download the converted output file."""
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")
    if not job.result_path:
        text_path = UPLOAD_DIR / f"{job_id}_output.md"
        text_path.write_text(job.result_text or "", encoding="utf-8")
        return FileResponse(
            path=text_path,
            filename=f"{Path(job.original_name).stem}.md",
            media_type="text/markdown",
        )

    result_path = Path(job.result_path)
    if result_path.is_dir():
        tmp_file = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_zip = Path(tmp_file.name)
        tmp_file.close()
        try:
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_in_dir in sorted(result_path.rglob("*")):
                    if file_in_dir.is_file():
                        zf.write(file_in_dir, file_in_dir.relative_to(result_path))
            return FileResponse(
                path=str(tmp_zip),
                filename=f"{Path(job.original_name).stem}.zip",
                media_type="application/zip",
                background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
            )
        except Exception:
            tmp_zip.unlink(missing_ok=True)
            raise

    ext = Path(job.result_path).suffix.lstrip(".") or "md"
    filename = f"{Path(job.original_name).stem}.{ext}"
    return FileResponse(
        path=job.result_path,
        filename=filename,
    )


# ------------------------------------------------------------------
# History
# ------------------------------------------------------------------


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> HistoryResponse:
    """List all conversion jobs (paginated)."""
    offset = (page - 1) * page_size

    # Query total count
    count_stmt = select(func.count(ConversionJob.id))
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    stmt = (
        select(ConversionJob)
        .order_by(ConversionJob.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    jobs = result.scalars().all()

    # Parse configs to extract converter
    res_jobs = []
    for j in jobs:
        converter = "PdfConverter"
        if j.config_json:
            try:
                import json
                cfg = json.loads(j.config_json)
                converter = cfg.get("converter_cls", "PdfConverter")
            except Exception:
                pass
        res_jobs.append(
            JobStatusResponse(
                job_id=j.id,
                status=j.status,
                progress=j.progress,
                error_message=j.error_message,
                result_text=None,  # Exclude from history - use /status endpoint for full text
                created_at=j.created_at,
                completed_at=j.completed_at,
                filename=j.original_name,
                output_format=j.output_format,
                converter=converter,
                conversion_metadata=_parse_conversion_metadata(j.result_metadata_json),
            )
        )

    return HistoryResponse(
        jobs=res_jobs,
        total=total
    )


# ------------------------------------------------------------------
# Delete / Cancel
# ------------------------------------------------------------------


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Cancel (if running) and delete a conversion job."""
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Cancel if still processing
    from app.main import _app_state

    await _app_state.task_manager.cancel_job(job_id)

    # Clean up uploaded file
    upload_path = UPLOAD_DIR / job.filename
    if upload_path.exists():
        upload_path.unlink()

    # Clean up result file
    if job.result_path:
        result_path = Path(job.result_path)
        if result_path.exists():
            if result_path.is_dir():
                import shutil
                shutil.rmtree(result_path)
            else:
                result_path.unlink()

    await db.delete(job)

    return {"status": "deleted", "job_id": job_id}


@router.get("/browse-folder")
async def browse_folder() -> dict[str, str]:
    """Open a native folder selection dialog and return the selected path."""
    raise HTTPException(
        status_code=501,
        detail="Local file/folder browsing is not supported in server/headless environments."
    )


@router.get("/browse-files")
async def browse_files() -> dict[str, list[str]]:
    """Open a native file selection dialog and return the selected paths."""
    raise HTTPException(
        status_code=501,
        detail="Local file/folder browsing is not supported in server/headless environments."
    )

