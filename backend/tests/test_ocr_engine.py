"""Tests for the pluggable OCR-engine seam (plan §5 / decision #5).

The seam ships Surya only; deferred engines must raise an explicit, actionable
error rather than silently degrading. The adapter must translate the native
LocalOcrService result into the engine-agnostic OCRResult and never raise.
"""

from __future__ import annotations

import pytest

from app.services.ocr_engine import (
    OCREngine,
    OCRResult,
    SuryaOCREngine,
    build_ocr_engine,
)


class _FakeLine:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeOcrResultObj:
    def __init__(self, lines: list[str]) -> None:
        self.text_lines = [_FakeLine(t) for t in lines]


class _FakeRecognition:
    """Stands in for a Surya RecognitionPredictor."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return [_FakeOcrResultObj(self._lines)]


def test_build_surya_engine_is_default():
    engine = build_ocr_engine("surya", recognition_model=_FakeRecognition(["hi"]))
    assert isinstance(engine, SuryaOCREngine)
    assert engine.available is True


def test_surya_engine_satisfies_protocol():
    engine = build_ocr_engine("surya", recognition_model=_FakeRecognition(["hi"]))
    # runtime_checkable Protocol: structural conformance.
    assert isinstance(engine, OCREngine)


def test_surya_engine_recognizes_text_to_html():
    engine = build_ocr_engine(
        "surya", recognition_model=_FakeRecognition(["Line one", "Line two"])
    )
    result = engine.recognize(_image())
    assert isinstance(result, OCRResult)
    assert result.error is None
    assert result.line_count == 2
    assert "Line one" in result.html
    assert result.mean_confidence == 1.0


def test_surya_engine_unavailable_without_model():
    engine = build_ocr_engine("surya", recognition_model=None)
    assert engine.available is False
    # recognize still never raises; it reports the error.
    result = engine.recognize(_image())
    assert result.error is not None


def test_deferred_engine_raises_actionable_error():
    with pytest.raises(NotImplementedError) as exc:
        build_ocr_engine("glm_ocr")
    # Points the operator at the benchmark gate, not a generic failure.
    assert "benchmark" in str(exc.value).lower()


def test_unknown_engine_raises_value_error():
    with pytest.raises(ValueError):
        build_ocr_engine("does_not_exist")


def _image():
    from PIL import Image

    return Image.new("RGB", (20, 20), color="white")
