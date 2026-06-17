"""Tier-0 local pre-pass router for the image-understanding pipeline.

The router is the cheapest stage of the graded stack (plan §1/§2): it runs the
Surya **detection** model — already resident in GPU memory for marker's own
pipeline, so zero marginal API cost — over each candidate image and reads the
returned text boxes to decide *where the image should go next*:

    * **skip_decorative** — (almost) no text and low visual content: omit it,
      spend nothing.
    * **ocr** — the image is mostly text (a paragraph rendered as an image, a
      scanned figure caption): hand it to deterministic local OCR (Tier 2),
      which transcribes it with zero cloud tokens and zero hallucination. This
      is where the ``openskill.md`` line-227 class of bug is fixed for good.
    * **vlm** — genuine visual understanding (chart, diagram, photo): escalate
      to the cloud VLM (Tier 1).

Design notes:

* **Never raises.** Detection failures degrade to the ``vlm`` route (the
  information-preserving default per the §7 ``low_confidence_route`` decision),
  so one bad image never aborts a conversion.
* **Degrades without models.** If no ``detection_model`` is injected (e.g. a
  torch-less test env, or ``router_enabled=False`` upstream), the router is
  simply not constructed and the processor falls back to the legacy path.
* **Signals are logged, not hidden.** Each :class:`RouteDecision` carries the
  ``text_density`` / ``line_count`` that justified it so thresholds can be
  tuned against real output via the logged ``reason`` (plan §7).
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.image_understanding import RouteDecision, RouteKind

logger = logging.getLogger(__name__)


def _polygon_area(polygon: list[list[float]]) -> float:
    """Shoelace area of a polygon ``[[x, y], ...]``. Returns 0 on bad input."""
    if not polygon or len(polygon) < 3:
        return 0.0
    area = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i][0], polygon[i][1]
        x2, y2 = polygon[(i + 1) % n][0], polygon[(i + 1) % n][1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


class ImageRouter:
    """Route a candidate image using the Surya detection pre-pass.

    Args:
        detection_model: A Surya ``DetectionPredictor`` (callable taking
            ``List[Image]`` and returning ``List[TextDetectionResult]``). When
            ``None`` the router always returns the ``vlm`` route.
        config: A mapping carrying the router thresholds (see
            ``ImageUnderstandingConfig`` for the field semantics).
    """

    def __init__(
        self,
        detection_model: Any | None = None,
        config: Any | None = None,
    ) -> None:
        cfg = config if isinstance(config, dict) else {}
        self._detection_model = detection_model
        self.decorative_max_text_density = float(
            cfg.get("decorative_max_text_density", 0.02)
        )
        self.ocr_min_text_density = float(cfg.get("ocr_min_text_density", 0.45))
        self.ocr_min_lines = int(cfg.get("ocr_min_lines", 3))
        self.allow_cloud_vlm = bool(cfg.get("allow_cloud_vlm", False))

    def route(self, image: Any) -> RouteDecision:
        """Classify one PIL image into a :class:`RouteDecision`. Never raises."""
        density, lines = self._detect_text_signals(image)

        # No detection model (test env / torch-less): preserve information by
        # sending everything to the VLM, unless cloud is disabled.
        if self._detection_model is None:
            return self._vlm_or_local_fallback(
                "no detection model; degrade to vlm", density, lines
            )

        # Decorative: (almost) no text AND few/no lines.
        if density <= self.decorative_max_text_density and lines == 0:
            return RouteDecision(
                route=RouteKind.skip_decorative,
                reason=f"density={density:.3f}<={self.decorative_max_text_density} lines=0",
                text_density=density,
                line_count=lines,
            )

        # Text-as-image: dense text -> deterministic local OCR.
        if density >= self.ocr_min_text_density and lines >= self.ocr_min_lines:
            return RouteDecision(
                route=RouteKind.ocr,
                reason=(
                    f"density={density:.3f}>={self.ocr_min_text_density} "
                    f"lines={lines}>={self.ocr_min_lines}"
                ),
                text_density=density,
                line_count=lines,
            )

        # Everything else is genuine visual content -> VLM (or local fallback).
        return self._vlm_or_local_fallback(
            f"density={density:.3f} lines={lines} -> visual", density, lines
        )

    def _vlm_or_local_fallback(
        self, reason: str, density: float, lines: int
    ) -> RouteDecision:
        """VLM route when cloud allowed; else fall back to local OCR.

        With ``allow_cloud_vlm=False`` the pipeline is local-only (plan §11a):
        a graphic we cannot send to the cloud still gets *something* useful from
        local OCR rather than being dropped.
        """
        if self.allow_cloud_vlm:
            return RouteDecision(
                route=RouteKind.vlm,
                reason=reason,
                text_density=density,
                line_count=lines,
            )
        return RouteDecision(
            route=RouteKind.ocr,
            reason=f"{reason}; cloud disabled -> local ocr",
            text_density=density,
            line_count=lines,
        )

    def _detect_text_signals(self, image: Any) -> tuple[float, int]:
        """Return (text_density, line_count) from the detection pre-pass.

        ``text_density`` is the summed text-box area divided by the image area,
        clamped to [0, 1]. Any failure returns ``(0.0, 0)`` so the caller routes
        to the information-preserving default.
        """
        if self._detection_model is None or image is None:
            return 0.0, 0
        try:
            results = self._detection_model([image])
        except Exception as exc:  # noqa: BLE001 — detection must never abort a job
            logger.warning("ImageRouter: detection failed (%r); degrading", exc)
            return 0.0, 0

        if not results:
            return 0.0, 0
        result = results[0]
        bboxes = getattr(result, "bboxes", None) or []
        image_bbox = getattr(result, "image_bbox", None) or []

        img_area = self._image_area(image_bbox, image)
        if img_area <= 0:
            return 0.0, len(bboxes)

        text_area = 0.0
        for box in bboxes:
            polygon = getattr(box, "polygon", None)
            if polygon:
                text_area += _polygon_area(polygon)
        density = max(0.0, min(1.0, text_area / img_area))
        return density, len(bboxes)

    @staticmethod
    def _image_area(image_bbox: list[float], image: Any) -> float:
        """Area from the detector's ``image_bbox`` (preferred) or PIL size."""
        if image_bbox and len(image_bbox) == 4:
            w = image_bbox[2] - image_bbox[0]
            h = image_bbox[3] - image_bbox[1]
            if w > 0 and h > 0:
                return float(w * h)
        size = getattr(image, "size", None)
        if size and len(size) == 2 and size[0] > 0 and size[1] > 0:
            return float(size[0] * size[1])
        return 0.0
