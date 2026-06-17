"""Tier-2 deterministic local OCR (plan §5b — the line-227 fix).

When the Tier-0 router decides an image is *text rendered as an image* (a
paragraph screenshotted into a figure, a scanned caption), the right answer is
to **transcribe** it, not to *describe* it. The old pipeline sent such images to
the cloud VLM, which would hallucinate a prose summary instead of returning the
text verbatim (the ``openskill.md`` line-227 failure). This module fixes that
class of bug for good: it runs Surya's **recognition** model — already resident
in GPU memory for marker's own pipeline — over the image and returns the
transcribed text as clean HTML, with **zero cloud tokens and zero
hallucination**.

It mirrors ``marker.builders.ocr.OcrBuilder.ocr_extraction``: the recognition
predictor runs detection internally (via ``det_predictor``) so a single call
yields ordered text lines for the whole crop.

* **Never raises.** Any failure returns ``OcrResult(error=...)`` so one bad
  image never aborts a conversion.
* **Degrades without models.** With no ``recognition_model`` injected (torch-less
  test env) the caller is expected not to route here, but a defensive guard
  still returns an empty error result rather than crashing.
"""

from __future__ import annotations

import html as _html
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Surya recognition task: OCR with internal box detection (matches marker's
# OcrBuilder default ``TaskNames.ocr_with_boxes``).
_OCR_TASK = "ocr_with_boxes"


class OcrResult:
    """Result of a local OCR pass over one image.

    Attributes:
        text: The joined transcription (one line per detected text line).
        line_count: Number of text lines recovered.
        html: ``text`` rendered as HTML paragraphs for marker's renderers.
        error: Non-empty when OCR failed; ``text``/``html`` are then empty.
        duration_ms: Wall-clock time spent in the recognition call.
    """

    __slots__ = ("text", "line_count", "html", "error", "duration_ms")

    def __init__(
        self,
        text: str = "",
        line_count: int = 0,
        html: str = "",
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        self.text = text
        self.line_count = line_count
        self.html = html
        self.error = error
        self.duration_ms = duration_ms


class LocalOcrService:
    """Run deterministic local OCR via Surya recognition + detection.

    Args:
        recognition_model: A Surya ``RecognitionPredictor``.
        detection_model: A Surya ``DetectionPredictor`` passed as the recognition
            predictor's ``det_predictor`` so line boxes are found internally.
        math_mode: When True (default), recognition keeps inline math markup,
            matching marker's default OCR behaviour.
    """

    def __init__(
        self,
        recognition_model: Any | None = None,
        detection_model: Any | None = None,
        math_mode: bool = True,
    ) -> None:
        self._recognition_model = recognition_model
        self._detection_model = detection_model
        self._math_mode = math_mode

    @property
    def available(self) -> bool:
        """True when a recognition model is present to run OCR."""
        return self._recognition_model is not None

    def ocr_image(self, image: Any) -> OcrResult:
        """Transcribe one PIL image to text + HTML. Never raises."""
        if self._recognition_model is None:
            return OcrResult(error="no recognition model")
        if image is None:
            return OcrResult(error="no image")

        t0 = time.perf_counter()
        try:
            results = self._recognition_model(
                images=[image],
                task_names=[_OCR_TASK],
                det_predictor=self._detection_model,
                math_mode=self._math_mode,
                sort_lines=True,
            )
        except Exception as exc:  # noqa: BLE001 — OCR must never abort a job
            logger.warning("LocalOcrService: recognition failed (%r)", exc)
            return OcrResult(
                error=str(exc),
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        lines = self._extract_lines(results)
        non_blank = [ln for ln in lines if ln.strip()]
        text = "\n".join(non_blank).strip()
        if not text:
            return OcrResult(
                line_count=0,
                error="no text recovered",
                duration_ms=duration_ms,
            )
        return OcrResult(
            text=text,
            line_count=len(non_blank),
            html=_lines_to_html(lines),
            duration_ms=duration_ms,
        )

    @staticmethod
    def _extract_lines(results: Any) -> list[str]:
        """Pull stripped text lines from a recognition result list.

        Blank lines are preserved (as empty strings) so :func:`_lines_to_html`
        can use them as paragraph separators; the caller filters them out when
        counting real lines and building the plain-text join.
        """
        if not results:
            return []
        first = results[0]
        text_lines = getattr(first, "text_lines", None) or []
        lines: list[str] = []
        for tl in text_lines:
            raw = getattr(tl, "text", None)
            if raw is None and isinstance(tl, dict):
                raw = tl.get("text")
            lines.append(str(raw or "").strip())
        return lines


def _lines_to_html(lines: list[str]) -> str:
    """Render transcribed lines as HTML.

    Blank lines split paragraphs; consecutive non-blank lines join with a
    ``<br/>`` so the original line breaks survive into the Markdown/HTML output
    without markdownify escaping the text content.
    """
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(_html.escape(line))
        elif paragraphs[-1]:
            paragraphs.append([])
    rendered = [
        "<p>" + "<br/>".join(p) + "</p>" for p in paragraphs if p
    ]
    return "\n".join(rendered)
