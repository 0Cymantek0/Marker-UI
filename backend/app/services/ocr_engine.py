"""Pluggable OCR-engine seam (plan §5 / decision #5).

The plan's long-term direction is to make local OCR/doc-parse engines
**plug-and-play** so a higher-accuracy engine (GLM-OCR, PP-OCRv5, Mistral OCR)
can be swapped in behind the §9.4 benchmark with minimal blast radius. This
module lays that seam *now* but ships only the Surya-backed default behind it —
no engine migration happens here. We earn the right to swap later by building
the interface first.

``OCREngine`` is a structural :class:`typing.Protocol`: any object exposing a
``recognize(image) -> OCRResult`` method and an ``available`` flag satisfies it,
so adding GLM-OCR later means writing one adapter, not touching the processor.

``SuryaOCREngine`` is the only implementation today — a thin adapter over
:class:`app.services.local_ocr.LocalOcrService` (Tier 1.5/2, already resident on
the GPU). ``build_ocr_engine`` is the single factory the processor calls; it
maps the ``ocr_engine`` config string to a concrete engine and raises a clear
error for not-yet-shipped engines rather than silently degrading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class OCRResult:
    """Engine-agnostic OCR result (plan §5 ``OCRResult`` shape).

    A normalised return type so the processor never depends on a specific
    engine's native result object. ``html`` is the renderer-ready fragment;
    ``text`` the plain transcription; ``mean_confidence`` lets a caller gate on
    low-confidence output (the §5 self-correction hook).
    """

    text: str = ""
    html: str = ""
    line_count: int = 0
    mean_confidence: float = 0.0
    error: str | None = None
    duration_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class OCREngine(Protocol):
    """Structural contract every local OCR engine must satisfy.

    Implementations transcribe a single PIL image. They must **never raise** —
    a failure is reported via ``OCRResult.error`` so one bad image never aborts
    a conversion (the project-wide fail-soft contract).
    """

    @property
    def available(self) -> bool:
        """True when the engine has everything it needs to run."""
        ...

    def recognize(self, image: Any) -> OCRResult:
        """Transcribe one image to text + HTML. Never raises."""
        ...


class SuryaOCREngine:
    """Default OCR engine — adapts :class:`LocalOcrService` to ``OCREngine``.

    Surya recognition is already loaded for marker's main pipeline, so this is
    the zero-marginal-cost default (decision #1/#2). The adapter translates
    ``LocalOcrService.ocr_image``'s native :class:`OcrResult` into the
    engine-agnostic :class:`OCRResult`.
    """

    name = "surya"

    def __init__(
        self,
        recognition_model: Any | None = None,
        detection_model: Any | None = None,
        math_mode: bool = True,
    ) -> None:
        from app.services.local_ocr import LocalOcrService

        self._svc = LocalOcrService(
            recognition_model=recognition_model,
            detection_model=detection_model,
            math_mode=math_mode,
        )

    @property
    def available(self) -> bool:
        return self._svc.available

    def recognize(self, image: Any) -> OCRResult:
        result = self._svc.ocr_image(image)
        return OCRResult(
            text=result.text,
            html=result.html,
            line_count=result.line_count,
            # LocalOcrService does not surface per-line confidence yet; report a
            # neutral 1.0 for recovered text so the confidence gate is a no-op
            # until an engine that exposes confidence is plugged in.
            mean_confidence=1.0 if result.text else 0.0,
            error=result.error,
            duration_ms=result.duration_ms,
        )


# Engines that are planned but deliberately NOT shipped in this phase. Listed so
# the factory can raise an explicit, actionable error instead of a generic one
# (plan §12 deferred work — gated behind the §9.4 benchmark).
# Specialist engines that live BEHIND the hybrid_ocr orchestrator, never as a
# user-facing ``ocr_engine`` value. Listed so this image-OCR seam raises a
# clear error if some caller still asks for one directly. ``mistral_ocr`` is
# gone entirely: it is cloud/API-based and conflicts with the local-first
# Hybrid OCR contract (blueprint §0).
_DEFERRED_ENGINES = {
    "glm_ocr": "GLM-OCR (internal hybrid_ocr specialist)",
    "paddleocr_vl": "PaddleOCR-VL (internal hybrid_ocr specialist)",
}


def build_ocr_engine(
    engine: str = "surya",
    *,
    recognition_model: Any | None = None,
    detection_model: Any | None = None,
    math_mode: bool = True,
) -> OCREngine:
    """Build the OCR engine named by config.

    Only ``surya`` ships today (decision #5: lay the seam, ship Surya). A
    deferred engine name raises a clear ``NotImplementedError`` pointing at the
    §9.4 benchmark gate; an unknown name raises ``ValueError``.
    """
    key = (engine or "surya").lower()
    if key == "surya":
        return SuryaOCREngine(
            recognition_model=recognition_model,
            detection_model=detection_model,
            math_mode=math_mode,
        )
    if key in _DEFERRED_ENGINES:
        raise NotImplementedError(
            f"OCR engine {key!r} ({_DEFERRED_ENGINES[key]}) is not shipped yet. "
            "It is gated behind the §9.4 benchmark — run the benchmark harness "
            "and only enable it if the data justifies the added dependency/VRAM."
        )
    raise ValueError(f"Unknown ocr_engine {engine!r}; known: surya")
