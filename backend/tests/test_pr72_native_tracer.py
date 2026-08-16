"""PDF + Office tracer-bullet evidence (PR72 §9.5).

One public anchor concept fed by two real extraction seams: a
hand-assembled two-column PDF (page-point geometry, text runs) and a
native OOXML package (bookmark identities, package paths, EMU drawing
geometry). Format differences must be represented, never flattened.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from app.conversion.native_sources import extract_docx_facts, extract_pdf_facts
from app.kernel.anchors import (
    COORDINATE_SPACE_OFFICE_EMU,
    COORDINATE_SPACE_PDF_PAGE_POINTS,
    GeometrySelector,
    NativeSelector,
    SourceAnchorRecord,
    TextQuoteSelector,
)
from app.kernel.reading_order import (
    NODE_KIND_REGION,
    ORDER_EDGE_BEFORE,
    ORDER_EDGE_CONTAINS,
    ORDER_EDGE_MEMBER_OF,
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    linearize,
    order_confidence,
)
from app.utils.canonical import CanonicalBox, CanonicalPoint
from tests.pr72_fixtures import build_native_docx, build_two_column_pdf

CONF = order_confidence("1.0")


def _blob_key(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def make_pdf_anchors(pdf_bytes: bytes, revision_ref: str) -> list[SourceAnchorRecord]:
    """Anchor every extracted text run (point + quote) and rectangle."""
    facts = extract_pdf_facts(pdf_bytes)
    page = facts.pages[0]
    anchors = []
    for run in page.text_runs:
        anchors.append(
            SourceAnchorRecord(
                record_id=f"pdf-run-{run.origin_x}-{run.origin_y}",
                content_revision_ref=revision_ref,
                locator=f"pdf:page:{run.page_number}",
                selectors={
                    "quote": TextQuoteSelector(quote=run.text),
                    "geometry": GeometrySelector(
                        geometry=CanonicalPoint.from_coordinates(
                            run.origin_x, run.origin_y
                        ),
                        space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                        boundary_convention="origin_point",
                    ),
                },
                evidence={"seam": "pypdf-content-stream", "media_box": list(page.media_box)},
            )
        )
    for rect in page.rectangles:
        anchors.append(
            SourceAnchorRecord(
                record_id=f"pdf-rect-{rect.x}-{rect.y}",
                content_revision_ref=revision_ref,
                locator=f"pdf:page:{rect.page_number}",
                selectors={
                    "geometry": GeometrySelector(
                        geometry=CanonicalBox.from_coordinates(
                            rect.x, rect.y, Decimal(rect.x) + Decimal(rect.width),
                            Decimal(rect.y) + Decimal(rect.height),
                        ),
                        space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                        boundary_convention="region_inclusive",
                    ),
                },
                evidence={"seam": "pypdf-content-stream"},
            )
        )
    return anchors


def make_docx_anchors(docx_bytes: bytes, revision_ref: str) -> list[SourceAnchorRecord]:
    """Anchor bookmarked paragraphs (native + quote) and the EMU drawing."""
    facts = extract_docx_facts(docx_bytes)
    anchors = []
    for paragraph in facts.paragraphs:
        for bookmark in paragraph.bookmarks:
            selectors = {
                "native": NativeSelector(
                    provider="ooxml",
                    native_kind="bookmark",
                    native_id=bookmark.bookmark_id,
                    package_path="word/document.xml",
                )
            }
            if paragraph.text:
                selectors["quote"] = TextQuoteSelector(quote=paragraph.text)
            anchors.append(
                SourceAnchorRecord(
                    record_id=f"docx-bkm-{bookmark.bookmark_id}",
                    content_revision_ref=revision_ref,
                    locator="ooxml:word/document.xml",
                    selectors=selectors,
                    evidence={"seam": "ooxml-package", "bookmark_name": bookmark.name},
                )
            )
        for drawing in paragraph.drawings:
            anchors.append(
                SourceAnchorRecord(
                    record_id=f"docx-draw-{drawing.offset_x_emu}-{drawing.offset_y_emu}",
                    content_revision_ref=revision_ref,
                    locator="ooxml:word/document.xml",
                    selectors={
                        "native": NativeSelector(
                            provider="ooxml",
                            native_kind="anchored_drawing",
                            native_id="7",
                            package_path="word/document.xml",
                        ),
                        "geometry": GeometrySelector(
                            geometry=CanonicalBox.from_coordinates(
                                drawing.offset_x_emu,
                                drawing.offset_y_emu,
                                drawing.offset_x_emu + drawing.extent_cx_emu,
                                drawing.offset_y_emu + drawing.extent_cy_emu,
                            ),
                            space=COORDINATE_SPACE_OFFICE_EMU,
                            boundary_convention="region_inclusive",
                        ),
                    },
                    evidence={"seam": "ooxml-package"},
                )
            )
    return anchors


class TestPdfTracer:
    def test_extracts_real_two_column_facts(self):
        pdf = build_two_column_pdf()
        facts = extract_pdf_facts(pdf)
        page = facts.pages[0]
        assert page.media_box == ("0", "0", "612", "792")
        assert len(page.text_runs) == 4 and len(page.rectangles) == 2
        left = [r for r in page.text_runs if Decimal(r.origin_x) < Decimal("306")]
        right = [r for r in page.text_runs if Decimal(r.origin_x) >= Decimal("306")]
        assert len(left) == 2 and len(right) == 2

    def test_pdf_anchors_bind_revision_and_stay_deterministic(self):
        pdf = build_two_column_pdf()
        revision_ref = f"rev-{_blob_key(pdf)[:16]}"
        first = make_pdf_anchors(pdf, revision_ref)
        # Simulated restart: rebuild fixture, re-extract, re-anchor.
        second = make_pdf_anchors(build_two_column_pdf(), revision_ref)
        assert [a.anchor_id() for a in first] == [a.anchor_id() for a in second]
        assert all(a.content_revision_ref == revision_ref for a in first)
        # Evidence-only seam metadata never touches identity.
        stripped = [
            SourceAnchorRecord(
                record_id=a.record_id,
                content_revision_ref=a.content_revision_ref,
                locator=a.locator,
                selectors=a.selectors,
            )
            for a in first
        ]
        assert [a.anchor_id() for a in stripped] == [a.anchor_id() for a in first]

    def test_same_quote_different_column_is_two_anchors(self):
        pdf = build_two_column_pdf()
        revision_ref = "rev-pdf"
        anchors = {a.selectors["quote"].quote: a for a in make_pdf_anchors(pdf, revision_ref)
                   if "quote" in a.selectors}
        left = anchors["Left column first line."]
        right = anchors["Right column first line."]
        assert left.anchor_id() != right.anchor_id()
        assert left.selectors["geometry"].space.space_id == "pdf.page_points.v1"

    def test_new_content_revision_rebinds_every_anchor(self):
        pdf = build_two_column_pdf()
        old = make_pdf_anchors(pdf, "rev-pdf-1")
        new = make_pdf_anchors(pdf, "rev-pdf-2")
        assert {a.anchor_id() for a in old}.isdisjoint({a.anchor_id() for a in new})


class TestDocxTracer:
    def test_extracts_native_package_facts(self):
        docx = build_native_docx()
        facts = extract_docx_facts(docx)
        assert "word/document.xml" in facts.package_parts
        bookmark_ids = [b.bookmark_id for p in facts.paragraphs for b in p.bookmarks]
        assert bookmark_ids == ["0", "1", "2"]
        drawings = [d for p in facts.paragraphs for d in p.drawings]
        assert len(drawings) == 1
        assert (drawings[0].offset_x_emu, drawings[0].extent_cx_emu) == (1828800, 914400)

    def test_docx_anchors_carry_native_and_emu_evidence(self):
        docx = build_native_docx()
        anchors = make_docx_anchors(docx, "rev-docx-1")
        bookmark_anchors = [a for a in anchors if "quote" in a.selectors]
        drawing_anchor = next(a for a in anchors if a.selectors["native"].native_kind == "anchored_drawing")

        assert all(a.evidence_class() == "exact_native" for a in anchors)
        for anchor in bookmark_anchors:
            native = anchor.selectors["native"]
            assert native.package_path == "word/document.xml"
        geometry = drawing_anchor.selectors["geometry"]
        assert geometry.space.space_id == "office.emu.v1"
        assert geometry.approximate is False
        # EMU integers quantized into the fixed-point profile (x1000).
        assert (geometry.geometry.x0, geometry.geometry.x1) == (1828800000, 2743200000)

    def test_pdf_points_and_office_emu_stay_distinct(self):
        pdf = build_two_column_pdf()
        docx = build_native_docx()
        pdf_anchor = next(
            a for a in make_pdf_anchors(pdf, "rev-shared")
            if isinstance(a.selectors.get("geometry"), GeometrySelector)
            and a.selectors["geometry"].boundary_convention == "region_inclusive"
        )
        docx_anchor = next(
            a for a in make_docx_anchors(docx, "rev-shared")
            if isinstance(a.selectors.get("geometry"), GeometrySelector)
        )
        assert (
            pdf_anchor.selectors["geometry"].space.space_id
            != docx_anchor.selectors["geometry"].space.space_id
        )
        assert pdf_anchor.anchor_id() != docx_anchor.anchor_id()

    def test_one_envelope_serializes_both_formats(self):
        pdf = build_two_column_pdf()
        docx = build_native_docx()
        all_anchors = make_pdf_anchors(pdf, "rev-pdf") + make_docx_anchors(docx, "rev-docx")
        payloads = [a.identity_payload() for a in all_anchors]
        # Same envelope keys everywhere; format specifics live inside
        # selectors, never in a second envelope shape.
        assert all(set(p) == {"content_revision_ref", "locator", "selectors"} for p in payloads)
        assert all(SourceAnchorRecord.from_payload(p, record_id="x").anchor_id() == a.anchor_id()
                   for p, a in zip(payloads, all_anchors))


class TestTracerReadingOrder:
    def test_two_columns_remain_partially_ordered_from_real_facts(self):
        pdf = build_two_column_pdf()
        facts = extract_pdf_facts(pdf)
        page = facts.pages[0]
        revision_ref = f"rev-{_blob_key(pdf)[:16]}"
        anchors = {a.record_id: a for a in make_pdf_anchors(pdf, revision_ref)}

        nodes = [
            OrderNode(node_id="page-1", kind=NODE_KIND_REGION),
            OrderNode(node_id="col-left", kind=NODE_KIND_REGION),
            OrderNode(node_id="col-right", kind=NODE_KIND_REGION),
        ]
        edges = [
            OrderEdge(kind=ORDER_EDGE_CONTAINS, source_id="page-1", target_id="col-left",
                      producer="layout", confidence=CONF),
            OrderEdge(kind=ORDER_EDGE_CONTAINS, source_id="page-1", target_id="col-right",
                      producer="layout", confidence=CONF),
        ]
        for run in page.text_runs:
            node_id = f"run-{run.origin_x}-{run.origin_y}"
            column = "col-left" if Decimal(run.origin_x) < Decimal("306") else "col-right"
            nodes.append(OrderNode(node_id=node_id, anchor_ref=anchors[f"pdf-run-{run.origin_x}-{run.origin_y}"].record_id))
            edges.append(
                OrderEdge(kind=ORDER_EDGE_MEMBER_OF, source_id=node_id, target_id=column,
                          producer="layout", confidence=CONF)
            )
        # Within-column order from extracted baseline origins (PDF y-up:
        # larger y reads first). Cross-column order is NOT asserted.
        by_column: dict[str, list] = {"col-left": [], "col-right": []}
        for run in page.text_runs:
            column = "col-left" if Decimal(run.origin_x) < Decimal("306") else "col-right"
            by_column[column].append(run)
        for column, runs in by_column.items():
            ordered = sorted(runs, key=lambda r: -Decimal(r.origin_y))
            for earlier, later in zip(ordered, ordered[1:]):
                edges.append(
                    OrderEdge(
                        kind=ORDER_EDGE_BEFORE,
                        source_id=f"run-{earlier.origin_x}-{earlier.origin_y}",
                        target_id=f"run-{later.origin_x}-{later.origin_y}",
                        producer="layout",
                        confidence=CONF,
                    )
                )

        graph = ReadingOrderGraph.build(nodes, edges)
        view = linearize(graph)
        sequence = view.sequence
        assert sequence.index("run-72-720") < sequence.index("run-72-700")
        assert sequence.index("run-316-720") < sequence.index("run-316-700")
        # No cross-column edge was invented anywhere.
        left_ids = {"run-72-720", "run-72-700"}
        right_ids = {"run-316-720", "run-316-700"}
        assert all(
            not ({e.source_id, e.target_id} & left_ids and {e.source_id, e.target_id} & right_ids)
            for e in graph.edges
            if e.kind == ORDER_EDGE_BEFORE
        )
        assert any(group == tuple(sorted(left_ids | right_ids)[:2]) or len(group) >= 2
                   for group in view.ambiguous_groups)
        # Deterministic across rebuilds from the same artifact.
        assert ReadingOrderGraph.build(list(reversed(nodes)), list(reversed(edges))).graph_id() == graph.graph_id()
