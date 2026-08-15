"""Fixed-point geometry (marker.geometry.fixed_point.v1) unit tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.utils.canonical import (
    GEOMETRY_PROFILE,
    GEOMETRY_SCALE,
    CanonicalBox,
    CanonicalPoint,
    CanonicalPolygon,
)
from app.utils.canonical.errors import CanonicalValueError


def test_profile_constants_declared() -> None:
    assert GEOMETRY_PROFILE == "marker.geometry.fixed_point.v1"
    assert GEOMETRY_SCALE == 1000


def test_canonical_form_contains_integers_and_profile_tag() -> None:
    box = CanonicalBox.from_bbox([72.0, 110.5, 540.25, 660.75])
    assert box.canonical_value() == {
        "geometry": "box",
        "profile": GEOMETRY_PROFILE,
        "x0": 72000,
        "y0": 110500,
        "x1": 540250,
        "y1": 660750,
    }
    assert CanonicalPoint.from_coordinates(1, -2).canonical_value() == {
        "geometry": "point",
        "profile": GEOMETRY_PROFILE,
        "x": 1000,
        "y": -2000,
    }


@pytest.mark.parametrize(
    ("raw", "scaled"),
    [
        (0, 0),
        ("0.0004", 0),  # 0.4 -> 0 under half-even
        ("0.0005", 0),  # exact tie at 0.5 -> even neighbor 0
        ("0.0006", 1),  # 0.6 -> 1
        ("0.0015", 2),  # exact tie at 1.5 -> even neighbor 2
        ("0.0025", 2),  # exact tie at 2.5 -> even neighbor 2
        ("0.0035", 4),  # exact tie at 3.5 -> even neighbor 4
        ("-0.0005", 0),  # -0.5 -> 0 (even)
        ("-0.0015", -2),  # -1.5 -> -2 (even)
        ("-0.0006", -1),
        ("1", 1000),
        ("-1", -1000),
        ("1e3", 1_000_000),  # exponent-string input accepted exactly
        (Decimal("12.3456789"), 12346),  # 12345.6789 -> 12346
        (-1_000_000_000, -1_000_000_000_000),  # range edge, exact
    ],
)
def test_quantization_boundaries(raw: object, scaled: int) -> None:
    assert CanonicalPoint.from_coordinates(raw, 0).x == scaled


def test_float_quantization_uses_exact_binary_expansion() -> None:
    # float 0.1 is exactly 0.1000000000000000055511151231257827...;
    # x1000 = 100.0000...0555 -> 100. Any correct decimal rounding gives 100,
    # but a float-multiply-first path (0.1*1000 = 100.00000000000001)
    # must also land on 100 here; the exactness rule matters for values
    # sitting on quantization ties.
    assert CanonicalPoint.from_coordinates(0.1, 0.0).x == 100
    # Negative zero collapses to zero.
    assert CanonicalPoint.from_coordinates(-0.0, "-0.0").canonical_value()["x"] == 0
    assert CanonicalPoint.from_coordinates(-0.0, "-0.0").y == 0


def test_equivalent_numeric_inputs_converge() -> None:
    forms = [1, 1.0, "1", "1.000", "1e0", Decimal("1.000000")]
    encodings = {
        canonical_bytes_of_point(CanonicalPoint.from_coordinates(f, 2))
        for f in forms
    }
    assert encodings == {canonical_bytes_of_point(CanonicalPoint.from_coordinates(1, 2))}


def canonical_bytes_of_point(point: CanonicalPoint) -> bytes:
    import json

    from app.utils.canonical import canonical_json_bytes

    ready = json.loads(json.dumps(point.canonical_value()))
    return canonical_json_bytes(ready)


def test_tiny_and_negative_coordinates() -> None:
    point = CanonicalPoint.from_coordinates("-0.0004", "-500000000")
    assert point.x == 0
    assert point.y == -500000000000


@pytest.mark.parametrize("raw", [1e10, "1e10", 1_000_000_001, float(2e9)])
def test_out_of_range_rejected(raw: object) -> None:
    with pytest.raises(CanonicalValueError, match="profile range"):
        CanonicalPoint.from_coordinates(raw, 0)


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"])
def test_non_finite_rejected(raw: object) -> None:
    with pytest.raises(CanonicalValueError, match="non-finite"):
        CanonicalPoint.from_coordinates(raw, 0)


def test_unsupported_coordinate_types_rejected() -> None:
    with pytest.raises(CanonicalValueError, match="not accepted"):
        CanonicalPoint.from_coordinates(None, 0)  # type: ignore[arg-type]
    with pytest.raises(CanonicalValueError, match="not coordinates"):
        CanonicalPoint.from_coordinates(True, 0)  # type: ignore[arg-type]


def test_malformed_numeric_string_rejected() -> None:
    with pytest.raises(CanonicalValueError, match="not a valid decimal"):
        CanonicalPoint.from_coordinates("12,5", 0)


def test_box_requires_positive_extent() -> None:
    with pytest.raises(CanonicalValueError, match="positive extent"):
        CanonicalBox.from_bbox([1, 1, 1, 2])  # zero width
    with pytest.raises(CanonicalValueError, match="positive extent"):
        CanonicalBox.from_bbox([5, 0, 1, 2])  # inverted
    with pytest.raises(CanonicalValueError, match="positive extent"):
        CanonicalBox.from_bbox([1, 2, 3, 2])  # zero height


def test_bbox_adapter_requires_four_elements() -> None:
    with pytest.raises(CanonicalValueError, match="exactly 4 elements"):
        CanonicalBox.from_bbox([1, 2, 3])
    with pytest.raises(CanonicalValueError, match="exactly 4 elements"):
        CanonicalBox.from_bbox([1, 2, 3, 4, 5])


def test_bbox_boundary_quantization_can_create_degenerate_box() -> None:
    # Two distinct floats 1.0001 apart quantize to the same scaled value
    # -> degenerate box is rejected loudly, not silently hashed.
    with pytest.raises(CanonicalValueError, match="positive extent"):
        CanonicalBox.from_bbox([0.0, 0.0, 0.0001, 1.0])


def test_polygon_minimum_vertices_and_closing_ring() -> None:
    triangle = CanonicalPolygon.from_coordinates([(0, 0), (10, 0), (0, 10)])
    closed = CanonicalPolygon.from_coordinates([(0, 0), (10, 0), (0, 10), (0, 0)])
    assert triangle == closed
    assert triangle.canonical_value() == {
        "geometry": "polygon",
        "profile": GEOMETRY_PROFILE,
        "points": [{"x": 0, "y": 0}, {"x": 10000, "y": 0}, {"x": 0, "y": 10000}],
    }
    with pytest.raises(CanonicalValueError, match="at least 3"):
        CanonicalPolygon.from_coordinates([(0, 0), (1, 1), (0, 0)])
    with pytest.raises(CanonicalValueError, match="at least 3"):
        CanonicalPolygon.from_coordinates([(0, 0), (1, 1)])


def test_polygon_accepts_engine_style_float_rings() -> None:
    polygon = CanonicalPolygon.from_coordinates(
        [(72.5, 90.25), (200.125, 90.25), (200.125, 300.0), (72.5, 300.0)]
    )
    value = polygon.canonical_value()
    assert value["points"] == [
        {"x": 72500, "y": 90250},
        {"x": 200125, "y": 90250},
        {"x": 200125, "y": 300000},
        {"x": 72500, "y": 300000},
    ]


def test_repeat_conversion_is_stable() -> None:
    raw = (72.0, 110.5, 540.25, 660.75)
    first = CanonicalBox.from_bbox(raw).canonical_value()
    for _ in range(5):
        assert CanonicalBox.from_bbox(raw).canonical_value() == first
