"""Shared helpers for reading Marker output manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


OUTPUT_MANIFEST_SCHEMA_VERSION = "marker.output_manifest.v1"


def manifest_for_output_path(path: Path) -> tuple[Path | None, dict[str, Any]]:
    """Find a Marker manifest for an output text path, manifest path, or bundle directory."""

    for candidate in _manifest_candidates(path.expanduser()):
        if not candidate.is_file():
            continue
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict) and manifest.get("schema_version") == OUTPUT_MANIFEST_SCHEMA_VERSION:
            return candidate, manifest
    return None, {}


def manifest_for_job_status(status: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    metadata = status.get("conversion_metadata") if isinstance(status, dict) else {}
    manifest_path = metadata.get("manifest_path") if isinstance(metadata, dict) else None
    if manifest_path:
        return manifest_for_output_path(Path(str(manifest_path)))
    result_path = status.get("result_path") if isinstance(status, dict) else None
    if result_path:
        return manifest_for_output_path(Path(str(result_path)))
    return None, {}


def output_text_path_from_manifest(
    manifest: dict[str, Any],
    *,
    manifest_path: Path | None = None,
) -> str | None:
    output = manifest.get("output") if isinstance(manifest, dict) else None
    if not isinstance(output, dict):
        return None
    text_path = output.get("text_path")
    if not text_path:
        return None
    path = Path(str(text_path))
    if manifest_path is not None and not path.is_absolute():
        path = manifest_path.parent / path
    return str(path)


def _manifest_candidates(path: Path) -> list[Path]:
    candidates: list[Path] = []
    if path.is_dir():
        candidates.extend(sorted(path.glob("*.marker.json")))
        return candidates

    candidates.append(path)
    if path.name.endswith(".marker.json"):
        return candidates
    sibling = path.with_name(f"{path.stem}.marker.json")
    candidates.append(sibling)
    return candidates
