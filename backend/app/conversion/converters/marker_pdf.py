"""MarkerPdfConverter — wraps the existing MarkerService unchanged.

This is the only converter registered in Phase 0.  It delegates entirely
to ``MarkerService.convert_file``, adapting its output to
``UniversalConversionResult``.  The marker options pipeline
(``build_marker_options``) is called BEFORE this converter — the config
dict arrives fully resolved.

The converter never loads models itself; ``MarkerService.initialize`` is
called lazily by ``MarkerService.convert_file``.
"""

from __future__ import annotations

from typing import Any

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo
from app.conversion.table_evidence import attach_table_evidence


class MarkerPdfConverter(BaseConverter):
    """Wraps ``MarkerService`` as a ``BaseConverter``.

    Handles PDF, images (jpg/png/webp/tiff/bmp/gif), and EPUB — every
    format the existing marker pipeline supports.
    """

    engine_name = "marker_pdf"
    priority = 100  # high — the established engine
    requires_marker_models = True
    requires_gpu = True

    _EXTENSIONS = frozenset({
        ".pdf",
        ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp", ".gif",
        ".epub",
    })

    def __init__(self, marker_service: Any) -> None:
        self._marker_service = marker_service

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
        """Delegate to ``MarkerService.convert_file`` and wrap the result."""
        result = self._marker_service.convert_file(filepath, dict(config), device=device)
        text = result.get("text", "")
        return UniversalConversionResult(
            text=text,
            extension=result.get("extension", "md"),
            images=result.get("images", {}),
            metadata=attach_table_evidence(result.get("metadata", {}), text),
        )

    def supports_multiple_formats(self) -> bool:
        """Marker parses once and renders N formats from one Document."""
        return True

    def convert_formats(
        self,
        filepath: str,
        config: dict[str, Any],
        formats: list[str],
        device: str | None = None,
    ) -> dict[str, UniversalConversionResult]:
        """Render several output formats from a single marker document parse.

        ``MarkerService.convert_file_formats`` builds the document once and
        renders each requested format from it, so multi-format output costs one
        parse rather than one per format (the "no reconverting" guarantee).
        """
        formats_out = self._marker_service.convert_file_formats(
            filepath, dict(config), list(formats), device=device
        )
        results: dict[str, UniversalConversionResult] = {}
        for fmt, payload in formats_out.items():
            text = payload.get("text", "")
            results[fmt] = UniversalConversionResult(
                text=text,
                extension=payload.get("extension", "md"),
                images=payload.get("images", {}),
                metadata=attach_table_evidence(payload.get("metadata", {}), text),
            )
        return results
