"""Helpers for conversion job upload/output artifact cleanup."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.core.config import UPLOAD_DIR
from app.models.job import ConversionJob


def job_artifact_paths(job: ConversionJob) -> list[Path]:
    """Return owned upload/output paths for a job, including file manifests."""

    paths: list[Path] = []
    upload_path = UPLOAD_DIR / job.filename
    if upload_path.exists():
        paths.append(upload_path)
    if job.result_path:
        result_path = Path(job.result_path)
        if result_path.exists():
            paths.append(result_path)
        if not result_path.is_dir():
            manifest_path = result_path.with_name(f"{result_path.stem}.marker.json")
            if manifest_path.exists():
                paths.append(manifest_path)
    return _dedupe(paths)


def remove_paths(paths: list[Path]) -> list[str]:
    """Remove files/directories best-effort and return paths actually removed."""

    removed: list[str] = []
    for path in _dedupe(paths):
        try:
            resolved = path.resolve(strict=False)
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                continue
            removed.append(str(resolved))
        except FileNotFoundError:
            continue
    return removed


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        key = str(path.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped
