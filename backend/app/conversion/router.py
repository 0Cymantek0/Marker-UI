"""Conversion router — decides which engine handles a file.

Pure function: takes ``StreamInfo`` + config, returns ``ConverterPlan``.
No side effects, no heavy imports, no model loading.  The registry then
checks whether the planned engine actually has a registered converter.
"""

from __future__ import annotations

from typing import Any

from app.conversion.probe import PdfProbeResult
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

_ENGINE_META: dict[str, tuple[str, bool, bool, float]] = {
    "marker_pdf": ("Marker PDF", True, True, 1.0),
    "liteparse_pdf": ("LiteParse Fast PDF", False, False, 0.9),
    "office_docx": ("Fast Office (Word)", False, False, 0.95),
    "office_pptx": ("Fast Office (PowerPoint)", False, False, 0.95),
    "spreadsheet": ("Fast Spreadsheet", False, False, 0.95),
    "text_data": ("Text / Data", False, False, 0.95),
    "xml_rss": ("XML / RSS", False, False, 0.90),
    "html": ("HTML", False, False, 0.90),
    "notebook": ("Jupyter Notebook", False, False, 0.95),
    "archive": ("Archive (ZIP)", False, False, 0.90),
}

_ENGINE_COMPATIBLE_EXTS: dict[str, frozenset[str]] = {
    "marker_pdf": frozenset({".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".bmp", ".gif", ".epub"}),
    "liteparse_pdf": frozenset({".pdf"}),
    "office_docx": frozenset({".docx"}),
    "office_pptx": frozenset({".pptx"}),
    "spreadsheet": frozenset({".xlsx", ".xls"}),
    "text_data": frozenset({".csv", ".json", ".jsonl", ".txt", ".md", ".rst", ".log"}),
    "xml_rss": frozenset({".xml", ".rss", ".atom"}),
    "html": frozenset({".html", ".htm"}),
    "notebook": frozenset({".ipynb"}),
    "archive": frozenset({".zip"}),
}


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
        override_plan = ConversionRouter._plan_engine_override(ext, config)
        if override_plan is not None:
            return override_plan

        if ext == ".pdf":
            return ConversionRouter._plan_pdf(config)

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

    @staticmethod
    def _plan_engine_override(ext: str, config: dict[str, Any]) -> ConverterPlan | None:
        engine = str(config.get("engine_override") or "").strip()
        if not engine:
            return None
        meta = _ENGINE_META.get(engine)
        compatible_exts = _ENGINE_COMPATIBLE_EXTS.get(engine, frozenset())
        if meta is None or ext not in compatible_exts:
            return None
        label, needs_marker, needs_gpu, confidence = meta
        if engine == "marker_pdf" and ext in _EXT_TO_ENTRY:
            _engine, label, needs_marker, needs_gpu, confidence = _EXT_TO_ENTRY[ext]
        return ConverterPlan(
            engine=engine,
            label=label,
            confidence=confidence,
            reasons=[f"User selected engine override '{engine}'"],
            needs_marker_models=needs_marker,
            needs_gpu=needs_gpu,
            execution_backend="marker_worker" if needs_marker else "cpu_thread",
            fallback_chain=["liteparse_pdf", "marker_pdf"] if engine == "liteparse_pdf" else [],
        )

    @staticmethod
    def _plan_pdf(config: dict[str, Any]) -> ConverterPlan:
        probe_data = config.get("probe_result")
        probe = PdfProbeResult.from_mapping(probe_data) if isinstance(probe_data, dict) else None
        reasons = list(probe.reasons) if probe else ["PDF complexity was not probed; using conservative Marker route"]
        warnings: list[str] = []
        if not probe:
            warnings.append("Preliminary filename-only plan; upload/local probing may change selected engine")

        profile = str(config.get("conversion_profile") or config.get("profile") or "").lower()
        converter_cls = str(config.get("converter_cls") or "")
        image_mode = config.get("image_handling_mode")
        image_understanding_requested = image_mode in ("understanding", "both")
        force_marker_reasons: list[str] = []
        if config.get("force_ocr"):
            force_marker_reasons.append("force_ocr is enabled")
        if image_understanding_requested and (probe is None or probe.sampled_image_count > 0):
            force_marker_reasons.append("image understanding needs Marker block/images")
        if profile in {"high_accuracy", "high-accuracy", "accuracy"}:
            force_marker_reasons.append("High Accuracy profile selected")
        if converter_cls in {"TableConverter", "OCRConverter"}:
            force_marker_reasons.append(f"{converter_cls} requires Marker")

        liteparse_safe = bool(
            probe
            and probe.text_layer_score >= 0.70
            and probe.text_quality_score >= 0.80
            and probe.scan_likelihood <= 0.20
            and probe.sandwich_likelihood <= 0.40
            and probe.visual_complexity_score <= 0.35
            and probe.layout_complexity_score <= 0.45
            and not force_marker_reasons
        )

        if liteparse_safe:
            return ConverterPlan(
                engine="liteparse_pdf",
                label="LiteParse Fast PDF",
                confidence=0.92,
                reasons=reasons + ["Routing to LiteParse because PDF is clean digital text"],
                needs_marker_models=False,
                needs_gpu=False,
                execution_backend="cpu_thread",
                fallback_chain=["liteparse_pdf", "marker_pdf"],
                warnings=warnings,
            )

        marker_reasons = reasons + force_marker_reasons
        return ConverterPlan(
            engine="marker_pdf",
            label="Marker PDF",
            confidence=1.0 if force_marker_reasons or probe else 0.75,
            reasons=marker_reasons,
            needs_marker_models=True,
            needs_gpu=True,
            execution_backend="marker_worker",
            warnings=warnings,
        )
