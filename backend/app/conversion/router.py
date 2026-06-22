"""Conversion router — decides which engine handles a file.

Pure function: takes ``StreamInfo`` + config, returns ``ConverterPlan``.
No side effects, no heavy imports, no model loading.  The registry then
checks whether the planned engine actually has a registered converter.
"""

from __future__ import annotations

from typing import Any

from app.conversion.result import ConverterPlan
from app.conversion.stream_info import StreamInfo

# ---------------------------------------------------------------------------
# Routing table
#
# Maps file extensions to (engine, label, needs_marker, needs_gpu, confidence).
# The router picks the first match.  Unknown extensions fall through to a
# low-confidence marker_pdf fallback.
# ---------------------------------------------------------------------------

_ROUTE_TABLE: list[tuple[frozenset[str], str, str, bool, bool, float]] = [
    # (extensions, engine, label, needs_marker_models, needs_gpu, confidence)
    (
        frozenset({".pdf"}),
        "marker_pdf",
        "Marker PDF",
        True,
        True,
        1.0,
    ),
    (
        frozenset({".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp", ".gif"}),
        "marker_pdf",
        "Marker Image OCR",
        True,
        True,
        1.0,
    ),
    (
        frozenset({".epub"}),
        "marker_pdf",
        "Marker EPUB",
        True,
        True,
        1.0,
    ),
    (
        frozenset({".docx"}),
        "office_docx",
        "Fast Office (Word)",
        False,
        False,
        0.95,
    ),
    (
        frozenset({".pptx"}),
        "office_pptx",
        "Fast Office (PowerPoint)",
        False,
        False,
        0.95,
    ),
    (
        frozenset({".xlsx", ".xls"}),
        "spreadsheet",
        "Fast Spreadsheet",
        False,
        False,
        0.95,
    ),
    (
        frozenset({".csv"}),
        "text_data",
        "Text / Data",
        False,
        False,
        0.95,
    ),
    (
        frozenset({".json", ".jsonl"}),
        "text_data",
        "Text / Data (JSON)",
        False,
        False,
        0.95,
    ),
    (
        frozenset({".xml", ".rss", ".atom"}),
        "xml_rss",
        "XML / RSS",
        False,
        False,
        0.90,
    ),
    (
        frozenset({".html", ".htm"}),
        "html",
        "HTML",
        False,
        False,
        0.90,
    ),
    (
        frozenset({".txt", ".md", ".rst", ".log"}),
        "text_data",
        "Plain Text",
        False,
        False,
        1.0,
    ),
    (
        frozenset({".ipynb"}),
        "notebook",
        "Jupyter Notebook",
        False,
        False,
        0.95,
    ),
    (
        frozenset({".zip"}),
        "archive",
        "Archive (ZIP)",
        False,  # depends on contents — conservative default
        False,
        0.90,
    ),
]

# Pre-built lookup for O(1) extension matching.
_EXT_TO_ENTRY: dict[str, tuple[str, str, bool, bool, float]] = {}
for _exts, _engine, _label, _marker, _gpu, _conf in _ROUTE_TABLE:
    for _ext in _exts:
        _EXT_TO_ENTRY[_ext] = (_engine, _label, _marker, _gpu, _conf)


class ConversionRouter:
    """Stateless router: extension → ConverterPlan."""

    @staticmethod
    def plan(stream_info: StreamInfo, config: dict[str, Any]) -> ConverterPlan:
        """Decide which engine should handle *stream_info*.

        Returns a ``ConverterPlan`` with the engine name, confidence, and
        resource requirements.  The plan is a recommendation — the caller
        checks whether the engine is actually registered.
        """
        # Normalize defensively: production paths (from_path, plan_by_metadata)
        # already lower-case the extension, but a caller building a raw StreamInfo
        # with an upper-case suffix should still route correctly rather than
        # falling through to the low-confidence marker_pdf fallback.
        ext = stream_info.extension.lower()

        entry = _EXT_TO_ENTRY.get(ext)
        if entry is not None:
            engine, label, needs_marker, needs_gpu, confidence = entry
            return ConverterPlan(
                engine=engine,
                label=label,
                confidence=confidence,
                reasons=[f"Matched extension '{ext}'"],
                needs_marker_models=needs_marker,
                needs_gpu=needs_gpu,
                execution_backend="marker_worker" if needs_marker else "cpu_thread",
            )

        # Unknown extension → low-confidence fallback to marker_pdf.
        return ConverterPlan(
            engine="marker_pdf",
            label="Marker PDF (fallback)",
            confidence=0.3,
            reasons=[f"Unknown extension '{ext}'; falling back to Marker PDF"],
            needs_marker_models=True,
            needs_gpu=True,
            execution_backend="marker_worker",
            warnings=[f"No dedicated converter for '{ext}'; using Marker PDF as fallback"],
        )
