"""Agent-facing conversion API shared by the CLI and MCP server.

This module deliberately reuses the same ConversionService and option builder
as the GUI route. It is the stable seam for headless users and coding agents.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.agent_contract import ConversionOptionsModel as AgentConversionOptions
from app.conversion.probe import probe_pdf
from app.core.config import OUTPUT_DIR, UPLOAD_DIR
from app.crypto import is_encrypted_field
from app.database import async_session_factory, create_tables
from app.errors import (
    InputNotAllowedError,
    InputNotFoundError,
    MarkerError,
    UnsupportedFormatError,
    UsageError,
    from_exception,
)
from app.models.job import ConversionJob
from app.models.settings import Setting
from app.routes.convert import (
    ALLOWED_EXTENSIONS,
    _planned_mixed_segments,
    _validate_page_range,
)
from app.services.conversion_service import ConversionService
from app.services.marker_service import MarkerService, build_marker_options
from app.services.output_writer import OUTPUT_MANIFEST_SCHEMA_VERSION, write_conversion_output
from app.services.audit import record_audit_event
from app.services.policy import (
    assert_local_input_allowed,
    assert_output_read_allowed,
    assert_output_write_allowed,
    output_root,
)
from app.services.safe_url_fetcher import SafeUrlFetchError, download_source_url
from app.utils.secrets import decrypt_value, encrypt_value, is_masked, is_sensitive_key, mask_value


SERVICE_NAME = "marker_mcp"
TOOL_NAMES = [
    "marker_list_capabilities",
    "marker_plan_conversion",
    "marker_submit_job",
    "marker_convert_file",
    "marker_read_output",
    "marker_list_jobs",
    "marker_get_job_status",
    "marker_delete_job",
    "marker_list_settings",
    "marker_get_setting",
    "marker_set_setting",
    "marker_delete_setting",
    "marker_self_test",
]

DEFAULT_PREVIEW_CHARS = 20_000
MAX_READ_CHARS = 100_000

_db_session_factory = async_session_factory
_db_tables_ready = False


def parse_extra_options(items: list[str] | None) -> dict[str, Any]:
    """Parse repeated ``key=value`` CLI options into typed JSON-ish values."""

    parsed: dict[str, Any] = {}
    for item in items or []:
        key, sep, raw_value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"Invalid --option '{item}'. Use key=value.")
        parsed[key.strip()] = _parse_scalar(raw_value.strip())
    return parsed


def parse_extra_options_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("extra_options_json must decode to a JSON object")
    return value


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def capabilities() -> dict[str, Any]:
    service = _conversion_service()
    converters = []
    for converter in service.registry.converters:
        converters.append(
            {
                "engine": converter.engine_name,
                "extensions": sorted(converter.supported_extensions),
                "needs_marker_models": converter.requires_marker_models,
                "needs_gpu": converter.requires_gpu,
            }
        )
    return {
        "service": SERVICE_NAME,
        "tools": TOOL_NAMES,
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        "output_formats": ["markdown", "json", "html", "chunks"],
        "conversion_profiles": ["auto", "fast", "high_accuracy"],
        "image_handling_modes": ["extraction", "understanding", "both"],
        "audio_output_modes": ["transcript", "enhanced", "notes", "meeting_notes", "lecture_notes"],
        "converters": converters,
        "agent_guidance": (
            "Use marker_plan_conversion before large PDFs. Use marker_convert_file "
            "with output_dir and a bounded max_chars. Use marker_read_output to page "
            "through long markdown results."
        ),
    }


def build_conversion_config(
    options: AgentConversionOptions,
    *,
    original_name: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "output_format": options.output_format,
        "original_name": original_name or "",
    }
    _put(config, "converter_cls", options.converter_cls)
    _put(config, "engine_override", options.engine_override)
    _put(config, "conversion_profile", options.conversion_profile)
    if options.use_llm:
        config["use_llm"] = True
    _put(config, "llm_provider", options.llm_provider)
    _put(config, "llm_model", options.llm_model)
    if options.image_handling_mode in {"extraction", "understanding", "both"}:
        config["image_handling_mode"] = options.image_handling_mode
    config["allow_cloud_vlm"] = options.allow_cloud_vlm
    _put_true(config, "force_ocr", options.force_ocr)
    _put_true(config, "paginate_output", options.paginate_output)
    _put_true(config, "disable_image_extraction", options.disable_image_extraction)
    _put(config, "page_range", options.page_range)
    _put(config, "lang", options.lang)
    if options.audio_output_mode in {"transcript", "enhanced", "notes", "meeting_notes", "lecture_notes"}:
        config["audio_output_mode"] = options.audio_output_mode
    _put(config, "audio_model", options.audio_model)
    _put(config, "audio_vocabulary", options.audio_vocabulary)
    _put(config, "audio_context", options.audio_context)
    if options.audio_low_confidence_threshold is not None:
        config["audio_low_confidence_threshold"] = options.audio_low_confidence_threshold
    _put_true(config, "audio_word_timestamps", options.audio_word_timestamps)
    _put_true(config, "disable_multiprocessing", options.disable_multiprocessing)
    _put_true(config, "strip_existing_ocr", options.strip_existing_ocr)
    _put_true(config, "redo_inline_math", options.redo_inline_math)
    _put_true(config, "debug", options.debug)
    if output_dir:
        config["output_dir"] = output_dir
    config.update(options.extra_options)
    return config


def _put(config: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        config[key] = value


def _put_true(config: dict[str, Any], key: str, value: bool) -> None:
    if value:
        config[key] = True


async def plan_conversion(
    *,
    local_file_path: str | None = None,
    filename: str | None = None,
    size: int = 0,
    options: AgentConversionOptions | None = None,
) -> dict[str, Any]:
    options = options or AgentConversionOptions()
    service = _conversion_service()
    config = build_conversion_config(options, original_name=filename or local_file_path)
    preliminary = True
    source_path = Path(local_file_path).expanduser() if local_file_path else None
    if source_path and source_path.is_file():
        path = source_path.resolve()
        _validate_supported_path(path)
        if path.suffix.lower() == ".pdf":
            probe_result = await asyncio.to_thread(probe_pdf, str(path))
            config["probe_result"] = probe_result.to_dict()
            if options.page_range and probe_result.page_count > 0:
                _validate_page_range_safe(options.page_range, probe_result.page_count)
        plan = service.plan(str(path), config)
        preliminary = False
        effective_filename = path.name
        effective_size = path.stat().st_size
    else:
        effective_filename = filename or (source_path.name if source_path else "")
        if not effective_filename:
            raise UsageError("Provide local_file_path or filename")
        plan = service.plan_by_metadata(effective_filename, size, config)
        effective_size = size
    return {
        "filename": effective_filename,
        "size": effective_size,
        "preliminary": preliminary,
        "plan": plan.to_dict(),
        "probe_result": config.get("probe_result"),
        "mixed_engine_segments": (
            _planned_mixed_segments(config.get("probe_result"))
            if plan.engine == "mixed_pdf"
            else None
        ),
    }


async def convert_document(
    *,
    local_file_path: str | None = None,
    source_url: str | None = None,
    output_dir: str | None = None,
    output_path: str | None = None,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
    options: AgentConversionOptions | None = None,
) -> dict[str, Any]:
    if bool(local_file_path) == bool(source_url):
        raise UsageError("Provide exactly one of local_file_path or source_url")
    options = options or AgentConversionOptions()
    max_chars = max(0, min(max_chars, MAX_READ_CHARS))
    output_base = Path(output_dir).expanduser() if output_dir else (output_root() or OUTPUT_DIR) / "agent"
    assert_output_write_allowed(output_base)
    if output_path:
        assert_output_write_allowed(Path(output_path).expanduser())
    output_base.mkdir(parents=True, exist_ok=True)

    if source_url:
        with tempfile.TemporaryDirectory(prefix="marker-agent-url-") as temp_dir:
            temp_path = Path(temp_dir) / "download"
            original_name, _suffix, safe_url = await _download_source_url_safe(source_url, temp_path)
            final_path = temp_path.with_suffix(Path(original_name).suffix)
            temp_path.replace(final_path)
            return await _convert_resolved_path(
                final_path,
                options,
                output_base=output_base,
                output_path=output_path,
                max_chars=max_chars,
                source_url=safe_url,
                original_name=original_name,
            )

    path = Path(local_file_path or "").expanduser().resolve()
    _validate_supported_path(path)
    return await _convert_resolved_path(
        path,
        options,
        output_base=output_base,
        output_path=output_path,
        max_chars=max_chars,
        original_name=path.name,
    )


async def submit_conversion_job(
    *,
    local_file_path: str | None = None,
    source_url: str | None = None,
    output_dir: str | None = None,
    options: AgentConversionOptions | None = None,
) -> dict[str, Any]:
    if bool(local_file_path) == bool(source_url):
        raise UsageError("Provide exactly one of local_file_path or source_url")
    await _ensure_db_tables()
    options = options or AgentConversionOptions()
    job_id = str(uuid.uuid4())
    is_local = False
    source_url_safe: str | None = None
    if output_dir:
        assert_output_write_allowed(Path(output_dir).expanduser())

    if source_url:
        download_path = UPLOAD_DIR / f"{job_id}.download"
        original_name, suffix, source_url_safe = await _download_source_url_safe(source_url, download_path)
        stored_path_obj = UPLOAD_DIR / f"{job_id}{suffix}"
        download_path.replace(stored_path_obj)
        stored_path = str(stored_path_obj)
        filename = stored_path_obj.name
    else:
        path = Path(local_file_path or "").expanduser().resolve()
        _validate_supported_path(path)
        original_name = path.name
        suffix = path.suffix.lower()
        stored_path = str(path)
        filename = original_name
        is_local = True

    input_format = suffix.lstrip(".")
    config = build_conversion_config(options, original_name=original_name, output_dir=output_dir)
    if is_local:
        config["local_filepath"] = stored_path
    if source_url_safe:
        config["source_url"] = source_url_safe
    if suffix == ".pdf":
        probe_result = await asyncio.to_thread(probe_pdf, stored_path)
        config["probe_result"] = probe_result.to_dict()
        if options.page_range and probe_result.page_count > 0:
            _validate_page_range_safe(options.page_range, probe_result.page_count)

    job = ConversionJob(
        id=job_id,
        filename=filename,
        original_name=original_name,
        status="pending",
        input_format=input_format,
        output_format=options.output_format,
        config_json=json.dumps(config),
    )
    app_state = _get_app_state()
    async with _db_session_factory() as session:
        session.add(job)
        from app.services.task_manager import TaskManager

        if isinstance(app_state.task_manager, TaskManager):
            await app_state.task_manager.enqueue_durable_job(
                session,
                job_id=job_id,
                filepath=stored_path,
                config=config,
                max_retries=int(config.get("max_retries") or 0),
            )
        await record_audit_event(
            session,
            event_type="job.submitted",
            surface="agent",
            resource_type="job",
            resource_id=job_id,
            status="success",
            payload={
                "input_format": input_format,
                "output_format": options.output_format,
                "source": "local_file" if is_local else "source_url",
                "allow_cloud_vlm": options.allow_cloud_vlm,
            },
        )
        if options.allow_cloud_vlm:
            await record_audit_event(
                session,
                event_type="cloud_vlm.requested",
                surface="agent",
                resource_type="job",
                resource_id=job_id,
                status="success",
                payload={"provider": options.llm_provider, "model": options.llm_model},
            )
        await session.commit()

    await _prepare_runtime(config)
    marker_options = build_marker_options(await _load_llm_config_for_options(config), config)
    app_state.task_manager.submit_job(
        job_id,
        stored_path,
        marker_options,
        app_state.conversion_service,
    )
    return {
        "job_id": job_id,
        "status": "pending",
        "filename": original_name,
        "output_format": options.output_format,
        "next_step": "Call marker_get_job_status until status is completed, failed, or cancelled.",
    }


async def _convert_resolved_path(
    path: Path,
    options: AgentConversionOptions,
    *,
    output_base: Path,
    output_path: str | None,
    max_chars: int,
    original_name: str,
    source_url: str | None = None,
) -> dict[str, Any]:
    config = build_conversion_config(options, original_name=original_name, output_dir=str(output_base))
    if source_url:
        config["source_url"] = source_url
    if path.suffix.lower() == ".pdf":
        probe_result = await asyncio.to_thread(probe_pdf, str(path))
        config["probe_result"] = probe_result.to_dict()
        if options.page_range and probe_result.page_count > 0:
            _validate_page_range_safe(options.page_range, probe_result.page_count)

    await _prepare_runtime(config)
    marker_options = build_marker_options(await _load_llm_config_for_options(config), config)
    service = _conversion_service()
    result = await asyncio.to_thread(service.convert_file, str(path), marker_options)
    saved = _save_result(
        result,
        source_name=original_name,
        output_base=output_base,
        output_path=Path(output_path).expanduser() if output_path else None,
        conversion_config=config,
        source_url=source_url,
    )
    text = result.get("text") or ""
    preview = text[:max_chars]
    return {
        "ok": True,
        "source": {"name": original_name, "path": str(path) if not source_url else None, "source_url": source_url},
        "output": saved,
        "text_preview": preview,
        "text_chars": len(text),
        "truncated": len(text) > max_chars,
        "metadata": result.get("metadata") or {},
        "next_step": (
            "Call marker_read_output with output.text_path and offset equal to preview length."
            if len(text) > max_chars
            else None
        ),
    }


def read_output(path: str, *, offset: int = 0, limit: int = DEFAULT_PREVIEW_CHARS) -> dict[str, Any]:
    output_path = Path(path).expanduser()
    _assert_output_read_permitted(output_path)
    if not output_path.is_file():
        raise InputNotFoundError(
            f"Output file not found: {path}",
            details={"path": str(path)},
        )
    offset = max(0, offset)
    limit = max(1, min(limit, MAX_READ_CHARS))
    text_chars = _count_text_chars(output_path)
    chunk = _read_text_chunk(output_path, offset=offset, limit=limit)
    next_offset = offset + len(chunk)
    return {
        "path": str(output_path.resolve()),
        "offset": offset,
        "limit": limit,
        "text": chunk,
        "text_chars": text_chars,
        "has_more": next_offset < text_chars,
        "next_offset": next_offset if next_offset < text_chars else None,
    }


def _count_text_chars(path: Path) -> int:
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            data = handle.read(64 * 1024)
            if not data:
                return total
            total += len(data)


def _read_text_chunk(path: Path, *, offset: int, limit: int) -> str:
    remaining_skip = offset
    parts: list[str] = []
    remaining_take = limit
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while remaining_skip > 0:
            skipped = handle.read(min(64 * 1024, remaining_skip))
            if not skipped:
                return ""
            remaining_skip -= len(skipped)
        while remaining_take > 0:
            data = handle.read(min(64 * 1024, remaining_take))
            if not data:
                break
            parts.append(data)
            remaining_take -= len(data)
    return "".join(parts)


async def list_jobs(*, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    await _ensure_db_tables()
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    async with _db_session_factory() as session:
        total = (await session.execute(select(func.count(ConversionJob.id)))).scalar() or 0
        rows = (
            await session.execute(
                select(ConversionJob)
                .order_by(ConversionJob.created_at.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).scalars().all()
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": offset + len(rows) < total,
        "next_page": page + 1 if offset + len(rows) < total else None,
        "jobs": [_job_to_dict(row, include_result_text=False) for row in rows],
    }


async def get_job_status(
    job_id: str,
    *,
    include_result_text: bool = False,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
) -> dict[str, Any]:
    await _ensure_db_tables()
    max_chars = max(0, min(max_chars, MAX_READ_CHARS))
    async with _db_session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        if not job:
            raise InputNotFoundError(
                f"Job not found: {job_id}",
                details={"job_id": job_id},
            )
        data = _job_to_dict(job, include_result_text=include_result_text, max_chars=max_chars)
    if data["status"] not in {"completed", "failed", "cancelled"}:
        live = _task_manager_status(job_id)
        if live.get("status") in {"pending", "processing", "completed", "failed", "cancelled"}:
            data["status"] = live.get("status", data["status"])
            data["progress"] = max(int(data.get("progress") or 0), int(live.get("progress") or 0))
            for key in ("message", "logs", "elapsed", "eta"):
                if key in live:
                    data[key] = live[key]
    return data


async def delete_job(job_id: str, *, delete_files: bool = True) -> dict[str, Any]:
    await _ensure_db_tables()
    async with _db_session_factory() as session:
        job = await session.get(ConversionJob, job_id)
        if not job:
            raise InputNotFoundError(
                f"Job not found: {job_id}",
                details={"job_id": job_id},
            )
        cleanup_paths = _job_cleanup_paths(job) if delete_files else []
        await _cancel_job_best_effort(job_id)
        await session.delete(job)
        await session.commit()
    removed = _remove_paths(cleanup_paths) if delete_files else []
    return {"status": "deleted", "job_id": job_id, "files_removed": removed}


async def list_settings(*, category: str | None = None) -> dict[str, Any]:
    await _ensure_db_tables()
    async with _db_session_factory() as session:
        stmt = select(Setting).order_by(Setting.category, Setting.key)
        if category:
            stmt = stmt.where(Setting.category == category)
        rows = (await session.execute(stmt)).scalars().all()
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row.category, []).append(_setting_to_dict(row))
    return {"settings": grouped, "total": len(rows), "masked": True}


async def get_setting(key: str) -> dict[str, Any]:
    await _ensure_db_tables()
    async with _db_session_factory() as session:
        row = await _get_setting_row(session, key)
        if not row:
            raise InputNotFoundError(
                f"Setting not found: {key}",
                details={"key": key},
            )
        return _setting_to_dict(row)


async def set_setting(key: str, value: str, *, category: str = "general") -> dict[str, Any]:
    await _ensure_db_tables()
    async with _db_session_factory() as session:
        row = await _get_setting_row(session, key)
        existed = row is not None
        save_value = value
        if is_encrypted_field(key) and row and is_masked(value):
            save_value = row.value
        elif is_encrypted_field(key) and value:
            from app.core.api_manager import update_secret_cache

            update_secret_cache(key, value)
            save_value = encrypt_value(value)
        if row:
            row.value = save_value
            row.category = category
        else:
            row = Setting(key=key, value=save_value, category=category)
            session.add(row)
        await record_audit_event(
            session,
            event_type="settings.write",
            surface="agent",
            resource_type="setting",
            resource_id=key,
            status="success",
            payload={
                "key": key,
                "category": category,
                "operation": "update" if existed else "create",
                "sensitive": is_sensitive_key(key),
            },
        )
        await session.commit()
        await session.refresh(row)
        return _setting_to_dict(row)


async def delete_setting(key: str) -> dict[str, str]:
    await _ensure_db_tables()
    async with _db_session_factory() as session:
        await session.execute(delete(Setting).where(Setting.key == key))
        await record_audit_event(
            session,
            event_type="settings.delete",
            surface="agent",
            resource_type="setting",
            resource_id=key,
            status="success",
            payload={"key": key, "sensitive": is_sensitive_key(key)},
        )
        await session.commit()
    return {"status": "deleted", "key": key}


async def self_test(*, include_conversion: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "service": SERVICE_NAME,
        "expected_tools": TOOL_NAMES,
        "capabilities_ok": False,
        "conversion_ok": None,
        "notes": [],
    }
    caps = capabilities()
    result["capabilities_ok"] = all(name in caps["tools"] for name in TOOL_NAMES)
    if include_conversion:
        with tempfile.TemporaryDirectory(prefix="marker-agent-self-test-") as temp_dir:
            fixture = Path(temp_dir) / "sample.tsv"
            fixture.write_text("name\tscore\nalpha\t1\nbeta\t2\n", encoding="utf-8")
            out = await convert_document(
                local_file_path=str(fixture),
                output_dir=temp_dir,
                max_chars=5_000,
                options=AgentConversionOptions(output_format="markdown"),
            )
            preview = out.get("text_preview", "")
            result["conversion_ok"] = "| name | score |" in preview and "| alpha | 1 |" in preview
            result["conversion_engine"] = (
                out.get("metadata", {}).get("engine", {}).get("engine")
                or out.get("metadata", {}).get("engine_detail", {}).get("format")
            )
    return result


def _conversion_service() -> ConversionService:
    return ConversionService(MarkerService())


def _job_to_dict(
    job: ConversionJob,
    *,
    include_result_text: bool,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
) -> dict[str, Any]:
    config = _json_obj(job.config_json)
    metadata = _json_obj(job.result_metadata_json)
    result_text = job.result_text or ""
    data: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "filename": job.original_name,
        "input_format": job.input_format,
        "output_format": job.output_format,
        "converter": config.get("converter_cls", "PdfConverter"),
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "completed_at": _iso(job.completed_at),
        "error_message": job.error_message,
        "result_path": job.result_path,
        "result_chars": len(result_text),
        "conversion_metadata": {
            key: metadata[key]
            for key in (
                "engine",
                "probe_result",
                "mixed_engine_segments",
                "image_understanding",
                "assets",
                "manifest_path",
            )
            if key in metadata and metadata[key]
        },
    }
    if include_result_text:
        data["result_text"] = result_text[:max_chars]
        data["truncated"] = len(result_text) > max_chars
        data["next_step"] = (
            "Use marker_read_output with result_path for file-backed outputs, or raise max_chars."
            if len(result_text) > max_chars
            else None
        )
    return data


def _setting_to_dict(row: Setting) -> dict[str, str]:
    value = row.value
    if row.key == "llm_providers":
        value = _mask_structured_setting(value)
    elif is_sensitive_key(row.key):
        value = _mask_secret_value(value)
    return {"key": row.key, "value": value, "category": row.category}


def _mask_structured_setting(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    return json.dumps(_mask_nested_secrets(parsed), ensure_ascii=False)


def _mask_nested_secrets(value: Any) -> Any:
    if isinstance(value, list):
        return [_mask_nested_secrets(item) for item in value]
    if not isinstance(value, dict):
        return value
    masked: dict[str, Any] = {}
    for key, item in value.items():
        key_lower = str(key).lower()
        if _is_nested_secret_key(key_lower):
            if isinstance(item, list):
                masked[key] = [_mask_secret_value(str(child)) for child in item if child]
            elif item:
                masked[key] = _mask_secret_value(str(item))
            else:
                masked[key] = item
        else:
            masked[key] = _mask_nested_secrets(item)
    return masked


def _is_nested_secret_key(key: str) -> bool:
    return (
        key.endswith("api_key")
        or key.endswith("api_keys")
        or key in {"secret", "token", "password", "credential", "credentials"}
    )


def _mask_secret_value(raw: str) -> str:
    if not raw:
        return raw
    decrypted = decrypt_value(raw)
    if decrypted == raw and raw.startswith("gAAAA"):
        return "****"
    return mask_value(decrypted)


async def _get_setting_row(session: Any, key: str) -> Setting | None:
    return (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()


def _json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _job_cleanup_paths(job: ConversionJob) -> list[Path]:
    paths: list[Path] = []
    upload_path = UPLOAD_DIR / job.filename
    if upload_path.exists():
        paths.append(upload_path)
    if job.result_path:
        paths.append(Path(job.result_path))
    return paths


def _assert_output_read_permitted(path: Path) -> None:
    """Allow output reads only from policy roots or Marker-owned output manifests."""

    assert_output_read_allowed(path)
    if output_root() is not None:
        return
    if _has_marker_output_manifest(path):
        return
    raise InputNotAllowedError(
        f"Output path is not a registered Marker output: {path}",
        hint="Read a path returned by marker_convert_file or configure MARKER_OUTPUT_ROOT.",
        details={"path": str(path), "policy": "registered_marker_output"},
    )


def _has_marker_output_manifest(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    manifest_path = resolved.with_name(f"{resolved.stem}.marker.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("schema_version") != OUTPUT_MANIFEST_SCHEMA_VERSION:
        return False
    output = manifest.get("output")
    if not isinstance(output, dict):
        return False
    allowed = [
        output.get("text_path"),
        output.get("final_path"),
    ]
    return any(_same_resolved_path(resolved, value) for value in allowed if value)


def _same_resolved_path(path: Path, raw: Any) -> bool:
    try:
        return path == Path(str(raw)).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return False


def _remove_paths(paths: list[Path]) -> list[str]:
    removed: list[str] = []
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            removed.append(str(path.resolve()))
        except FileNotFoundError:
            continue
    return removed


async def _cancel_job_best_effort(job_id: str) -> None:
    try:
        from app.main import _app_state

        await _app_state.task_manager.cancel_job(job_id)
    except Exception:
        return


def _task_manager_status(job_id: str) -> dict[str, Any]:
    try:
        return _get_app_state().task_manager.get_status(job_id)
    except Exception:
        return {}


def _get_app_state() -> Any:
    from app.main import _app_state

    return _app_state


async def _ensure_db_tables() -> None:
    global _db_tables_ready
    if _db_tables_ready or _db_session_factory is not async_session_factory:
        return
    await create_tables()
    _db_tables_ready = True


async def _load_llm_config_for_options(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("use_llm"):
        return {"providers": [], "active": {"provider_id": "none", "model_id": ""}}
    from app.database import async_session_factory
    from app.routes.convert import _load_llm_config

    async with async_session_factory() as session:
        return await _load_llm_config(session)


async def _prepare_runtime(config: dict[str, Any]) -> None:
    if not (
        config.get("use_llm")
        or config.get("image_handling_mode") in {"understanding", "both"}
        or config.get("allow_cloud_vlm")
    ):
        return
    from app.core.api_manager import load_secrets_from_db, setup_api_manager_monkeypatch

    await load_secrets_from_db()
    setup_api_manager_monkeypatch()


def _validate_supported_path(path: Path) -> None:
    if not path.is_file():
        raise InputNotFoundError(
            f"Input file not found: {path}",
            hint="Check the path or pass --source-url for remote files.",
            details={"path": str(path)},
        )
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"Unsupported file type '{path.suffix}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            details={"suffix": path.suffix, "allowed": sorted(ALLOWED_EXTENSIONS)},
        )
    assert_local_input_allowed(path)


def _validate_page_range_safe(page_range: str, page_count: int) -> None:
    try:
        _validate_page_range(page_range, page_count)
    except HTTPException as exc:
        raise UsageError(str(exc.detail), details={"page_range": page_range}) from exc


async def _download_source_url_safe(raw_url: str, destination: Path) -> tuple[str, str, str]:
    async def audit_hook(event: str, payload: dict[str, Any]) -> None:
        await record_audit_event(
            None,
            event_type=event,
            surface="agent",
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
            audit_hook=audit_hook,
        )
        return downloaded.original_name, downloaded.suffix, downloaded.safe_url
    except SafeUrlFetchError as exc:
        detail = str(exc.detail)
        await record_audit_event(
            None,
            event_type="url_fetch.blocked" if exc.category in {"blocked", "unsafe"} else "url_fetch.failed",
            surface="agent",
            resource_type="source_url",
            resource_id=raw_url,
            status="denied" if exc.category in {"blocked", "unsafe"} else "failed",
            payload={"url": raw_url, "category": exc.category, "detail": detail},
        )
        if exc.category == "unsafe":
            from app.errors import UrlUnsafeError

            raise UrlUnsafeError(detail, details={"url": raw_url}) from exc
        if exc.category == "blocked":
            from app.errors import NetworkBlockedError

            raise NetworkBlockedError(detail, details={"url": raw_url}) from exc
        from app.errors import NetworkFetchFailedError

        raise NetworkFetchFailedError(detail, details={"url": raw_url}, retryable=True) from exc


def _save_result(
    result: dict[str, Any],
    *,
    source_name: str,
    output_base: Path,
    output_path: Path | None,
    conversion_config: dict[str, Any],
    source_url: str | None,
) -> dict[str, Any]:
    written = write_conversion_output(
        result,
        source_name=source_name,
        output_base=output_base,
        output_path=output_path,
        output_format=None,
        conversion_config=conversion_config,
        layout="file",
        source_url=source_url,
    )
    return written.to_agent_output()
