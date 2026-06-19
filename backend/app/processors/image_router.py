"""Tier-0 local pre-pass router for the image-understanding pipeline.

The router is the cheapest stage of the graded stack (plan §1/§2): it decides
*where a candidate image should go next* — ``skip_decorative`` (omit, zero cost),
``ocr`` (deterministic local Surya OCR, zero cloud tokens, the ``openskill.md``
line-227 fix), or ``vlm`` (genuine visual understanding — chart/diagram/photo).

It has two routing brains, selected by ``SmartRouterLevel``:

* **disabled** — density-only: run Surya *detection* and route on text-box area
  fraction + line count. Cheapest, but a text-heavy chart looks "mostly text"
  and a textless infographic looks "decorative".
* **smart** (default) — re-run Surya *layout* on the crop and route on its label
  (``Table`` / ``Equation`` / ``Text`` / ``Code`` / ``Form`` / ``Picture`` …),
  a far richer signal than density. One extra local forward pass, no API cost.
* **beeg_brain** — layout **and** density fused: when the label and density
  disagree, escalate to the VLM instead of guessing, and only drop a decorative
  when both signals agree. Highest accuracy, most local GPU.

Design notes:

* **Never raises.** Detection/layout failures degrade to the density brain and
  then to the ``vlm`` route (the information-preserving default per the §7
  ``low_confidence_route`` decision), so one bad image never aborts a conversion.
* **Degrades without models.** With no ``detection_model`` the router is not
  constructed upstream; with no ``layout_model`` every level falls back to the
  density brain.
* **Signals are logged, not hidden.** Each :class:`RouteDecision` carries the
  ``layout_label`` / ``text_density`` / ``line_count`` that justified it so
  thresholds can be tuned against real output via the logged ``reason`` (§7).
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.image_understanding import (
    RouteDecision,
    RouteKind,
    SmartRouterLevel,
)

logger = logging.getLogger(__name__)


# Surya layout labels (see surya/layout/label.py) grouped by routing intent.
# Text-like regions transcribe deterministically with zero hallucination; a
# table or equation needs the VLM's structure/LaTeX extraction; a picture/figure
# is a genuine graphic that the VLM classifies into the 17-type taxonomy.
_TEXT_LABELS = frozenset(
    {
        "Text",
        "ListItem",
        "Caption",
        "Code",
        "Footnote",
        "PageHeader",
        "PageFooter",
        "SectionHeader",
        "TableOfContents",
        "Form",
    }
)
_VLM_LABELS = frozenset({"Table", "Equation"})
_GRAPHIC_LABELS = frozenset({"Picture", "Figure"})


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
    """Route a candidate image using the Surya detection/layout pre-pass.

    Args:
        detection_model: A Surya ``DetectionPredictor`` (callable taking
            ``List[Image]`` and returning ``List[TextDetectionResult]``). When
            ``None`` the router always returns the ``vlm`` route.
        layout_model: A Surya ``LayoutPredictor`` (callable taking
            ``List[Image]`` and returning ``List[LayoutResult]``). When ``None``
            (or the level is ``disabled``) the router uses the density brain.
        config: A mapping carrying the router thresholds (see
            ``ImageUnderstandingConfig`` for the field semantics).
    """

    def __init__(
        self,
        detection_model: Any | None = None,
        layout_model: Any | None = None,
        config: Any | None = None,
    ) -> None:
        cfg = config if isinstance(config, dict) else {}
        self._detection_model = detection_model
        self._layout_model = layout_model
        self.level = self._coerce_level(cfg.get("smart_router_level"))
        self.decorative_max_text_density = float(
            cfg.get("decorative_max_text_density", 0.02)
        )
        self.ocr_min_text_density = float(cfg.get("ocr_min_text_density", 0.45))
        self.ocr_min_lines = int(cfg.get("ocr_min_lines", 3))
        self.allow_cloud_vlm = bool(cfg.get("allow_cloud_vlm", False))

    @staticmethod
    def _coerce_level(value: Any) -> SmartRouterLevel:
        """Accept an enum, its string value, or None -> default ``smart``."""
        if isinstance(value, SmartRouterLevel):
            return value
        try:
            return SmartRouterLevel(value)
        except ValueError:
            return SmartRouterLevel.smart

    def route(self, image: Any) -> RouteDecision:
        """Classify one PIL image into a :class:`RouteDecision`. Never raises."""
        density, lines = self._detect_text_signals(image)

        # No detection model (test env / torch-less): preserve information by
        # sending everything to the VLM, unless cloud is disabled.
        if self._detection_model is None:
            return self._vlm_or_local_fallback(
                "no detection model; degrade to vlm", density, lines
            )

        # Layout brain (smart / beeg_brain): route on the per-crop layout label
        # when a layout model is present and produced one.
        if self.level is not SmartRouterLevel.disabled and self._layout_model is not None:
            label = self._classify_layout(image)
            if label:
                return self._route_by_layout(label, density, lines)

        return self._route_by_density(density, lines)

    # -- density brain (the original heuristic, also the fallback) -----------

    def _route_by_density(self, density: float, lines: int) -> RouteDecision:
        """Route purely on text-box density + line count."""
        if density <= self.decorative_max_text_density and lines == 0:
            return RouteDecision(
                route=RouteKind.skip_decorative,
                reason=f"density={density:.3f}<={self.decorative_max_text_density} lines=0",
                text_density=density,
                line_count=lines,
            )
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
        return self._vlm_or_local_fallback(
            f"density={density:.3f} lines={lines} -> visual", density, lines
        )

    # -- layout brain (smart / beeg_brain) ----------------------------------

    def _route_by_layout(
        self, label: str, density: float, lines: int
    ) -> RouteDecision:
        """Map a Surya layout label to a route (plan: label->route table).

        ``beeg_brain`` adds conservative escalation: a text label with sparse
        text (low density) disagrees with the "dense text-as-image" assumption,
        so it escalates to the VLM rather than risk feeding a mislabelled chart
        to OCR; and a decorative drop requires both label *and* density to agree.
        """
        fuse = self.level is SmartRouterLevel.beeg_brain

        if label in _VLM_LABELS:
            return self._vlm_or_local_fallback(
                f"layout={label} -> vlm", density, lines, layout_label=label
            )

        if label in _TEXT_LABELS:
            # beeg_brain: label says text but density says sparse -> disagreement
            # -> escalate to VLM (don't OCR a likely-mislabelled graphic).
            if fuse and density < self.ocr_min_text_density:
                return self._vlm_or_local_fallback(
                    f"layout={label} but density={density:.3f}"
                    f"<{self.ocr_min_text_density}; disagree -> vlm",
                    density,
                    lines,
                    layout_label=label,
                )
            return RouteDecision(
                route=RouteKind.ocr,
                reason=f"layout={label} -> ocr (density={density:.3f} lines={lines})",
                layout_label=label,
                text_density=density,
                line_count=lines,
            )

        # Picture / Figure / unknown label -> genuine graphic. Drop only when it
        # really is decorative: (almost) no text AND no lines. beeg_brain keeps
        # the same agreement requirement (label is graphic + density says empty).
        if density <= self.decorative_max_text_density and lines == 0:
            return RouteDecision(
                route=RouteKind.skip_decorative,
                reason=(
                    f"layout={label} density={density:.3f}"
                    f"<={self.decorative_max_text_density} lines=0 -> decorative"
                ),
                layout_label=label,
                text_density=density,
                line_count=lines,
            )
        return self._vlm_or_local_fallback(
            f"layout={label} -> vlm", density, lines, layout_label=label
        )

    def _vlm_or_local_fallback(
        self,
        reason: str,
        density: float,
        lines: int,
        layout_label: str = "",
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
                layout_label=layout_label,
                text_density=density,
                line_count=lines,
            )
        return RouteDecision(
            route=RouteKind.ocr,
            reason=f"{reason}; cloud disabled -> local ocr",
            layout_label=layout_label,
            text_density=density,
            line_count=lines,
        )

    # -- signal extraction ---------------------------------------------------

    def _classify_layout(self, image: Any) -> str:
        """Return the dominant Surya layout label for the crop, or "" on failure.

        "Dominant" = the label of the largest-area layout box, so a small caption
        inside a figure does not flip a whole chart to the ``ocr`` route.
        """
        if self._layout_model is None or image is None:
            return ""
        try:
            results = self._layout_model([image])
        except Exception as exc:  # noqa: BLE001 — layout must never abort a job
            logger.warning("ImageRouter: layout failed (%r); degrading", exc)
            return ""
        if not results:
            return ""
        bboxes = getattr(results[0], "bboxes", None) or []
        best_label = ""
        best_area = -1.0
        for box in bboxes:
            label = getattr(box, "label", None)
            if not label:
                continue
            polygon = getattr(box, "polygon", None)
            area = _polygon_area(polygon) if polygon else 0.0
            if area > best_area:
                best_area = area
                best_label = str(label)
        return best_label

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
