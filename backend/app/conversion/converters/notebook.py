"""Jupyter notebook converter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.conversion.converters.text_data import decode_text_file
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


class NotebookConverter(BaseConverter):
    """Convert .ipynb notebooks to Markdown with code and text outputs."""

    engine_name = "notebook"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    _EXTENSIONS = frozenset({".ipynb"})

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
        notebook = json.loads(decode_text_file(filepath))
        lines = [f"# {Path(filepath).stem}", ""]
        cell_count = 0
        for cell in notebook.get("cells", []):
            cell_count += 1
            cell_type = cell.get("cell_type")
            source = _join_source(cell.get("source", []))
            if cell_type == "markdown":
                lines.append(source.strip())
                lines.append("")
                continue
            if cell_type == "code":
                lines.append("```python")
                lines.append(source.rstrip())
                lines.append("```")
                output_text = _render_outputs(cell.get("outputs", []))
                if output_text:
                    lines.append("")
                    lines.append("Output:")
                    lines.append("")
                    lines.append("```")
                    lines.append(output_text.rstrip())
                    lines.append("```")
                lines.append("")
        return UniversalConversionResult(
            text="\n".join(lines).strip(),
            extension="md",
            metadata={"engine_detail": {"format": "ipynb", "cells": cell_count}},
        )


def _join_source(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value or "")


def _render_outputs(outputs: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for output in outputs:
        text = output.get("text")
        if text:
            chunks.append(_join_source(text))
            continue
        data = output.get("data") or {}
        plain = data.get("text/plain")
        markdown = data.get("text/markdown")
        if markdown:
            chunks.append(_join_source(markdown))
        elif plain:
            chunks.append(_join_source(plain))
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip())
