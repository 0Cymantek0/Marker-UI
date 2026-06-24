"""Lightweight rendered-text table evidence extraction.

This is benchmark/conversion metadata only. It does not claim full PDF table
structure recognition and must not drive LiteParse routing decisions.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup


_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CAPTION_RE = re.compile(r"^\**\s*(table|figure)\s+\d+[:.]", re.IGNORECASE)
_JSON_TABLE_BLOCK_TYPES = {"Table"}
_JSON_TABLE_CELL_BLOCK_TYPES = {"TableCell"}


def _clean_cell(value: Any) -> str:
    text = unescape(str(value or ""))
    text = _BR_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    return " ".join(text.split())


def _caption_before(lines: list[str], start_index: int) -> str | None:
    idx = start_index - 1
    while idx >= 0:
        candidate = lines[idx].strip()
        if candidate:
            cleaned = _clean_cell(candidate.strip("* "))
            return cleaned if _CAPTION_RE.match(cleaned) else None
        idx -= 1
    return None


def _table_payload(
    *,
    source: str,
    headers: list[str],
    rows: list[list[str]],
    caption: str | None = None,
    cells: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    width = max([len(headers), *(len(row) for row in rows)] or [0])
    normalized_headers = headers + [""] * (width - len(headers))
    normalized_rows = [row + [""] * (width - len(row)) for row in rows]
    payload: dict[str, Any] = {
        "source": source,
        "caption": caption,
        "headers": normalized_headers,
        "rows": normalized_rows,
        "row_count": len(normalized_rows),
        "column_count": width,
    }
    if cells is not None:
        payload["cells"] = cells
    return payload


def _header_key(table: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_clean_cell(cell).casefold() for cell in table.get("headers") or [])


def _can_stitch_table(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_headers = _header_key(previous)
    current_headers = _header_key(current)
    if not previous_headers or previous_headers != current_headers:
        return False
    if previous.get("source") != current.get("source"):
        return False
    if previous.get("column_count") != current.get("column_count"):
        return False
    previous_caption = previous.get("caption")
    current_caption = current.get("caption")
    return not current_caption or current_caption == previous_caption


def _append_stitched_cells(
    previous: dict[str, Any],
    current: dict[str, Any],
    row_offset: int,
) -> None:
    current_cells = current.get("cells")
    if not isinstance(current_cells, list):
        return
    previous_cells = previous.setdefault("cells", [])
    if not isinstance(previous_cells, list):
        return
    for cell in current_cells:
        if not isinstance(cell, dict):
            continue
        cell_row = int(cell.get("row") or 0)
        if cell_row == 0:
            continue
        adjusted = dict(cell)
        adjusted["row"] = row_offset + cell_row - 1
        previous_cells.append(adjusted)


def stitch_continued_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join consecutive rendered tables that repeat the same headers."""
    stitched: list[dict[str, Any]] = []
    for table in tables:
        if stitched and _can_stitch_table(stitched[-1], table):
            previous = stitched[-1]
            row_offset = len(previous.get("rows") or []) + 1
            previous["rows"].extend(table.get("rows") or [])
            previous["row_count"] = len(previous["rows"])
            previous["segment_count"] = int(previous.get("segment_count") or 1) + 1
            previous["multi_page"] = True
            previous.setdefault("continued_sources", []).append({
                "source": table.get("source"),
                "caption": table.get("caption"),
                "row_count": table.get("row_count"),
            })
            _append_stitched_cells(previous, table, row_offset)
            continue
        copied = dict(table)
        copied["segment_count"] = 1
        stitched.append(copied)
    return stitched


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_clean_cell(cell) for cell in stripped.split("|")]


def _is_separator_cell(cell: str) -> bool:
    token = cell.strip()
    if not token:
        return True
    if len(token) < 2:
        return False
    token = token.strip(":")
    return bool(token) and set(token) <= {"-"}


def _is_separator_row(cells: list[str]) -> bool:
    return (
        bool(cells)
        and any("-" in cell for cell in cells)
        and all(_is_separator_cell(cell) for cell in cells)
    )


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("```"):
        return False
    return stripped.count("|") >= 1


def extract_markdown_tables(text: str) -> list[dict[str, Any]]:
    """Extract pipe-style Markdown tables from rendered text.

    Returns a small, stable evidence shape:
    ``{"source": "markdown_pipe_table", "headers": [...], "rows": [[...]], ...}``.
    It accepts conventional pipe tables and simple malformed tables that omit
    the leading/trailing pipe.
    """
    lines = (text or "").splitlines()
    tables: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        if not _looks_like_table_row(lines[idx]):
            idx += 1
            continue

        start_index = idx
        block: list[str] = []
        while idx < len(lines) and _looks_like_table_row(lines[idx]):
            block.append(lines[idx])
            idx += 1

        if len(block) < 2:
            continue

        rows = [_split_markdown_row(line) for line in block]
        header_index = 0
        data_start = 1
        if len(rows) >= 2 and _is_separator_row(rows[1]):
            data_start = 2

        headers = rows[header_index]
        data_rows = rows[data_start:]
        if headers and all(not cell for cell in headers) and data_rows:
            headers = data_rows[0]
            data_rows = data_rows[1:]
        if not headers or not data_rows:
            continue

        tables.append(_table_payload(
            source="markdown_pipe_table",
            caption=_caption_before(lines, start_index),
            headers=headers,
            rows=data_rows,
        ))

    return tables


def extract_html_tables(text: str) -> list[dict[str, Any]]:
    """Extract HTML tables, preserving captions and span metadata.

    The public scoring grid remains ``headers`` + ``rows``. Detailed ``cells``
    carry ``rowspan`` / ``colspan`` so later layout-aware work has a stable
    metadata place without changing the benchmark report shape.
    """
    if "<table" not in (text or "").lower():
        return []

    soup = BeautifulSoup(text or "", "html.parser")
    tables: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        caption_tag = table.find("caption")
        caption = (
            _clean_cell(caption_tag.get_text(" ", strip=True))
            if caption_tag is not None
            else None
        )
        grid: list[list[str]] = []
        cells: list[dict[str, Any]] = []
        occupied: set[tuple[int, int]] = set()

        for row_index, row in enumerate(table.find_all("tr")):
            while len(grid) <= row_index:
                grid.append([])
            col_index = 0
            for cell in row.find_all(["th", "td"], recursive=False):
                while (row_index, col_index) in occupied:
                    col_index += 1
                text_value = _clean_cell(cell.get_text(" ", strip=True))
                rowspan = max(1, int(cell.get("rowspan", 1) or 1))
                colspan = max(1, int(cell.get("colspan", 1) or 1))
                for r_offset in range(rowspan):
                    target_row = row_index + r_offset
                    while len(grid) <= target_row:
                        grid.append([])
                    while len(grid[target_row]) < col_index + colspan:
                        grid[target_row].append("")
                    for c_offset in range(colspan):
                        target_col = col_index + c_offset
                        occupied.add((target_row, target_col))
                        grid[target_row][target_col] = (
                            text_value if r_offset == 0 and c_offset == 0 else ""
                        )
                cells.append({
                    "row": row_index,
                    "column": col_index,
                    "text": text_value,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "header": cell.name == "th",
                })
                col_index += colspan

        if not grid:
            continue
        headers = grid[0]
        rows = grid[1:]
        if not headers or not rows:
            continue
        tables.append(_table_payload(
            source="html_table",
            caption=caption,
            headers=headers,
            rows=rows,
            cells=cells,
        ))
    return tables


def _block_type_name(block_type: Any) -> str:
    return str(block_type or "").rsplit(".", 1)[-1]


def _json_block_children(block: dict[str, Any]) -> list[dict[str, Any]]:
    children = block.get("children")
    return [child for child in children if isinstance(child, dict)] if isinstance(children, list) else []


def _json_cell_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for child in _json_block_children(block):
        block_type = _block_type_name(child.get("block_type"))
        if block_type in _JSON_TABLE_CELL_BLOCK_TYPES:
            cells.append(child)
        cells.extend(_json_cell_blocks(child))
    return cells


def _enrich_json_table(
    table: dict[str, Any],
    block: dict[str, Any],
    *,
    page_number: int | None,
) -> dict[str, Any]:
    enriched = dict(table)
    enriched["source"] = "marker_json_table"
    enriched["page_number"] = page_number
    enriched["bbox"] = block.get("bbox")
    enriched["polygon"] = block.get("polygon")
    cell_blocks = _json_cell_blocks(block)
    cells = enriched.get("cells")
    if isinstance(cells, list):
        for cell, cell_block in zip(cells, cell_blocks):
            if not isinstance(cell, dict):
                continue
            cell["bbox"] = cell_block.get("bbox")
            cell["polygon"] = cell_block.get("polygon")
            cell["page_number"] = page_number
            cell["block_id"] = cell_block.get("id")
    return enriched


def extract_marker_json_tables(text: str) -> list[dict[str, Any]]:
    """Extract table evidence from Marker JSON renderer output."""
    try:
        payload = json.loads(text or "")
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []

    tables: list[dict[str, Any]] = []
    page_counter = 0

    def visit(block: dict[str, Any], *, page_number: int | None = None) -> None:
        nonlocal page_counter
        block_type = _block_type_name(block.get("block_type"))
        next_page = page_number
        if block_type == "Page":
            page_counter += 1
            next_page = page_counter
        if block_type in _JSON_TABLE_BLOCK_TYPES:
            html_tables = extract_html_tables(str(block.get("html") or ""))
            if html_tables:
                tables.append(_enrich_json_table(
                    html_tables[0],
                    block,
                    page_number=next_page,
                ))
        for child in _json_block_children(block):
            visit(child, page_number=next_page)

    for child in _json_block_children(payload):
        visit(child)

    return tables


def extract_tables(text: str) -> list[dict[str, Any]]:
    """Extract all supported rendered table forms from text."""
    json_tables = extract_marker_json_tables(text)
    if json_tables:
        return stitch_continued_tables(json_tables)
    return stitch_continued_tables(
        extract_markdown_tables(text)
        + extract_html_tables(text)
    )



def attach_table_evidence(
    metadata: dict[str, Any] | None,
    text: str,
) -> dict[str, Any]:
    """Return metadata with table evidence inferred from Markdown when needed."""
    updated = dict(metadata or {})
    existing = updated.get("table")
    existing_tables = updated.get("tables")
    if existing is not None or existing_tables:
        return updated

    tables = extract_tables(text)
    if not tables:
        return updated

    updated["table"] = {
        "headers": tables[0]["headers"],
        "rows": tables[0]["rows"],
    }
    updated["tables"] = tables
    updated["table_evidence"] = {
        "source": "rendered_text_tables",
        "table_count": len(tables),
        "sources": sorted({str(table.get("source")) for table in tables}),
    }
    return updated
