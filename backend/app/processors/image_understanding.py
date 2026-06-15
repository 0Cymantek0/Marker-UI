"""Image-understanding processor for marker-pdf Picture blocks.

The VLM extraction result is written into ``Picture.html`` as **HTML**, not
Markdown. marker renders every document by serialising the block tree to HTML
and then running it through ``markdownify`` (for the Markdown renderer) or
returning it verbatim (for the HTML / JSON renderers). markdownify *escapes*
Markdown metacharacters in raw text nodes (``$$`` -> ``\\$\\$``, ``a_1`` ->
``a\\_1``, ``**x**`` -> ``\\*\\*x\\*\\*``), so injecting raw Markdown here would
corrupt LaTeX, Mermaid, and bold in the final output. Emitting HTML lets
marker's own renderers convert it cleanly and uniformly — the same approach
marker uses for every other block type.
"""

from __future__ import annotations

import html as _html
import logging
import time
from io import BytesIO
from typing import Any

from marker.processors import BaseProcessor
from marker.schema import BlockTypes

from app.models.image_understanding import ImageHandlingMode, ImageType
from app.services.vlm_service import VLMService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# markdownify patches (applied once at import).
#
# marker constructs ``Markdownify`` without a ``code_language_callback`` and has
# no converter for our ``<marker-comment>`` sidecar tag, so we add both here.
# Patching the class (not an instance) is the only injection point, since the
# Markdown renderer builds a fresh ``Markdownify`` per call.
# ---------------------------------------------------------------------------

try:
    from marker.renderers.markdown import Markdownify

    def _convert_marker_comment(self, el, text, parent_tags):
        # Carry per-image metadata into the Markdown as an HTML comment so it
        # survives for downstream LLMs / grep without rendering visibly.
        content = el.get_text() or ""
        return f"\n<!-- {content} -->\n"

    Markdownify.convert_marker_comment = _convert_marker_comment

    _orig_convert_pre = Markdownify.convert_pre

    def _convert_pre(self, el, text, parent_tags):
        # Preserve the ```<lang> info string from <code class="language-xxx">.
        # Without this, our Mermaid fences collapse to a bare ``` fence and no
        # renderer (or react-markdown) can identify them as Mermaid.
        code_el = el.find("code") if hasattr(el, "find") else None
        lang = ""
        if code_el is not None and code_el.has_attr("class"):
            for cls in code_el["class"]:
                if cls.startswith("language-"):
                    lang = cls[len("language-"):]
                    break
        if not lang:
            return _orig_convert_pre(self, el, text, parent_tags)
        if not text:
            return ""
        return f"\n\n```{lang}\n{text}\n```\n\n"

    Markdownify.convert_pre = _convert_pre
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

            # Single source of truth for per-image metadata: both the markdown
            # comment channel (for downstream LLMs / grep) and the sidecar
            # channel (for the badge UI) derive from this one dict.
            meta = {
                "image_name": image_name,
                "image_type": classification.image_type.value,
                "confidence": float(classification.confidence),
                "model": model_id,
                "omitted": classification.image_type == ImageType.decorative,
                "duration_ms": duration_ms,
            }
            _mutate_picture(
                picture,
                image_type=classification.image_type,
                payload=extraction.payload,
                mode=self.image_handling_mode,
                meta=meta,
                include_original_ref=self.include_original_ref,
            )
            # Sidecar carries the badge-relevant subset (no duration_ms).
            self._image_meta.append(
                {
                    "image_name": meta["image_name"],
                    "image_type": meta["image_type"],
                    "confidence": meta["confidence"],
                    "model": meta["model"],
                    "omitted": meta["omitted"],
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
            if (getattr(prev, "heading_level", None) or 0) <= 1:
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
    meta: dict[str, Any],
    include_original_ref: bool = True,
) -> None:
    """Replace a Picture block's output with the VLM textual representation.

    Writes **HTML** into ``picture.html`` (see module docstring for why HTML and
    not Markdown). Emits two channels from the single ``meta`` dict:
      * a ``<marker-comment>`` tag that markdownify (patched at import) renders
        as an HTML comment carrying per-image metadata for downstream LLMs/grep.
      * the rendered representation (table / mermaid / latex / description) as
        HTML the renderers convert uniformly.

    ``both`` mode additionally keeps an ``<img>`` reference to the original
    image so the file stays linked for audit / ZIP packaging — unless
    ``include_original_ref`` is disabled.
    """
    rendered = render_extraction(image_type, payload)
    if not rendered:
        return

    image_name = meta["image_name"]
    keep_original = (
        mode == ImageHandlingMode.both
        and include_original_ref
        and image_type != ImageType.decorative
    )

    meta_str = (
        f"marker-ui image-understanding: "
        f"type={meta['image_type']} model={meta['model'] or 'unknown'} "
        f"confidence={float(meta['confidence']):.2f} "
        f"cost_usd=0 duration_ms={meta['duration_ms']}"
    )
    html_parts = [f"<marker-comment>{_escape(meta_str)}</marker-comment>"]
    if keep_original:
        html_parts.append(
            f"<marker-comment>original_image: {_escape(image_name)}</marker-comment>"
        )
    html_parts.append(rendered)
    if keep_original:
        html_parts.append(f'<img src="{_escape(image_name)}" />')

    picture.html = "\n".join(html_parts)
    picture.description = None


def render_extraction(image_type: ImageType, payload: dict[str, Any]) -> str:
    """Render an extraction payload as HTML for marker's renderers.

    The Markdown renderer pipes this through markdownify (-> a Markdown table /
    fenced Mermaid block / ``$$`` math / prose); the HTML and JSON renderers
    keep it as-is. Returning Markdown here would be escaped by markdownify and
    corrupt LaTeX / Mermaid / bold — see the module docstring.
    """
    if image_type in _CHART_TYPES:
        return _render_chart(payload)
    if image_type == ImageType.table_image:
        return _render_table(payload)
    if image_type in _DIAGRAM_TYPES:
        return _render_diagram(payload)
    if image_type == ImageType.equation:
        return _render_equation(payload)
    if image_type == ImageType.screenshot_ui:
        return _render_screenshot(payload)
    if image_type == ImageType.decorative:
        return "<p><em>Decorative element omitted.</em></p>"
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

    rows = [[x, *[rows_by_x[x].get(col, "") for col in columns[1:]]] for x in x_values]
    table = _table_html(columns, rows)

    title = str(payload.get("title", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    parts = []
    if title:
        parts.append(f"<p><strong>{_escape(title)}</strong></p>")
    parts.append(table)
    if notes:
        parts.append(f"<p>{_escape(notes)}</p>")
    return "\n".join(parts)


def _render_table(payload: dict[str, Any]) -> str:
    headers = [str(h) for h in payload.get("headers") or []]
    rows = payload.get("rows") or []
    if not headers and rows:
        headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
    if not headers:
        return _render_description(payload)

    norm_rows = [
        list(row)[: len(headers)] + [""] * max(0, len(headers) - len(row))
        for row in rows
    ]
    table = _table_html(headers, norm_rows)
    caption = str(payload.get("caption", "")).strip()
    parts = []
    if caption:
        parts.append(f"<p>{_escape(caption)}</p>")
    parts.append(table)
    return "\n".join(parts)


def _render_diagram(payload: dict[str, Any]) -> str:
    mermaid = str(payload.get("mermaid", "")).strip()
    caption = str(payload.get("caption", "")).strip()
    parts = []
    if caption:
        parts.append(f"<p>{_escape(caption)}</p>")
    parts.append(
        f'<pre><code class="language-mermaid">{_escape(mermaid)}</code></pre>'
    )
    return "\n".join(parts)


def _render_equation(payload: dict[str, Any]) -> str:
    latex = str(payload.get("latex", "")).strip()
    caption = str(payload.get("caption", "")).strip()
    parts = []
    if caption:
        parts.append(f"<p>{_escape(caption)}</p>")
    parts.append(f'<math display="block">{_escape(latex)}</math>')
    return "\n".join(parts)


def _render_screenshot(payload: dict[str, Any]) -> str:
    app = str(payload.get("application", "")).strip()
    area = str(payload.get("area", "")).strip()
    if app or area:
        heading = f"Screenshot of {app or 'application'}: {area or 'screen'}"
    else:
        heading = "Screenshot"
    parts = [f"<h1>{_escape(heading)}</h1>"]
    summary = str(payload.get("summary", "")).strip()
    if summary:
        parts.append(f"<p>{_escape(summary)}</p>")

    items = []
    for region in payload.get("regions") or []:
        name = str(region.get("name", "Region")).strip() or "Region"
        desc = str(region.get("description", "")).strip()
        ocr = str(region.get("ocr_text", "")).strip()
        line = f"{name}: {desc}" + (f" Text: {ocr}" if ocr else "")
        items.append(f"<li>{_escape(line)}</li>")
    if items:
        parts.append("<ul>" + "".join(items) + "</ul>")
    return "\n".join(parts)


def _render_description(payload: dict[str, Any]) -> str:
    alt = str(payload.get("alt_text") or payload.get("summary") or "").strip()
    details = [str(d).strip() for d in payload.get("details") or [] if str(d).strip()]
    parts = []
    if alt:
        parts.append(f"<p>{_escape(alt)}</p>")
    if details:
        parts.append("<ul>" + "".join(f"<li>{_escape(d)}</li>" for d in details) + "</ul>")
    return "\n".join(parts)


def _block_text(block: Any, document: Any) -> str:
    raw_text = getattr(block, "raw_text", None)
    if callable(raw_text):
        try:
            return str(raw_text(document)).strip()
        except Exception:  # noqa: BLE001 - context is best effort
            pass
    return str(getattr(block, "html", "") or getattr(block, "text", "") or "").strip()


def _table_html(headers: list[Any], rows: list[list[Any]]) -> str:
    """Render an HTML table; markdownify converts it to a Markdown table and
    the HTML/JSON renderers keep it as-is. Cell content is HTML-escaped, so
    Markdown metacharacters in the data survive the round-trip intact."""
    head = "<tr>" + "".join(f"<th>{_escape(h)}</th>" for h in headers) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table>{head}{body}</table>"


def _escape(value: Any) -> str:
    return _html.escape(str(value), quote=True)


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
