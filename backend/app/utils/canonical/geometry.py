"""Canonical fixed-point geometry for identity-bearing records.

Engine-native coordinates are floats (or float-derived strings) whose
binary representation and formatting vary across engines, runtimes,
and versions. They must never be hashed directly. This module is the
conversion boundary: every coordinate entering canonical identity is
quantized once, deterministically, to a signed integer.

v1 fixed-point profile (``marker.geometry.fixed_point.v1``):

* **Coordinate space:** source-document units (PDF points, pixels, or
  any declared engine space) on a Y-down, top-left-origin axis
  convention; the profile stores raw quantized integers only, so
  consumers must carry axis semantics alongside.
* **Unit/scale:** 1/1000 of a source unit (``GEOMETRY_SCALE = 1000``).
* **Quantization:** exact decimal arithmetic on the *exact* value of
  the input (``Decimal(float)`` expands the binary value exactly, so
  identical float64 bits always quantize identically on every
  platform), then round-half-even to an integer.
* **Valid range:** source-unit magnitude at most ``MAX_ABS_UNIT``
  (1e9), so quantized coordinates stay well inside int64 and the JCS
  safe-integer range.
* **Invalid input:** NaN, infinities, non-numeric strings, malformed
  shapes (empty/degenerate boxes, polygons with fewer than 3 points)
  raise :class:`CanonicalValueError`.

Canonical forms are tagged, self-describing objects; the profile
string is part of the object, so bumping the profile changes identity
hashes automatically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Iterable, Sequence

from .errors import CanonicalValueError

#: Identity-affecting version of the fixed-point geometry profile.
GEOMETRY_PROFILE = "marker.geometry.fixed_point.v1"

#: Quantization scale: 1 unit == 1000 fixed-point units.
GEOMETRY_SCALE = 1000

#: Maximum absolute source-unit coordinate accepted by the profile.
MAX_ABS_UNIT = 1_000_000_000

_MAX_SCALED = MAX_ABS_UNIT * GEOMETRY_SCALE

CoordinateInput = int | float | str | Decimal


def _quantize(name: str, value: CoordinateInput) -> int:
    if isinstance(value, bool):
        raise CanonicalValueError(f"{name}: booleans are not coordinates")
    if isinstance(value, int):
        exact = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalValueError(
                f"{name}: non-finite coordinate {value!r} cannot enter identity"
            )
        # Exact binary expansion: deterministic from the float64 bits.
        exact = Decimal(value)
    elif isinstance(value, (str, Decimal)):
        try:
            exact = value if isinstance(value, Decimal) else Decimal(value)
        except ArithmeticError as exc:
            raise CanonicalValueError(
                f"{name}: coordinate {value!r} is not a valid decimal number"
            ) from exc
        if not exact.is_finite():
            raise CanonicalValueError(
                f"{name}: non-finite coordinate {value!r} cannot enter identity"
            )
    else:
        raise CanonicalValueError(
            f"{name}: coordinate type {type(value).__name__} is not accepted "
            "(use int, float, str, or Decimal)"
        )
    scaled = (exact * GEOMETRY_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(scaled) > _MAX_SCALED:
        raise CanonicalValueError(
            f"{name}: |{value}| exceeds the profile range of {MAX_ABS_UNIT} "
            "source units after quantization"
        )
    return int(scaled)


@dataclass(frozen=True)
class CanonicalPoint:
    """A single quantized point in the v1 fixed-point profile."""

    x: int
    y: int

    def __post_init__(self) -> None:
        if max(abs(self.x), abs(self.y)) > _MAX_SCALED:
            raise CanonicalValueError("point coordinate exceeds the profile range")

    @classmethod
    def from_coordinates(cls, x: CoordinateInput, y: CoordinateInput) -> CanonicalPoint:
        return cls(x=_quantize("point.x", x), y=_quantize("point.y", y))

    def canonical_value(self) -> dict[str, Any]:
        return {"geometry": "point", "profile": GEOMETRY_PROFILE, "x": self.x, "y": self.y}


@dataclass(frozen=True)
class CanonicalBox:
    """An axis-aligned box (x0, y0, x1, y1) with strictly positive extent."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise CanonicalValueError(
                f"box must have positive extent, got x=[{self.x0}, {self.x1}] "
                f"y=[{self.y0}, {self.y1}]; degenerate or inverted boxes are "
                "not valid identity geometry"
            )
        if max(abs(self.x0), abs(self.x1), abs(self.y0), abs(self.y1)) > _MAX_SCALED:
            raise CanonicalValueError("box coordinate exceeds the profile range")

    @classmethod
    def from_coordinates(
        cls,
        x0: CoordinateInput,
        y0: CoordinateInput,
        x1: CoordinateInput,
        y1: CoordinateInput,
    ) -> CanonicalBox:
        return cls(
            x0=_quantize("box.x0", x0),
            y0=_quantize("box.y0", y0),
            x1=_quantize("box.x1", x1),
            y1=_quantize("box.y1", y1),
        )

    @classmethod
    def from_bbox(cls, bbox: Sequence[CoordinateInput]) -> CanonicalBox:
        """Adapt a Marker-style ``[x0, y0, x1, y1]`` bbox (floats/strings)."""
        if len(bbox) != 4:
            raise CanonicalValueError(
                f"bbox must have exactly 4 elements [x0, y0, x1, y1], got {len(bbox)}"
            )
        x0, y0, x1, y1 = bbox
        return cls.from_coordinates(x0, y0, x1, y1)

    def canonical_value(self) -> dict[str, Any]:
        return {
            "geometry": "box",
            "profile": GEOMETRY_PROFILE,
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
        }


@dataclass(frozen=True)
class CanonicalPolygon:
    """A closed polygon with at least 3 distinct vertices."""

    points: tuple[CanonicalPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise CanonicalValueError(
                f"polygon needs at least 3 vertices, got {len(self.points)}"
            )
        for point in self.points:
            if not isinstance(point, CanonicalPoint):
                raise CanonicalValueError(
                    f"polygon vertices must be CanonicalPoint, got {type(point).__name__}"
                )

    @classmethod
    def from_coordinates(
        cls, points: Iterable[Sequence[CoordinateInput]]
    ) -> CanonicalPolygon:
        vertices = [
            CanonicalPoint.from_coordinates(pair[0], pair[1]) for pair in points
        ]
        if len(vertices) > 1 and vertices[0] == vertices[-1]:
            vertices = vertices[:-1]  # tolerate explicitly-closed rings
        return cls(tuple(vertices))

    def canonical_value(self) -> dict[str, Any]:
        return {
            "geometry": "polygon",
            "profile": GEOMETRY_PROFILE,
            "points": [{"x": p.x, "y": p.y} for p in self.points],
        }
