"""Tests for ConversionService — orchestrator integration.

Verifies the full pipeline: StreamInfo → Router → Registry → Converter,
returning the legacy envelope dict. Uses a fake MarkerService so no GPU
or model loads occur.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import pytest

from app.services.conversion_service import ConversionService


class _FakeMarkerServiceForConversion:
    """Minimal fake that returns a deterministic result envelope."""

    def __init__(self) -> None:
        self._initialized = False
        self.convert_calls: list[tuple[str, dict]] = []
        self.convert_format_calls: list[tuple[str, list[str], dict]] = []

    def initialize(self) -> None:
        self._initialized = True

    def convert_file(
        self, filepath: str, options: dict[str, Any], device: str | None = None
    ) -> dict[str, Any]:
        self.convert_calls.append((filepath, options))
        return {
            "text": "# Fake PDF Output\n\nConverted.",
            "extension": "md",
            "images": {},
            "metadata": {"pages": 2},
        }

    def convert_file_formats(
        self,
        filepath: str,
        options: dict[str, Any],
        formats: list[str],
        device: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        self.convert_format_calls.append((filepath, list(formats), dict(options)))
        payloads = {
            "markdown": {
                "text": "# Fake PDF Output\n\nConverted.",
                "extension": "md",
                "images": {},
                "metadata": {"pages": 2},
            },
            "json": {
                "text": '{"document": true}',
                "extension": "json",
                "images": {},
                "metadata": {"pages": 2},
            },
            "chunks": {
                "text": '{"marker_native_chunks": true}',
                "extension": "json",
                "images": {},
                "metadata": {"pages": 2},
            },
        }
        return {fmt: payloads[fmt] for fmt in formats if fmt in payloads}


class TestConversionService:
    """ConversionService orchestration tests."""

    def _make_service(self) -> tuple[ConversionService, _FakeMarkerServiceForConversion]:
        fake_ms = _FakeMarkerServiceForConversion()
        svc = ConversionService(fake_ms)
        return svc, fake_ms

    def test_convert_pdf_returns_legacy_envelope(self, tmp_path: Any) -> None:
        """PDF file → legacy {text, extension, images, metadata} dict."""
        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake content")

        result = svc.convert_file(str(pdf_path), {})

        # Legacy envelope keys
        assert "text" in result
        assert "extension" in result
        assert "images" in result
        assert "metadata" in result
        assert result["text"] == "# Fake PDF Output\n\nConverted."
        assert result["extension"] == "md"

    def test_convert_pdf_delegates_to_marker_service(self, tmp_path: Any) -> None:
        """ConversionService delegates PDF to MarkerPdfConverter → MarkerService."""
        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "document.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")

        svc.convert_file(str(pdf_path), {"output_format": "markdown"})

        assert len(fake_ms.convert_calls) == 1
        called_path, called_config = fake_ms.convert_calls[0]
        assert called_path == str(pdf_path)

    def test_convert_image_routes_to_marker(self, tmp_path: Any) -> None:
        """Image files route to marker_pdf engine."""
        svc, fake_ms = self._make_service()
        for ext in [".jpg", ".png", ".webp"]:
            img_path = tmp_path / f"test{ext}"
            img_path.write_bytes(b"\x89PNG\r\n")

            result = svc.convert_file(str(img_path), {})
            assert result["text"] == "# Fake PDF Output\n\nConverted."

        assert len(fake_ms.convert_calls) == 3

    def test_convert_epub_routes_to_marker(self, tmp_path: Any) -> None:
        """EPUB routes to marker_pdf engine."""
        svc, fake_ms = self._make_service()
        epub_path = tmp_path / "book.epub"
        epub_path.write_bytes(b"PK epub content")

        result = svc.convert_file(str(epub_path), {})
        assert result["text"] == "# Fake PDF Output\n\nConverted."

    def test_metadata_includes_engine_plan(self, tmp_path: Any) -> None:
        """Result metadata contains the engine plan for UI display."""
        svc, _ = self._make_service()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF")

        result = svc.convert_file(str(pdf_path), {})
        engine_meta = result["metadata"]["engine"]

        assert engine_meta["engine"] == "marker_pdf"
        assert engine_meta["label"] == "Marker PDF"
        assert engine_meta["confidence"] == 0.75
        assert engine_meta["needs_marker_models"] is True
        assert isinstance(engine_meta["reasons"], list)

    def test_native_converter_derives_chunks_from_markdown_output(self, tmp_path: Any) -> None:
        svc, fake_ms = self._make_service()
        source = tmp_path / "scores.tsv"
        source.write_text("name\tscore\nAda\t10\nGrace\t11\n", encoding="utf-8")
        config = {"output_format": "chunks", "output_formats": ["chunks"]}

        assert svc.supports_multiple_formats(str(source), config) is True
        result = svc.convert_file_formats(str(source), config, ["chunks"])

        assert len(fake_ms.convert_calls) == 0
        assert set(result) == {"chunks"}
        assert result["chunks"]["extension"] == "json"
        payload = json.loads(result["chunks"]["text"])
        assert payload["schema_version"] == "marker.chunks.v1"
        assert payload["chunk_kind"] == "semantic_markdown"
        assert payload["source"]["name"] == "scores.tsv"
        assert payload["chunk_count"] >= 1
        assert "| Ada | 10 |" in payload["chunks"][-1]["text"]
        assert result["chunks"]["metadata"]["chunking"]["chunk_kind"] == "semantic_markdown"
        assert result["chunks"]["metadata"]["chunking"]["chunking_strategy"] == "markdown_heading_blocks_v2"

    def test_native_derived_chunks_honor_explicit_chunking_strategy(self, tmp_path: Any) -> None:
        pytest.importorskip("unstructured.partition.md")
        pytest.importorskip("unstructured.chunking.title")
        svc, fake_ms = self._make_service()
        source = tmp_path / "notes.md"
        source.write_text("# Title\n\nIntro paragraph.\n\n## Details\n\nFirst fact.", encoding="utf-8")
        config = {
            "output_format": "chunks",
            "output_formats": ["chunks"],
            "chunking_strategy": "unstructured_by_title",
        }

        result = svc.convert_file_formats(str(source), config, ["chunks"])

        assert len(fake_ms.convert_calls) == 0
        payload = json.loads(result["chunks"]["text"])
        assert payload["chunking_strategy"] == "unstructured_by_title"
        assert result["chunks"]["metadata"]["chunking"]["chunking_strategy"] == "unstructured_by_title"
        assert result["chunks"]["metadata"]["chunking"]["requested_strategy"] == "unstructured_by_title"

    def test_marker_chunks_honor_explicit_chunking_strategy_by_deriving_from_markdown(self, tmp_path: Any) -> None:
        svc, fake_ms = self._make_service()
        source = tmp_path / "paper.pdf"
        source.write_bytes(b"%PDF")
        config = {
            "output_format": "chunks",
            "output_formats": ["json", "chunks"],
            "chunking_strategy": "markdown_heading_blocks_v2",
        }

        result = svc.convert_file_formats(str(source), config, ["json", "chunks"])

        assert [call[1] for call in fake_ms.convert_format_calls] == [["markdown", "json"]]
        assert set(result) == {"json", "chunks"}
        assert result["json"]["text"] == '{"document": true}'
        payload = json.loads(result["chunks"]["text"])
        assert payload["schema_version"] == "marker.chunks.v1"
        assert payload["source"]["name"] == "paper.pdf"
        assert result["chunks"]["metadata"]["chunking"]["requested_strategy"] == "markdown_heading_blocks_v2"

    def test_unknown_extension_does_not_claim_derived_chunks_support(self, tmp_path: Any) -> None:
        """Derived chunks still require a converter that accepts the source."""
        svc, _fake_ms = self._make_service()
        source = tmp_path / "unknown.binpack"
        source.write_text("payload", encoding="utf-8")

        assert (
            svc.supports_multiple_formats(
                str(source),
                {"output_format": "chunks", "output_formats": ["chunks"]},
            )
            is False
        )

    def test_unregistered_native_engine_does_not_fall_back_to_marker(self, tmp_path: Any) -> None:
        """Missing native converters fail directly instead of crossing into Marker."""
        svc, fake_ms = self._make_service()
        svc.registry.unregister("office_docx")

        docx_path = tmp_path / "test.docx"
        docx_path.write_bytes(b"PK docx content")

        with pytest.raises(RuntimeError, match="No converter registered for engine 'office_docx'"):
            svc.convert_file(str(docx_path), {})

        assert fake_ms.convert_calls == []

    def test_plan_method_returns_converter_plan(self, tmp_path: Any) -> None:
        """plan() returns a ConverterPlan without executing conversion."""
        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF")

        plan = svc.plan(str(pdf_path), {})

        assert plan.engine == "marker_pdf"
        assert plan.needs_marker_models is True
        assert plan.confidence == 0.75
        # plan() should NOT trigger conversion
        assert len(fake_ms.convert_calls) == 0

    def test_plan_for_unregistered_engine(self, tmp_path: Any) -> None:
        """plan() for unregistered engine shows fallback info."""
        svc, _ = self._make_service()
        svc.registry.unregister("office_docx")
        
        docx_path = tmp_path / "report.docx"
        docx_path.write_bytes(b"PK")

        plan = svc.plan(str(docx_path), {})

        assert plan.engine == "office_docx"
        assert plan.fallback_chain == []
        assert "not available" in plan.warnings[0]

    def test_plan_for_unregistered_liteparse_pdf_falls_back_to_marker(self, tmp_path: Any) -> None:
        """PDF fast-path fallback remains available because Marker accepts PDFs."""
        svc, _ = self._make_service()
        svc.registry.unregister("liteparse_pdf")

        pdf_path = tmp_path / "clean.pdf"
        pdf_path.write_bytes(b"%PDF")
        plan = svc.plan(
            str(pdf_path),
            {
                "probe_result": {
                    "page_count": 1,
                    "text_layer_score": 0.9,
                    "text_quality_score": 0.95,
                    "scan_likelihood": 0.0,
                    "sandwich_likelihood": 0.0,
                    "layout_complexity_score": 0.0,
                    "visual_complexity_score": 0.0,
                    "recommended_engine": "liteparse",
                    "reasons": ["strong extractable text layer"],
                    "sampled_image_count": 0,
                }
            },
        )

        assert plan.engine == "marker_pdf"
        assert plan.fallback_chain == ["liteparse_pdf", "marker_pdf"]

    def test_registry_has_marker_pdf(self) -> None:
        """Registry contains marker_pdf after construction."""
        svc, _ = self._make_service()
        assert svc.registry.has("marker_pdf")
        assert "marker_pdf" in svc.registry.engine_names

    def test_convert_file_config_passthrough(self, tmp_path: Any) -> None:
        """Config dict is passed through to the underlying converter."""
        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF")

        config = {"output_format": "json", "force_ocr": True}
        svc.convert_file(str(pdf_path), config)

        _, called_config = fake_ms.convert_calls[0]
        assert called_config["output_format"] == "json"
        assert called_config["force_ocr"] is True

    def test_legacy_envelope_serializable(self, tmp_path: Any) -> None:
        """Legacy envelope is JSON-serializable (no PIL objects in fake)."""
        svc, _ = self._make_service()
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF")

        result = svc.convert_file(str(pdf_path), {})

        # Must not raise — result is plain dict with JSON-safe values
        serialized = json.dumps(result, default=str)
        assert '"text"' in serialized

    def test_native_runtime_failure_does_not_fallback_to_marker(self, tmp_path: Any) -> None:
        """A failing native converter preserves its own error and does not invoke Marker."""
        svc, fake_ms = self._make_service()
        docx_path = tmp_path / "corrupt.docx"
        docx_path.write_bytes(b"PK not really a docx")

        office = svc.registry.get("office_docx")
        assert office is not None
        original = office.convert

        def raising_convert(filepath, config, device=None):
            raise RuntimeError("simulated BadZipFile")

        office.convert = raising_convert  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="simulated BadZipFile"):
                svc.convert_file(str(docx_path), {})
        finally:
            office.convert = original  # type: ignore[assignment]

        assert fake_ms.convert_calls == []

    def test_liteparse_runtime_fallback_preserves_probe_metadata(self, tmp_path: Any) -> None:
        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "clean.pdf"
        pdf_path.write_bytes(b"%PDF")
        config = {
            "probe_result": {
                "page_count": 2,
                "text_layer_score": 0.9,
                "text_quality_score": 0.95,
                "scan_likelihood": 0.05,
                "sandwich_likelihood": 0.1,
                "layout_complexity_score": 0.1,
                "visual_complexity_score": 0.0,
                "recommended_engine": "liteparse",
                "reasons": ["strong extractable text layer"],
                "sampled_image_count": 0,
            }
        }

        liteparse = svc.registry.get("liteparse_pdf")
        assert liteparse is not None
        original = liteparse.convert

        def raising_convert(filepath, config, device=None):
            raise RuntimeError("simulated liteparse failure")

        liteparse.convert = raising_convert  # type: ignore[assignment]
        try:
            result = svc.convert_file(str(pdf_path), config)
        finally:
            liteparse.convert = original  # type: ignore[assignment]

        assert len(fake_ms.convert_calls) == 1
        assert result["metadata"]["probe_result"]["page_count"] == 2
        assert result["metadata"]["engine"]["fallback_chain"] == ["liteparse_pdf", "marker_pdf"]

    def test_liteparse_short_output_falls_back_to_marker(self, tmp_path: Any) -> None:
        """LiteParse fast path retries Marker when output is too short."""
        from app.conversion.result import UniversalConversionResult

        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "clean.pdf"
        pdf_path.write_bytes(b"%PDF")
        config = {
            "probe_result": {
                "page_count": 3,
                "text_layer_score": 0.9,
                "text_quality_score": 0.95,
                "scan_likelihood": 0.05,
                "sandwich_likelihood": 0.1,
                "layout_complexity_score": 0.1,
                "visual_complexity_score": 0.0,
                "recommended_engine": "liteparse",
                "reasons": ["strong extractable text layer"],
                "sampled_image_count": 0,
            }
        }

        liteparse = svc.registry.get("liteparse_pdf")
        assert liteparse is not None
        original = liteparse.convert

        def short_convert(filepath, config, device=None):
            return UniversalConversionResult(text="too short", extension="md")

        liteparse.convert = short_convert  # type: ignore[assignment]
        try:
            result = svc.convert_file(str(pdf_path), config)
        finally:
            liteparse.convert = original  # type: ignore[assignment]

        assert len(fake_ms.convert_calls) == 1
        engine_meta = result["metadata"]["engine"]
        assert engine_meta["engine"] == "marker_pdf"
        assert "short-output fallback" in engine_meta["label"]
        assert engine_meta["fallback_chain"] == ["liteparse_pdf", "marker_pdf"]

    def test_liteparse_short_output_threshold_respects_page_range(self, tmp_path: Any) -> None:
        """A one-page range from a long PDF should not require full-doc output length."""
        from app.conversion.result import UniversalConversionResult

        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "clean.pdf"
        pdf_path.write_bytes(b"%PDF")
        config = {
            "page_range": "3",
            "probe_result": {
                "page_count": 25,
                "text_layer_score": 0.9,
                "text_quality_score": 0.95,
                "scan_likelihood": 0.05,
                "sandwich_likelihood": 0.1,
                "layout_complexity_score": 0.1,
                "visual_complexity_score": 0.0,
                "recommended_engine": "liteparse",
                "reasons": ["strong extractable text layer"],
                "sampled_image_count": 0,
            },
        }

        liteparse = svc.registry.get("liteparse_pdf")
        assert liteparse is not None
        original = liteparse.convert

        def range_convert(filepath, config, device=None):
            return UniversalConversionResult(text="x" * 150, extension="md")

        liteparse.convert = range_convert  # type: ignore[assignment]
        try:
            result = svc.convert_file(str(pdf_path), config)
        finally:
            liteparse.convert = original  # type: ignore[assignment]

        assert len(fake_ms.convert_calls) == 0
        assert result["metadata"]["engine"]["engine"] == "liteparse_pdf"

    def test_auto_mixed_pdf_routing_skips_when_engine_override_set(self, tmp_path: Any) -> None:
        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "mixed.pdf"
        pdf_path.write_bytes(b"%PDF")
        config = {
            "engine_override": "marker_pdf",
            "probe_result": _mixed_probe_result(),
        }

        result = svc.convert_file(str(pdf_path), config)

        assert result["metadata"]["engine"]["engine"] == "marker_pdf"
        assert "mixed_engine_segments" not in result["metadata"]
        assert len(fake_ms.convert_calls) == 1

    def test_auto_mixed_pdf_routing_runs_segments_and_merges_metadata(self, tmp_path: Any) -> None:
        from app.conversion.result import UniversalConversionResult

        svc, fake_ms = self._make_service()
        pdf_path = tmp_path / "mixed.pdf"
        pdf_path.write_bytes(b"%PDF")
        config = {
            "probe_result": _mixed_probe_result(),
        }
        liteparse = svc.registry.get("liteparse_pdf")
        marker = svc.registry.get("marker_pdf")
        assert liteparse is not None
        assert marker is not None
        liteparse_calls: list[dict[str, Any]] = []
        marker_calls: list[dict[str, Any]] = []
        original_liteparse = liteparse.convert
        original_marker = marker.convert

        def liteparse_convert(filepath, config, device=None):
            liteparse_calls.append(dict(config))
            return UniversalConversionResult(
                text="liteparse segment output " * 20,
                extension="md",
                metadata={"segment_engine": "liteparse"},
            )

        def marker_convert(filepath, config, device=None):
            marker_calls.append(dict(config))
            return UniversalConversionResult(
                text=(
                    "marker segment output\n\n"
                    "| Quarter | Revenue | Cost |\n"
                    "| --- | --- | --- |\n"
                    "| Q1 | 100 | 40 |"
                ),
                extension="md",
                images={"image.png": b"data"},
                metadata={"segment_engine": "marker"},
            )

        liteparse.convert = liteparse_convert  # type: ignore[assignment]
        marker.convert = marker_convert  # type: ignore[assignment]
        try:
            result = svc.convert_file(str(pdf_path), config)
        finally:
            liteparse.convert = original_liteparse  # type: ignore[assignment]
            marker.convert = original_marker  # type: ignore[assignment]

        assert len(fake_ms.convert_calls) == 0
        assert [call["page_range"] for call in liteparse_calls] == ["1-2"]
        assert [call["page_range"] for call in marker_calls] == ["2"]
        assert "<!-- pages: 1-2 -->" in result["text"]
        assert "<!-- pages: 3 -->" in result["text"]
        assert "segment_2_image.png" in result["images"]
        assert result["metadata"]["engine"]["engine"] == "mixed_pdf"
        assert result["metadata"]["table"] == {
            "headers": ["Quarter", "Revenue", "Cost"],
            "rows": [["Q1", "100", "40"]],
        }
        assert result["metadata"]["table_evidence"]["table_count"] == 1
        assert "PDF probe found page-level engine split" in result["metadata"]["engine"]["reasons"]
        segments = result["metadata"]["mixed_engine_segments"]
        assert [(item["page_range"], item["actual_engine"]) for item in segments] == [
            ("1-2", "liteparse_pdf"),
            ("3", "marker_pdf"),
        ]

    def test_mixed_pdf_plan_reports_segments_for_auto_pdf(self, tmp_path: Any) -> None:
        svc, _fake_ms = self._make_service()
        pdf_path = tmp_path / "mixed.pdf"
        pdf_path.write_bytes(b"%PDF")

        plan = svc.plan(str(pdf_path), {"probe_result": _mixed_probe_result()})

        assert plan.engine == "mixed_pdf"
        assert plan.label == "Mixed PDF routing"
        assert plan.needs_marker_models is True
        assert "1-2:liteparse_pdf" in plan.reasons[-1]
        assert "3:marker_pdf" in plan.reasons[-1]

    def test_mixed_pdf_routing_falls_back_only_short_liteparse_segment(
        self,
        tmp_path: Any,
    ) -> None:
        from app.conversion.result import UniversalConversionResult

        svc, _fake_ms = self._make_service()
        pdf_path = tmp_path / "mixed.pdf"
        pdf_path.write_bytes(b"%PDF")
        config = {
            "enable_mixed_pdf_routing": True,
            "probe_result": _mixed_probe_result(first_segment_pages=[1]),
        }
        liteparse = svc.registry.get("liteparse_pdf")
        marker = svc.registry.get("marker_pdf")
        assert liteparse is not None
        assert marker is not None
        marker_ranges: list[str] = []
        original_liteparse = liteparse.convert
        original_marker = marker.convert

        def short_liteparse(filepath, config, device=None):
            return UniversalConversionResult(text="short", extension="md")

        def marker_convert(filepath, config, device=None):
            marker_ranges.append(config["page_range"])
            return UniversalConversionResult(
                text=f"marker output for {config['page_range']} " + ("x" * 120),
                extension="md",
            )

        liteparse.convert = short_liteparse  # type: ignore[assignment]
        marker.convert = marker_convert  # type: ignore[assignment]
        try:
            result = svc.convert_file(str(pdf_path), config)
        finally:
            liteparse.convert = original_liteparse  # type: ignore[assignment]
            marker.convert = original_marker  # type: ignore[assignment]

        assert marker_ranges == ["0", "1-2"]
        segments = result["metadata"]["mixed_engine_segments"]
        assert segments[0]["requested_engine"] == "liteparse_pdf"
        assert segments[0]["actual_engine"] == "marker_pdf"
        assert "short" in segments[0]["fallback_reason"].lower()
        assert segments[1]["actual_engine"] == "marker_pdf"


def _mixed_probe_result(first_segment_pages: list[int] | None = None) -> dict[str, Any]:
    first_segment_pages = first_segment_pages or [1, 2]
    page_results: list[dict[str, Any]] = []
    for page in first_segment_pages:
        page_results.append(
            {
                "page_number": page,
                "text_layer_score": 0.9,
                "text_quality_score": 1.0,
                "scan_likelihood": 0.0,
                "sandwich_likelihood": 0.0,
                "layout_complexity_score": 0.0,
                "visual_complexity_score": 0.0,
                "recommended_engine": "liteparse",
                "reasons": ["LiteParse fast path is safe"],
                "text_chars": 1000,
                "image_count": 0,
                "full_page_image": False,
            }
        )
    for page in [p for p in [1, 2, 3] if p not in first_segment_pages]:
        page_results.append(
            {
                "page_number": page,
                "text_layer_score": 0.0,
                "text_quality_score": 0.0,
                "scan_likelihood": 1.0,
                "sandwich_likelihood": 0.8,
                "layout_complexity_score": 0.0,
                "visual_complexity_score": 0.9,
                "recommended_engine": "marker",
                "reasons": ["scan likelihood is high"],
                "text_chars": 0,
                "image_count": 1,
                "full_page_image": True,
            }
        )
    return {
        "page_count": 3,
        "text_layer_score": 0.5,
        "text_quality_score": 0.7,
        "scan_likelihood": 0.5,
        "sandwich_likelihood": 0.5,
        "layout_complexity_score": 0.0,
        "visual_complexity_score": 0.5,
        "recommended_engine": "marker",
        "reasons": ["mixed page risk"],
        "sampled_image_count": 1,
        "page_results": sorted(page_results, key=lambda item: item["page_number"]),
    }
