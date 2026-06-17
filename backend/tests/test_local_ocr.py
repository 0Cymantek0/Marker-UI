"""Unit tests for Tier-2 deterministic local OCR (plan §5b, the line-227 fix).

These prove the OCR path transcribes text-as-image with no cloud call, joins
lines into clean HTML, and degrades safely on failure — all with a fake
recognition model (no torch / Surya / GPU).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.local_ocr import LocalOcrService, _lines_to_html


@dataclass
class FakeTextLine:
    text: str


@dataclass
class FakeOcrResult:
    text_lines: list = field(default_factory=list)
    image_bbox: list = field(default_factory=lambda: [0, 0, 100, 100])


class FakeRecognitionModel:
    def __init__(self, lines=None, boom=False):
        self._lines = lines or []
        self._boom = boom
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        if self._boom:
            raise RuntimeError("recognition exploded")
        return [FakeOcrResult(text_lines=[FakeTextLine(t) for t in self._lines])]


class _Img:
    size = (100, 100)


def test_ocr_transcribes_lines_to_html():
    rec = FakeRecognitionModel(lines=["First line", "Second line"])
    svc = LocalOcrService(recognition_model=rec, detection_model=object())
    result = svc.ocr_image(_Img())

    assert result.error is None
    assert result.line_count == 2
    assert result.text == "First line\nSecond line"
    assert "<p>First line<br/>Second line</p>" in result.html
    # The detection model is passed through as det_predictor (marker parity).
    assert rec.last_kwargs["det_predictor"] is not None
    assert rec.last_kwargs["task_names"] == ["ocr_with_boxes"]


def test_ocr_skips_blank_lines_and_splits_paragraphs():
    rec = FakeRecognitionModel(lines=["Para one", "", "Para two"])
    svc = LocalOcrService(recognition_model=rec)
    result = svc.ocr_image(_Img())

    assert "<p>Para one</p>" in result.html
    assert "<p>Para two</p>" in result.html


def test_ocr_escapes_html_metacharacters():
    rec = FakeRecognitionModel(lines=["a < b && c > d"])
    svc = LocalOcrService(recognition_model=rec)
    result = svc.ocr_image(_Img())
    assert "&lt;" in result.html and "&gt;" in result.html
    assert "<p>a < b" not in result.html


def test_ocr_no_text_returns_error():
    rec = FakeRecognitionModel(lines=["   ", ""])
    svc = LocalOcrService(recognition_model=rec)
    result = svc.ocr_image(_Img())
    assert result.error == "no text recovered"
    assert result.html == ""


def test_ocr_recognition_failure_is_caught():
    rec = FakeRecognitionModel(boom=True)
    svc = LocalOcrService(recognition_model=rec)
    result = svc.ocr_image(_Img())
    assert result.error is not None
    assert "exploded" in result.error
    assert result.html == ""


def test_ocr_unavailable_without_model():
    svc = LocalOcrService(recognition_model=None)
    assert svc.available is False
    result = svc.ocr_image(_Img())
    assert result.error == "no recognition model"


def test_lines_to_html_empty():
    assert _lines_to_html([]) == ""
