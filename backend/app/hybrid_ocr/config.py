"""Configuration parsing for local Hybrid OCR."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

HybridOcrProfile = Literal["balanced", "max_accuracy", "low_vram"]


@dataclass(frozen=True)
class HybridOcrConfig:
    enabled: bool = False
    profile: HybridOcrProfile = "balanced"
    require_specialists: bool = False
    debug: bool = False


def parse_hybrid_ocr_config(options: dict[str, Any]) -> HybridOcrConfig:
    profile = options.get("hybrid_ocr_profile") or "balanced"
    if profile not in {"balanced", "max_accuracy", "low_vram"}:
        raise ValueError("Invalid hybrid_ocr_profile; expected balanced, max_accuracy, or low_vram.")
    return HybridOcrConfig(
        enabled=options.get("ocr_engine") == "hybrid_ocr",
        profile=profile,
        require_specialists=bool(options.get("hybrid_ocr_require_specialists")),
        debug=bool(options.get("debug")),
    )

