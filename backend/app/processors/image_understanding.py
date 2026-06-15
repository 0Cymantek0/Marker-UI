"""Image-understanding processor for marker-pdf Picture blocks."""

from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any

from marker.processors import BaseProcessor
from marker.schema import BlockTypes

from app.models.image_understanding import ImageHandlingMode, ImageType
from app.services.vlm_service import VLMService

logger = logging.getLogger(__name__)


try:
    from marker.renderers.markdown import Markdownify

    def convert_marker_comment(self, el, text, parent_tags):
        content = el.get_text() or ""
        return f"\n<!-- {content} -->\n"

    Markdownify.convert_marker_comment = convert_marker_comment
except ImportError:
    pass


class ImageUnderstandingProcessor(BaseProcessor):
    """Mutate Picture blocks in-place with VLM-derived text.

    The default mode is ``extraction`` so existing marker image extraction is
    unchanged unless a caller explicitly chooses ``understanding`` or ``both``.
    """

    block_types = (BlockTypes.Picture, BlockTypes.PictureGroup)

    def __init__(
        self,
        config: Any | None = None,
        vlm_service: Any | None = None,
    ) -> None:
        super().__init__(config)
        cfg = config if isinstance(config, dict) else {}
        self.image_handling_mode = ImageHandlingMode(
            cfg.get("image_handling_mode", ImageHandlingMode.extraction)
        )
        self.vlm_model = cfg.get("vlm_model")
        self.max_images_per_doc = int(cfg.get("max_images_per_doc", 50))
        self.context_window_size = int(cfg.get("context_window_size", 2))
        self.include_original_ref = bool(cfg.get("include_original_ref", True))
        self._vlm_service = vlm_service
        # Sidecar metadata collected during __call__ for the badge UI.
        # marker's MarkdownRenderer strips HTML comments, so a <!-- ... -->
        # channel does not survive to output. Instead we collect per-image
        # metadata here and marker_service reads it after the converter runs.
        self._image_meta: list[dict[str, Any]] = []

    @property
    def image_meta(self) -> list[dict[str, Any]]:
        """Per-image classification metadata for the badge UI (sidecar channel)."""
        return self._image_meta

    def __call__(self, document: Any, *args: Any, **kwargs: Any) -> None:
        if self.image_handling_mode == ImageHandlingMode.extraction:
            return

        pictures = list(document.contained_blocks([BlockTypes.Picture]))
        processed = 0
        for picture in pictures:
            if processed >= self.max_images_per_doc:
                break

            image_bytes = _picture_to_png_bytes(picture, document)
            if image_bytes is None:
                continue

            heading_chain, surrounding = gather_local_context(
                document,
                picture,
                n=self.context_window_size,
            )

            t0 = time.perf_counter()
            try:
                service = self._get_vlm_service()
                classification = service.classify(
                    image_bytes,
                    "image/png",
                    heading_chain,
                    surrounding,
                )
                extraction = service.extract(
                    image_bytes,
                    "image/png",
                    classification.image_type,
                    heading_chain,
                    surrounding,
                )
            except Exception as exc:  # noqa: BLE001 - fail-soft processor
                logger.warning("Image understanding skipped for picture: %r", exc)
                continue
            duration_ms = int((time.perf_counter() - t0) * 1000)

            if extraction.error:
                logger.warning(
                    "Image understanding extraction failed for %s: %s",
                    classification.image_type,
                    extraction.error,
                )
                continue

            model_id = self._resolved_model_id()
            image_name = _picture_image_name(picture)

            _mutate_picture(
                picture,
                image_type=classification.image_type,
                payload=extraction.payload,
                mode=self.image_handling_mode,
                confidence=classification.confidence,
                model=model_id,
                duration_ms=duration_ms,
                image_name=image_name,
            )
            self._image_meta.append(
                {
                    "image_name": image_name,
                    "image_type": classification.image_type.value,
                    "confidence": float(classification.confidence),
                    "model": model_id,
                    "omitted": classification.image_type == ImageType.decorative,
                }
            )
            processed += 1

    def _get_vlm_service(self) -> VLMService:
        if self._vlm_service is None:
            self._vlm_service = VLMService(model_id=self.vlm_model)
        return self._vlm_service

    def _resolved_model_id(self) -> str | None:
        """Best-effort model id used for extraction, for the badge modal."""
        try:
            service = self._get_vlm_service()
            return getattr(service, "model_id", None) or self.vlm_model
        except Exception:  # noqa: BLE001 - metadata is best effort
            return self.vlm_model


def gather_local_context(document: Any, picture_block: Any, n: int = 2) -> tuple[str, str]:
    """Return heading chain and +/-N text-block context around a Picture."""
    headings: list[str] = []
    before: list[str] = []
    after: list[str] = []

    prev = document.get_prev_block(picture_block)
    while prev is not None:
        if getattr(prev, "block_type", None) == BlockTypes.SectionHeader:
            text = _block_text(prev, document)
            if text:
                headings.append(text)
            if getattr(prev, "heading_level", 0) <= 1:
                break
        prev = document.get_prev_block(prev)
    headings.reverse()

    prev = document.get_prev_block(picture_block)
    while prev is not None and len(before) < n:
        if getattr(prev, "block_type", None) == BlockTypes.Text:
            text = _block_text(prev, document)
            if text:
                before.append(text)
        prev = document.get_prev_block(prev)
    before.reverse()

    nxt = document.get_next_block(picture_block)
    while nxt is not None and len(after) < n:
        if getattr(nxt, "block_type", None) == BlockTypes.Text:
            text = _block_text(nxt, document)
            if text:
                after.append(text)
        nxt = document.get_next_block(nxt)

    return "\n".join(headings), "\n".join([*before, *after])


def _picture_to_png_bytes(picture: Any, document: Any) -> bytes | None:
    image = picture.get_image(document)
    if image is None:
        return None
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _mutate_picture(
    picture: Any,
    *,
    image_type: ImageType,
    payload: dict[str, Any],
    mode: ImageHandlingMode,
    confidence: float,
    model: str | None,
    duration_ms: int,
    image_name: str,
) -> None:
    rendered = render_extraction(image_type, payload)
    if not rendered:
        return

    model_str = model or "unknown"
    meta_str = f"marker-ui image-understanding: type={image_type.value} model={model_str} confidence={confidence:.2f} cost_usd=0 duration_ms={duration_ms}"

    html_parts = [f"<marker-comment>{meta_str}</marker-comment>"]

    if mode == ImageHandlingMode.both and image_type != ImageType.decorative:
        html_parts.append(f"<marker-comment>original_image: {image_name}</marker-comment>")

    html_parts.append(f"\n\n{rendered}\n")

    if mode == ImageHandlingMode.both and image_type != ImageType.decorative:
        html_parts.append(f'<img src="{image_name}" />')

    picture.html = "\n".join(html_parts)
    picture.description = None


def render_extraction(image_type: ImageType, payload: dict[str, Any]) -> str:
    if image_type in _CHART_TYPES:
        return _render_chart(payload)
    if image_type == ImageType.table_image:
        return _render_table(payload)
    if image_type in _DIAGRAM_TYPES:
        mermaid = str(payload.get("mermaid", "")).strip()
        caption = str(payload.get("caption", "")).strip()
        return f"{caption}\n\n```mermaid\n{mermaid}\n```".strip()
    if image_type == ImageType.equation:
        latex = str(payload.get("latex", "")).strip()
        caption = str(payload.get("caption", "")).strip()
        return f"{caption}\n\n$$\n{latex}\n$$".strip()
    if image_type == ImageType.screenshot_ui:
        return _render_screenshot(payload)
    if image_type == ImageType.decorative:
        return "_Decorative element omitted._"
    return _render_description(payload)


def _render_chart(payload: dict[str, Any]) -> str:
    series = payload.get("series") or []
    if not series:
        return _render_description(payload)

    columns = ["x", *[str(s.get("name") or f"series_{i + 1}") for i, s in enumerate(series)]]
    x_values: list[Any] = []
    rows_by_x: dict[Any, dict[str, Any]] = {}
    for col, s in zip(columns[1:], series):
        for point in s.get("points") or []:
            x = point.get("x", "")
            if x not in rows_by_x:
                rows_by_x[x] = {}
                x_values.append(x)
            rows_by_x[x][col] = point.get("y", "")

    lines = [_table_row(columns), _table_row(["---"] * len(columns))]
    for x in x_values:
        lines.append(_table_row([x, *[rows_by_x[x].get(col, "") for col in columns[1:]]]))
    notes = str(payload.get("notes", "")).strip()
    title = str(payload.get("title", "")).strip()
    parts = [p for p in [f"**{title}**" if title else "", "\n".join(lines), notes] if p]
    return "\n\n".join(parts)


def _render_table(payload: dict[str, Any]) -> str:
    headers = [str(h) for h in payload.get("headers") or []]
    rows = payload.get("rows") or []
    if not headers and rows:
        headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
    if not headers:
        return _render_description(payload)

    lines = [_table_row(headers), _table_row(["---"] * len(headers))]
    for row in rows:
        padded = list(row)[: len(headers)] + [""] * max(0, len(headers) - len(row))
        lines.append(_table_row(padded))
    caption = str(payload.get("caption", "")).strip()
    return "\n\n".join([p for p in [caption, "\n".join(lines)] if p])


def _render_screenshot(payload: dict[str, Any]) -> str:
    title = "# Screenshot"
    app = str(payload.get("application", "")).strip()
    area = str(payload.get("area", "")).strip()
    if app or area:
        title = f"# Screenshot of {app or 'application'}: {area or 'screen'}"
    lines = [title]
    summary = str(payload.get("summary", "")).strip()
    if summary:
        lines.extend(["", summary])
    for region in payload.get("regions") or []:
        name = str(region.get("name", "Region")).strip() or "Region"
        desc = str(region.get("description", "")).strip()
        ocr = str(region.get("ocr_text", "")).strip()
        lines.append(f"- {name}: {desc}" + (f" Text: {ocr}" if ocr else ""))
    return "\n".join(lines)


def _render_description(payload: dict[str, Any]) -> str:
    alt = str(payload.get("alt_text") or payload.get("summary") or "").strip()
    details = [str(d).strip() for d in payload.get("details") or [] if str(d).strip()]
    return "\n".join([alt, *[f"- {d}" for d in details]]).strip()


def _block_text(block: Any, document: Any) -> str:
    raw_text = getattr(block, "raw_text", None)
    if callable(raw_text):
        try:
            return str(raw_text(document)).strip()
        except Exception:  # noqa: BLE001 - context is best effort
            pass
    return str(getattr(block, "html", "") or getattr(block, "text", "") or "").strip()


def _table_row(values: list[Any]) -> str:
    return "| " + " | ".join(_escape_cell(v) for v in values) + " |"


def _escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|")


def _picture_image_name(picture: Any) -> str:
    """Predict the image filename marker's renderer will emit for this picture.

    MarkdownRenderer.extract_html builds image names as
    ``f"{block_id.to_path()}.{OUTPUT_IMAGE_FORMAT.lower()}"``. We mirror that
    here so the badge UI can pair sidecar metadata to rendered ``![](name)``
    tokens. Falls back to a stable string when the block id shape is unexpected.
    """
    from marker.settings import settings

    ext = str(settings.OUTPUT_IMAGE_FORMAT).lower()
    block_id = getattr(picture, "id", None)
    to_path = getattr(block_id, "to_path", None)
    if callable(to_path):
        return f"{to_path()}.{ext}"
    return f"{_picture_ref(picture)}.{ext}"


def _picture_ref(picture: Any) -> str:
    return str(getattr(picture, "id", None) or getattr(picture, "block_id", "unknown"))


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

_DESCRIPTION_TYPES = {
    ImageType.screenshot_ui,
    ImageType.photo,
    ImageType.figure_technical,
    ImageType.other,
}
