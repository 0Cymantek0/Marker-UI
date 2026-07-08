"""ConversionService — the universal conversion orchestrator.

Sits between ``TaskManager`` and the format-specific converters.  For
Phase 0, only ``MarkerPdfConverter`` is registered, so all files route
through the existing ``MarkerService``.  Future PRs register additional
converters without changing this module.

The service returns the legacy ``{text, extension, images, metadata}``
dict so ``_finalize_job`` is untouched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.conversion.converters.archive import ArchiveConverter
from app.conversion.converters.audio import AudioConverter
from app.conversion.converters.html import HtmlConverter
from app.conversion.converters.marker_pdf import MarkerPdfConverter
from app.conversion.converters.notebook import NotebookConverter
from app.conversion.converters.liteparse_pdf import LiteParsePdfConverter
from app.conversion.converters.office_docx import OfficeDocxConverter
from app.conversion.converters.office_pptx import OfficePptxConverter
from app.conversion.converters.outlook_msg import OutlookMsgConverter
from app.conversion.converters.spreadsheet import SpreadsheetConverter
from app.conversion.converters.text_data import TextDataConverter
from app.conversion.converters.video import VideoConverter
from app.conversion.converters.xml_rss import XmlRssConverter
from app.conversion.probe import (
    PdfProbeResult,
    PdfRoutingSegment,
    missing_probe_pages,
    plan_pdf_routing_segments,
    probe_coverage_label,
    probe_has_full_page_coverage,
)
from app.conversion.registry import ConverterRegistry
from app.conversion.result import ConverterPlan, UniversalConversionResult
from app.conversion.router import ConversionRouter
from app.conversion.stream_info import StreamInfo
from app.services.chunking import build_chunks_envelope
from app.conversion.table_evidence import attach_table_evidence
from app.errors import UnsupportedFormatError

logger = logging.getLogger(__name__)

_DERIVABLE_MARKDOWN_FORMATS = frozenset({"markdown", "chunks"})
_EXPECTED_FORMAT_EXTENSIONS = {
    "markdown": frozenset({"md", "markdown"}),
    "html": frozenset({"html", "htm"}),
    "json": frozenset({"json"}),
    "chunks": frozenset({"json"}),
}


def _count_page_range(page_range: Any) -> int | None:
    if not page_range:
        return None
    try:
        total = 0
        for part in str(page_range).split(","):
            token = part.strip()
            if not token:
                continue
            if "-" in token:
                start_s, end_s = token.split("-", 1)
                start = int(start_s)
                end = int(end_s)
                if start <= 0 or end < start:
                    return None
                total += end - start + 1
            else:
                page = int(token)
                if page <= 0:
                    return None
                total += 1
        return total or None
    except (TypeError, ValueError):
        return None


def _expected_liteparse_pages(config: dict[str, Any]) -> int:
    probe_data = config.get("probe_result") if isinstance(config, dict) else None
    probed_pages = (
        int((probe_data or {}).get("page_count") or 1)
        if isinstance(probe_data, dict)
        else 1
    )
    requested_pages = _count_page_range(config.get("page_range"))
    if requested_pages is None:
        return max(1, probed_pages)
    return max(1, min(requested_pages, max(1, probed_pages)))


def _pages_to_range(pages: list[int]) -> str:
    if not pages:
        return ""
    sorted_pages = sorted(pages)
    ranges: list[str] = []
    start = prev = sorted_pages[0]
    for page in sorted_pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = page
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _pages_to_marker_range(pages: list[int]) -> str:
    return _pages_to_range([page - 1 for page in pages if page > 0])


def _segment_engine_name(segment: PdfRoutingSegment) -> str:
    return "liteparse_pdf" if segment.engine == "liteparse" else "marker_pdf"


def _namespace_images(
    images: dict[str, Any],
    *,
    segment_index: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    namespaced: dict[str, Any] = {}
    name_map: dict[str, str] = {}
    for name, value in images.items():
        new_name = f"segment_{segment_index}_{name}"
        namespaced[new_name] = value
        name_map[name] = new_name
    return namespaced, name_map


def _rewrite_image_links(text: str, name_map: dict[str, str]) -> str:
    updated = text or ""
    for old_name, new_name in name_map.items():
        updated = updated.replace(f"]({old_name})", f"]({new_name})")
        updated = updated.replace(f'src="{old_name}"', f'src="{new_name}"')
        updated = updated.replace(f"src='{old_name}'", f"src='{new_name}'")
    return updated


def _mixed_pdf_routing_blocked_by_config(config: dict[str, Any]) -> list[str]:
    profile = str(config.get("conversion_profile") or config.get("profile") or "").lower()
    converter_cls = str(config.get("converter_cls") or "")
    image_mode = config.get("image_handling_mode")
    blockers: list[str] = []
    if config.get("engine_override"):
        blockers.append("engine override selected")
    if config.get("page_range"):
        blockers.append("explicit page range selected")
    if config.get("force_ocr"):
        blockers.append("force_ocr is enabled")
    if image_mode in {"understanding", "both"}:
        blockers.append("image understanding needs whole Marker context")
    if profile in {"fast", "fast_path", "fast-path"}:
        blockers.append("Fast profile selected")
    if profile in {"high_accuracy", "high-accuracy", "accuracy"}:
        blockers.append("High Accuracy profile selected")
    if converter_cls in {"TableConverter", "OCRConverter"}:
        blockers.append(f"{converter_cls} requires whole-file Marker")
    return blockers


def _mixed_pdf_plan(segments: list[PdfRoutingSegment], *, explicit: bool, probe: PdfProbeResult) -> ConverterPlan:
    page_summary = ", ".join(
        f"{_pages_to_range(segment.pages)}:{_segment_engine_name(segment)}"
        for segment in segments
    )
    reasons = [
        "PDF probe found page-level engine split",
        f"Full-page probe coverage confirmed ({probe.page_count}/{probe.page_count} pages)",
        f"Planned segments: {page_summary}",
    ]
    if explicit:
        reasons.insert(0, "Explicit mixed PDF routing enabled")
    return ConverterPlan(
        engine="mixed_pdf",
        label="Mixed PDF routing",
        confidence=0.86 if not explicit else 0.8,
        reasons=reasons,
        needs_marker_models=any(segment.engine == "marker" for segment in segments),
        needs_gpu=any(segment.engine == "marker" for segment in segments),
        execution_backend="marker_worker",
        needs_cloud=False,
        optional_dependencies=[],
        fallback_chain=[],
        warnings=[
            "Mixed PDF routing uses per-segment fallback and preserves page order"
        ],
    )


def _sampled_mixed_probe_warning(probe: PdfProbeResult) -> str:
    missing = missing_probe_pages(probe)
    missing_text = _pages_to_range(missing[:10])
    if len(missing) > 10:
        missing_text = f"{missing_text},..."
    return (
        "Mixed PDF routing skipped because probe was sampled, not full-page "
        f"({probe_coverage_label(probe)}; missing pages: {missing_text or 'none'}). "
        "Run a full-page probe before enabling mixed execution."
    )


def _segments_cover_all_pages(segments: list[PdfRoutingSegment], page_count: int) -> bool:
    seen: list[int] = []
    for segment in segments:
        seen.extend(segment.pages)
    return page_count > 0 and sorted(seen) == list(range(1, page_count + 1))


class ConversionService:
    """Orchestrates file conversion through the registry and router.

    Construction wires up the registry with all available converters.
    ``convert_file`` is the single entry point called by ``TaskManager``.
    """

    def __init__(self, marker_service: Any) -> None:
        self._marker_service = marker_service
        self._registry = ConverterRegistry()
        self._router = ConversionRouter()

        # Register Phase 0 converters.
        self._registry.register(MarkerPdfConverter(marker_service))
        self._registry.register(AudioConverter())
        self._registry.register(LiteParsePdfConverter())
        self._registry.register(OfficeDocxConverter(marker_service))
        self._registry.register(OfficePptxConverter(marker_service))
        self._registry.register(OutlookMsgConverter())
        self._registry.register(SpreadsheetConverter())
        self._registry.register(TextDataConverter())
        self._registry.register(VideoConverter())
        self._registry.register(XmlRssConverter())
        self._registry.register(HtmlConverter())
        self._registry.register(NotebookConverter())
        self._registry.register(ArchiveConverter())

    @property
    def registry(self) -> ConverterRegistry:
        """Expose registry for tests and capability queries."""
        return self._registry

    def _resolve_fallback(
        self,
        plan: ConverterPlan,
        stream_info: StreamInfo,
        config: dict[str, Any],
    ) -> ConverterPlan:
        """Fallback only when the planned engine has an explicit safe fallback."""
        if not self._registry.has(plan.engine):
            marker = self._registry.get("marker_pdf")
            if (
                plan.engine != "liteparse_pdf"
                or marker is None
                or not marker.accepts(stream_info, config)
            ):
                plan.warnings.append(
                    f"Converter for '{plan.engine}' not available in this build"
                )
                return plan
            return ConverterPlan(
                engine="marker_pdf",
                label=f"{plan.label} → Marker PDF (no converter registered)",
                confidence=min(plan.confidence, 0.5),
                reasons=plan.reasons + [
                    f"Engine '{plan.engine}' not registered; "
                    f"falling back to marker_pdf"
                ],
                needs_marker_models=True,
                needs_gpu=True,
                execution_backend="marker_worker",
                fallback_chain=[plan.engine, "marker_pdf"],
                warnings=plan.warnings + [
                    f"Converter for '{plan.engine}' not available in this build"
                ],
            )
        return plan

    def plan(self, filepath: str, config: dict[str, Any]) -> ConverterPlan:
        """Plan (but don't execute) a conversion — useful for the /plan API."""
        mixed_plan = self._mixed_pdf_plan_for_config(filepath, config)
        if mixed_plan is not None:
            return mixed_plan
        stream_info = StreamInfo.from_path(filepath)
        plan = self._router.plan(stream_info, config)
        plan = self._resolve_fallback(plan, stream_info, config)
        self._annotate_sampled_mixed_probe_skip(stream_info.extension, config, plan)
        return plan

    def plan_by_metadata(self, filename: str, size: int, config: dict[str, Any]) -> ConverterPlan:
        """Plan a conversion based on filename and size without local filesystem storage."""
        import mimetypes
        from pathlib import Path

        p = Path(filename)
        extension = p.suffix.lower() if p.suffix else ""
        mime_type, _ = mimetypes.guess_type(p.name)
        if not mime_type:
            mime_type = "application/octet-stream"

        stream_info = StreamInfo(
            path=filename,
            extension=extension,
            mime_type=mime_type,
            size=size,
            sample=b"",
        )
        plan = self._router.plan(stream_info, config)
        return self._resolve_fallback(plan, stream_info, config)


    def convert_file(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> dict[str, Any]:
        """Convert a file and return the legacy envelope dict.

        This is the drop-in replacement for ``marker_service.convert_file``
        that ``TaskManager._run_conversion`` calls.
        """
        if self._should_use_mixed_pdf_routing(filepath, config):
            return self._convert_mixed_pdf_segments(filepath, config, device=device)

        stream_info = StreamInfo.from_path(filepath)
        plan = self.plan(filepath, config)

        converter = self._registry.get(plan.engine)
        if converter is None:
            # Should not happen after plan() fallback, but defensive.
            raise RuntimeError(
                f"No converter registered for engine '{plan.engine}' "
                f"(file: {filepath})"
            )
        if not converter.accepts(stream_info, config):
            raise RuntimeError(
                f"Converter '{plan.engine}' does not accept extension "
                f"'{stream_info.extension}' (file: {filepath})"
            )
        requested_format = str(config.get("output_format") or "markdown").strip().lower()
        if requested_format in {"json", "html"} and not getattr(
            converter, "supports_multiple_formats", lambda: False
        )():
            raise UnsupportedFormatError(
                f"Output format '{requested_format}' is not supported for engine '{plan.engine}' on '{Path(filepath).name}'. "
                "Use markdown/chunks, or choose a Marker-backed PDF/image/EPUB route for json/html.",
                details={
                    "source": Path(filepath).name,
                    "engine": plan.engine,
                    "requested_formats": [requested_format],
                    "supported_formats": ["markdown", "chunks"],
                },
            )

        logger.info(
            "Converting '%s' with engine=%s (confidence=%.2f)",
            filepath,
            plan.engine,
            plan.confidence,
        )

        try:
            result = converter.convert(filepath, config, device=device)
        except Exception as exc:
            # Only LiteParse may retry through Marker, and only for PDF-like
            # streams Marker declares it can handle. Native parser failures keep
            # their original error instead of crossing into GPU/Marker work.
            if plan.engine == "marker_pdf":
                raise
            fb_converter = self._registry.get("marker_pdf")
            if (
                plan.engine != "liteparse_pdf"
                or fb_converter is None
                or not fb_converter.accepts(stream_info, config)
            ):
                raise
            logger.warning(
                "Engine '%s' failed on '%s' (%s); falling back to marker_pdf",
                plan.engine,
                filepath,
                exc,
            )
            fb_plan = ConverterPlan(
                engine="marker_pdf",
                label=f"{plan.label} -> Marker PDF (runtime fallback)",
                confidence=min(plan.confidence, 0.5),
                reasons=plan.reasons + [
                    f"Engine '{plan.engine}' raised {type(exc).__name__} at "
                    f"runtime; falling back to marker_pdf"
                ],
                needs_marker_models=True,
                needs_gpu=True,
                execution_backend="marker_worker",
                fallback_chain=[plan.engine, "marker_pdf"],
                warnings=plan.warnings + [f"Runtime fallback: {exc}"],
            )
            result = fb_converter.convert(filepath, config, device=device)
            result.metadata["engine"] = fb_plan.to_dict()
            probe_data = config.get("probe_result") if isinstance(config, dict) else None
            if isinstance(probe_data, dict):
                result.metadata["probe_result"] = probe_data
            return _envelope_for_requested_format(
                result.to_legacy_envelope(),
                filepath,
                requested_format,
                config,
            )

        if plan.engine == "liteparse_pdf":
            probe_data = config.get("probe_result") if isinstance(config, dict) else None
            page_count = _expected_liteparse_pages(config)
            min_chars = max(100, page_count * 100)
            if len((result.text or "").strip()) < min_chars:
                logger.warning(
                    "LiteParse output for '%s' looked too short (%d chars, %d pages); falling back to marker_pdf",
                    filepath,
                    len((result.text or "").strip()),
                    page_count,
                )
                fb_plan = ConverterPlan(
                    engine="marker_pdf",
                    label=f"{plan.label} -> Marker PDF (short-output fallback)",
                    confidence=min(plan.confidence, 0.5),
                    reasons=plan.reasons + [
                        "LiteParse returned suspiciously short output (<100 chars/page); falling back to marker_pdf"
                    ],
                    needs_marker_models=True,
                    needs_gpu=True,
                    execution_backend="marker_worker",
                    fallback_chain=["liteparse_pdf", "marker_pdf"],
                    warnings=plan.warnings + ["LiteParse output was too short"],
                )
                fb_converter = self._registry.get("marker_pdf")
                if fb_converter is None:
                    raise RuntimeError("marker_pdf fallback converter is not registered")
                result = fb_converter.convert(filepath, config, device=device)
                result.metadata["engine"] = fb_plan.to_dict()
                if isinstance(probe_data, dict):
                    result.metadata["probe_result"] = probe_data
                return _envelope_for_requested_format(
                    result.to_legacy_envelope(),
                    filepath,
                    requested_format,
                    config,
                )

        # Inject the plan into metadata so job status/history can show it.
        result.metadata["engine"] = plan.to_dict()
        probe_data = config.get("probe_result") if isinstance(config, dict) else None
        if isinstance(probe_data, dict):
            result.metadata["probe_result"] = probe_data

        return _envelope_for_requested_format(
            result.to_legacy_envelope(),
            filepath,
            requested_format,
            config,
        )

    # ------------------------------------------------------------------
    # Multi-format output
    # ------------------------------------------------------------------

    def supports_multiple_formats(self, filepath: str, config: dict[str, Any]) -> bool:
        """True when the resolved engine can render several formats from one parse.

        Marker-backed engines parse a Document and render N formats from it.
        Markdown-only native engines can also produce a derived ``chunks`` JSON
        artifact from their Markdown output. They still cannot produce real
        JSON/HTML renderers.
        """
        requested = _requested_formats(config)
        can_derive_chunks = bool(
            requested and set(requested).issubset({"markdown", "chunks"}) and "chunks" in requested
        )
        if self._should_use_mixed_pdf_routing(filepath, config):
            return can_derive_chunks
        plan = self.plan(filepath, config)
        converter = self._registry.get(plan.engine)
        if converter is None:
            return False
        stream_info = StreamInfo.from_path(filepath)
        if not converter.accepts(stream_info, config):
            return False
        if can_derive_chunks:
            return True
        return bool(getattr(converter, "supports_multiple_formats", lambda: False)())

    def convert_file_formats(
        self,
        filepath: str,
        config: dict[str, Any],
        formats: list[str],
        device: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Render multiple output formats and return ``{format: legacy_envelope}``.

        For marker-backed engines this is the single-parse / N-render path: marker
        builds the document once and each renderer consumes it. The primary format
        (``formats[0]``) drives the persisted file extension + images; the others
        are carried as text payloads in the returned envelopes and cached in the
        job's ``formats_json`` so preview tabs never reconvert.
        """
        if self._should_use_mixed_pdf_routing(filepath, config):
            # Mixed routing produces stitched Markdown; derive chunks if asked.
            envelope = self._convert_mixed_pdf_segments(filepath, config, device=device)
            return _derived_markdown_formats(envelope, filepath, formats, config=config)

        plan = self.plan(filepath, config)
        converter = self._registry.get(plan.engine)
        if converter is None or not getattr(converter, "supports_multiple_formats", lambda: False)():
            # Markdown-only engine: preserve Markdown and derive chunks if asked.
            envelope = self.convert_file(filepath, config, device=device)
            return _derived_markdown_formats(envelope, filepath, formats, config=config)

        requested_formats = _normalize_requested_formats(formats)
        derive_chunks = bool(config.get("chunking_strategy") and "chunks" in requested_formats)
        render_formats = requested_formats
        if derive_chunks:
            render_formats = [fmt for fmt in requested_formats if fmt != "chunks"]
            if "markdown" not in render_formats:
                render_formats.insert(0, "markdown")

        results = converter.convert_formats(filepath, config, render_formats, device=device)

        # Stamp the engine plan + probe into each format's metadata so the routing
        # analysis card renders identically regardless of which format a tab shows.
        probe_data = config.get("probe_result") if isinstance(config, dict) else None
        envelopes: dict[str, dict[str, Any]] = {}
        for fmt, result in results.items():
            result.metadata.setdefault("engine", plan.to_dict())
            if isinstance(probe_data, dict):
                result.metadata.setdefault("probe_result", probe_data)
            envelope = result.to_legacy_envelope()
            _validate_format_artifact(fmt, envelope, source=Path(filepath).name)
            envelopes[fmt] = envelope
        missing_formats = [fmt for fmt in render_formats if fmt not in envelopes]
        if missing_formats:
            raise RuntimeError(
                "Renderer did not produce requested output format(s) "
                f"{', '.join(missing_formats)} for '{Path(filepath).name}'."
            )
        if derive_chunks:
            markdown_envelope = envelopes.get("markdown")
            if markdown_envelope is None:
                markdown_result = converter.convert_formats(filepath, config, ["markdown"], device=device).get("markdown")
                if markdown_result is None:
                    raise RuntimeError("Markdown renderer produced no output for explicit chunking strategy.")
                markdown_result.metadata.setdefault("engine", plan.to_dict())
                if isinstance(probe_data, dict):
                    markdown_result.metadata.setdefault("probe_result", probe_data)
                markdown_envelope = markdown_result.to_legacy_envelope()
                _validate_format_artifact("markdown", markdown_envelope, source=Path(filepath).name)
            derived_chunks = _derived_markdown_formats(
                markdown_envelope,
                filepath,
                ["chunks"],
                config=config,
            )["chunks"]
            ordered: dict[str, dict[str, Any]] = {}
            for fmt in requested_formats:
                if fmt == "chunks":
                    ordered["chunks"] = derived_chunks
                elif fmt in envelopes:
                    ordered[fmt] = envelopes[fmt]
            return ordered
        return envelopes

    def _should_use_mixed_pdf_routing(self, filepath: str, config: dict[str, Any]) -> bool:
        return self._mixed_pdf_plan_for_config(filepath, config) is not None

    def _mixed_pdf_plan_for_config(
        self,
        filepath: str,
        config: dict[str, Any],
    ) -> ConverterPlan | None:
        explicit = bool(config.get("enable_mixed_pdf_routing"))
        if not explicit and _mixed_pdf_routing_blocked_by_config(config):
            return None
        stream_info = StreamInfo.from_path(filepath)
        if stream_info.extension.lower() != ".pdf":
            return None
        probe_data = config.get("probe_result") if isinstance(config, dict) else None
        if not isinstance(probe_data, dict) or not probe_data.get("page_results"):
            return None
        probe = PdfProbeResult.from_mapping(probe_data)
        segments = plan_pdf_routing_segments(probe)
        if len(segments) <= 1:
            return None
        if not probe_has_full_page_coverage(probe):
            return None
        if not _segments_cover_all_pages(segments, probe.page_count):
            return None
        return _mixed_pdf_plan(segments, explicit=explicit, probe=probe)

    def _annotate_sampled_mixed_probe_skip(
        self,
        extension: str,
        config: dict[str, Any],
        plan: ConverterPlan,
    ) -> None:
        if extension.lower() != ".pdf":
            return
        probe_data = config.get("probe_result") if isinstance(config, dict) else None
        if not isinstance(probe_data, dict) or not probe_data.get("page_results"):
            return
        probe = PdfProbeResult.from_mapping(probe_data)
        segments = plan_pdf_routing_segments(probe)
        if len(segments) <= 1 or probe_has_full_page_coverage(probe):
            return
        warning = _sampled_mixed_probe_warning(probe)
        if warning not in plan.warnings:
            plan.warnings.append(warning)
        reason = "Sampled page-level probe is not eligible for mixed PDF execution"
        if reason not in plan.reasons:
            plan.reasons.append(reason)

    def _convert_segment(
        self,
        filepath: str,
        config: dict[str, Any],
        segment: PdfRoutingSegment,
        *,
        device: str | None,
    ) -> tuple[UniversalConversionResult, dict[str, Any]]:
        engine = _segment_engine_name(segment)
        converter = self._registry.get(engine)
        if converter is None:
            engine = "marker_pdf"
            converter = self._registry.get(engine)
        if converter is None:
            raise RuntimeError("marker_pdf fallback converter is not registered")

        page_range = _pages_to_range(segment.pages)
        requested_engine = _segment_engine_name(segment)

        def config_for_engine(engine_name: str) -> dict[str, Any]:
            segment_config = dict(config)
            segment_config["page_range"] = (
                _pages_to_marker_range(segment.pages)
                if engine_name == "marker_pdf"
                else page_range
            )
            segment_config["mixed_pdf_segment"] = {
                "pages": list(segment.pages),
                "requested_engine": requested_engine,
            }
            return segment_config

        actual_engine = engine
        fallback_reason: str | None = None
        try:
            result = converter.convert(filepath, config_for_engine(engine), device=device)
        except Exception as exc:
            if engine == "marker_pdf":
                raise
            fallback_reason = f"{engine} raised {type(exc).__name__}; retried segment with marker_pdf"
            actual_engine = "marker_pdf"
            marker = self._registry.get("marker_pdf")
            if marker is None:
                raise RuntimeError("marker_pdf fallback converter is not registered") from exc
            result = marker.convert(filepath, config_for_engine("marker_pdf"), device=device)

        if actual_engine == "liteparse_pdf":
            expected_pages = max(1, len(segment.pages))
            min_chars = max(100, expected_pages * 100)
            if len((result.text or "").strip()) < min_chars:
                fallback_reason = "LiteParse returned suspiciously short segment output"
                actual_engine = "marker_pdf"
                marker = self._registry.get("marker_pdf")
                if marker is None:
                    raise RuntimeError("marker_pdf fallback converter is not registered")
                result = marker.convert(filepath, config_for_engine("marker_pdf"), device=device)

        segment_meta = {
            "pages": list(segment.pages),
            "page_range": page_range,
            "requested_engine": requested_engine,
            "actual_engine": actual_engine,
            "reasons": list(segment.reasons),
            "fallback_chain": (
                [requested_engine, "marker_pdf"]
                if fallback_reason
                else [requested_engine, "marker_pdf"]
                if requested_engine == "liteparse_pdf"
                else []
            ),
            "fallback_reason": fallback_reason,
        }
        return result, segment_meta

    def _convert_mixed_pdf_segments(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> dict[str, Any]:
        probe_data = config.get("probe_result")
        probe = PdfProbeResult.from_mapping(probe_data)
        segments = plan_pdf_routing_segments(probe)
        if not probe_has_full_page_coverage(probe):
            raise RuntimeError(_sampled_mixed_probe_warning(probe))
        if not _segments_cover_all_pages(segments, probe.page_count):
            raise RuntimeError("Mixed PDF routing requires segments to cover every page exactly once")
        if len(segments) <= 1:
            return self.convert_file(
                filepath,
                {k: v for k, v in config.items() if k != "enable_mixed_pdf_routing"},
                device=device,
            )

        text_parts: list[str] = []
        images: dict[str, Any] = {}
        segment_metadata: list[dict[str, Any]] = []
        for index, segment in enumerate(segments, start=1):
            result, segment_meta = self._convert_segment(
                filepath,
                config,
                segment,
                device=device,
            )
            namespaced_images, name_map = _namespace_images(result.images, segment_index=index)
            images.update(namespaced_images)
            text = _rewrite_image_links(result.text, name_map).strip()
            if text:
                text_parts.append(f"<!-- pages: {segment_meta['page_range']} -->\n\n{text}")
            segment_meta["image_names"] = sorted(namespaced_images)
            segment_meta["metadata"] = result.metadata
            segment_metadata.append(segment_meta)

        plan = _mixed_pdf_plan(segments, explicit=bool(config.get("enable_mixed_pdf_routing")), probe=probe)
        engine_meta = {
            "engine": plan.engine,
            "label": plan.label,
            "confidence": plan.confidence,
            "reasons": plan.reasons,
            "needs_marker_models": any(
                item["actual_engine"] == "marker_pdf" for item in segment_metadata
            ),
            "needs_gpu": any(item["actual_engine"] == "marker_pdf" for item in segment_metadata),
            "execution_backend": plan.execution_backend,
            "needs_cloud": plan.needs_cloud,
            "optional_dependencies": plan.optional_dependencies,
            "fallback_chain": plan.fallback_chain,
            "warnings": plan.warnings,
        }
        merged_text = "\n\n".join(text_parts)
        metadata = attach_table_evidence(
            {
                "engine": engine_meta,
                "probe_result": probe.to_dict(),
                "mixed_engine_segments": segment_metadata,
            },
            merged_text,
        )
        return {
            "text": merged_text,
            "extension": "md",
            "images": images,
            "metadata": metadata,
        }


def _requested_formats(config: dict[str, Any]) -> list[str]:
    raw = config.get("output_formats")
    if isinstance(raw, list) and raw:
        return [str(fmt).strip().lower() for fmt in raw if fmt]
    return [str(config.get("output_format") or "markdown").strip().lower()]


def _normalize_requested_formats(formats: list[str]) -> list[str]:
    return [fmt for fmt in dict.fromkeys(str(fmt).strip().lower() for fmt in formats) if fmt]


def _raise_unsupported_markdown_derivation(
    filepath: str,
    formats: list[str],
    *,
    engine: str,
) -> None:
    unsupported = [fmt for fmt in formats if fmt not in _DERIVABLE_MARKDOWN_FORMATS]
    if not unsupported:
        return
    requested = ", ".join(unsupported)
    raise UnsupportedFormatError(
        f"Output format(s) {requested} are not supported for engine '{engine}' on '{Path(filepath).name}'. "
        "Markdown-only converters can produce markdown and derived chunks only.",
        details={
            "source": Path(filepath).name,
            "engine": engine,
            "requested_formats": formats,
            "supported_formats": sorted(_DERIVABLE_MARKDOWN_FORMATS),
        },
    )


def _validate_format_artifact(
    fmt: str,
    envelope: dict[str, Any],
    *,
    source: str,
) -> None:
    expected = _EXPECTED_FORMAT_EXTENSIONS.get(fmt)
    if expected is None:
        return
    extension = str(envelope.get("extension") or "").strip().lower().lstrip(".")
    if extension not in expected:
        expected_label = "/".join(f".{ext}" for ext in sorted(expected))
        got_label = f".{extension}" if extension else "<none>"
        raise RuntimeError(
            f"Renderer produced {got_label} for output_format '{fmt}' on '{source}', "
            f"expected {expected_label}."
        )
    if fmt in {"json", "chunks"}:
        text = str(envelope.get("text") or "")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Renderer produced invalid JSON for output_format '{fmt}' on '{source}'."
            ) from exc


def _envelope_for_requested_format(
    envelope: dict[str, Any],
    filepath: str,
    requested_format: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    envelope_extension = str(envelope.get("extension") or "").lstrip(".").lower()
    if requested_format == "chunks" and envelope_extension in {"md", "markdown"}:
        envelope = build_chunks_envelope(
            str(envelope.get("text") or ""),
            source_name=Path(filepath).name,
            metadata=dict(envelope.get("metadata") or {}),
            strategy=str(config.get("chunking_strategy") or "markdown_heading_blocks_v2"),
        )
    if requested_format in _EXPECTED_FORMAT_EXTENSIONS:
        _validate_format_artifact(requested_format, envelope, source=Path(filepath).name)
    return envelope


def _derived_markdown_formats(
    markdown_envelope: dict[str, Any],
    filepath: str,
    formats: list[str],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    requested = _normalize_requested_formats(formats)
    engine = (
        (markdown_envelope.get("metadata") or {}).get("engine", {}).get("engine")
        if isinstance(markdown_envelope.get("metadata"), dict)
        and isinstance((markdown_envelope.get("metadata") or {}).get("engine"), dict)
        else "markdown"
    )
    _raise_unsupported_markdown_derivation(filepath, requested, engine=str(engine or "markdown"))
    text = str(markdown_envelope.get("text") or "")
    for fmt in requested:
        if fmt == "markdown":
            outputs["markdown"] = markdown_envelope
        elif fmt == "chunks":
            outputs["chunks"] = build_chunks_envelope(
                text,
                source_name=Path(filepath).name,
                metadata=dict(markdown_envelope.get("metadata") or {}),
                strategy=str((config or {}).get("chunking_strategy") or "markdown_heading_blocks_v2"),
            )
            _validate_format_artifact("chunks", outputs["chunks"], source=Path(filepath).name)
    if not outputs:
        outputs["markdown"] = markdown_envelope
    if "markdown" in outputs:
        _validate_format_artifact("markdown", outputs["markdown"], source=Path(filepath).name)
    return outputs
