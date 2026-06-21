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
        return UniversalConversionResult(
            text=result.get("text", ""),
            extension=result.get("extension", "md"),
            images=result.get("images", {}),
            metadata=result.get("metadata", {}),
        )
