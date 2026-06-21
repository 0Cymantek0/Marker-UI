"""Unit tests for the OfficePptxConverter."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from app.conversion.converters.office_pptx import OfficePptxConverter
from app.conversion.result import UniversalConversionResult
from app.models.image_understanding import ImageType
from app.conversion.stream_info import StreamInfo
from tests.test_embedded_image import FakeVLM

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "conversion"


@patch("app.conversion.embedded_image.VLMService")
@patch("app.services.ocr_engine.build_ocr_engine")
def test_office_pptx_headings_notes(mock_build_ocr, mock_vlm_cls):
    converter = OfficePptxConverter()
    filepath = FIXTURE_DIR / "title_text_notes.pptx"
    
    result = converter.convert(str(filepath), {})
    assert isinstance(result, UniversalConversionResult)
    # Check slide title
    assert "Slide Title 1" in result.text
    # Check paragraph
    assert "First paragraph in body text." in result.text
    # Check bullets/level indentation
    assert "  - Second paragraph (bullet point)." in result.text
    # Check presenter notes
    assert "Presenter notes for Slide 1." in result.text
    assert result.images == {}


@patch("app.conversion.embedded_image.VLMService")
@patch("app.services.ocr_engine.build_ocr_engine")
def test_office_pptx_tables_charts_images(mock_build_ocr, mock_vlm_cls):
    # Mock VLM to classify as bar chart and extract structured content
    fake_vlm = FakeVLM(
        image_type=ImageType.chart_bar,
        payload={"title": "VLM Chart Title", "series": [{"name": "S1", "points": [{"x": "A", "y": 10}]}]}
    )
    mock_vlm_cls.return_value = fake_vlm

    converter = OfficePptxConverter()
    filepath = FIXTURE_DIR / "table_chart_images.pptx"
    
    config = {
        "image_handling_mode": "understanding",
        "router_enabled": False,
        "allow_cloud_vlm": True,
        "vlm_model": "test-vlm-model",
    }
    
    result = converter.convert(str(filepath), config)
    
    # 1. Table check
    assert "Header A" in result.text
    assert "Header B" in result.text
    assert "Cell 1-1" in result.text
    assert "Cell 1-2" in result.text

    # 2. Chart check (data series extraction)
    assert "Series 1" in result.text
    assert "Series 2" in result.text
    assert "Category A" in result.text
    assert "Category B" in result.text

    # 3. Image check (routed through VLM)
    assert "image_1.png" in result.images
    assert len(result.images["image_1.png"]) > 0
    assert "image_understanding" in result.metadata
    assert len(result.metadata["image_understanding"]) == 1
    assert result.metadata["image_understanding"][0]["image_name"] == "image_1.png"


def test_office_pptx_failed_parse():
    converter = OfficePptxConverter()
    with pytest.raises(Exception):
        converter.convert("non_existent.pptx", {})


def test_office_pptx_chart_extraction_exception():
    from unittest.mock import PropertyMock
    converter = OfficePptxConverter()
    
    mock_shape = MagicMock()
    mock_shape.shape_type = 3  # not group/picture
    mock_shape.has_table = False
    mock_shape.has_chart = True
    mock_shape.has_text_frame = False
    
    mock_chart = MagicMock()
    type(mock_chart).plots = PropertyMock(side_effect=RuntimeError("plots unavailable"))
    mock_chart.has_title = True
    mock_chart.chart_title.text_frame.text = "My Custom Chart"
    mock_shape.chart = mock_chart
    
    mock_slide = MagicMock()
    mock_shapes = MagicMock()
    mock_shapes.__iter__.return_value = [mock_shape]
    mock_shapes.title = None
    mock_slide.shapes = mock_shapes
    mock_slide.has_notes_slide = False
    
    mock_prs = MagicMock()
    mock_prs.slides = [mock_slide]
    
    with patch("app.conversion.converters.office_pptx.Presentation", return_value=mock_prs):
        result = converter.convert("dummy.pptx", {})
        
    assert "<!-- [Chart: My Custom Chart] (Data extraction unavailable) -->" in result.text


def test_office_pptx_shape_sorting_missing_coords():
    converter = OfficePptxConverter()
    
    s1 = MagicMock()
    s1.shape_type = 2
    s1.has_table = False
    s1.has_chart = False
    s1.has_text_frame = True
    s1.text_frame.paragraphs = [MagicMock(text="Shape 1", level=0)]
    type(s1).top = PropertyMock(return_value=None)
    type(s1).left = PropertyMock(return_value=None)
    
    s2 = MagicMock()
    s2.shape_type = 2
    s2.has_table = False
    s2.has_chart = False
    s2.has_text_frame = True
    s2.text_frame.paragraphs = [MagicMock(text="Shape 2", level=0)]
    del s2.top
    del s2.left
    
    s3 = MagicMock()
    s3.shape_type = 2
    s3.has_table = False
    s3.has_chart = False
    s3.has_text_frame = True
    s3.text_frame.paragraphs = [MagicMock(text="Shape 3", level=0)]
    s3.top = 1000000
    s3.left = 500000
    
    mock_slide = MagicMock()
    mock_shapes = MagicMock()
    mock_shapes.__iter__.return_value = [s1, s2, s3]
    mock_shapes.title = None
    mock_slide.shapes = mock_shapes
    mock_slide.has_notes_slide = False
    
    mock_prs = MagicMock()
    mock_prs.slides = [mock_slide]
    
    with patch("app.conversion.converters.office_pptx.Presentation", return_value=mock_prs):
        result = converter.convert("dummy.pptx", {})
        
    assert "Shape 1" in result.text
    assert "Shape 2" in result.text
    assert "Shape 3" in result.text


def test_office_pptx_chart_series_names():
    converter = OfficePptxConverter()
    
    mock_shape = MagicMock()
    mock_shape.shape_type = 3
    mock_shape.has_table = False
    mock_shape.has_chart = True
    mock_shape.has_text_frame = False
    
    mock_series_1 = MagicMock()
    mock_series_1.name = None
    mock_series_1.values = [10, 20]
    
    mock_series_2 = MagicMock()
    mock_series_2.name = 123.45
    mock_series_2.values = [15, 25]
    
    mock_plot = MagicMock()
    mock_plot.categories = ["Cat A", "Cat B"]
    
    mock_chart = MagicMock()
    mock_chart.plots = [mock_plot]
    mock_chart.series = [mock_series_1, mock_series_2]
    mock_shape.chart = mock_chart
    
    mock_slide = MagicMock()
    mock_shapes = MagicMock()
    mock_shapes.__iter__.return_value = [mock_shape]
    mock_shapes.title = None
    mock_slide.shapes = mock_shapes
    mock_slide.has_notes_slide = False
    
    mock_prs = MagicMock()
    mock_prs.slides = [mock_slide]
    
    with patch("app.conversion.converters.office_pptx.Presentation", return_value=mock_prs):
        result = converter.convert("dummy.pptx", {})
        
    assert "| Series | Cat A | Cat B |" in result.text
    assert "|  | 10 | 20 |" in result.text
    assert "| 123.45 | 15 | 25 |" in result.text
