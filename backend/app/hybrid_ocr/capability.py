"""Specialist capability detection.

Detection is intentionally shallow and side-effect-free: no model imports, no
downloads, no network calls.
"""

from __future__ import annotations

import os
import shlex
import importlib.util
from pathlib import Path
from dataclasses import dataclass, field

from app.hybrid_ocr.contracts import HybridEngine
from app.hybrid_ocr.locality import is_local_endpoint
from app.hybrid_ocr.setup import model_snapshot_present


@dataclass(frozen=True)
class HybridCapabilities:
    available: set[HybridEngine] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def is_available(self, engine: HybridEngine) -> bool:
        return engine == HybridEngine.SURYA or engine in self.available


def detect_capabilities() -> HybridCapabilities:
    available: set[HybridEngine] = {HybridEngine.SURYA}
    warnings: list[str] = []
    if _glm_runtime_configured() or _native_transformers_ready("glm_ocr"):
        available.add(HybridEngine.GLM_OCR)
    else:
        warnings.append("GLM-OCR worker unavailable; table/formula targets kept as Surya baseline")
    if not model_snapshot_present("glm_ocr"):
        warnings.append("GLM-OCR model snapshot not found; run `marker hybrid-ocr setup --engine glm_ocr`")
    if _paddle_runtime_configured() or _native_transformers_ready("paddleocr_vl"):
        available.add(HybridEngine.PADDLEOCR_VL)
    else:
        warnings.append("PaddleOCR-VL worker unavailable; degraded-text targets kept as Surya baseline")
    if not model_snapshot_present("paddleocr_vl"):
        warnings.append("PaddleOCR-VL model snapshot not found; run `marker hybrid-ocr setup --engine paddleocr_vl`")
    return HybridCapabilities(available=available, warnings=warnings)


def _glm_runtime_configured() -> bool:
    endpoint = os.environ.get("MARKER_GLM_OCR_ENDPOINT")
    command = os.environ.get("MARKER_GLM_OCR_COMMAND")
    return bool((endpoint and is_local_endpoint(endpoint)) or _executable_exists(command))


def _paddle_runtime_configured() -> bool:
    endpoint = os.environ.get("MARKER_PADDLE_OCR_VL_ENDPOINT")
    command = os.environ.get("MARKER_PADDLE_OCR_VL_COMMAND")
    return bool((endpoint and is_local_endpoint(endpoint)) or _executable_exists(command))


def _native_transformers_ready(engine: str) -> bool:
    if engine == "glm_ocr" and os.environ.get("MARKER_HYBRID_OCR_ENABLE_NATIVE_TRANSFORMERS") != "true":
        return False
    if not model_snapshot_present(engine):
        return False
    required = ["torch", "transformers", "PIL"]
    return all(importlib.util.find_spec(name) is not None for name in required)


def _executable_exists(command: str | None) -> bool:
    if not command:
        return False
    parts = shlex.split(command, posix=False)
    if not parts:
        return False
    executable = parts[0].strip('"')
    if Path(executable).expanduser().exists():
        return True
    return any(
        (Path(path) / executable).exists()
        for path in os.environ.get("PATH", "").split(os.pathsep)
        if path
    )

