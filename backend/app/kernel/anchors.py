"""Layered SourceAnchor semantics (V3.2 TB2 Slice B / PR72).

A SourceAnchor is a source-resolvable selector bound to one immutable
:class:`~app.kernel.records.ContentRevisionRecord`. It carries several
complementary selectors rather than betting identity on one brittle
offset, bounding box, or quote:

* ``native``   — provider/native object or package-path identity;
* ``quote``    — exact source text/value plus local prefix/suffix context;
* ``position`` — byte/character offsets (declared brittle evidence);
* ``geometry`` — fixed-point geometry plus the coordinate space and
  render state needed to interpret it.

Identity rules (master-plan amendment 8C/4C):

* the anchor id is the PR61 framed identity hash of
  ``{content_revision_ref, locator, selectors}`` — never a raw float,
  never an ad-hoc JSON dump;
* access-policy state never participates, so an ACL-only revision
  change cannot remint an anchor;
* a different content revision produces a different exact anchor id
  while historical anchors stay inspectable;
* producer/lineage/timestamps are evidence-only and excluded from
  identity, so two producers reporting the same selector facts under
  one revision converge to one anchor (mirrors PR70/71 acquisition
  convergence);
* unknown selector families, unknown coordinate spaces, render space
  without render state, and float coordinates all fail closed;
* raw Unicode quotes are preserved exactly — no NFC/NFKC folding.

Selector input order is structurally irrelevant: selectors live in
family-keyed slots (at most one per family), so construction order can
never leak into identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from app.kernel.errors import KernelError
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import (
    CanonicalBox,
    CanonicalPoint,
    CanonicalPolygon,
    record_identity_hash,
    to_json_ready,
)

# ---------------------------------------------------------------------------
# Coordinate spaces
# ---------------------------------------------------------------------------

#: Declared coordinate-space registry for this slice. Geometry without a
#: declared space is meaningless: an Office EMU box and a PDF-point box
#: with identical integer tuples must never collide as "the same place".
COORDINATE_SPACE_PDF_PAGE_POINTS = "pdf.page_points.v1"
COORDINATE_SPACE_OFFICE_EMU = "office.emu.v1"
COORDINATE_SPACE_RENDER_PIXEL = "render.pixel.v1"

_AXIS_Y_UP = "y_up"
_AXIS_Y_DOWN = "y_down"


@dataclass(frozen=True)
class CoordinateSpace:
    """A declared, versioned source coordinate space.

    ``space_id`` is the identity-bearing token; ``unit`` and
    ``axis_convention`` are carried alongside so canonical payloads are
    self-describing. Spaces not in the registry are rejected — adding a
    space is an intentional, identity-affecting contract change.
    """

    space_id: str
    unit: str
    axis_convention: str

    @classmethod
    def from_id(cls, space_id: str) -> CoordinateSpace:
        try:
            return _COORDINATE_SPACES[space_id]
        except KeyError:
            raise KernelError(
                f"unknown coordinate space {space_id!r}; declared spaces: "
                f"{sorted(_COORDINATE_SPACES)}"
            ) from None

    @property
    def is_render_space(self) -> bool:
        return self.space_id.startswith("render.")

    def canonical_value(self) -> dict[str, Any]:
        return {
            "space_id": self.space_id,
            "unit": self.unit,
            "axis": self.axis_convention,
        }


_COORDINATE_SPACES: dict[str, CoordinateSpace] = {
    space.space_id: space
    for space in (
        CoordinateSpace(COORDINATE_SPACE_PDF_PAGE_POINTS, "pt", _AXIS_Y_UP),
        CoordinateSpace(COORDINATE_SPACE_OFFICE_EMU, "emu", _AXIS_Y_DOWN),
        CoordinateSpace(COORDINATE_SPACE_RENDER_PIXEL, "px", _AXIS_Y_DOWN),
    )
}


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

SELECTOR_FAMILY_NATIVE = "native"
SELECTOR_FAMILY_QUOTE = "quote"
SELECTOR_FAMILY_POSITION = "position"
SELECTOR_FAMILY_GEOMETRY = "geometry"

SELECTOR_FAMILIES = frozenset(
    {
        SELECTOR_FAMILY_NATIVE,
        SELECTOR_FAMILY_QUOTE,
        SELECTOR_FAMILY_POSITION,
        SELECTOR_FAMILY_GEOMETRY,
    }
)

_BOUNDARY_REGION_INCLUSIVE = "region_inclusive"
_BOUNDARY_ORIGIN_POINT = "origin_point"
_BOUNDARY_CONVENTIONS = frozenset({_BOUNDARY_REGION_INCLUSIVE, _BOUNDARY_ORIGIN_POINT})

_POSITION_SCOPES = frozenset({"content_bytes", "extraction_text"})

_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

#: Locators name page/slide/sheet/package positions and therefore admit
#: package-path separators that record ids do not.
_LOCATOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\\@-]{0,255}$")


@dataclass(frozen=True)
class NativeSelector:
    """Provider/native object identity (strongest selector family).

    Example facts: an OOXML bookmark id inside ``word/document.xml``,
    a PDF indirect-object reference, a worksheet cell reference.
    """

    provider: str
    native_kind: str
    native_id: str
    package_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_PATTERN.fullmatch(
            self.provider
        ):
            raise KernelError(
                f"invalid native provider {self.provider!r} must match "
                f"{_PROVIDER_PATTERN.pattern}"
            )
        if not isinstance(self.native_kind, str) or not self.native_kind:
            raise KernelError(f"invalid native_kind: {self.native_kind!r}")
        if not isinstance(self.native_id, str) or not self.native_id:
            raise KernelError(f"invalid native_id: {self.native_id!r}")
        if self.package_path is not None and (
            not isinstance(self.package_path, str) or not self.package_path
        ):
            raise KernelError(f"invalid package_path: {self.package_path!r}")

    def canonical_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "family": SELECTOR_FAMILY_NATIVE,
            "provider": self.provider,
            "native_kind": self.native_kind,
            "native_id": self.native_id,
        }
        if self.package_path is not None:
            value["package_path"] = self.package_path
        return value


@dataclass(frozen=True)
class TextQuoteSelector:
    """Exact source text/value quote with local prefix/suffix context.

    ``quote`` is preserved byte-exact — no Unicode normalization. Quote
    selectors can be ambiguous (same text repeated); ambiguity is a
    resolution-time fact, not a reason to weaken the stored evidence.
    """

    quote: str
    prefix: str = ""
    suffix: str = ""

    def __post_init__(self) -> None:
        for name, value in (("quote", self.quote), ("prefix", self.prefix), ("suffix", self.suffix)):
            if not isinstance(value, str):
                raise KernelError(f"invalid {name}: {value!r} must be a string")
        if not self.quote:
            raise KernelError("quote selector requires non-empty quote text")

    def canonical_value(self) -> dict[str, Any]:
        return {
            "family": SELECTOR_FAMILY_QUOTE,
            "quote": self.quote,
            "prefix": self.prefix,
            "suffix": self.suffix,
        }


@dataclass(frozen=True)
class PositionSelector:
    """Byte/character offsets — brittle by declaration.

    Position selectors are exact for one revision and drift immediately
    when content changes. They are accepted as honest evidence but never
    promoted: an anchor whose only selectors are position/geometry is
    classified positional/approximate by :meth:`SourceAnchorRecord.evidence_class`.
    """

    scope: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.scope not in _POSITION_SCOPES:
            raise KernelError(
                f"invalid position scope {self.scope!r}; allowed: {sorted(_POSITION_SCOPES)}"
            )
        for name, value in (("start", self.start), ("end", self.end)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise KernelError(f"invalid {name}: {value!r} must be an int")
        if self.start < 0 or self.end <= self.start:
            raise KernelError(
                f"invalid position range [{self.start}, {self.end}); "
                "must be non-negative and non-empty"
            )

    def canonical_value(self) -> dict[str, Any]:
        return {
            "family": SELECTOR_FAMILY_POSITION,
            "scope": self.scope,
            "start": self.start,
            "end": self.end,
        }


_GeometryValue = CanonicalPoint | CanonicalBox | CanonicalPolygon


def _validate_render_state(render_state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if render_state is None:
        return None
    if not isinstance(render_state, Mapping) or not render_state:
        raise KernelError("render_state must be a non-empty mapping when present")
    ready: dict[str, Any] = {}
    for key, value in render_state.items():
        if not isinstance(key, str) or not key:
            raise KernelError(f"invalid render_state key: {key!r}")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise KernelError(
                f"invalid render_state[{key!r}]: {value!r}; only str/int render "
                "facts enter identity (floats are rejected by the canonical layer)"
            )
        ready[key] = value
    return ready


@dataclass(frozen=True)
class GeometrySelector:
    """Fixed-point geometry in a declared coordinate space.

    Coordinates are the PR61 quantized integers. Native-space geometry
    is exact source evidence; render-space geometry is approximate by
    construction and must carry the render state (renderer, version,
    scale) needed to reproduce it.
    """

    geometry: _GeometryValue
    space: CoordinateSpace
    boundary_convention: str
    render_state: Mapping[str, Any] | None = None
    approximate: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.space, str):
            object.__setattr__(self, "space", CoordinateSpace.from_id(self.space))
        if not isinstance(self.space, CoordinateSpace):
            raise KernelError(f"space must be a CoordinateSpace, got {self.space!r}")
        if self.boundary_convention not in _BOUNDARY_CONVENTIONS:
            raise KernelError(
                f"invalid boundary_convention {self.boundary_convention!r}; "
                f"allowed: {sorted(_BOUNDARY_CONVENTIONS)}"
            )
        point_like = isinstance(self.geometry, CanonicalPoint)
        if point_like and self.boundary_convention != _BOUNDARY_ORIGIN_POINT:
            raise KernelError("point geometry requires the origin_point boundary convention")
        if not point_like and self.boundary_convention != _BOUNDARY_REGION_INCLUSIVE:
            raise KernelError("box/polygon geometry requires the region_inclusive boundary convention")
        if self.space.is_render_space:
            if not self.render_state:
                raise KernelError(
                    f"render space {self.space.space_id!r} requires render_state "
                    "(renderer identity/version/scale) so the geometry is reproducible"
                )
            if not self.approximate:
                raise KernelError(
                    "render-derived geometry is approximate by construction; "
                    "approximate=False is a false exactness claim"
                )
        elif self.render_state is not None:
            raise KernelError(
                "render_state belongs to render spaces only; native-space "
                "geometry must not carry renderer metadata"
            )
        object.__setattr__(
            self, "render_state", _validate_render_state(self.render_state)
        )

    def canonical_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "family": SELECTOR_FAMILY_GEOMETRY,
            "space": self.space.canonical_value(),
            "boundary": self.boundary_convention,
            "geometry": self.geometry.canonical_value(),
            "approximate": self.approximate,
        }
        if self.render_state is not None:
            value["render_state"] = dict(self.render_state)
        return value


# ---------------------------------------------------------------------------
# Geometry rematerialization (canonical dicts -> typed geometry)
# ---------------------------------------------------------------------------


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KernelError(
            f"canonical geometry field {name} must be an int, got {value!r}; "
            "float coordinates cannot enter anchor identity or rematerialization"
        )
    return value


def geometry_from_canonical(value: Mapping[str, Any]) -> _GeometryValue:
    """Rebuild typed geometry from its canonical dict, failing closed.

    Only already-quantized integers are accepted; any float, missing
    profile tag, or unknown geometry kind is rejected.
    """
    if not isinstance(value, Mapping):
        raise KernelError(f"canonical geometry must be a mapping, got {value!r}")
    kind = value.get("geometry")
    profile = value.get("profile")
    if profile is None:
        raise KernelError("canonical geometry is missing its profile tag")
    if kind == "point":
        return CanonicalPoint(
            x=_require_int(value.get("x"), "x"), y=_require_int(value.get("y"), "y")
        )
    if kind == "box":
        return CanonicalBox(
            x0=_require_int(value.get("x0"), "x0"),
            y0=_require_int(value.get("y0"), "y0"),
            x1=_require_int(value.get("x1"), "x1"),
            y1=_require_int(value.get("y1"), "y1"),
        )
    if kind == "polygon":
        points = value.get("points")
        if not isinstance(points, list):
            raise KernelError("canonical polygon points must be a list")
        vertices = tuple(
            CanonicalPoint(
                x=_require_int(point.get("x"), "x"), y=_require_int(point.get("y"), "y")
            )
            for point in points
            if isinstance(point, Mapping)
        )
        if len(vertices) != len(points):
            raise KernelError("canonical polygon vertices must all be mappings")
        return CanonicalPolygon(points=vertices)
    raise KernelError(f"unknown canonical geometry kind {kind!r}")


# ---------------------------------------------------------------------------
# The anchor record
# ---------------------------------------------------------------------------

_RECORD_CLASS_SOURCE_ANCHOR = "source_anchor"
RECORD_TYPE_SOURCE_ANCHOR = "marker.kernel.source_anchor.v1"

_EVIDENCE_CLASS_EXACT_NATIVE = "exact_native"
_EVIDENCE_CLASS_QUOTE_CONTEXT = "quote_context"
_EVIDENCE_CLASS_NATIVE_GEOMETRY = "native_geometry"
_EVIDENCE_CLASS_APPROXIMATE_GEOMETRY = "approximate_geometry"
_EVIDENCE_CLASS_POSITIONAL = "positional_only"


def _parse_selector(family: str, value: Any) -> Any:
    """Fail-closed selector parsing (typed object or canonical dict)."""
    typed = _SELECTOR_TYPES[family]
    if isinstance(value, typed):
        return value
    if isinstance(value, Mapping):
        if value.get("family") not in (None, family):
            raise KernelError(
                f"selector family mismatch: slot {family!r} carries "
                f"family {value.get('family')!r}"
            )
        unknown = set(value) - _EXPECTED_SELECTOR_KEYS[family]
        if unknown:
            raise KernelError(
                f"unknown {family!r} selector fields {sorted(unknown)}; "
                "identity-bearing extensions must be declared, not ignored"
            )
        if family == SELECTOR_FAMILY_NATIVE:
            return NativeSelector(
                provider=value["provider"],
                native_kind=value["native_kind"],
                native_id=value["native_id"],
                package_path=value.get("package_path"),
            )
        if family == SELECTOR_FAMILY_QUOTE:
            return TextQuoteSelector(
                quote=value["quote"], prefix=value.get("prefix", ""), suffix=value.get("suffix", "")
            )
        if family == SELECTOR_FAMILY_POSITION:
            return PositionSelector(scope=value["scope"], start=value["start"], end=value["end"])
        space_value = value.get("space")
        if not isinstance(space_value, Mapping) or "space_id" not in space_value:
            raise KernelError("geometry selector requires a declared coordinate space")
        render_state = value.get("render_state")
        return GeometrySelector(
            geometry=geometry_from_canonical(value["geometry"]),
            space=CoordinateSpace.from_id(space_value["space_id"]),
            boundary_convention=value["boundary"],
            render_state=dict(render_state) if render_state is not None else None,
            approximate=value["approximate"],
        )
    raise KernelError(
        f"selector for family {family!r} must be a typed selector or a "
        f"canonical mapping, got {type(value).__name__}"
    )


_EXPECTED_SELECTOR_KEYS: dict[str, frozenset[str]] = {
    SELECTOR_FAMILY_NATIVE: frozenset(
        {"family", "provider", "native_kind", "native_id", "package_path"}
    ),
    SELECTOR_FAMILY_QUOTE: frozenset({"family", "quote", "prefix", "suffix"}),
    SELECTOR_FAMILY_POSITION: frozenset({"family", "scope", "start", "end"}),
    SELECTOR_FAMILY_GEOMETRY: frozenset(
        {"family", "space", "boundary", "geometry", "render_state", "approximate"}
    ),
}

_SELECTOR_TYPES: dict[str, type] = {
    SELECTOR_FAMILY_NATIVE: NativeSelector,
    SELECTOR_FAMILY_QUOTE: TextQuoteSelector,
    SELECTOR_FAMILY_POSITION: PositionSelector,
    SELECTOR_FAMILY_GEOMETRY: GeometrySelector,
}


@dataclass(kw_only=True)
class SourceAnchorRecord(KernelRecord):
    """Revision-bound layered source anchor (identity contract above)."""

    record_class: ClassVar[str] = _RECORD_CLASS_SOURCE_ANCHOR
    record_type: ClassVar[str] = RECORD_TYPE_SOURCE_ANCHOR
    schema_version: ClassVar[str] = "1.0.0"

    content_revision_ref: str
    selectors: Mapping[str, Any]
    #: page/slide/sheet/package location string, e.g. ``pdf:page:2`` or
    #: ``ooxml:word/document.xml``
    locator: str | None = None
    #: producer/lineage/timestamps — evidence only, never identity
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.content_revision_ref, field_name="content_revision_ref")
        if self.locator is not None:
            if not isinstance(self.locator, str) or not _LOCATOR_PATTERN.fullmatch(self.locator):
                raise KernelError(
                    f"invalid locator: {self.locator!r} must match {_LOCATOR_PATTERN.pattern}"
                )
        if not isinstance(self.selectors, Mapping) or not self.selectors:
            raise KernelError(
                "an anchor requires at least one selector; selector-free "
                "anchors have no source evidence to resolve"
            )
        unknown = set(self.selectors) - SELECTOR_FAMILIES
        if unknown:
            raise KernelError(
                f"unknown selector families {sorted(unknown)}; allowed families: "
                f"{sorted(SELECTOR_FAMILIES)} — unknown identity-bearing "
                "extensions fail closed"
            )
        parsed = {family: _parse_selector(family, value) for family, value in self.selectors.items()}
        self.selectors = parsed
        if not isinstance(self.evidence, Mapping):
            raise KernelError(f"evidence must be a mapping, got {self.evidence!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "content_revision_ref": self.content_revision_ref,
            "locator": self.locator,
            "selectors": {
                family: selector.canonical_value()
                for family, selector in self.selectors.items()
            },
        }

    def anchor_id(self) -> str:
        """Deterministic identity of this anchor under the framing domain."""
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=to_json_ready(self.identity_payload()),
        )

    def evidence_class(self) -> str:
        """Derived (non-identity) strength classification.

        Complementary selectors strengthen resolution, but the class
        never fabricates exactness: positional-only anchors stay
        positional, render geometry stays approximate.
        """
        selectors = self.selectors
        if SELECTOR_FAMILY_NATIVE in selectors:
            return _EVIDENCE_CLASS_EXACT_NATIVE
        if SELECTOR_FAMILY_QUOTE in selectors:
            return _EVIDENCE_CLASS_QUOTE_CONTEXT
        geometry = selectors.get(SELECTOR_FAMILY_GEOMETRY)
        if isinstance(geometry, GeometrySelector):
            if geometry.approximate:
                return _EVIDENCE_CLASS_APPROXIMATE_GEOMETRY
            return _EVIDENCE_CLASS_NATIVE_GEOMETRY
        return _EVIDENCE_CLASS_POSITIONAL

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> SourceAnchorRecord:
        """Rematerialize a typed anchor from a replayed record payload.

        ``record_id`` is the durable id the anchor was committed under;
        unknown payload fields fail closed so durable anchors can never
        silently drop identity-bearing extensions.
        """
        if not isinstance(payload, Mapping):
            raise KernelError(f"anchor payload must be a mapping, got {payload!r}")
        allowed = {"content_revision_ref", "locator", "selectors"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown anchor payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            content_revision_ref=payload["content_revision_ref"],
            locator=payload.get("locator"),
            selectors=dict(payload["selectors"]),
        )

