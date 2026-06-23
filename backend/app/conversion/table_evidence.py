"""Lightweight Markdown table evidence extraction.

This is benchmark/conversion metadata only. It does not claim full PDF table
structure recognition and must not drive LiteParse routing decisions.
"""

from __future__ import annotations

from typing import Any


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_cell(cell: str) -> bool:
    token = cell.strip()
    if len(token) < 3:
        return False
    token = token.strip(":")
    return bool(token) and set(token) <= {"-"}


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(_is_separator_cell(cell) for cell in cells)


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3


def extract_markdown_tables(text: str) -> list[dict[str, Any]]:
    """Extract pipe-style Markdown tables from rendered text.

    Returns a small, stable evidence shape:
    ``{"source": "markdown", "headers": [...], "rows": [[...]], ...}``.
    The parser intentionally handles only conventional pipe tables. That keeps
    this suitable for benchmark evidence without pretending to be layout OCR.
    """
    lines = (text or "").splitlines()
    tables: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        if not _looks_like_table_row(lines[idx]):
            idx += 1
            continue

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
        if not headers or not data_rows:
            continue

        width = max(len(headers), *(len(row) for row in data_rows))
        normalized_headers = headers + [""] * (width - len(headers))
        normalized_rows = [row + [""] * (width - len(row)) for row in data_rows]
        tables.append(
            {
                "source": "markdown",
                "headers": normalized_headers,
                "rows": normalized_rows,
                "row_count": len(normalized_rows),
                "column_count": width,
            }
        )

    return tables


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

    tables = extract_markdown_tables(text)
    if not tables:
        return updated

    updated["table"] = {
        "headers": tables[0]["headers"],
        "rows": tables[0]["rows"],
    }
    updated["tables"] = tables
    updated["table_evidence"] = {
        "source": "markdown_pipe_table",
        "table_count": len(tables),
    }
    return updated
