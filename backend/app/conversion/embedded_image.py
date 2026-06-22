"""EmbeddedImageService — shared image analysis and routing policy.

This service processes embedded graphics for non-PDF converters (docx, pptx, etc.)
reusing the same routing, VLM, and local OCR primitives as the PDF processor.
"""

from __future__ import annotations

import io
import logging
from typing import Any
from PIL import Image
import markdownify

from app.models.image_understanding import RouteKind, ImageType
from app.processors.image_router import ImageRouter
from app.services.vlm_service import VLMService

logger = logging.getLogger(__name__)

# Replicate the types from image_understanding
_CHART_TYPES = {
    ImageType.chart_bar,
    ImageType.chart_line,
    ImageType.chart_pie,
    ImageType.chart_scatter,
    ImageType.chart_other,
}

_DIAGRAM_TYPES = {
    ImageType.diagram_flow,
    ImageType.diagram_sequence,
    ImageType.diagram_state,
    ImageType.diagram_class,
    ImageType.diagram_architecture,
}


class EmbeddedImageService:
    """Processes embedded images using the local router/OCR/VLM pipeline."""

    def __init__(self, marker_service: Any = None) -> None:
        self._marker_service = marker_service
        self._dedup_cache: list[tuple[str, dict[str, Any]]] = []

    def clear_cache(self) -> None:
        """Clear the duplicate detection cache."""
        self._dedup_cache.clear()

    @property
    def detection_model(self) -> Any | None:
        """Dynamically fetch detection model from MarkerService if initialized."""
        if self._marker_service and getattr(self._marker_service, "_initialized", False):
            return (getattr(self._marker_service, "_model_dict", None) or {}).get("detection")
        return None

    @property
    def layout_model(self) -> Any | None:
        """Dynamically fetch layout model from MarkerService if initialized."""
        if self._marker_service and getattr(self._marker_service, "_initialized", False):
            return (getattr(self._marker_service, "_model_dict", None) or {}).get("layout")
        return None

    @property
    def recognition_model(self) -> Any | None:
        """Dynamically fetch recognition model from MarkerService if initialized."""
        if self._marker_service and getattr(self._marker_service, "_initialized", False):
            return (getattr(self._marker_service, "_model_dict", None) or {}).get("recognition")
        return None

    def process_image(
        self,
        image_bytes_or_pil: bytes | Any,
        context_text: str,
        options: dict[str, Any],
        image_name: str,
    ) -> dict[str, Any]:
        """Route, dedup, and extract/transcribe one embedded image."""
        # 1. Extraction mode check
        image_handling_mode = options.get("image_handling_mode", "extraction")
        if image_handling_mode == "extraction":
            return {
                "markdown": f"![{image_name}]({image_name})",
                "route": "extraction",
                "confidence": 1.0,
                "cost_usd": 0.0,
                "image_name": image_name,
                "image_type": "other",
                "omitted": False,
            }

        # 2. Load image
        image = None
        if isinstance(image_bytes_or_pil, bytes):
            try:
                image = Image.open(io.BytesIO(image_bytes_or_pil))
                image.load()
            except Exception as exc:
                logger.warning("Failed to open image bytes: %r", exc)
        else:
            image = image_bytes_or_pil

        if image is None:
            return {
                "markdown": f"![{image_name}]({image_name})",
                "route": "error",
                "confidence": 0.0,
                "cost_usd": 0.0,
                "image_name": image_name,
                "image_type": "other",
                "omitted": False,
            }

        # 3. Hashing and Dedup lookup
        dedup_enabled = bool(options.get("dedup_enabled", True))
        image_hash = None
        if dedup_enabled:
            try:
                from app.utils.image_hash import average_hash
                image_hash = average_hash(image)
            except Exception as exc:
                logger.warning("Average hash failed: %r", exc)

        if image_hash is not None:
            from app.utils.image_hash import hamming_distance
            dedup_max_distance = int(options.get("dedup_max_distance", 0))
            for cached_hash, outcome in self._dedup_cache:
                if hamming_distance(image_hash, cached_hash) <= dedup_max_distance:
                    outcome_copy = dict(outcome)
                    outcome_copy["cost_usd"] = 0.0
                    return self._format_markdown_result(
                        outcome_copy, image_name, image_handling_mode, options
                    )

        # 4. Route decision
        router_enabled = bool(options.get("router_enabled", True))
        if not router_enabled:
            allow_cloud_vlm = bool(options.get("allow_cloud_vlm", False))
            route_kind = RouteKind.vlm if allow_cloud_vlm else RouteKind.ocr
        else:
            router = ImageRouter(
                detection_model=self.detection_model,
                layout_model=self.layout_model,
                config=options,
            )
            decision = router.route(image)
            route_kind = decision.route

        # 5. Process route
        ocr_success = False
        ocr_html = ""

        if route_kind == RouteKind.skip_decorative:
            outcome = {
                "route": "skip_decorative",
                "image_type": "decorative",
                "payload": {},
                "confidence": 1.0,
                "cost_usd": 0.0,
                "omitted": True,
            }
            if image_hash is not None:
                self._dedup_cache.append((image_hash, outcome))
            return self._format_markdown_result(
                outcome, image_name, image_handling_mode, options
            )

        if route_kind == RouteKind.ocr:
            rec_model = self.recognition_model
            det_model = self.detection_model
            if rec_model is not None:
                from app.services.ocr_engine import build_ocr_engine
                ocr_engine_name = str(options.get("ocr_engine", "surya"))
                try:
                    ocr_engine = build_ocr_engine(
                        ocr_engine_name,
                        recognition_model=rec_model,
                        detection_model=det_model,
                    )
                    if ocr_engine and ocr_engine.available:
                        result = ocr_engine.recognize(image)
                        if not result.error and result.html:
                            ocr_html = result.html
                            ocr_success = True
                except Exception as exc:
                    logger.warning("Local OCR failed: %r", exc)

            if ocr_success:
                outcome = {
                    "route": "ocr",
                    "image_type": "other",
                    "payload": {},
                    "ocr_html": ocr_html,
                    "confidence": 1.0,
                    "cost_usd": 0.0,
                    "omitted": False,
                }
                if image_hash is not None:
                    self._dedup_cache.append((image_hash, outcome))
                return self._format_markdown_result(
                    outcome, image_name, image_handling_mode, options
                )
            else:
                logger.debug("OCR empty or unavailable; escalating to VLM")

        # 6. VLM processing
        allow_cloud_vlm = bool(options.get("allow_cloud_vlm", False))
        if not allow_cloud_vlm:
            outcome = {
                "route": "error",
                "image_type": "other",
                "payload": {},
                "confidence": 0.0,
                "cost_usd": 0.0,
                "omitted": False,
            }
            return self._format_markdown_result(
                outcome, image_name, image_handling_mode, options
            )

        try:
            if options.get("downscale_vlm_crops", True):
                from app.utils.image_downscale import downscale_to_max
                image_to_send = downscale_to_max(image, options.get("vlm_crop_max_px", 768))
            else:
                image_to_send = image

            buf = io.BytesIO()
            image_to_send.save(buf, format="PNG")
            image_png_bytes = buf.getvalue()

            vlm_model = options.get("vlm_model")
            vlm_service = VLMService(model_id=vlm_model)

            # Classify
            classification = vlm_service.classify(
                image_png_bytes,
                "image/png",
                "",  # heading_chain
                context_text,
            )

            # Extract
            extraction = vlm_service.extract(
                image_png_bytes,
                "image/png",
                classification.image_type,
                "",  # heading_chain
                context_text,
            )

            if extraction.route == "decorative":
                outcome = {
                    "route": "skip_decorative",
                    "image_type": "decorative",
                    "payload": {},
                    "confidence": float(classification.confidence),
                    "cost_usd": float(extraction.cost_usd or 0.0),
                    "omitted": True,
                    "vlm_decided": True,
                }
            elif extraction.route == "ocr_sufficient":
                ocr_success = False
                ocr_html = ""
                rec_model = self.recognition_model
                if rec_model is not None:
                    from app.services.ocr_engine import build_ocr_engine
                    try:
                        ocr_engine = build_ocr_engine(
                            str(options.get("ocr_engine", "surya")),
                            recognition_model=rec_model,
                            detection_model=self.detection_model,
                        )
                        if ocr_engine and ocr_engine.available:
                            result = ocr_engine.recognize(image)
                            if not result.error and result.html:
                                ocr_html = result.html
                                ocr_success = True
                    except Exception as exc:
                        logger.warning("Local OCR failed: %r", exc)

                if ocr_success:
                    outcome = {
                        "route": "ocr",
                        "image_type": "other",
                        "payload": {},
                        "ocr_html": ocr_html,
                        "confidence": float(classification.confidence),
                        "cost_usd": float(extraction.cost_usd or 0.0),
                        "omitted": False,
                    }
                else:
                    outcome = {
                        "route": "error",
                        "image_type": "other",
                        "payload": {},
                        "confidence": 0.0,
                        "cost_usd": float(extraction.cost_usd or 0.0),
                        "omitted": False,
                    }
            else:
                outcome = {
                    "route": "vlm",
                    "image_type": classification.image_type.value,
                    "payload": extraction.payload,
                    "confidence": float(classification.confidence),
                    "cost_usd": float(extraction.cost_usd or 0.0),
                    "omitted": False,
                }

        except Exception as exc:
            logger.warning("VLM call failed: %r", exc)
            outcome = {
                "route": "error",
                "image_type": "other",
                "payload": {},
                "confidence": 0.0,
                "cost_usd": 0.0,
                "omitted": False,
            }

        if image_hash is not None:
            self._dedup_cache.append((image_hash, outcome))
        return self._format_markdown_result(
            outcome, image_name, image_handling_mode, options
        )

    def _format_markdown_result(
        self,
        outcome: dict[str, Any],
        image_name: str,
        image_handling_mode: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert outcome to markdown and metadata structures."""
        route = outcome["route"]
        image_type_str = outcome["image_type"]
        confidence = outcome["confidence"]
        cost_usd = outcome["cost_usd"]
        ocr_html = outcome.get("ocr_html", "")
        payload = outcome.get("payload", {})
        omitted = outcome.get("omitted", False)

        model_name = options.get("vlm_model") or "unknown"
        if route == "ocr" or ocr_html:
            model_name = "local-ocr"
        elif route == "skip_decorative" and not outcome.get("vlm_decided", False):
            model_name = "router-local"

        meta_str = (
            f"marker-ui image-understanding: "
            f"type={image_type_str} model={model_name} "
            f"confidence={confidence:.2f} "
            f"cost_usd={cost_usd:.6f} duration_ms=0"
        )
        if route == "ocr":
            meta_str += " route=ocr"

        html_parts = [
            f"<!-- {meta_str} -->"
        ]

        markdown = ""
        if omitted:
            markdown = ""
        elif route == "ocr" and ocr_html:
            ocr_md = markdownify.markdownify(ocr_html).strip()
            markdown = "\n".join(html_parts + [ocr_md])
        elif route == "vlm" and payload:
            from app.conversion.image_rendering import render_extraction  # marker-free, avoids importing marker for office docs
            image_type = ImageType(image_type_str)
            rendered_html = render_extraction(image_type, payload)
            vlm_md = markdownify.markdownify(rendered_html).strip()

            keep_original = (
                image_handling_mode == "both"
                or image_type not in (ImageType.equation, ImageType.decorative)
            )

            parts = list(html_parts)
            if keep_original:
                parts.append(f"<!-- original_image: {image_name} -->")
                parts.append(f"![{image_name}]({image_name})")
            parts.append(vlm_md)
            markdown = "\n".join(parts)
        else:
            parts = list(html_parts)
            parts.append(f"![{image_name}]({image_name})")
            markdown = "\n".join(parts)

        return {
            "markdown": markdown,
            "route": route,
            "confidence": confidence,
            "cost_usd": cost_usd,
            "image_name": image_name,
            "image_type": image_type_str,
            "omitted": omitted,
        }
