"""Route Hybrid OCR targets to local specialist engines."""

from __future__ import annotations

from app.hybrid_ocr.contracts import HybridEngine, HybridTarget, TargetKind


def route_target(target: HybridTarget) -> list[HybridEngine]:
    if target.target_kind in {TargetKind.TABLE, TargetKind.FORMULA}:
        return [HybridEngine.GLM_OCR, HybridEngine.PADDLEOCR_VL, HybridEngine.SURYA]
    if target.target_kind in {
        TargetKind.DEGRADED_TEXT,
        TargetKind.FULL_PAGE_SCAN,
        TargetKind.SEAL_OR_STAMP,
    }:
        return [HybridEngine.PADDLEOCR_VL, HybridEngine.GLM_OCR, HybridEngine.SURYA]
    return [HybridEngine.SURYA]

