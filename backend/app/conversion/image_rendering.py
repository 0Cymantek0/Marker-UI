"""Marker-free image-understanding payload rendering helpers."""

from __future__ import annotations

import html as _html
from typing import Any

from app.models.image_understanding import ImageType


def render_extraction(image_type: ImageType, payload: dict[str, Any]) -> str:
    """Render an extraction payload as HTML without importing marker modules."""
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
    if not mermaid:
        desc = _render_description(payload)
        if desc:
            return desc
        if caption:
            return f"<p>{_escape(caption)}</p>"
        return ""
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


def _table_html(headers: list[Any], rows: list[list[Any]]) -> str:
    head = "<tr>" + "".join(f"<th>{_escape(h)}</th>" for h in headers) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table>{head}{body}</table>"


def _escape(value: Any) -> str:
    return _html.escape(str(value), quote=True)


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
