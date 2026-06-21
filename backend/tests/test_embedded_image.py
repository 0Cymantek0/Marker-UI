"""Unit tests for EmbeddedImageService."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch
import pytest
from PIL import Image

from app.conversion.embedded_image import EmbeddedImageService
from app.models.image_understanding import (
    ClassificationResult,
    ExtractionResult,
    ImageType,
    RouteKind,
)
from tests.test_image_router import FakeDetectionModel, FakeDetectionResult, _box, _img


class FakeVLM:
    def __init__(self, image_type=ImageType.chart_bar, payload=None, route=None):
        self.image_type = image_type
        self.payload = payload or {"title": "Chart Title", "series": []}
        self.calls = []
        self.route = route
        self.cost_usd = 0.005

    def classify(self, image_bytes, mime_type, heading_chain, surrounding_paragraphs):
        self.calls.append(("classify", image_bytes))
        return ClassificationResult(
            image_type=self.image_type,
            confidence=0.95,
            rationale="test",
        )

    def extract(self, image_bytes, mime_type, image_type, heading_chain, surrounding_paragraphs):
        self.calls.append(("extract", image_bytes))
        return ExtractionResult(
            image_type=image_type,
            payload=self.payload,
            raw_response="{}",
            confidence=0.9,
            route=self.route,
            cost_usd=self.cost_usd,
        )


class FakeOcrResult:
    def __init__(self, html="<p>transcribed text</p>", error=None):
        self.html = html
        self.error = error
        self.line_count = 1
        self.duration_ms = 100


class FakeOcrEngine:
    def __init__(self, result=None):
        self.result = result or FakeOcrResult()
        self.available = True

    def recognize(self, image):
        return self.result


def test_extraction_mode_returns_direct_reference():
    service = EmbeddedImageService()
    res = service.process_image(
        image_bytes_or_pil=b"fake-bytes",
        context_text="test context",
        options={"image_handling_mode": "extraction"},
        image_name="img_1.png"
    )
    assert res["route"] == "extraction"
    assert res["markdown"] == "![img_1.png](img_1.png)"
    assert not res["omitted"]


def test_skip_decorative_route():
    # Empty image leads to decorative routing decision
    service = EmbeddedImageService()
    
    # We construct a fake marker service that returns initialized models
    fake_marker = MagicMock()
    fake_marker._initialized = True
    fake_marker._model_dict = {"detection": FakeDetectionModel(FakeDetectionResult())}
    service._marker_service = fake_marker

    options = {
        "image_handling_mode": "understanding",
        "router_enabled": True,
        "allow_cloud_vlm": True,
        "dedup_enabled": False,
    }
    
    img = _img()
    res = service.process_image(
        image_bytes_or_pil=img,
        context_text="decorative context",
        options=options,
        image_name="img_dec.png"
    )
    assert res["route"] == "skip_decorative"
    assert res["markdown"] == ""
    assert res["omitted"]


@patch("app.services.ocr_engine.build_ocr_engine")
def test_ocr_route_success(mock_build_ocr):
    # Set up mock ocr engine
    mock_build_ocr.return_value = FakeOcrEngine()

    service = EmbeddedImageService()
    boxes = [
        _box(0, 0, 100, 12),
        _box(0, 14, 100, 26),
        _box(0, 28, 100, 40),
        _box(0, 42, 100, 54),
        _box(0, 56, 100, 68),
    ]
    result = FakeDetectionResult(bboxes=boxes, image_bbox=[0, 0, 100, 100])
    
    fake_marker = MagicMock()
    fake_marker._initialized = True
    fake_marker._model_dict = {
        "detection": FakeDetectionModel(result),
        "recognition": MagicMock(),
    }
    service._marker_service = fake_marker

    options = {
        "image_handling_mode": "understanding",
        "router_enabled": True,
        "allow_cloud_vlm": False,
        "dedup_enabled": False,
        "ocr_engine": "surya",
    }

    res = service.process_image(
        image_bytes_or_pil=_img(),
        context_text="ocr context",
        options=options,
        image_name="img_ocr.png"
    )
    assert res["route"] == "ocr"
    assert "transcribed text" in res["markdown"]
    assert "route=ocr" in res["markdown"]
    assert not res["omitted"]


@patch("app.conversion.embedded_image.VLMService")
@patch("app.services.ocr_engine.build_ocr_engine")
def test_ocr_route_fails_escalates_to_vlm(mock_build_ocr, mock_vlm_cls):
    # OCR recognition returns empty text/error
    mock_build_ocr.return_value = FakeOcrEngine(FakeOcrResult(html="", error="empty"))
    fake_vlm = FakeVLM(image_type=ImageType.equation, payload={"latex": "E=mc^2"})
    mock_vlm_cls.return_value = fake_vlm

    service = EmbeddedImageService()
    boxes = [
        _box(0, 0, 100, 12),
        _box(0, 14, 100, 26),
        _box(0, 28, 100, 40),
        _box(0, 42, 100, 54),
        _box(0, 56, 100, 68),
    ]
    result = FakeDetectionResult(bboxes=boxes, image_bbox=[0, 0, 100, 100])
    
    fake_marker = MagicMock()
    fake_marker._initialized = True
    fake_marker._model_dict = {
        "detection": FakeDetectionModel(result),
        "recognition": MagicMock(),
    }
    service._marker_service = fake_marker

    options = {
        "image_handling_mode": "understanding",
        "router_enabled": True,
        "allow_cloud_vlm": True,
        "dedup_enabled": False,
        "ocr_engine": "surya",
    }

    res = service.process_image(
        image_bytes_or_pil=_img(),
        context_text="escalation context",
        options=options,
        image_name="img_esc.png"
    )
    # Escalated to VLM, which returned equation -> formatted
    assert res["route"] == "vlm"
    assert "E=mc^2" in res["markdown"]
    # Equation is replace_safe, so keep_original is False
    assert "original_image" not in res["markdown"]


@patch("app.conversion.embedded_image.VLMService")
def test_vlm_route_success_chart(mock_vlm_cls):
    # No models loaded -> router defaults to VLM
    fake_vlm = FakeVLM(
        image_type=ImageType.chart_bar,
        payload={
            "title": "Bar Chart",
            "series": [{"name": "S1", "points": [{"x": "A", "y": 10}]}],
            "notes": "Chart Notes"
        }
    )
    mock_vlm_cls.return_value = fake_vlm

    service = EmbeddedImageService()
    options = {
        "image_handling_mode": "understanding",
        "router_enabled": True,
        "allow_cloud_vlm": True,
        "dedup_enabled": False,
    }

    res = service.process_image(
        image_bytes_or_pil=_img(),
        context_text="chart context",
        options=options,
        image_name="img_chart.png"
    )
    assert res["route"] == "vlm"
    assert "Bar Chart" in res["markdown"]
    # Chart is not replace_safe, so keep_original is True
    assert "original_image: img_chart.png" in res["markdown"]
    assert "![img_chart.png](img_chart.png)" in res["markdown"]
    assert res["cost_usd"] == 0.005


def test_allow_cloud_vlm_false_prevents_cloud_call():
    service = EmbeddedImageService()
    options = {
        "image_handling_mode": "understanding",
        "router_enabled": False,
        "allow_cloud_vlm": False,
        "dedup_enabled": False,
    }

    # Should fall back to reference/error route since cloud is disabled and ocr models not present
    res = service.process_image(
        image_bytes_or_pil=_img(),
        context_text="no cloud context",
        options=options,
        image_name="img_nocloud.png"
    )
    assert res["route"] == "error"
    assert "![img_nocloud.png](img_nocloud.png)" in res["markdown"]


@patch("app.conversion.embedded_image.VLMService")
def test_dedup_cache(mock_vlm_cls):
    fake_vlm = FakeVLM(image_type=ImageType.equation, payload={"latex": "a^2+b^2=c^2"})
    mock_vlm_cls.return_value = fake_vlm

    service = EmbeddedImageService()
    options = {
        "image_handling_mode": "understanding",
        "router_enabled": False,
        "allow_cloud_vlm": True,
        "dedup_enabled": True,
        "dedup_max_distance": 0,
    }

    img = _img()

    # First call: processes normally
    res1 = service.process_image(
        image_bytes_or_pil=img,
        context_text="ctx",
        options=options,
        image_name="img_first.png"
    )
    assert res1["cost_usd"] == 0.005

    # Second call: resolves from cache with same content but 0.0 cost
    res2 = service.process_image(
        image_bytes_or_pil=img,
        context_text="ctx",
        options=options,
        image_name="img_second.png"
    )
    assert res2["route"] == "vlm"
    assert "a^2+b^2=c^2" in res2["markdown"]
    assert res2["cost_usd"] == 0.0


def test_malformed_image_handling():
    service = EmbeddedImageService()
    options = {
        "image_handling_mode": "understanding",
        "allow_cloud_vlm": True,
    }
    
    # Passing malformed bytes should fail PIL open but return gracefully
    res = service.process_image(
        image_bytes_or_pil=b"not-an-image",
        context_text="bad context",
        options=options,
        image_name="img_bad.png"
    )
    assert res["route"] == "error"
    assert res["markdown"] == "![img_bad.png](img_bad.png)"
    assert not res["omitted"]
