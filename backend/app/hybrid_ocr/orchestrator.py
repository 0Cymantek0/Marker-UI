"""Local Hybrid OCR orchestration."""

from __future__ import annotations

import time
import tempfile
from pathlib import Path
from typing import Any

from app.hybrid_ocr.capability import detect_capabilities
from app.hybrid_ocr.collector import collect_targets
from app.hybrid_ocr.config import parse_hybrid_ocr_config
from app.hybrid_ocr.contracts import (
    HybridEngine,
    HybridResult,
    ReplacementPolicy,
    TargetKind,
)
from app.hybrid_ocr.merger import merge_results
from app.hybrid_ocr.router import route_target
from app.hybrid_ocr.validators import validate_for_kind


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
            if config.profile == "low_vram" and marker_service is not None:
                release = getattr(marker_service, "release_models", None)
                if callable(release):
                    release()

            mock_results = _coerce_mock_results(options.get("hybrid_ocr_mock_results") or {}, targets)
            results: list[HybridResult] = []
            skipped_missing_engine = 0
            engines_requested: set[HybridEngine] = {HybridEngine.SURYA}
            engines_used: set[HybridEngine] = set()

            for target in targets:
                route = route_target(target)
                engines_requested.update(route)
                engine = next((candidate for candidate in route if capabilities.is_available(candidate)), HybridEngine.SURYA)
                if engine == HybridEngine.SURYA and route[0] != HybridEngine.SURYA:
                    skipped_missing_engine += 1
                if target.target_id in mock_results:
                    result = mock_results[target.target_id]
                    engines_used.add(result.engine)
                    results.append(result)
                elif engine != HybridEngine.SURYA:
                    engines_used.add(engine)
                    results.append(_no_change_result(target.target_id, engine, target.target_kind))

            replacements = merge_results(document, targets, results)
            accepted = sum(1 for result in results if result.validation.accepted and result.status == "ok")
            failed = sum(1 for result in results if result.status in {"failed", "timeout"})
            rejected = sum(1 for result in results if result.status == "ok" and not result.validation.accepted)
            warnings = list(capabilities.warnings)
            if not targets:
                warnings.append("Hybrid OCR found no specialist targets; Surya baseline kept")
            if skipped_missing_engine:
                warnings.append("Hybrid OCR specialists unavailable for one or more targets; Surya baseline kept")
            if config.require_specialists and skipped_missing_engine:
                raise RuntimeError("Hybrid OCR specialists are required but unavailable.")

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
                        "engine": result.engine.value,
                        "accepted": result.validation.accepted,
                        "validation_score": result.validation.score,
                        "replacement_policy": result.replacement_policy.value,
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


def _no_change_result(target_id: str, engine: HybridEngine, kind: TargetKind) -> HybridResult:
    validation = validate_for_kind(kind, "", "", baseline_text="")
    return HybridResult(
        target_id=target_id,
        engine=engine,
        status="skipped",
        output_kind=kind,
        text="",
        markdown="",
        html="",
        json_payload={},
        confidence=None,
        duration_ms=0,
        validation=validation,
        replacement_policy=ReplacementPolicy.NO_CHANGE,
        warnings=["Specialist worker not implemented in this build; Surya baseline kept"],
    )


def _coerce_mock_results(raw: dict[str, Any], targets: list[Any]) -> dict[str, HybridResult]:
    target_by_id = {target.target_id: target for target in targets}
    out: dict[str, HybridResult] = {}
    if not isinstance(raw, dict):
        return out
    for target_id, payload in raw.items():
        target = target_by_id.get(str(target_id))
        if target is None or not isinstance(payload, dict):
            continue
        engine = HybridEngine(payload.get("engine") or HybridEngine.GLM_OCR)
        text = str(payload.get("text") or "")
        markdown = str(payload.get("markdown") or text)
        validation = validate_for_kind(target.target_kind, text, markdown, baseline_text=target.baseline_text)
        out[target.target_id] = HybridResult(
            target_id=target.target_id,
            engine=engine,
            status=str(payload.get("status") or "ok"),
            output_kind=target.target_kind,
            text=text,
            markdown=markdown,
            html=str(payload.get("html") or ""),
            json_payload=payload.get("json_payload") if isinstance(payload.get("json_payload"), dict) else {},
            confidence=payload.get("confidence") if isinstance(payload.get("confidence"), (int, float)) else None,
            duration_ms=int(payload.get("duration_ms") or 0),
            validation=validation,
            replacement_policy=ReplacementPolicy(payload.get("replacement_policy") or ReplacementPolicy.REPLACE_BLOCK),
            warnings=list(payload.get("warnings") or []),
            error=payload.get("error"),
        )
    return out

