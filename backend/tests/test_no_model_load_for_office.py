"""Assert that Office conversions never initialize the heavy Marker models."""

from __future__ import annotations

from unittest.mock import MagicMock
from pathlib import Path
from app.services.conversion_service import ConversionService

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "conversion"


def test_no_model_load_for_office_docx():
    # Construct mock marker service
    mock_marker_service = MagicMock()
    mock_marker_service._initialized = False

    # Construct conversion service with mock
    conv_service = ConversionService(mock_marker_service)
    
    filepath = FIXTURE_DIR / "simple_headings.docx"
    
    # Run conversion
    result = conv_service.convert_file(str(filepath), {})
    
    # Assert
    # 1. initialize is NOT called
    mock_marker_service.initialize.assert_not_called()
    # 2. convert_file is NOT called
    mock_marker_service.convert_file.assert_not_called()
    
    # 3. Output check
    assert "Heading 1" in result["text"]
    # 4. Engine metadata check
    assert "engine" in result["metadata"]
    assert result["metadata"]["engine"]["engine"] == "office_docx"


def test_no_model_load_for_office_pptx():
    # Construct mock marker service
    mock_marker_service = MagicMock()
    mock_marker_service._initialized = False

    # Construct conversion service with mock
    conv_service = ConversionService(mock_marker_service)
    
    filepath = FIXTURE_DIR / "title_text_notes.pptx"
    
    # Run conversion
    result = conv_service.convert_file(str(filepath), {})
    
    # Assert
    # 1. initialize is NOT called
    mock_marker_service.initialize.assert_not_called()
    # 2. convert_file is NOT called
    mock_marker_service.convert_file.assert_not_called()
    
    # 3. Output check
    assert "Slide Title 1" in result["text"]
    # 4. Engine metadata check
    assert "engine" in result["metadata"]
    assert result["metadata"]["engine"]["engine"] == "office_pptx"
