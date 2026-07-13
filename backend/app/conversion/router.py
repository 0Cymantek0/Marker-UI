"""Conversion router — decides which engine handles a file.

Pure function: takes ``StreamInfo`` + config, returns ``ConverterPlan``.
No side effects, no heavy imports, no model loading.  The registry then
checks whether the planned engine actually has a registered converter.
"""

from __future__ import annotations

from typing import Any

from app.conversion.engine_policy import ENGINE_COMPATIBLE_EXTENSIONS
from app.conversion.formats import INPUT_FORMATS
from app.conversion.probe import PdfProbeResult
from app.conversion.result import ConverterPlan
from app.conversion.stream_info import StreamInfo

# Pre-built lookup for O(1) extension matching. PDF still routes through
# _plan_pdf because probe/config can choose LiteParse or Marker.
_EXT_TO_ENTRY = {
    ext: (
        spec.engine,
        spec.label,
        spec.needs_marker_models,
        spec.needs_gpu,
        spec.confidence,
    )
    for spec in INPUT_FORMATS
    for ext in spec.extensions
    if ext != ".pdf"
}

def _engine_meta_from_input_formats() -> dict[str, tuple[str, bool, bool, float]]:
    meta: dict[str, tuple[str, bool, bool, float]] = {}
    for spec in INPUT_FORMATS:
        meta.setdefault(
            spec.engine,
            (
                spec.label,
                spec.needs_marker_models,
                spec.needs_gpu,
                spec.confidence,
            ),
        )
    return meta


_ENGINE_META: dict[str, tuple[str, bool, bool, float]] = {
    **_engine_meta_from_input_formats(),
    "liteparse_pdf": ("LiteParse Fast PDF", False, False, 0.9),
}

_ENGINE_COMPATIBLE_EXTS: dict[str, frozenset[str]] = {
    engine: ENGINE_COMPATIBLE_EXTENSIONS.get(engine, frozenset())
    for engine in _ENGINE_META
}


def _converter_short_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw.rsplit(".", 1)[-1]


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
        else:
            if probe.text_layer_score < 0.70:
                reasons.append("text layer score is below LiteParse threshold")
            if probe.text_quality_score < 0.80:
                reasons.append("text quality score is below LiteParse threshold")
            if probe.scan_likelihood > 0.20:
                reasons.append("scan likelihood is above LiteParse threshold")
            if probe.sandwich_likelihood > 0.40:
                reasons.append("sandwich likelihood is above LiteParse threshold")
            if probe.visual_complexity_score > 0.35:
                reasons.append("visual/image complexity is above LiteParse threshold")
            if probe.layout_complexity_score > 0.45:
                reasons.append("layout complexity is above LiteParse threshold")

        profile = str(config.get("conversion_profile") or config.get("profile") or "").lower()
        converter_cls = _converter_short_name(config.get("converter_cls"))
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
        hard_marker_reasons = list(force_marker_reasons)

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

        fast_profile = profile in {"fast", "fast_path", "fast-path"}
        if fast_profile and not hard_marker_reasons:
            fast_warnings = warnings + [
                "Fast profile forces LiteParse before Marker and can reduce accuracy on scanned or complex PDFs",
                "Short or empty LiteParse output will still retry with Marker",
            ]
            fast_reasons = reasons + [
                "Fast profile selected; routing to LiteParse despite conservative probe risk scores",
            ]
            return ConverterPlan(
                engine="liteparse_pdf",
                label="LiteParse Fast PDF",
                confidence=0.65 if probe else 0.55,
                reasons=fast_reasons,
                needs_marker_models=False,
                needs_gpu=False,
                execution_backend="cpu_thread",
                fallback_chain=["liteparse_pdf", "marker_pdf"],
                warnings=fast_warnings,
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
