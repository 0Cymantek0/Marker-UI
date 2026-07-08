"""Shared conversion output writer.

This module is intentionally lightweight: it must stay importable by CLI/MCP
paths without loading Marker models. It centralizes path collision handling,
atomic file writes, sidecar asset persistence, and output manifest creation.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.errors import OutputExistsError, OutputWriteFailedError


OUTPUT_MANIFEST_SCHEMA_VERSION = "marker.output_manifest.v1"
OutputLayout = Literal["file", "directory_if_assets"]


@dataclass(frozen=True)
class WrittenOutput:
    """Resolved output paths and manifest data for one conversion."""

    final_path: Path
    text_path: Path
    manifest_path: Path
    asset_paths: list[Path]
    asset_entries: list[dict[str, Any]]
    media_type: str

    def to_agent_output(self) -> dict[str, Any]:
        return {
            "text_path": str(self.text_path.resolve()),
            "manifest_path": str(self.manifest_path.resolve()),
            "asset_paths": [str(path.resolve()) for path in self.asset_paths],
            "media_type": self.media_type,
        }


def write_conversion_output(
    result: dict[str, Any],
    *,
    source_name: str,
    output_base: Path,
    output_path: Path | None = None,
    output_format: str | None = None,
    conversion_config: dict[str, Any] | None = None,
    layout: OutputLayout = "file",
    disable_image_extraction: bool = False,
    overwrite: bool = False,
    job_id: str | None = None,
    source_url: str | None = None,
) -> WrittenOutput:
    """Persist one conversion result and create its manifest.

    ``layout="file"`` keeps the agent/CLI shape: a text file plus an optional
    ``*_assets`` sibling directory. ``layout="directory_if_assets"`` preserves
    GUI async job behavior: jobs with images/assets return a result directory.
    """

    output_base = Path(output_base).expanduser()
    text = str(result.get("text") or "")
    extension = _extension_from_result(result, output_format)
    metadata = result.get("metadata") or {}
    images = result.get("images") or {}
    if disable_image_extraction:
        images = {}
    assets = result.get("assets") or []
    has_sidecars = bool(images) or bool(assets)
    stem = _safe_stem(source_name)

    if output_path is not None:
        text_path = Path(output_path).expanduser()
        final_path = text_path
        asset_base = text_path.with_suffix("")
        asset_dir = asset_base.parent / f"{asset_base.name}_assets"
        manifest_path = text_path.with_name(f"{text_path.stem}.marker.json")
        _ensure_available(text_path, overwrite=overwrite)
        _ensure_available(manifest_path, overwrite=overwrite)
    elif layout == "directory_if_assets" and has_sidecars:
        bundle_dir = _next_available_dir(output_base / stem, overwrite=overwrite)
        final_path = bundle_dir
        text_path = bundle_dir / f"{stem}.{extension}"
        asset_dir = bundle_dir
        manifest_path = bundle_dir / f"{stem}.marker.json"
    else:
        text_path = _next_available_file(output_base / f"{stem}.{extension}", overwrite=overwrite)
        final_path = text_path
        asset_base = text_path.with_suffix("")
        asset_dir = asset_base.parent / f"{asset_base.name}_assets"
        manifest_path = text_path.with_name(f"{text_path.stem}.marker.json")

    text_path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(text_path, text)

    asset_entries: list[dict[str, Any]] = []
    used_asset_paths: set[str] = set()
    for entry in _write_images(images, asset_dir, used_asset_paths=used_asset_paths, overwrite=overwrite):
        asset_entries.append(entry)
    for entry in _write_assets(assets, asset_dir, used_asset_paths=used_asset_paths, overwrite=overwrite):
        asset_entries.append(entry)
    asset_paths = [
        (asset_dir / str(entry["relative_path"])).resolve()
        for entry in asset_entries
    ]

    manifest = _build_manifest(
        source_name=source_name,
        source_url=source_url,
        job_id=job_id,
        text_path=text_path,
        final_path=final_path,
        manifest_path=manifest_path,
        text=text,
        asset_entries=asset_entries,
        metadata=metadata,
        conversion_config=conversion_config or {},
        media_type=_media_type_from_result(result, output_format, text_path),
    )
    _write_json_atomic(manifest_path, manifest)
    return WrittenOutput(
        final_path=final_path,
        text_path=text_path,
        manifest_path=manifest_path,
        asset_paths=asset_paths,
        asset_entries=asset_entries,
        media_type=manifest["output"]["media_type"],
    )


def _extension_from_result(result: dict[str, Any], output_format: str | None) -> str:
    ext = str(result.get("extension") or "").lstrip(".")
    if not ext and output_format:
        ext = {
            "markdown": "md",
            "json": "json",
            "html": "html",
            "chunks": "json",
        }.get(output_format, output_format)
    return (ext or "md").lstrip(".")


def _media_type_from_result(result: dict[str, Any], output_format: str | None, text_path: Path) -> str:
    ext = _extension_from_result(result, output_format).lower()
    media_by_ext = {
        "md": "text/markdown",
        "markdown": "text/markdown",
        "html": "text/html",
        "htm": "text/html",
        "json": "application/json",
        "txt": "text/plain",
    }
    if ext in media_by_ext:
        return media_by_ext[ext]
    return mimetypes.guess_type(text_path.name)[0] or "text/markdown"


def _safe_stem(name: str) -> str:
    stem = Path(str(name or "")).stem or "converted"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return cleaned[:80] or "converted"


def _next_available_file(path: Path, *, overwrite: bool) -> Path:
    if overwrite:
        return path
    if not path.exists() and not path.with_name(f"{path.stem}.marker.json").exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        manifest = candidate.with_name(f"{candidate.stem}.marker.json")
        if not candidate.exists() and not manifest.exists():
            return candidate
    raise OutputWriteFailedError(
        f"No available output filename for: {path}",
        details={"path": str(path)},
    )


def _next_available_dir(path: Path, *, overwrite: bool) -> Path:
    if overwrite:
        path.mkdir(parents=True, exist_ok=True)
        return path
    if not path.exists():
        path.mkdir(parents=True)
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
    raise OutputWriteFailedError(
        f"No available output directory for: {path}",
        details={"path": str(path)},
    )


def _ensure_available(path: Path, *, overwrite: bool) -> None:
    if overwrite:
        return
    if path.exists():
        raise OutputExistsError(
            f"Output file already exists: {path}",
            hint="Pass --overwrite to replace it, or choose a different output path.",
            details={"path": str(path)},
        )


def _write_images(
    images: dict[str, Any],
    asset_dir: Path,
    *,
    used_asset_paths: set[str],
    overwrite: bool,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_name, value in images.items():
        name = _safe_leaf_name(raw_name, fallback="image")
        target = _next_available_asset_path(
            asset_dir,
            Path(name),
            used_paths=used_asset_paths,
            overwrite=overwrite,
        )
        relative_name = target.relative_to(asset_dir).as_posix()
        try:
            if hasattr(value, "save"):
                _save_pil_atomic(value, target)
            elif isinstance(value, (bytes, bytearray)):
                _write_bytes_atomic(target, bytes(value))
            else:
                _write_text_atomic(target, str(value))
        except OSError as exc:
            raise OutputWriteFailedError(
                f"Failed to save image asset: {name}",
                details={"path": str(target), "error": str(exc)},
            ) from exc
        entries.append(_asset_entry(name=relative_name, path=target, media_type=_guess_media_type(target)))
    return entries


def _write_assets(
    assets: list[Any],
    asset_dir: Path,
    *,
    used_asset_paths: set[str],
    overwrite: bool,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_asset in assets:
        name, media_type, payload, pil = _asset_parts(raw_asset)
        if not name:
            continue
        relative = _safe_relative_path(name)
        if relative is None:
            continue
        target = _next_available_asset_path(
            asset_dir,
            relative,
            used_paths=used_asset_paths,
            overwrite=overwrite,
        )
        relative_name = target.relative_to(asset_dir).as_posix()
        try:
            if pil is not None and hasattr(pil, "save"):
                _save_pil_atomic(pil, target)
            elif isinstance(payload, (bytes, bytearray)):
                _write_bytes_atomic(target, bytes(payload))
            else:
                continue
        except OSError as exc:
            raise OutputWriteFailedError(
                f"Failed to save sidecar asset: {name}",
                details={"path": str(target), "error": str(exc)},
            ) from exc
        entries.append(
            _asset_entry(
                name=relative_name,
                path=target,
                media_type=media_type or "application/octet-stream",
            )
        )
    return entries


def _next_available_asset_path(
    asset_dir: Path,
    relative_path: Path,
    *,
    used_paths: set[str],
    overwrite: bool,
) -> Path:
    candidate = asset_dir / relative_path
    if _claim_asset_path(candidate, used_paths, overwrite=overwrite):
        return candidate

    suffix = candidate.suffix
    stem = candidate.stem if suffix else candidate.name
    for index in range(1, 10_000):
        next_name = f"{stem}-{index}{suffix}" if suffix else f"{stem}-{index}"
        next_candidate = candidate.with_name(next_name)
        if _claim_asset_path(next_candidate, used_paths, overwrite=overwrite):
            return next_candidate
    raise OutputWriteFailedError(
        f"No available asset filename for: {relative_path}",
        details={"path": str(asset_dir / relative_path)},
    )


def _claim_asset_path(path: Path, used_paths: set[str], *, overwrite: bool) -> bool:
    key = os.path.normcase(str(path.resolve(strict=False)))
    if key in used_paths:
        return False
    if path.exists() and not overwrite:
        return False
    used_paths.add(key)
    return True


def _asset_parts(raw_asset: Any) -> tuple[str, str, Any, Any]:
    if isinstance(raw_asset, dict):
        return (
            str(raw_asset.get("name") or "").strip(),
            str(raw_asset.get("media_type") or "application/octet-stream"),
            raw_asset.get("data"),
            raw_asset.get("pil"),
        )
    return (
        str(getattr(raw_asset, "name", "") or "").strip(),
        str(getattr(raw_asset, "media_type", None) or "application/octet-stream"),
        getattr(raw_asset, "data", None),
        getattr(raw_asset, "pil", None),
    )


def _safe_leaf_name(raw_name: Any, *, fallback: str) -> str:
    name = Path(str(raw_name or "").replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned[:120] or fallback


def _safe_relative_path(name: str) -> Path | None:
    parts: list[str] = []
    for part in Path(name.replace("\\", "/")).parts:
        if part in {"", ".", ".."}:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", part).strip("._")
        if cleaned:
            parts.append(cleaned[:120])
    if not parts:
        return None
    return Path(*parts)


def _asset_entry(*, name: str, path: Path, media_type: str) -> dict[str, Any]:
    relative_path = Path(name.replace("\\", "/")).as_posix()
    return {
        "name": name,
        "relative_path": relative_path,
        "path": relative_path,
        "media_type": media_type,
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _guess_media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _build_manifest(
    *,
    source_name: str,
    source_url: str | None,
    job_id: str | None,
    text_path: Path,
    final_path: Path,
    manifest_path: Path,
    text: str,
    asset_entries: list[dict[str, Any]],
    metadata: dict[str, Any],
    conversion_config: dict[str, Any],
    media_type: str,
) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_MANIFEST_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "source": {
            "name": source_name,
            "source_url": source_url,
        },
        "output": {
            "final_path": _manifest_relative_path(final_path, manifest_path),
            "text_path": _manifest_relative_path(text_path, manifest_path),
            "manifest_path": _manifest_relative_path(manifest_path, manifest_path),
            "media_type": media_type,
            "text_chars": len(text),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "asset_count": len(asset_entries),
            "assets": asset_entries,
        },
        "conversion": {
            "config": _redact_config(conversion_config),
            "metadata": _safe_json(metadata),
        },
    }


def _manifest_relative_path(path: Path, manifest_path: Path) -> str:
    try:
        rel = path.resolve(strict=False).relative_to(manifest_path.parent.resolve(strict=False))
    except ValueError:
        rel = Path(path.name)
    value = rel.as_posix()
    return value or "."


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _redact_value(str(key), value)
        for key, value in (config or {}).items()
    }


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(token in lowered for token in ("key", "secret", "token", "password", "credential")):
        return "****" if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    return _safe_json(value)


def _safe_json(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, bytearray):
        data = bytes(value)
        return {"type": "bytes", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return str(value)


def _write_text_atomic(path: Path, text: str) -> None:
    _write_bytes_atomic(path, text.encode("utf-8"))


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _save_pil_atomic(image: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix or ".png"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=suffix, dir=str(path.parent))
    os.close(fd)
    try:
        image.save(tmp_name)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
