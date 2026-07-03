"""Validate requested output formats against the resolved conversion engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from app.conversion.formats import (
    MARKER_MULTI_FORMAT_EXTENSIONS,
    requested_output_formats_from_config,
)
from app.errors import UnsupportedFormatError


class FormatAwareConversionService(Protocol):
    def supports_multiple_formats(self, filepath: str, config: dict[str, Any]) -> bool: ...

    def plan(self, filepath: str, config: dict[str, Any]): ...


def require_supported_output_formats(
    filepath: str,
    config: dict[str, Any],
    service: FormatAwareConversionService,
    *,
    source_name: str | None = None,
) -> list[str]:
    """Ensure requested formats are actually renderable before queue/convert.

    Native converters emit Markdown and can derive semantic Markdown chunks.
    JSON/HTML are valid only when the route can use Marker's multi-render path.
    """
    requested = requested_output_formats_from_config(config)
    config["output_format"] = requested[0]
    if "output_formats" in config:
        config["output_formats"] = requested

    structured = [fmt for fmt in requested if fmt != "markdown"]
    if not structured:
        return requested

    ext = Path(filepath).suffix.lower()
    if (
        ext in MARKER_MULTI_FORMAT_EXTENSIONS
        and not config.get("engine_override")
        and not config.get("enable_mixed_pdf_routing")
    ):
        config["engine_override"] = "marker_pdf"

    if service.supports_multiple_formats(filepath, config):
        return requested

    plan = service.plan(filepath, config)
    source = source_name or Path(filepath).name
    formats = ", ".join(structured)
    raise UnsupportedFormatError(
        f"Output format(s) {formats} are not supported for engine '{plan.engine}' on '{source}'. "
        "Use markdown/chunks, or choose a Marker-backed PDF/image/EPUB route for json/html.",
        details={
            "source": source,
            "engine": plan.engine,
            "requested_formats": requested,
            "supported_formats": ["markdown", "chunks"],
        },
    )
