"""SourceAnchor identity semantics (PR72, plan §9.1/§9.2).

Pure identity-matrix tests over the layered anchor contract: selector
plurality, coordinate-space separation, revision binding, fail-closed
extensions, and honest evidence classification.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.kernel.anchors import (
    COORDINATE_SPACE_OFFICE_EMU,
    COORDINATE_SPACE_PDF_PAGE_POINTS,
    COORDINATE_SPACE_RENDER_PIXEL,
    GeometrySelector,
    NativeSelector,
    PositionSelector,
    SourceAnchorRecord,
    TextQuoteSelector,
    geometry_from_canonical,
)
from app.kernel.errors import KernelError
from app.utils.canonical import CanonicalBox, canonical_json_str, to_json_ready
from app.utils.canonical.errors import CanonicalValueError

REVISION_A = "rev-0001"
REVISION_B = "rev-0002"


def make_anchor(revision: str = REVISION_A, **selectors) -> SourceAnchorRecord:
    return SourceAnchorRecord(
        record_id="anchor-evt-1",
        content_revision_ref=revision,
        locator="pdf:page:1",
        selectors=dict(selectors),
    )


PDF_BOX = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
    space=COORDINATE_SPACE_PDF_PAGE_POINTS,
    boundary_convention="region_inclusive",
)
EMU_BOX = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72000, 640000, 272000, 660000]),
    space=COORDINATE_SPACE_OFFICE_EMU,
    boundary_convention="region_inclusive",
)


class TestAnchorIdentity:
    def test_same_facts_converge_across_construction(self):
        a = make_anchor(geometry=PDF_BOX, quote=TextQuoteSelector(quote="Quarterly"))
        b = SourceAnchorRecord(
            record_id="different-event-id",
            content_revision_ref=REVISION_A,
            locator="pdf:page:1",
            selectors={
                "quote": TextQuoteSelector(quote="Quarterly"),
                "geometry": GeometrySelector(
                    geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
                    space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                    boundary_convention="region_inclusive",
                ),
            },
        )
        assert a.anchor_id() == b.anchor_id()

    def test_equivalent_numeric_geometry_inputs_converge(self):
        # int, exact decimal string, and Decimal of the same value must
        # quantize identically through the fixed-point profile.
        from_int = GeometrySelector(
            geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
            space=COORDINATE_SPACE_PDF_PAGE_POINTS,
            boundary_convention="region_inclusive",
        )
        from_str = GeometrySelector(
            geometry=CanonicalBox.from_bbox(["72.000", "640.0", "272.00", "660.0"]),
            space=COORDINATE_SPACE_PDF_PAGE_POINTS,
            boundary_convention="region_inclusive",
        )
        from_decimal = GeometrySelector(
            geometry=CanonicalBox.from_bbox(
                [Decimal("72"), Decimal(640), Decimal("272.000"), Decimal(660)]
            ),
            space=COORDINATE_SPACE_PDF_PAGE_POINTS,
            boundary_convention="region_inclusive",
        )
        ids = {
            make_anchor(geometry=box).anchor_id() for box in (from_int, from_str, from_decimal)
        }
        assert len(ids) == 1

    def test_selector_construction_order_is_irrelevant(self):
        forward = SourceAnchorRecord(
            record_id="a1",
            content_revision_ref=REVISION_A,
            selectors={"native": NativeSelector("ooxml", "bookmark", "bkm0", "word/document.xml"),
                       "quote": TextQuoteSelector("Quarterly", "Report: ", " revenue"),
                       "geometry": PDF_BOX,
                       "position": PositionSelector("content_bytes", 10, 18)},
        )
        reverse = SourceAnchorRecord(
            record_id="a2",
            content_revision_ref=REVISION_A,
            selectors={"position": PositionSelector("content_bytes", 10, 18),
                       "geometry": GeometrySelector(
                           geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
                           space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                           boundary_convention="region_inclusive",
                       ),
                       "quote": TextQuoteSelector("Quarterly", "Report: ", " revenue"),
                       "native": NativeSelector("ooxml", "bookmark", "bkm0", "word/document.xml")},
        )
        assert forward.anchor_id() == reverse.anchor_id()
        # JCS key sorting makes the canonical payload bytes identical too.
        assert canonical_json_str(to_json_ready(forward.identity_payload())) == canonical_json_str(
            to_json_ready(reverse.identity_payload())
        )

    def test_repeated_construction_is_byte_deterministic(self):
        anchor = make_anchor(geometry=PDF_BOX, quote=TextQuoteSelector("Quarterly"))
        again = make_anchor(geometry=PDF_BOX, quote=TextQuoteSelector("Quarterly"))
        assert anchor.anchor_id() == again.anchor_id()
        assert canonical_json_str(to_json_ready(anchor.identity_payload())) == canonical_json_str(
            to_json_ready(again.identity_payload())
        )

    def test_producer_evidence_is_not_identity(self):
        bare = make_anchor(geometry=PDF_BOX)
        witnessed = SourceAnchorRecord(
            record_id="anchor-evt-2",
            content_revision_ref=REVISION_A,
            locator="pdf:page:1",
            selectors={"geometry": PDF_BOX},
            evidence={"producer": "marker", "observed_at": "2026-08-17T00:00:00Z"},
        )
        assert bare.anchor_id() == witnessed.anchor_id()

    def test_locator_participates_in_identity(self):
        page1 = make_anchor(geometry=PDF_BOX)
        page2 = make_anchor(geometry=PDF_BOX)
        page2.locator = "pdf:page:2"
        assert page1.anchor_id() != page2.anchor_id()


class TestCoordinateSpaceSeparation:
    def test_pdf_points_and_office_emu_cannot_collide(self):
        # Identical quantized tuples, different declared spaces.
        pdf = GeometrySelector(
            geometry=CanonicalBox.from_bbox([1000, 2000, 3000, 4000]),
            space=COORDINATE_SPACE_PDF_PAGE_POINTS,
            boundary_convention="region_inclusive",
        )
        emu = GeometrySelector(
            geometry=CanonicalBox.from_bbox([1000, 2000, 3000, 4000]),
            space=COORDINATE_SPACE_OFFICE_EMU,
            boundary_convention="region_inclusive",
        )
        assert make_anchor(geometry=pdf).anchor_id() != make_anchor(geometry=emu).anchor_id()

    def test_unknown_space_fails_closed(self):
        with pytest.raises(KernelError, match="unknown coordinate space"):
            GeometrySelector(
                geometry=CanonicalBox.from_bbox([1, 2, 3, 4]),
                space="coordinate.made_up.v9",
                boundary_convention="region_inclusive",
            )

    def test_render_space_requires_state_and_approximate(self):
        box = CanonicalBox.from_bbox([10, 20, 30, 40])
        with pytest.raises(KernelError, match="render_state"):
            GeometrySelector(
                geometry=box,
                space=COORDINATE_SPACE_RENDER_PIXEL,
                boundary_convention="region_inclusive",
                approximate=True,
            )
        with pytest.raises(KernelError, match="approximate"):
            GeometrySelector(
                geometry=box,
                space=COORDINATE_SPACE_RENDER_PIXEL,
                boundary_convention="region_inclusive",
                render_state={"renderer": "pdfium", "dpi": 150},
            )

    def test_native_space_rejects_render_state(self):
        with pytest.raises(KernelError, match="render spaces only"):
            GeometrySelector(
                geometry=CanonicalBox.from_bbox([1, 2, 3, 4]),
                space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                boundary_convention="region_inclusive",
                render_state={"renderer": "pdfium"},
            )

    def test_render_state_is_identity_bearing(self):
        base = dict(renderer="pdfium", dpi=150)
        other = dict(renderer="pdfium", dpi=300)
        a = make_anchor(
            geometry=GeometrySelector(
                geometry=CanonicalBox.from_bbox([10, 20, 30, 40]),
                space=COORDINATE_SPACE_RENDER_PIXEL,
                boundary_convention="region_inclusive",
                render_state=base,
                approximate=True,
            )
        )
        b = make_anchor(
            geometry=GeometrySelector(
                geometry=CanonicalBox.from_bbox([10, 20, 30, 40]),
                space=COORDINATE_SPACE_RENDER_PIXEL,
                boundary_convention="region_inclusive",
                render_state=other,
                approximate=True,
            )
        )
        assert a.anchor_id() != b.anchor_id()

    def test_approximate_flag_distinguishes_evidence_class(self):
        exact = make_anchor(geometry=PDF_BOX)
        approximate = make_anchor(
            geometry=GeometrySelector(
                geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
                space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                boundary_convention="region_inclusive",
                approximate=True,
            )
        )
        assert exact.anchor_id() != approximate.anchor_id()
        assert exact.evidence_class() == "native_geometry"
        assert approximate.evidence_class() == "approximate_geometry"


class TestRevisionBinding:
    def test_acl_only_change_does_not_remint(self):
        # An access-policy revision is a separate record family; the
        # anchor payload has no policy field, so minting one cannot
        # touch anchor identity.
        before = make_anchor(geometry=PDF_BOX)
        after = make_anchor(geometry=PDF_BOX)
        assert before.anchor_id() == after.anchor_id()
        assert "policy" not in canonical_json_str(to_json_ready(before.identity_payload()))

    def test_content_revision_change_mints_new_anchor(self):
        old = make_anchor(REVISION_A, geometry=PDF_BOX, quote=TextQuoteSelector("Quarterly"))
        new = make_anchor(REVISION_B, geometry=PDF_BOX, quote=TextQuoteSelector("Quarterly"))
        assert old.anchor_id() != new.anchor_id()

    def test_historical_anchor_stays_inspectable_after_new_revision(self):
        old = make_anchor(REVISION_A, geometry=PDF_BOX)
        make_anchor(REVISION_B, geometry=PDF_BOX)
        assert old.anchor_id() == make_anchor(REVISION_A, geometry=PDF_BOX).anchor_id()
        assert SourceAnchorRecord.from_payload(
            old.identity_payload(), record_id=old.record_id
        ).anchor_id() == old.anchor_id()


class TestSelectorPluralityAndHonesty:
    def test_all_four_families_coexist(self):
        anchor = SourceAnchorRecord(
            record_id="layered-1",
            content_revision_ref=REVISION_A,
            locator="ooxml:word/document.xml",
            selectors={
                "native": NativeSelector("ooxml", "bookmark", "bkm0", "word/document.xml"),
                "quote": TextQuoteSelector("Quarterly", "Report: ", " revenue"),
                "position": PositionSelector("content_bytes", 10, 19),
                "geometry": EMU_BOX,
            },
        )
        assert set(anchor.selectors) == {"native", "quote", "position", "geometry"}
        assert anchor.evidence_class() == "exact_native"

    def test_missing_native_id_leaves_anchor_valid(self):
        anchor = make_anchor(quote=TextQuoteSelector("Quarterly"), geometry=PDF_BOX)
        assert "native" not in anchor.selectors
        assert anchor.evidence_class() == "quote_context"

    def test_position_only_is_classified_brittle(self):
        anchor = make_anchor(position=PositionSelector("content_bytes", 0, 9))
        assert anchor.evidence_class() == "positional_only"

    def test_no_selectors_rejected(self):
        with pytest.raises(KernelError, match="at least one selector"):
            SourceAnchorRecord(
                record_id="empty", content_revision_ref=REVISION_A, selectors={}
            )

    def test_unknown_family_fails_closed(self):
        with pytest.raises(KernelError, match="unknown selector families"):
            SourceAnchorRecord(
                record_id="x",
                content_revision_ref=REVISION_A,
                selectors={"semantic_embedding": {"vector": "not identity"}},
            )

    def test_unknown_selector_fields_fail_closed(self):
        with pytest.raises(KernelError, match="unknown.*fields"):
            SourceAnchorRecord(
                record_id="x",
                content_revision_ref=REVISION_A,
                selectors={
                    "quote": {
                        "family": "quote",
                        "quote": "Quarterly",
                        "prefix": "",
                        "suffix": "",
                        "similarity": "0.99",
                    }
                },
            )

    def test_unicode_quote_preserved_exactly(self):
        composed = TextQuoteSelector(quote="café")
        decomposed = TextQuoteSelector(quote="cafe\u0301")
        assert make_anchor(quote=composed).anchor_id() != make_anchor(quote=decomposed).anchor_id()
        payload = make_anchor(quote=composed).identity_payload()
        assert payload["selectors"]["quote"]["quote"] == "café"

    def test_identical_quote_twice_is_two_valid_distinct_anchors(self):
        # Repeated text on one page: both occurrences are honest anchors;
        # disambiguation needs geometry/native evidence, never invented.
        first = make_anchor(
            quote=TextQuoteSelector("Quarterly"),
            geometry=GeometrySelector(
                geometry=CanonicalBox.from_bbox([72, 700, 272, 720]),
                space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                boundary_convention="region_inclusive",
            ),
        )
        second = make_anchor(
            quote=TextQuoteSelector("Quarterly"),
            geometry=GeometrySelector(
                geometry=CanonicalBox.from_bbox([300, 700, 500, 720]),
                space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                boundary_convention="region_inclusive",
            ),
        )
        assert first.anchor_id() != second.anchor_id()
        # Same quote with no distinguishing selector is ONE anchor id —
        # the ambiguity is a resolution fact, not a stored fork.
        assert make_anchor(quote=TextQuoteSelector("Quarterly")).anchor_id() == make_anchor(
            quote=TextQuoteSelector("Quarterly")
        ).anchor_id()


class TestRematerialization:
    def test_from_payload_round_trips_typed_anchor(self):
        anchor = SourceAnchorRecord(
            record_id="rt-1",
            content_revision_ref=REVISION_A,
            locator="ooxml:word/document.xml",
            selectors={
                "native": NativeSelector("ooxml", "bookmark", "bkm0", "word/document.xml"),
                "quote": TextQuoteSelector("Quarterly", "Report: ", " revenue"),
                "geometry": EMU_BOX,
                "position": PositionSelector("content_bytes", 10, 19),
            },
        )
        rematerialized = SourceAnchorRecord.from_payload(
            anchor.identity_payload(), record_id="rt-1"
        )
        assert rematerialized.anchor_id() == anchor.anchor_id()
        assert isinstance(rematerialized.selectors["geometry"], GeometrySelector)
        assert rematerialized.evidence_class() == anchor.evidence_class()

    def test_from_payload_rejects_float_geometry(self):
        with pytest.raises(KernelError, match="must be an int"):
            geometry_from_canonical(
                {
                    "geometry": "box",
                    "profile": "marker.geometry.fixed_point.v1",
                    "x0": 1.5,
                    "y0": 2,
                    "x1": 3,
                    "y1": 4,
                }
            )

    def test_from_payload_rejects_unknown_fields(self):
        with pytest.raises(KernelError, match="unknown anchor payload fields"):
            SourceAnchorRecord.from_payload(
                {
                    "content_revision_ref": REVISION_A,
                    "locator": None,
                    "selectors": {"quote": TextQuoteSelector("x").canonical_value()},
                    "embedding": "later-mapping-concern",
                },
                record_id="rt-2",
            )

    def test_degenerate_box_rejected_by_geometry_profile(self):
        with pytest.raises(CanonicalValueError):
            CanonicalBox.from_bbox([10, 10, 10, 10])
