"""Specialist capability detection.

Detection is intentionally shallow and side-effect-free: no model imports, no
downloads, no network calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.hybrid_ocr.contracts import HybridEngine


@dataclass(frozen=True)
class HybridCapabilities:
    available: set[HybridEngine] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def is_available(self, engine: HybridEngine) -> bool:
        return engine == HybridEngine.SURYA or engine in self.available


def detect_capabilities() -> HybridCapabilities:
    available: set[HybridEngine] = {HybridEngine.SURYA}
    warnings: list[str] = []
    if os.environ.get("MARKER_GLM_PYTHON") or os.environ.get("MARKER_GLM_OCR_ENDPOINT"):
        available.add(HybridEngine.GLM_OCR)
    else:
        warnings.append("GLM-OCR unavailable; table/formula targets kept as Surya baseline")
    if os.environ.get("MARKER_PADDLE_PYTHON"):
        available.add(HybridEngine.PADDLEOCR_VL)
    else:
        warnings.append("PaddleOCR-VL unavailable; degraded-text targets kept as Surya baseline")
    return HybridCapabilities(available=available, warnings=warnings)

