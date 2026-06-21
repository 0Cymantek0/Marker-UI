"""Unit tests for the OfficeDocxConverter."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from app.conversion.converters.office_docx import OfficeDocxConverter
from app.conversion.result import UniversalConversionResult
from app.models.image_understanding import ImageType
from app.conversion.stream_info import StreamInfo
from tests.test_embedded_image import FakeVLM, FakeOcrEngine, FakeOcrResult

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "conversion"


@patch("app.conversion.embedded_image.VLMService")
@patch("app.services.ocr_engine.build_ocr_engine")
def test_office_docx_headings(mock_build_ocr, mock_vlm_cls):
    converter = OfficeDocxConverter()
    filepath = FIXTURE_DIR / "simple_headings.docx"
    
    result = converter.convert(str(filepath), {})
    assert isinstance(result, UniversalConversionResult)
    assert "Heading 1" in result.text
    assert "Heading 2" in result.text
    assert "This is a paragraph under heading 1." in result.text
    assert "This is a paragraph under heading 2." in result.text
    assert result.images == {}


@patch("app.conversion.embedded_image.VLMService")
@patch("app.services.ocr_engine.build_ocr_engine")
def test_office_docx_tables_lists(mock_build_ocr, mock_vlm_cls):
    converter = OfficeDocxConverter()
    filepath = FIXTURE_DIR / "tables_links_lists.docx"
    
    result = converter.convert(str(filepath), {})
    
    # Bullet lists check
    assert "First item" in result.text
    assert "Second item" in result.text
    
    # Table check (markdownify converts <table> structure to markdown table)
    assert "Header A" in result.text
    assert "Header B" in result.text
    assert "Value A" in result.text
    assert "Value B" in result.text


@patch("app.conversion.embedded_image.VLMService")
@patch("app.services.ocr_engine.build_ocr_engine")
def test_office_docx_embedded_image_vlm(mock_build_ocr, mock_vlm_cls):
    # Mock VLM to classify as bar chart and extract structured content
    fake_vlm = FakeVLM(
        image_type=ImageType.chart_bar,
        payload={"title": "VLM Chart Title", "series": [{"name": "S1", "points": [{"x": "A", "y": 10}]}]}
    )
    mock_vlm_cls.return_value = fake_vlm

    converter = OfficeDocxConverter()
    filepath = FIXTURE_DIR / "embedded_text_screenshot.docx"
    
    config = {
        "image_handling_mode": "understanding",
        "router_enabled": False,
        "allow_cloud_vlm": True,
        "vlm_model": "test-vlm-model",
    }
    
    result = converter.convert(str(filepath), config)
    
    # Check VLM text replacement
    assert "VLM Chart Title" in result.text
    # Keep original image reference is true for charts
    assert "image_1.png" in result.text
    assert "![image_1.png](image_1.png)" in result.text
    # Output images has the image bytes
    assert "image_1.png" in result.images
    assert len(result.images["image_1.png"]) > 0
    # Metadata contains the image understanding record
    assert "image_understanding" in result.metadata
    assert len(result.metadata["image_understanding"]) == 1
    assert result.metadata["image_understanding"][0]["image_name"] == "image_1.png"
    assert result.metadata["image_understanding"][0]["image_type"] == "chart_bar"


@patch("mammoth.convert_to_html")
def test_office_docx_fallback_raw_text(mock_convert):
    # Mammoth HTML fails, falls back to raw text extraction
    mock_convert.side_effect = RuntimeError("Mammoth exploded")
    
    converter = OfficeDocxConverter()
    filepath = FIXTURE_DIR / "simple_headings.docx"
    
    result = converter.convert(str(filepath), {})
    assert result.metadata.get("mammoth_fallback") is True
    # The raw text contains content but without HTML structure
    assert "Heading 1" in result.text
    assert "Heading 2" in result.text
