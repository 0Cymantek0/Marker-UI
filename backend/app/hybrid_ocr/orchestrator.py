"""Local Hybrid OCR orchestration."""

from __future__ import annotations

import time
import tempfile
import logging
from pathlib import Path
from typing import Any

from app.hybrid_ocr.adapters import run_specialist_worker
from app.hybrid_ocr.capability import detect_capabilities
from app.hybrid_ocr.collector import collect_targets
from app.hybrid_ocr.config import parse_hybrid_ocr_config
from app.hybrid_ocr.contracts import (
    HybridEngine,
)
from app.hybrid_ocr.cropper import materialize_target_crops
from app.hybrid_ocr.merger import merge_results
from app.hybrid_ocr.router import route_target

logger = logging.getLogger(__name__)


class HybridOcrOrchestrator:
    """Refine a Marker document with local specialist OCR when available."""

    def refine(
        self,
        *,
        document: Any,
        filepath: str,
        options: dict[str, Any],
        marker_service: Any | None = None,
        converter: Any | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        config = parse_hybrid_ocr_config(options)
        if not config.enabled:
            return document, {}

        start = time.perf_counter()
        capabilities = detect_capabilities()
        with tempfile.TemporaryDirectory(prefix="marker-hybrid-ocr-") as temp_dir:
            targets = collect_targets(document, filepath=filepath, job_dir=Path(temp_dir))
            materialize_target_crops(filepath, targets)
            logger.info(
                "Hybrid OCR start: targets=%d kinds=%s profile=%s",
                len(targets),
                _targets_by_kind(targets),
                config.profile,
            )
            if config.profile == "low_vram" and marker_service is not None:
                release = getattr(marker_service, "release_models", None)
                if callable(release):
                    release()

            results = []
            skipped_missing_engine = 0
            engines_requested: set[HybridEngine] = {HybridEngine.SURYA}
            targets_by_engine: dict[HybridEngine, list[Any]] = {}

            for target in targets:
                route = route_target(target)
                engines_requested.update(route)
                engine = next(
                    (
                        candidate
                        for candidate in route
                        if candidate != HybridEngine.SURYA and capabilities.is_available(candidate)
                    ),
                    None,
                )
                if engine is None:
                    skipped_missing_engine += 1
                    continue
                targets_by_engine.setdefault(engine, []).append(target)

            for engine, engine_targets in targets_by_engine.items():
                logger.info("Hybrid OCR worker: engine=%s targets=%d", engine.value, len(engine_targets))
                results.extend(
                    run_specialist_worker(
                        engine,
                        engine_targets,
                        timeout_s=config.worker_timeout_s,
                    )
                )

            replacements = merge_results(document, targets, results)
            accepted = sum(1 for result in results if result.validation.accepted and result.status == "ok")
            failed = sum(1 for result in results if result.status in {"failed", "timeout"})
            rejected = sum(1 for result in results if result.status == "ok" and not result.validation.accepted)
            engines_used = {result.engine for result in results}
            warnings = list(capabilities.warnings)
            if not targets:
                warnings.append("Hybrid OCR found no specialist targets; Surya baseline kept")
            if skipped_missing_engine:
                warnings.append("Hybrid OCR specialists unavailable for one or more targets; Surya baseline kept")
            if config.require_specialists and skipped_missing_engine:
                raise RuntimeError("Hybrid OCR specialists are required but unavailable.")
            logger.info(
                "Hybrid OCR done: targets=%d used=%s accepted=%d rejected=%d failed=%d replacements=%d skipped_missing=%d",
                len(targets),
                sorted(engine.value for engine in engines_used),
                accepted,
                rejected,
                failed,
                replacements,
                skipped_missing_engine,
            )

            meta = {
                "enabled": True,
                "profile": config.profile,
                "local_only": True,
                "targets_total": len(targets),
                "targets_by_kind": _targets_by_kind(targets),
                "engines_requested": sorted(engine.value for engine in engines_requested),
                "engines_available": sorted(engine.value for engine in capabilities.available),
                "engines_used": sorted(engine.value for engine in engines_used),
                "specialist_results": {
                    "accepted": accepted,
                    "rejected": rejected,
                    "failed": failed,
                    "skipped_missing_engine": skipped_missing_engine,
                    "replacements": replacements,
                },
                "duration_ms": int((time.perf_counter() - start) * 1000),
                "peak_vram_mb": None,
                "warnings": warnings,
                "target_summaries": [
                    {
                        "target_id": result.target_id,
                        "page": next((target.page_number for target in targets if target.target_id == result.target_id), None),
                        "engine": result.engine.value,
                        "status": result.status,
                        "accepted": result.validation.accepted,
                        "validation_score": result.validation.score,
                        "replacement_policy": result.replacement_policy.value,
                        "error": result.error,
                    }
                    for result in results
                ],
            }
            return document, meta


def _targets_by_kind(targets: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for target in targets:
        counts[target.target_kind.value] = counts.get(target.target_kind.value, 0) + 1
    return counts

