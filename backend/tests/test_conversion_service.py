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

    def test_unregistered_engine_falls_back_to_marker(self, tmp_path: Any) -> None:
        """If router picks an engine with no registered converter, fall back to marker_pdf."""
        svc, fake_ms = self._make_service()
        # Unregister office_docx to test fallback
        svc.registry.unregister("office_docx")
        
        docx_path = tmp_path / "test.docx"
        docx_path.write_bytes(b"PK docx content")

        result = svc.convert_file(str(docx_path), {})

        # Should fall back to marker_pdf
        engine_meta = result["metadata"]["engine"]
        assert engine_meta["engine"] == "marker_pdf"
        assert "fallback" in engine_meta["label"].lower() or "no converter" in engine_meta["label"].lower()
        assert len(engine_meta["warnings"]) > 0
        assert engine_meta["confidence"] <= 0.5

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
        # Unregister office_docx to test fallback
        svc.registry.unregister("office_docx")
        
        docx_path = tmp_path / "report.docx"
        docx_path.write_bytes(b"PK")

        plan = svc.plan(str(docx_path), {})

        # Falls back to marker_pdf since office_docx isn't registered
        assert plan.engine == "marker_pdf"
        assert len(plan.fallback_chain) == 2
        assert plan.fallback_chain == ["office_docx", "marker_pdf"]

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

    def test_runtime_fallback_to_marker_when_converter_raises(self, tmp_path: Any) -> None:
        """BUG-B: a failing office converter falls back to marker_pdf at runtime."""
        svc, fake_ms = self._make_service()
        docx_path = tmp_path / "corrupt.docx"
        docx_path.write_bytes(b"PK not really a docx")

        # Make the office_docx converter raise at runtime (e.g. BadZipFile).
        office = svc.registry.get("office_docx")
        assert office is not None
        original = office.convert

        def raising_convert(filepath, config, device=None):
            raise RuntimeError("simulated BadZipFile")

        office.convert = raising_convert  # type: ignore[assignment]
        try:
            result = svc.convert_file(str(docx_path), {})
        finally:
            office.convert = original  # type: ignore[assignment]

        # The runtime fallback retried via marker_pdf (the fake marker service).
        assert len(fake_ms.convert_calls) == 1
        engine_meta = result["metadata"]["engine"]
        assert engine_meta["engine"] == "marker_pdf"
        assert "runtime fallback" in engine_meta["label"].lower()
        assert engine_meta["fallback_chain"] == ["office_docx", "marker_pdf"]

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
