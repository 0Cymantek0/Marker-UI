"""Lightweight converters for plain text and structured text data."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl"})


def decode_text_file(filepath: str | Path) -> str:
    data = Path(filepath).read_bytes()
    if not data:
        return ""
    try:
        from charset_normalizer import from_bytes

        match = from_bytes(data).best()
        if match is not None:
            return str(match)
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")


def _escape_table_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def rows_to_markdown_table(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = [_escape_table_cell(cell) for cell in normalized[0]]
    body = [[_escape_table_cell(cell) for cell in row] for row in normalized[1:]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


class TextDataConverter(BaseConverter):
    """Convert text, CSV, JSON, and JSONL files without Marker models."""

    engine_name = "text_data"
    priority = 10
    requires_marker_models = False
    requires_gpu = False

    @property
    def supported_extensions(self) -> frozenset[str]:
        return _TEXT_EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension in _TEXT_EXTENSIONS

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        ext = Path(filepath).suffix.lower()
        text = decode_text_file(filepath)

        if ext in {".csv", ".tsv"}:
            sample = text[:4096]
            if ext == ".tsv":
                dialect = csv.excel_tab
            else:
                try:
                    dialect = csv.Sniffer().sniff(sample)
                except csv.Error:
                    dialect = csv.excel
            rows = list(csv.reader(io.StringIO(text), dialect))
            max_rows = int(config.get("text_data_max_rows", 500))
            truncated = len(rows) > max_rows
            markdown = rows_to_markdown_table(rows[:max_rows])
            if truncated:
                markdown += f"\n\n_Only first {max_rows} rows shown._"
            return UniversalConversionResult(
                text=markdown,
                extension="md",
                metadata={"engine_detail": {"format": ext.lstrip("."), "rows": len(rows), "truncated": truncated}},
            )

        if ext == ".json":
            parsed = json.loads(text) if text.strip() else None
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False) if parsed is not None else ""
            return UniversalConversionResult(
                text=f"```json\n{pretty}\n```" if pretty else "",
                extension="md",
                metadata={"engine_detail": {"format": "json"}},
            )

        if ext == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            pretty = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
            return UniversalConversionResult(
                text=f"```jsonl\n{pretty}\n```" if pretty else "",
                extension="md",
                metadata={"engine_detail": {"format": "jsonl", "rows": len(rows)}},
            )

        return UniversalConversionResult(
            text=text,
            extension="md",
            metadata={"engine_detail": {"format": ext.lstrip(".") or "text"}},
        )
