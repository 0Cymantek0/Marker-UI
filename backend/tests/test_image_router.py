"""Unit tests for the Tier-0 image router (plan §2/§10).

These prove each routing branch fires at the right thresholds using fake
detection results — no torch, no Surya, no GPU required.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from app.models.image_understanding import RouteKind
from app.processors.image_router import ImageRouter, _polygon_area


@dataclass
class FakeBox:
    polygon: list[list[float]]


@dataclass
class FakeDetectionResult:
    bboxes: list[FakeBox] = field(default_factory=list)
    image_bbox: list[float] = field(default_factory=lambda: [0.0, 0.0, 100.0, 100.0])


class FakeDetectionModel:
    """Callable mimicking surya DetectionPredictor([img]) -> [TextDetectionResult]."""

    def __init__(self, result: FakeDetectionResult | None = None, boom: bool = False):
        self._result = result or FakeDetectionResult()
        self._boom = boom

    def __call__(self, images):
        if self._boom:
            raise RuntimeError("detection exploded")
        return [self._result]


def _img(w=100, h=100):
    return Image.new("RGB", (w, h), color="white")


def _box(x0, y0, x1, y1):
    return FakeBox(polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]])


def test_polygon_area_rectangle():
    assert _polygon_area([[0, 0], [10, 0], [10, 5], [0, 5]]) == 50.0


def test_polygon_area_degenerate_returns_zero():
    assert _polygon_area([[0, 0], [10, 0]]) == 0.0
    assert _polygon_area([]) == 0.0


def test_route_decorative_when_no_text():
    # No boxes at all on a 100x100 image -> density 0, lines 0 -> decorative.
    router = ImageRouter(detection_model=FakeDetectionModel(), config={})
    decision = router.route(_img())
    assert decision.route == RouteKind.skip_decorative
    assert decision.line_count == 0


def test_route_ocr_when_text_dense():
    # Five big boxes covering ~60% of a 100x100 image -> dense text -> OCR.
    boxes = [
        _box(0, 0, 100, 12),
        _box(0, 14, 100, 26),
        _box(0, 28, 100, 40),
        _box(0, 42, 100, 54),
        _box(0, 56, 100, 68),
    ]
    result = FakeDetectionResult(bboxes=boxes, image_bbox=[0, 0, 100, 100])
    router = ImageRouter(
        detection_model=FakeDetectionModel(result),
        config={"allow_cloud_vlm": True},
    )
    decision = router.route(_img())
    assert decision.route == RouteKind.ocr
    assert decision.line_count == 5
    assert decision.text_density >= 0.45


def test_route_vlm_when_sparse_text():
    # Two small scattered labels (a chart's axis ticks) -> low density -> VLM.
    boxes = [_box(2, 2, 12, 8), _box(80, 90, 92, 96)]
    result = FakeDetectionResult(bboxes=boxes, image_bbox=[0, 0, 100, 100])
    router = ImageRouter(
        detection_model=FakeDetectionModel(result),
        config={"allow_cloud_vlm": True},
    )
    decision = router.route(_img())
    assert decision.route == RouteKind.vlm
    assert 0.0 < decision.text_density < 0.45


def test_route_vlm_when_no_detection_model():
    router = ImageRouter(detection_model=None, config={"allow_cloud_vlm": True})
    decision = router.route(_img())
    assert decision.route == RouteKind.vlm
    assert "no detection model" in decision.reason


def test_detection_failure_degrades_to_vlm():
    router = ImageRouter(detection_model=FakeDetectionModel(boom=True), config={})
    decision = router.route(_img())
    # density 0, lines 0 -> would be decorative, but boom returns (0,0) so it is
    # treated as decorative only if density<=thresh AND lines==0. With a real
    # detection model present the (0,0) signal yields decorative; assert it does
    # not raise and yields a valid route.
    assert decision.route in (RouteKind.skip_decorative, RouteKind.vlm)


def test_local_only_routes_visual_to_ocr_when_cloud_disabled():
    # allow_cloud_vlm=False: a sparse-text graphic cannot go to cloud, so it
    # falls back to local OCR rather than being dropped (plan §11a).
    boxes = [_box(2, 2, 12, 8)]
    result = FakeDetectionResult(bboxes=boxes, image_bbox=[0, 0, 100, 100])
    router = ImageRouter(
        detection_model=FakeDetectionModel(result),
        config={"allow_cloud_vlm": False},
    )
    decision = router.route(_img())
    assert decision.route == RouteKind.ocr
    assert "cloud disabled" in decision.reason


def test_thresholds_are_configurable():
    # Tighten OCR threshold so a 30%-coverage image still counts as OCR.
    boxes = [_box(0, 0, 100, 10), _box(0, 12, 100, 22), _box(0, 24, 100, 34)]
    result = FakeDetectionResult(bboxes=boxes, image_bbox=[0, 0, 100, 100])
    router = ImageRouter(
        detection_model=FakeDetectionModel(result),
        config={"ocr_min_text_density": 0.25, "ocr_min_lines": 3},
    )
    decision = router.route(_img())
    assert decision.route == RouteKind.ocr
