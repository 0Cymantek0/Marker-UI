"""Conversion endpoints - upload, status, download, history."""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
import zipfile
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

import aiofiles

from app.core.config import MAX_UPLOAD_SIZE, UPLOAD_DIR
from app.agent_contract import AUDIO_OUTPUT_MODES
from app.conversion.dependencies import get_engine_status
from app.conversion.engine_policy import validate_engine_override
from app.conversion.formats import (
    OUTPUT_FORMAT_SET,
    OUTPUT_FORMATS_DESCRIPTION,
    UPLOAD_ALLOWED_EXTENSIONS,
    renderable_output_formats_for_engine,
)
from app.conversion.probe import PdfProbeResult, plan_pdf_routing_segments, probe_pdf
from app.database import get_db
from app.errors import InputNotAllowedError, UnsupportedFormatError, UsageError
from app.models.job import ConversionJob
from app.models.schemas import ConversionResponse, JobStatusResponse, HistoryResponse, ConvertPlanRequest, ConverterPlanResponse, RetryJobRequest
from app.operational.as_of import AsOfVerification, derive_as_of, verify_as_of
from app.audio.providers.registry import (
    validate_audio_benchmark_selection,
    validate_audio_diarization_selection,
    validate_audio_fusion_selection,
    validate_provider_selection,
)
from app.services.policy import (
    assert_rest_local_input_allowed,
    assert_rest_output_write_allowed,
)
from app.services.source_acquisition import (
    SOURCE_CONFIG_KEY,
    default_source_acquisition_service,
)
from app.services.audit import record_audit_event
from app.services.safe_url_fetcher import (
    SafeUrlFetchError,
    assert_safe_source_url,
    download_source_url,
)
from app.services.format_store import (
    available_formats as available_cached_formats,
    normalize_formats,
    parse_formats as parse_cached_formats,
)
from app.services.output_format_policy import require_supported_output_formats
from app.services.job_artifacts import job_artifact_paths, remove_paths

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = UPLOAD_ALLOWED_EXTENSIONS
MAX_PAGE_RANGE_PAGES = 500
HARD_MAX_PAGE_RANGE_PAGES = 2000
AUDIO_PROVIDER_VALIDATED_EXTENSIONS = frozenset(
    {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4", ".mov", ".mkv", ".webm", ".avi"}
)
HISTORY_STATUS_ALIASES = {"queued": "pending"}

# MAX_UPLOAD_SIZE is imported from app.core.config so the upload + source_url
# download paths share a single source of truth driven by
# MARKER_MAX_UPLOAD_SIZE_MB.

router = APIRouter(prefix="/api/convert", tags=["convert"])

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _assert_safe_source_url(raw_url: str) -> None:
    try:
        assert_safe_source_url(raw_url)
    except SafeUrlFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _download_source_url(raw_url: str, destination: Path, db: AsyncSession) -> tuple[str, str, str]:
    async def audit_hook(event: str, payload: dict[str, Any]) -> None:
        await record_audit_event(
            db,
            event_type=event,
            surface="rest",
            resource_type="source_url",
            resource_id=payload.get("url"),
            status="success",
            payload=payload,
        )

    try:
        downloaded = await download_source_url(
            raw_url,
            destination,
            allowed_extensions=ALLOWED_EXTENSIONS,
            max_bytes=MAX_UPLOAD_SIZE,
            audit_hook=audit_hook,
        )
    except SafeUrlFetchError as exc:
        await record_audit_event(
            db,
            event_type="url_fetch.blocked" if exc.category in {"blocked", "unsafe"} else "url_fetch.failed",
            surface="rest",
            resource_type="source_url",
            resource_id=raw_url,
            status="denied" if exc.category in {"blocked", "unsafe"} else "failed",
            payload={"url": raw_url, "category": exc.category, "detail": exc.detail},
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return downloaded.original_name, downloaded.suffix, downloaded.safe_url


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


def _parse_formats(formats_json: str | None) -> dict[str, str] | None:
    """Read the cached ``{format: text}`` map for a job.

    Returns None when the job has no cached formats (legacy single-format jobs,
    or jobs that never completed), so the response omits the field and the UI
    falls back to the single-format preview.
    """
    return parse_cached_formats(formats_json)


def _as_of_headers(verification: AsOfVerification) -> dict[str, str]:
    """Label every export response with the as-of state it actually serves.

    ``verified``: the caller presented an observed state token and it matched
    the current derivation. ``historical``: no currency claim was made, so the
    response carries the actual current state rather than an implied one.
    """

    return {
        "X-Marker-As-Of-State": verification.current.state_token,
        "X-Marker-As-Of-Mode": verification.mode,
        "X-Marker-As-Of-Completeness": verification.current.completeness,
    }


def _stale_state_detail(verification: AsOfVerification) -> dict[str, Any]:
    """Typed 409 body for a rejected stale action.

    The ``code`` discriminator lets the frontend distinguish staleness from a
    generic failure without parsing prose; ``current_as_of`` gives it the
    refreshed contract without a second round-trip.
    """

    return {
        "code": "stale_state",
        "message": (
            "The observed as-of state no longer matches this job's current "
            "state. Refresh the status and retry against the current state."
        ),
        "observed_state_token": verification.observed,
        "current_state_token": verification.current.state_token,
        "current_as_of": verification.current.model_dump(mode="json"),
    }


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
        for key in (
            "engine",
            "probe_result",
            "mixed_engine_segments",
            "hybrid_ocr",
            "audio",
            "audio_batch",
            "video",
            "assets",
            "manifest_path",
            "purged_artifacts",
        )
        if key in parsed and parsed[key]
    }
    return metadata or None


async def _resolve_audio_vocabulary_packs(
    db: AsyncSession,
    pack_ids: Any,
) -> list[list[str]]:
    """Resolve saved vocabulary pack ids into term lists for conversion.

    The UI sends stable ids, while the audio prompt compiler consumes terms.
    Missing ids are ignored so stale presets degrade without failing a job.
    """

    if not isinstance(pack_ids, list):
        return []
    requested = {str(item) for item in pack_ids if str(item).strip()}
    if not requested:
        return []
    from app.models.settings import Setting

    row = (
        await db.execute(select(Setting).where(Setting.key == "audio_vocabulary_packs"))
    ).scalar_one_or_none()
    if not row:
        return []
    try:
        packs = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(packs, list):
        return []
    resolved: list[list[str]] = []
    for pack in packs:
        if not isinstance(pack, dict) or str(pack.get("id")) not in requested:
            continue
        terms = [str(term).strip() for term in pack.get("terms") or [] if str(term).strip()]
        if terms:
            resolved.append(terms)
    return resolved


async def _load_active_audio_defaults(db: AsyncSession) -> dict[str, str]:
    """Return active audio provider/model defaults from settings, if present."""

    from app.models.settings import Setting

    row = (
        await db.execute(select(Setting).where(Setting.key == "audio_active_provider"))
    ).scalar_one_or_none()
    if not row:
        return {}
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    defaults: dict[str, str] = {}
    provider = str(data.get("provider_id") or "").strip()
    model = str(data.get("model_id") or "").strip()
    if provider:
        defaults["audio_provider"] = provider
    if model:
        defaults["audio_model"] = model
    return defaults


def _parse_available_formats(job: ConversionJob) -> list[str]:
    """The list of output formats currently viewable for a completed job.

    Derived from the cached ``formats_json`` (written at finalize / regenerate).
    For legacy single-format jobs the cached list is absent, so we fall back to
    the job's ``output_format`` column so older jobs still expose one tab.
    """
    return available_cached_formats(job.formats_json, job.output_format)


def _parse_formats_json(formats_json: str | None) -> dict[str, str] | None:
    """Parse the ``{format: text}`` cache written at finalize/regenerate."""
    return parse_cached_formats(formats_json)


def _safe_asset_request_path(asset_path: str) -> str | None:
    raw = str(asset_path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        return None
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in {".", ".."} or ":" in part for part in parts):
        return None
    return "/".join(parts)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _manifest_candidates_for_result(result_path: Path) -> list[Path]:
    if result_path.is_dir():
        return [
            result_path / f"{result_path.name}.marker.json",
            *sorted(result_path.glob("*.marker.json")),
        ]
    return [result_path.with_name(f"{result_path.stem}.marker.json")]


def _load_result_manifest(result_path: Path) -> dict[str, Any] | None:
    for candidate in _manifest_candidates_for_result(result_path):
        if not candidate.is_file():
            continue
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _portable_manifest_for_zip(manifest_path: Path, result_root: Path) -> str:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "{}"
    if not isinstance(manifest, dict):
        return "{}"
    output = manifest.get("output")
    if isinstance(output, dict):
        for key in ("final_path", "text_path", "manifest_path"):
            if key in output:
                output[key] = _portable_manifest_member(output[key], result_root)
        assets = output.get("assets")
        if isinstance(assets, list):
            for entry in assets:
                if not isinstance(entry, dict):
                    continue
                relative = _safe_asset_request_path(str(entry.get("relative_path") or entry.get("name") or ""))
                if relative is None:
                    relative = _portable_manifest_member(entry.get("path"), result_root)
                entry["relative_path"] = relative
                entry["path"] = relative
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)


def _portable_manifest_member(raw_path: Any, result_root: Path) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve(strict=False).relative_to(result_root.resolve(strict=False)).as_posix()
        except ValueError:
            return path.name
    safe = _safe_asset_request_path(value)
    return safe or Path(value.replace("\\", "/")).name


def _asset_entry_for_request(manifest: dict[str, Any], asset_path: str) -> dict[str, Any] | None:
    output = manifest.get("output") if isinstance(manifest, dict) else None
    assets = output.get("assets") if isinstance(output, dict) else None
    if not isinstance(assets, list):
        return None
    for entry in assets:
        if not isinstance(entry, dict):
            continue
        name = _safe_asset_request_path(str(entry.get("relative_path") or entry.get("name") or ""))
        if name == asset_path:
            return entry
    return None


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
    output_format: str = Query("markdown", description=f"Output format: {OUTPUT_FORMATS_DESCRIPTION}"),
    output_formats: Optional[str] = Query(None, description="Comma-separated output formats for multi-format rendering (e.g. markdown,json)"),
    chunking_strategy: Optional[str] = Query(
        None,
        description="Chunking strategy for derived chunks: markdown_heading_blocks_v2 or unstructured_by_title",
    ),
    chunk_max_tokens: Optional[int] = Query(
        None,
        ge=16,
        description="Optional tokenizer-backed maximum token budget for derived Markdown chunks",
    ),
    allow_chunking_fallback: bool = Query(
        False,
        description="Allow optional chunking strategies to fall back to markdown_heading_blocks_v2 when unavailable",
    ),
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
    audio_output_mode: Optional[str] = Query(None, description="Audio output mode."),
    audio_model: Optional[str] = Query(None, description="Local STT model name for faster-whisper"),
    audio_vocabulary: Optional[str] = Query(None, description="Comma/newline-separated vocabulary hints for audio transcription"),
    audio_context: Optional[str] = Query(None, description="Context used only to organize audio batch output"),
    audio_low_confidence_threshold: Optional[float] = Query(None, ge=0.0, le=1.0, description="Segment confidence threshold for audio warnings"),
    audio_word_timestamps: bool = Query(False, description="Request word-level timestamps from the STT engine when supported"),
    audio_config: Optional[str] = Query(
        None,
        description=(
            "JSON object of advanced audio controls (plan §5.5): audio_provider, "
            "audio_diarization, audio_vocabulary_pack_ids, audio_confidence_heatmap, "
            "audio_text_enhancement_strength, audio_structural_enhancement_mode, "
            "audio_fusion_mode, audio_allow_cloud_stt, etc. Flat audio_* params above "
            "take precedence on conflict."
        ),
    ),
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
    ocr_engine: Optional[str] = Query(None, description="Local OCR engine: surya | hybrid_ocr"),
    hybrid_ocr_profile: Optional[str] = Query(None, description="Hybrid OCR routing/resource profile: balanced | max_accuracy | low_vram"),
    hybrid_ocr_require_specialists: bool = Query(False, description="Fail conversion if Hybrid OCR specialists are unavailable instead of falling back to Surya"),
    decorative_max_text_density: Optional[float] = Query(None, ge=0.0, le=1.0, description="Text-density at/below which an image is decorative"),
    ocr_min_text_density: Optional[float] = Query(None, ge=0.0, le=1.0, description="Text-density at/above which an image routes to local OCR"),
    ocr_min_lines: Optional[int] = Query(None, ge=1, description="Min detected text lines to consider the OCR route"),
    dedup_max_distance: Optional[int] = Query(None, ge=0, le=64, description="Max aHash Hamming distance treated as duplicate (0 = exact)"),
    vlm_crop_max_px: Optional[int] = Query(None, ge=64, le=4096, description="Longest-side pixel cap applied to a crop before VLM send"),
    vlm_batch_size: Optional[int] = Query(None, ge=1, le=64, description="Images per batched VLM call"),
    max_batch_retries: Optional[int] = Query(None, ge=0, le=5, description="Max extra batch calls to recover missing/garbled indices"),
    archive_recursive: Optional[bool] = Query(None, description="Recursively convert safe deterministic children inside archives"),
    archive_max_files: Optional[int] = Query(None, ge=1, le=1000, description="Max files to scan inside the archive"),
    archive_inline_bytes: Optional[int] = Query(None, ge=1, description="Max bytes to inline per archive text child"),
    archive_max_converted_children: Optional[int] = Query(None, ge=1, le=100, description="Max child files to convert inside the archive"),
    archive_max_child_bytes: Optional[int] = Query(None, ge=1, description="Max file size limit per child to parse (bytes)"),
    archive_max_total_uncompressed_bytes: Optional[int] = Query(None, ge=1, description="Max total uncompressed archive bytes to inspect"),
    archive_max_compression_ratio: Optional[float] = Query(None, ge=1.0, description="Max allowed compression ratio for archive entries"),
    archive_max_depth: Optional[int] = Query(None, ge=0, le=10, description="Max recursive archive conversion depth"),
    enable_mixed_pdf_routing: bool = Query(False, description="Enable mixed PDF routing; requires a full-page probe"),
    full_page_probe: bool = Query(False, description="Probe every PDF page before planning/routing"),
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
        try:
            assert_rest_local_input_allowed(path)
        except InputNotAllowedError as exc:
            await record_audit_event(
                db,
                event_type="policy.denied",
                surface="rest",
                resource_type="local_input",
                resource_id=path.name,
                status="denied",
                payload={"reason": exc.message, "path": str(path)},
            )
            await db.commit()
            raise HTTPException(status_code=400, detail=exc.message) from exc
        original_name = path.name
        suffix = path.suffix.lower()
        stored_path = str(path)
        is_local = True
    elif source_url:
        stored_path_obj = UPLOAD_DIR / f"{job_id}.download"
        original_name, suffix, source_url_safe = await _download_source_url(source_url, stored_path_obj, db)
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

    # Pre-flight capability gate: reject uploads whose engine reports a missing
    # native dependency BEFORE the file is stored/queued. Prevents the user from
    # waiting for a full upload + worker dispatch only to get a generic failure.
    _NATIVE_DEP_ENGINES: dict[str, frozenset[str]] = {
        "video": frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"}),
    }
    for engine_name, engine_exts in _NATIVE_DEP_ENGINES.items():
        if suffix in engine_exts:
            engine_status = get_engine_status().get(engine_name, "ready")
            if engine_status in ("missing_optional_dependency", "missing_native_dependency"):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "NATIVE_DEPENDENCY_MISSING",
                        "engine": engine_name,
                        "message": (
                            f"The {engine_name} engine cannot process this file because "
                            f"required native dependencies (ffmpeg/ffprobe) are not available."
                        ),
                        "install_hint": "Install ffmpeg (ships ffprobe) on the host or in the container.",
                    },
                )

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

    primary_format = output_format.strip().lower()
    if primary_format not in OUTPUT_FORMAT_SET:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output_format '{output_format}'. Expected one of: {OUTPUT_FORMATS_DESCRIPTION}.",
        )

    # Build conversion config from query params
    config: dict[str, Any] = {
        "output_format": primary_format,
        "original_name": original_name,
    }
    if output_formats:
        raw_formats = [part.strip().lower() for part in output_formats.split(",") if part.strip()]
        invalid_formats = [fmt for fmt in raw_formats if fmt not in OUTPUT_FORMAT_SET]
        if invalid_formats:
            if not is_local:
                Path(stored_path).unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported output_formats value(s): {', '.join(invalid_formats)}. "
                    f"Expected one or more of: {OUTPUT_FORMATS_DESCRIPTION}."
                ),
            )
        fmt_list = normalize_formats(raw_formats)
        if fmt_list:
            config["output_formats"] = fmt_list
            config["output_format"] = fmt_list[0]
    if chunking_strategy:
        if chunking_strategy not in {"markdown_heading_blocks_v2", "unstructured_by_title"}:
            if not is_local:
                Path(stored_path).unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail="Unsupported chunking_strategy. Expected markdown_heading_blocks_v2 or unstructured_by_title.",
            )
        config["chunking_strategy"] = chunking_strategy
    if chunk_max_tokens is not None:
        config["chunk_max_tokens"] = chunk_max_tokens
    if allow_chunking_fallback:
        config["allow_chunking_fallback"] = True
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
    if audio_output_mode in AUDIO_OUTPUT_MODES:
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
    # Advanced audio controls arrive as one typed JSON blob (plan §5.5) so the
    # route layer isn't choked with ~30 provider/diarization/enhancement/fusion
    # params. Flat audio_* params above take precedence on key conflict — a
    # caller using the legacy contract never has its explicit choice overridden
    # by a stale value sitting inside the blob.
    if audio_config:
        try:
            blob = json.loads(audio_config)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="audio_config must be a JSON object of audio controls.",
            )
        if not isinstance(blob, dict):
            raise HTTPException(
                status_code=400,
                detail="audio_config must be a JSON object of audio controls.",
            )
        for key, value in blob.items():
            if not isinstance(key, str) or not key.startswith("audio_"):
                continue
            # Flat params already won; never let the blob clobber them.
            config.setdefault(key, value)
    try:
        validate_engine_override(config, suffix)
    except UsageError as exc:
        if not is_local:
            Path(stored_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=exc.message) from exc
    active_audio_defaults = await _load_active_audio_defaults(db)
    for key, value in active_audio_defaults.items():
        config.setdefault(key, value)
    resolved_vocab_packs = await _resolve_audio_vocabulary_packs(
        db,
        config.get("audio_vocabulary_pack_ids"),
    )
    if resolved_vocab_packs:
        config["audio_vocabulary_packs"] = resolved_vocab_packs
    if suffix in AUDIO_PROVIDER_VALIDATED_EXTENSIONS:
        try:
            validate_audio_benchmark_selection(config)
            validate_audio_fusion_selection(config)
            capability = validate_provider_selection(
                config.get("audio_provider"),
                allow_cloud_stt=_truthy(config.get("audio_allow_cloud_stt")),
            )
            validate_audio_diarization_selection(config, capability)
        except (NotImplementedError, PermissionError, ValueError) as exc:
            if not is_local:
                Path(stored_path).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    if archive_recursive is not None:
        config["archive_recursive"] = archive_recursive
    if archive_max_files is not None:
        config["archive_max_files"] = archive_max_files
    if archive_inline_bytes is not None:
        config["archive_inline_bytes"] = archive_inline_bytes
    if archive_max_converted_children is not None:
        config["archive_max_converted_children"] = archive_max_converted_children
    if archive_max_child_bytes is not None:
        config["archive_max_child_bytes"] = archive_max_child_bytes
    if archive_max_total_uncompressed_bytes is not None:
        config["archive_max_total_uncompressed_bytes"] = archive_max_total_uncompressed_bytes
    if archive_max_compression_ratio is not None:
        config["archive_max_compression_ratio"] = archive_max_compression_ratio
    if archive_max_depth is not None:
        config["archive_max_depth"] = archive_max_depth
    if ocr_engine in ("surya", "hybrid_ocr"):
        config["ocr_engine"] = ocr_engine
    elif ocr_engine in ("glm_ocr", "paddleocr_vl"):
        raise HTTPException(
            status_code=400,
            detail="Use ocr_engine=hybrid_ocr instead of individual specialist engines.",
        )
    elif ocr_engine == "mistral_ocr":
        raise HTTPException(
            status_code=400,
            detail="mistral_ocr is not supported as a local OCR engine.",
        )
    elif ocr_engine is not None:
        raise HTTPException(
            status_code=400,
            detail="Invalid ocr_engine; expected surya or hybrid_ocr.",
        )
    if hybrid_ocr_profile is not None:
        if hybrid_ocr_profile not in ("balanced", "max_accuracy", "low_vram"):
            raise HTTPException(
                status_code=400,
                detail="Invalid hybrid_ocr_profile; expected balanced, max_accuracy, or low_vram.",
            )
        config["hybrid_ocr_profile"] = hybrid_ocr_profile
    if hybrid_ocr_require_specialists:
        config["hybrid_ocr_require_specialists"] = True
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
    if enable_mixed_pdf_routing:
        config["enable_mixed_pdf_routing"] = True
    if full_page_probe:
        config["full_page_probe"] = True
    if local_filepath:
        config["local_filepath"] = local_filepath
    if source_url_safe:
        config["source_url"] = source_url_safe
    if output_dir:
        try:
            assert_rest_output_write_allowed(Path(output_dir))
        except InputNotAllowedError as exc:
            await record_audit_event(
                db,
                event_type="policy.denied",
                surface="rest",
                resource_type="output_dir",
                resource_id=Path(output_dir).name,
                status="denied",
                payload={"reason": exc.message, "path": output_dir},
            )
            await db.commit()
            raise HTTPException(status_code=400, detail=exc.message) from exc
        config["output_dir"] = output_dir

    # Kernel-mode source truth (PR70/71 local slice): acquire the source
    # into a committed content revision BEFORE probing so the probe, the
    # persisted config, and the eventual conversion all observe the same
    # immutable revision instead of a mutable external path.
    from app.main import _app_state
    from app.core.config import KERNEL_RUNTIME_ENABLED
    from app.services.task_manager import TaskManager as _TaskManager

    kernel_active = KERNEL_RUNTIME_ENABLED and isinstance(
        _app_state.task_manager, _TaskManager
    ) and _app_state.task_manager.kernel_runtime is not None

    # The marker-owned upload/URL copy is what error paths may clean up;
    # a content-addressed artifact is shared truth and must never be
    # unlinked by request validation.
    ingress_cleanup_path = stored_path if not is_local else None

    if kernel_active:
        from app.kernel.source_store import IncoherentSourceError

        acquisition_service = default_source_acquisition_service()
        try:
            acquired = await acquisition_service.acquire(
                Path(stored_path),
                source_kind="local_path" if is_local else ("url" if source_url_safe else "upload"),
                suffix=suffix.lower(),
                job_id=job_id,
                source_key_override=(f"url:{source_url_safe}" if source_url_safe else None),
            )
        except IncoherentSourceError as exc:
            if ingress_cleanup_path:
                Path(ingress_cleanup_path).unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail=f"Source changed while being acquired; retry the submission. ({exc})",
            ) from exc
        stored_path = str(await acquisition_service.consumable_path_for(acquired))
        config[SOURCE_CONFIG_KEY] = acquired.to_config()
        config["durable_filepath"] = stored_path

    if suffix == ".pdf":
        probe_result = await asyncio.to_thread(
            probe_pdf,
            stored_path,
            full_page_probe=bool(config.get("enable_mixed_pdf_routing") or config.get("full_page_probe")),
        )
        config["probe_result"] = probe_result.to_dict()
        if page_range and probe_result.page_count > 0:
            _validate_page_range(page_range, probe_result.page_count)

    conversion_service = _app_state.conversion_service
    try:
        requested_formats = require_supported_output_formats(
            stored_path,
            config,
            conversion_service,
            source_name=original_name,
        )
    except UnsupportedFormatError as exc:
        if ingress_cleanup_path:
            Path(ingress_cleanup_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=exc.message) from exc
    effective_output_format = requested_formats[0]

    # The kernel dispatcher resolves the source from the persisted config
    # (the legacy durable queue used to inject this via enqueue). Without
    # it, uploaded/URL-downloaded sources would be invisible at launch.
    config.setdefault("durable_filepath", stored_path)

    job = ConversionJob(
        id=job_id,
        filename=stored_name if not is_local else original_name,
        original_name=original_name,
        status="pending",
        input_format=input_format,
        output_format=effective_output_format,
        config_json=json.dumps(config),
        # Kernel-mode rows are marked at creation so the restart sweep
        # (which fails queue_backend-less stale rows) can never destroy a
        # job that merely lost the race between row commit and kernel
        # authorization. The marker is metadata; dispatch authority is the
        # kernel outbox.
        queue_backend="kernel" if kernel_active else None,
    )
    db.add(job)
    await db.flush()
    await record_audit_event(
        db,
        event_type="job.submitted",
        surface="rest",
        resource_type="job",
        resource_id=job_id,
        status="success",
        payload={
            "input_format": input_format,
            "output_format": effective_output_format,
            "source": "local_file" if is_local else "source_url" if source_url_safe else "upload",
            "allow_cloud_vlm": allow_cloud_vlm,
        },
    )
    if allow_cloud_vlm:
        await record_audit_event(
            db,
            event_type="cloud_vlm.requested",
            surface="rest",
            resource_type="job",
            resource_id=job_id,
            status="success",
            payload={"provider": llm_provider, "model": llm_model},
        )

    task_manager = _app_state.task_manager

    llm_config = await _load_llm_config(db)

    from app.services.marker_service import build_marker_options
    options = build_marker_options(llm_config, config)

    from app.services.task_manager import TaskManager

    if isinstance(task_manager, TaskManager) and not kernel_active:
        await task_manager.enqueue_durable_job(
            db,
            job_id=job_id,
            filepath=stored_path,
            config=config,
            max_retries=int(config.get("max_retries") or 0),
        )

    # Commit the job row (and durable-queue metadata) BEFORE scheduling any
    # work. A fast CPU/native converter can finish in the worker thread and
    # reach _finalize_job — which opens a fresh session — before the get_db
    # dependency's implicit post-return commit runs. Without this explicit
    # commit the worker cannot see the row, silently skips finalization, and
    # the job hangs at "pending" forever (MUI-003).
    await db.commit()

    if kernel_active:
        # Kernel runtime (PR67B): the row is durable; authorize it as
        # exactly one kernel work item. The fair scheduler claims and
        # executes it — direct executor submission is no longer ownership.
        await task_manager.submit_conversion(
            job_id, stored_path, config, conversion_service
        )
    else:
        task_manager.submit_job(job_id, stored_path, options, conversion_service)

    return ConversionResponse(
        job_id=job_id,
        status="pending",
        filename=original_name,
        output_format=effective_output_format,
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
    if req.enable_mixed_pdf_routing:
        config["enable_mixed_pdf_routing"] = True
    if req.full_page_probe:
        config["full_page_probe"] = True
    plan_suffix = Path(req.filename).suffix
    if req.local_filepath:
        path = Path(req.local_filepath)
        if path.is_absolute() and path.is_file():
            plan_suffix = path.suffix
            try:
                assert_rest_local_input_allowed(path)
            except InputNotAllowedError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc
        if path.is_absolute() and path.is_file() and path.suffix.lower() == ".pdf":
            probe_result = await asyncio.to_thread(
                probe_pdf,
                path,
                full_page_probe=bool(config.get("enable_mixed_pdf_routing") or config.get("full_page_probe")),
            )
            config["probe_result"] = probe_result.to_dict()
            preliminary = False
    try:
        validate_engine_override(config, plan_suffix)
    except UsageError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

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
        output_formats=renderable_output_formats_for_engine(plan.engine, (plan_suffix.lower(),)),
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
        as_of=derive_as_of(job, effective_status=status),
        result_text=job.result_text,
        image_understanding=_parse_image_understanding(job.result_metadata_json),
        conversion_metadata=_parse_conversion_metadata(job.result_metadata_json),
        available_formats=_parse_available_formats(job),
        formats=_parse_formats(job.formats_json),
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


@router.get("/{job_id}/llm-traces")
async def llm_traces(job_id: str) -> dict[str, Any]:
    """Return the captured LLM call traces for a job.

    Each entry is one LLM generation call with structured ``parts`` (text/image)
    so the frontend trace viewer can render the prompt and preview any images.
    Polled by the eye-icon viewer while a job is running; traces persist after
    completion until the job is removed.
    """
    from app.core import llm_trace

    return {"job_id": job_id, "traces": llm_trace.get_traces(job_id)}


# ------------------------------------------------------------------
# Download
# ------------------------------------------------------------------


@router.get("/download/{job_id}")
async def download_result(
    job_id: str,
    format: Optional[str] = Query(None, description="Specific format to download: markdown, html, json, chunks, or all"),
    as_of: Optional[str] = Query(
        None,
        description=(
            "As-of state token previously observed via as_of.state_token in "
            "status/history. When supplied, the server verifies the observed "
            "state is still current before exporting; a mismatch returns 409 "
            "stale_state. Omitting it requests the stored representation "
            "explicitly as a historical export."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download the converted output file(s).

    Stale-export contract (invariant 56): the server — not the browser — is
    the arbiter. A presented token is re-derived and compared against the
    durable row, so a representation observed before a material change
    (regenerate, lifecycle transition, artifact purge) can never be silently
    substituted with newer content. Tokenless downloads stay possible as
    explicitly historical exports, labeled with the actual current state via
    response headers.
    """
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")

    verification = verify_as_of(job, as_of)
    if verification.mode == "verified" and not verification.fresh:
        raise HTTPException(status_code=409, detail=_stale_state_detail(verification))
    as_of_headers = _as_of_headers(verification)

    formats_map = parse_cached_formats(job.formats_json) or {}
    if "markdown" not in formats_map and job.result_text:
        formats_map["markdown"] = job.result_text

    stem = Path(job.original_name).stem

    # Determine which formats to include
    requested_format = (format or job.output_format or "markdown").strip().lower()
    if requested_format == "all":
        target_formats = list(formats_map.keys())
    else:
        if requested_format not in formats_map:
            raise HTTPException(
                status_code=400,
                detail=f"Format '{requested_format}' has not been generated for this job."
            )
        target_formats = [requested_format]

    ext_map = {
        "markdown": "md",
        "html": "html",
        "json": "json",
        "chunks": "chunks.json",
    }

    # Only an explicit all-format download returns an asset package. A specific
    # format must stay as the requested file even when conversion saved assets.
    has_assets = False
    result_path = Path(job.result_path) if job.result_path else None
    if result_path and result_path.is_dir():
        has_assets = True

    should_zip = requested_format == "all" and (has_assets or len(target_formats) > 1)

    if should_zip:
        tmp_file = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_zip = Path(tmp_file.name)
        tmp_file.close()
        try:
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                # 1. Write the format text files
                for fmt in target_formats:
                    text_content = formats_map[fmt]
                    ext = ext_map.get(fmt, fmt)
                    zf.writestr(f"{stem}.{ext}", text_content)

                # 2. Write the manifest file if it exists
                if result_path and result_path.is_dir():
                    manifest_file = result_path / f"{result_path.name}.marker.json"
                    if manifest_file.exists():
                        zf.writestr(manifest_file.name, _portable_manifest_for_zip(manifest_file, result_path))
                    else:
                        for f in result_path.glob("*.marker.json"):
                            zf.writestr(f.name, _portable_manifest_for_zip(f, result_path))

                    # 3. Write all assets (images/diagrams/etc.)
                    for file_in_dir in sorted(result_path.rglob("*")):
                        if file_in_dir.is_file() and not file_in_dir.name.endswith(".marker.json") and file_in_dir.suffix.lower() not in [".md", ".html", ".json", ".txt"]:
                            zf.write(file_in_dir, file_in_dir.relative_to(result_path))

            return FileResponse(
                path=str(tmp_zip),
                filename=f"{stem}.zip",
                media_type="application/zip",
                headers=as_of_headers,
                background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
            )
        except Exception:
            tmp_zip.unlink(missing_ok=True)
            raise
    else:
        # Single format with no assets: return the file directly
        fmt = target_formats[0]
        ext = ext_map.get(fmt, fmt)
        text_content = formats_map[fmt]
        
        try:
            tmp_file = tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False, mode="w", encoding="utf-8")
            tmp_path = Path(tmp_file.name)
            tmp_file.write(text_content)
            tmp_file.close()
        except Exception:
            # Guard against a leak if the temp file is created but writing fails.
            try:
                tmp_path.unlink(missing_ok=True)
            except NameError:
                pass
            raise

        media_types = {
            "md": "text/markdown",
            "html": "text/html",
            "json": "application/json",
            "chunks.json": "application/json",
            "txt": "text/plain",
        }

        return FileResponse(
            path=str(tmp_path),
            filename=f"{stem}.{ext}",
            media_type=media_types.get(ext, "text/plain"),
            headers=as_of_headers,
            background=BackgroundTask(tmp_path.unlink, missing_ok=True),
        )


@router.get("/assets/{job_id}/{asset_path:path}")
async def get_output_asset(
    job_id: str,
    asset_path: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Serve one manifest-listed sidecar asset for a completed job.

    The browser preview uses this route for local images referenced by Markdown.
    Only assets recorded in the job's output manifest are reachable, and the
    resolved file must stay under the job result directory.
    """

    safe_asset_path = _safe_asset_request_path(asset_path)
    if safe_asset_path is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=400, detail="Job not yet completed")
    if not job.result_path:
        raise HTTPException(status_code=404, detail="Asset not found")

    result_path = Path(job.result_path)
    manifest = _load_result_manifest(result_path)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Asset manifest not found")
    entry = _asset_entry_for_request(manifest, safe_asset_path)
    if entry is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset_file = Path(str(entry.get("path") or ""))
    if not asset_file.is_absolute():
        asset_file = (result_path if result_path.is_dir() else result_path.parent) / safe_asset_path
    try:
        asset_resolved = asset_file.resolve(strict=True)
    except OSError:
        raise HTTPException(status_code=404, detail="Asset not found") from None

    root = (result_path if result_path.is_dir() else result_path.parent).resolve()
    if not _path_is_within(asset_resolved, root):
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(
        path=str(asset_resolved),
        media_type=str(entry.get("media_type") or "application/octet-stream"),
        filename=asset_resolved.name,
    )


# ------------------------------------------------------------------
# History
# ------------------------------------------------------------------


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    converter: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> HistoryResponse:
    """List all conversion jobs (paginated, with optional filtering)."""
    offset = (page - 1) * page_size

    # Build filter conditions
    conditions = []
    if search and (search_term := search.strip()):
        search_pattern = f"%{_escape_like(search_term)}%"
        conditions.append(
            or_(
                ConversionJob.filename.ilike(search_pattern, escape="\\"),
                ConversionJob.original_name.ilike(search_pattern, escape="\\"),
            )
        )
    if status and status != "all":
        conditions.append(ConversionJob.status == HISTORY_STATUS_ALIASES.get(status, status))
    if converter and converter != "all":
        converter_pattern = _escape_like(converter.strip())
        converter_spaced = f'%"converter_cls": "{converter_pattern}"%'
        converter_compact = f'%"converter_cls":"{converter_pattern}"%'
        if converter.strip() == "PdfConverter":
            conditions.append(
                (ConversionJob.config_json.is_(None)) |
                (~ConversionJob.config_json.like('%"converter_cls"%')) |
                (ConversionJob.config_json.like(converter_spaced, escape="\\")) |
                (ConversionJob.config_json.like(converter_compact, escape="\\"))
            )
        else:
            conditions.append(
                (ConversionJob.config_json.like(converter_spaced, escape="\\")) |
                (ConversionJob.config_json.like(converter_compact, escape="\\"))
            )

    # Query total count
    count_stmt = select(func.count(ConversionJob.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    # Query paginated records
    stmt = select(ConversionJob).order_by(ConversionJob.created_at.desc())
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = stmt.offset(offset).limit(page_size)

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
                as_of=derive_as_of(j),
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


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cancel a pending/running conversion job without deleting its record or files."""
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    previous_status = job.status
    if previous_status in {"completed", "failed", "cancelled"}:
        return {
            "status": previous_status,
            "job_id": job_id,
            "cancelled": previous_status == "cancelled",
        }

    from app.main import _app_state

    await _app_state.task_manager.cancel_job(job_id)
    await db.refresh(job)
    if job.status in {"completed", "failed", "cancelled"}:
        # The job reached terminal truth between the initial read and the
        # cancel (e.g. an accepted publication committed first); report
        # the real state instead of overwriting it.
        return {
            "status": job.status,
            "job_id": job_id,
            "cancelled": job.status == "cancelled",
        }
    job.status = "cancelled"
    job.progress = 0
    await record_audit_event(
        db,
        event_type="job.cancelled",
        surface="rest",
        resource_type="job",
        resource_id=job_id,
        status="success",
        payload={"previous_status": previous_status},
    )
    await db.commit()
    return {"status": "cancelled", "job_id": job_id, "cancelled": True}


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    force: bool = Query(False, description="Explicitly cancel and delete a non-terminal live job."),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete a terminal conversion job, or force-delete a live job explicitly."""
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in {"completed", "failed", "cancelled"} and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} is {job.status}; cancel it first or pass force=true to delete a live job.",
        )
    if job.status not in {"completed", "failed", "cancelled"}:
        from app.main import _app_state

        await _app_state.task_manager.cancel_job(job_id)

    cleanup_paths = job_artifact_paths(job)
    removed = remove_paths(cleanup_paths)

    await db.delete(job)
    await record_audit_event(
        db,
        event_type="job.deleted",
        surface="rest",
        resource_type="job",
        resource_id=job_id,
        status="success",
        payload={"force": force, "files_removed": removed},
    )

    return {"status": "deleted", "job_id": job_id, "files_removed": removed}


@router.post("/{job_id}/purge-files")
async def purge_job_files(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Remove upload/output files for a terminal job while keeping its history row."""
    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id} is {job.status}; cancel or wait for terminal status before purging files.",
        )

    cleanup_paths = job_artifact_paths(job)
    removed = remove_paths(cleanup_paths)
    try:
        metadata = json.loads(job.result_metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["purged_artifacts"] = {
        "files_removed": removed,
        "purged_at": datetime.now(timezone.utc).isoformat(),
    }
    job.result_metadata_json = json.dumps(metadata)
    if job.result_path and not Path(job.result_path).exists():
        job.result_path = None
    await record_audit_event(
        db,
        event_type="job.files_purged",
        surface="rest",
        resource_type="job",
        resource_id=job_id,
        status="success",
        payload={"files_removed": removed},
    )
    await db.commit()
    return {"status": "purged", "job_id": job_id, "files_removed": removed}


# ------------------------------------------------------------------
# Regenerate one output format for an existing job
# ------------------------------------------------------------------


def _read_stored_config(job: ConversionJob) -> dict[str, Any]:
    """Parse a job's stored config_json, tolerating empty/corrupt values."""
    if not job.config_json:
        return {}
    try:
        parsed = json.loads(job.config_json)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _job_source_path(job: ConversionJob) -> Path | None:
    """Resolve the stored source file for a job (revision artifact, upload copy, or local path).

    Kernel-mode jobs carry a committed source revision whose artifact
    outranks the external world: it is tried first so retries and format
    regeneration reuse owned immutable bytes even after the original
    upload/local file disappeared or changed. Upload copies live in
    UPLOAD_DIR under ``job.filename``; local-path jobs keep their
    original absolute path.
    """
    cfg = _read_stored_config(job)
    block = cfg.get(SOURCE_CONFIG_KEY)
    if isinstance(block, dict):
        service = default_source_acquisition_service()
        resolved = await service.resolve(block)
        if resolved is not None:
            return Path(await service.artifact_path_for(resolved))
    local = cfg.get("local_filepath")
    if local and Path(local).is_file():
        return Path(local)
    upload_path = UPLOAD_DIR / job.filename
    if upload_path.is_file():
        return upload_path
    return None


@router.post("/{job_id}/regenerate")
async def regenerate_format(
    job_id: str,
    format: str = Query(..., description=f"Output format to regenerate: {OUTPUT_FORMATS_DESCRIPTION}"),
    as_of: Optional[str] = Query(
        None,
        description=(
            "Optional as-of state token observed via status/history. When "
            "supplied, regeneration refuses to mutate a job whose state moved "
            "since observation (409 stale_state)."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Render one additional output format for an existing completed job.

    Reuses the job's stored source file and config, so it does NOT create a new
    queue entry or card. The rendered text is merged into the job's
    ``formats_json`` cache and the format becomes instantly viewable in the
    preview tabs without re-running the primary conversion.

    This is a mutating action on a previously observed result, so it honors
    the same as-of precondition as download: a caller who pins the observed
    state token gets a typed 409 when the row moved. Without a token the
    action proceeds against the current row — regeneration reads current
    state by design and merges additively, so there is no observed
    representation to substitute.
    """
    if format not in OUTPUT_FORMAT_SET:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{format}'. Allowed: {OUTPUT_FORMATS_DESCRIPTION}.",
        )

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(
            status_code=400,
            detail="Job must be completed before regenerating a format.",
        )

    verification = verify_as_of(job, as_of)
    if verification.mode == "verified" and not verification.fresh:
        raise HTTPException(status_code=409, detail=_stale_state_detail(verification))

    source_path = await _job_source_path(job)
    if source_path is None:
        raise HTTPException(
            status_code=409,
            detail="Source file is no longer available for this job.",
        )

    from app.main import _app_state

    conversion_service = _app_state.conversion_service

    # The stored config carries the resolved engine + image-understanding knobs;
    # we only override the target output format for this single render.
    config = _read_stored_config(job)
    config["output_format"] = format
    config["output_formats"] = [format]
    try:
        require_supported_output_formats(
            str(source_path),
            config,
            conversion_service,
            source_name=job.original_name,
        )
    except UnsupportedFormatError as exc:
        raise HTTPException(
            status_code=409,
            detail=exc.message,
        ) from exc

    llm_config = await _load_llm_config(db)
    from app.services.marker_service import build_marker_options
    options = build_marker_options(llm_config, config)

    try:
        envelopes = await asyncio.to_thread(
            conversion_service.convert_file_formats,
            str(source_path), options, [format]
        )
    except Exception as exc:  # noqa: BLE001 - surface a typed error to the UI
        logger.exception("Format regeneration failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {exc}") from exc

    rendered = envelopes.get(format)
    if not rendered:
        raise HTTPException(status_code=500, detail="Renderer produced no output for the format.")

    # Merge into the existing formats cache via the injected session so the
    # write is visible to tests and respects the same dependency override as
    # every other endpoint (no separate production session).
    from app.services.format_store import merge_formats
    existing = {}
    try:
        existing = json.loads(job.formats_json) if job.formats_json else {}
        if not isinstance(existing, dict):
            existing = {}
    except (json.JSONDecodeError, TypeError):
        existing = {}

    # Store FLAT text ({format: text}) so the status endpoint's cache and the
    # finalize-time write share one shape — never a nested {"text": ...} dict.
    merged = merge_formats(existing, {format: rendered.get("text", "")})
    job.formats_json = json.dumps(merged) if merged else None
    await db.commit()

    return {
        "status": "regenerated",
        "job_id": job_id,
        "format": format,
        "available_formats": sorted(merged.keys()),
    }


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    body: RetryJobRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-run a terminal job from its stored source file, optionally with a
    different LLM provider or model.

    Creates a NEW job row (the original stays in history for audit). The
    source file is resolved via ``_job_source_path`` (upload copy or local
    path). LLM responses already cached for the same prompt are replayed
    instantly, so only the work that did not complete is re-done.

    A non-terminal job cannot be retried — cancel it first. An unknown
    provider is rejected with 400. A missing source file is rejected with 409.
    """
    from app.core.config import KERNEL_RUNTIME_ENABLED
    from app.main import _app_state as _app_state_ref
    from app.services.task_manager import TaskManager as _TaskManager

    kernel_active = KERNEL_RUNTIME_ENABLED and isinstance(
        _app_state_ref.task_manager, _TaskManager
    ) and _app_state_ref.task_manager.kernel_runtime is not None

    stmt = select(ConversionJob).where(ConversionJob.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("pending", "processing"):
        raise HTTPException(
            status_code=409,
            detail="Job is still running. Cancel it before retrying.",
        )

    source_path = await _job_source_path(job)
    if source_path is None:
        raise HTTPException(
            status_code=409,
            detail="Source file is no longer available for this job.",
        )

    config = _read_stored_config(job)
    # Link the new job to its origin for audit trail.
    config["retried_from"] = job_id

    # Validate + apply provider/model overrides.
    if body.llm_provider or body.llm_model:
        llm_config = await _load_llm_config(db)
        providers = llm_config.get("providers", [])
        if body.llm_provider:
            prov = next((p for p in providers if p["id"] == body.llm_provider), None)
            if not prov:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown LLM provider '{body.llm_provider}'.",
                )
            config["llm_provider"] = body.llm_provider
            # Clear a stale model override when the provider changes.
            if body.llm_model:
                model_cfg = next(
                    (m for m in prov.get("models", []) if m["model_id"] == body.llm_model),
                    None,
                )
                if not model_cfg:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Model '{body.llm_model}' not configured for provider '{body.llm_provider}'.",
                    )
                config["llm_model"] = body.llm_model
            else:
                config.pop("llm_model", None)
        elif body.llm_model:
            # Same provider, different model.
            config["llm_model"] = body.llm_model

    # Reset durable-queue retry state for the new job; the retried job's
    # durable source is this run's resolved path (set once stored_path
    # is known below, where the legacy enqueue used to inject it).
    config.pop("durable_filepath", None)

    from app.main import _app_state
    task_manager = _app_state.task_manager
    conversion_service = _app_state.conversion_service

    llm_config = await _load_llm_config(db)
    from app.services.marker_service import build_marker_options
    options = build_marker_options(llm_config, config)

    new_job_id = str(uuid.uuid4())
    stored_path = str(source_path)
    original_name = config.get("original_name") or job.original_name
    input_format = (Path(original_name).suffix.lstrip(".") or job.input_format or "").lower()

    if kernel_active:
        # Retry source truth: reuse the original job's committed revision
        # when its bytes are still owned; otherwise re-acquire from the
        # resolved fallback source (a NEW revision — never silent reuse
        # of dead truth). A changed revision invalidates the stored probe
        # result, which described different bytes.
        from app.kernel.source_store import IncoherentSourceError as _Incoherent

        retry_service = default_source_acquisition_service()
        old_block = config.get(SOURCE_CONFIG_KEY)
        resolved = (
            await retry_service.resolve(old_block) if isinstance(old_block, dict) else None
        )
        try:
            if resolved is not None:
                acquired = resolved
            else:
                fallback = Path(stored_path)
                marker_owned = UPLOAD_DIR.resolve() in fallback.resolve().parents
                acquired = await retry_service.acquire(
                    fallback,
                    source_kind="upload" if marker_owned else "local_path",
                    suffix=fallback.suffix.lower(),
                    job_id=new_job_id,
                    source_key_override=(
                        f"url:{config['source_url']}" if config.get("source_url") else None
                    ),
                )
        except _Incoherent as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Source changed while being re-acquired for retry. ({exc})",
            ) from exc
        stored_path = str(await retry_service.consumable_path_for(acquired))
        config[SOURCE_CONFIG_KEY] = acquired.to_config()
        if (
            isinstance(old_block, dict)
            and old_block.get("content_revision_id") != acquired.content_revision_id
        ):
            config.pop("probe_result", None)
            if Path(original_name).suffix.lower() == ".pdf":
                probe_result = await asyncio.to_thread(probe_pdf, stored_path)
                config["probe_result"] = probe_result.to_dict()

    # The kernel dispatcher (and legacy recovery) resolve the source from
    # the persisted config; the retried job's source is this run's path.
    config["durable_filepath"] = stored_path

    new_job = ConversionJob(
        id=new_job_id,
        filename=job.filename,
        original_name=original_name,
        status="pending",
        input_format=input_format,
        output_format=job.output_format,
        config_json=json.dumps(config),
        queue_backend="kernel" if kernel_active else None,
    )
    db.add(new_job)
    await db.flush()

    await record_audit_event(
        db,
        event_type="job.retry_submitted",
        surface="rest",
        resource_type="job",
        resource_id=new_job_id,
        status="success",
        payload={
            "retried_from": job_id,
            "llm_provider": config.get("llm_provider"),
            "llm_model": config.get("llm_model"),
        },
    )

    from app.services.task_manager import TaskManager
    if isinstance(task_manager, TaskManager) and not kernel_active:
        await task_manager.enqueue_durable_job(
            db,
            job_id=new_job_id,
            filepath=stored_path,
            config=config,
            max_retries=int(config.get("max_retries") or 0),
        )

    await db.commit()

    if kernel_active:
        await task_manager.submit_conversion(
            new_job_id, stored_path, config, conversion_service
        )
    else:
        task_manager.submit_job(new_job_id, stored_path, options, conversion_service)

    return {
        "new_job_id": new_job_id,
        "source_job_id": job_id,
        "status": "pending",
    }


