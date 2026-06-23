"""Spreadsheet converter for XLSX workbooks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.conversion.converters.text_data import rows_to_markdown_table
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


class SpreadsheetConverter(BaseConverter):
    """Convert XLSX sheets to Markdown tables using openpyxl."""

    engine_name = "spreadsheet"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    _EXTENSIONS = frozenset({".xlsx"})

    @property
    def supported_extensions(self) -> frozenset[str]:
        return self._EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension in self._EXTENSIONS

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        from openpyxl import load_workbook

        workbook = load_workbook(filepath, read_only=True, data_only=True)
        max_rows = int(config.get("spreadsheet_max_rows_per_sheet", 200))
        parts = [f"# {Path(filepath).stem}"]
        sheet_meta: list[dict[str, Any]] = []
        for sheet in workbook.worksheets:
            rows: list[list[Any]] = []
            for idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if idx > max_rows:
                    break
                rows.append(list(row))
            parts.append(f"\n## Sheet: {sheet.title}\n")
            table = rows_to_markdown_table(rows)
            parts.append(table if table else "_Empty sheet._")
            truncated = bool(sheet.max_row and sheet.max_row > max_rows)
            if truncated:
                parts.append(f"\n_Only first {max_rows} rows shown._")
            sheet_meta.append(
                {
                    "name": sheet.title,
                    "rows": sheet.max_row,
                    "columns": sheet.max_column,
                    "truncated": truncated,
                }
            )
        workbook.close()
        return UniversalConversionResult(
            text="\n".join(parts).strip(),
            extension="md",
            metadata={"engine_detail": {"format": "xlsx", "sheets": sheet_meta}},
        )
