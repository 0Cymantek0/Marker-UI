"""Adoption/compatibility demonstration for the canonical identity layer.

Proves, with current Marker-UI-shaped data:

1. engine-facing float geometry (Hybrid OCR targets, table evidence
   cells) crosses the new canonical fixed-point boundary cleanly;
2. a future-shaped identity record can be built from that data today,
   alongside (not instead of) the legacy stable-ID scheme;
3. legacy public identifiers are byte-for-byte unchanged.
"""

from __future__ import annotations

from app.hybrid_ocr.contracts import HybridTarget, TargetKind
from app.services.chunking import MarkdownBlock, _stable_chunk_id
from app.utils.canonical import (
    CanonicalBox,
    CanonicalPolygon,
    CanonicalSet,
    DecimalValue,
    record_identity_hash,
)


def _hybrid_style_target() -> HybridTarget:
    # Shaped like collector.py produces: engine floats, pixels.
    return HybridTarget(
        target_id="t1",
        document_id="doc1",
        page_index=1,
        page_number=2,
        block_id="b1",
        block_type="table",
        target_kind=TargetKind.TABLE,
        bbox=[72.0, 110.5, 540.25, 660.75],
        polygon=[[72.0, 110.5], [540.25, 110.5], [540.25, 660.75], [72.0, 660.75]],
        crop_path="crops/t1.png",
        crop_width=468,
        crop_height=550,
        baseline_text="table",
        baseline_html="<table></table>",
        baseline_confidence=0.9,
        baseline_source="marker",
    )


def test_hybrid_ocr_bbox_crosses_the_canonical_boundary() -> None:
    target = _hybrid_style_target()
    box = CanonicalBox.from_bbox(target.bbox)
    assert box.canonical_value() == {
        "geometry": "box",
        "profile": "marker.geometry.fixed_point.v1",
        "x0": 72000,
        "y0": 110500,
        "x1": 540250,
        "y1": 660750,
    }


def test_table_evidence_cell_record_gets_stable_canonical_identity() -> None:
    # Shaped like conversion/table_evidence.py enriched cell payloads:
    # raw pass-through floats for bbox/polygon plus text and page info.
    cell = {
        "cell_id": "r0c1",
        "text": "1 000,50 €",
        "bbox": [110.0, 200.25, 220.75, 240.0],
        "polygon": [[110.0, 200.25], [220.75, 200.25], [220.75, 240.0], [110.0, 240.0]],
        "page_number": 2,
    }
    payload = {
        "cell_id": cell["cell_id"],
        "text": cell["text"],
        "page": cell["page_number"],
        "bbox": CanonicalBox.from_bbox(cell["bbox"]),
        "polygon": CanonicalPolygon.from_coordinates(cell["polygon"]),
        "amount": DecimalValue("1000.50"),
        "tags": CanonicalSet(["finance", "table-cell"]),
    }
    first = record_identity_hash(
        record_type="marker.table_cell_identity.v1",
        schema_version="marker.table_cell_identity.v1",
        payload=payload,
    )
    # Same semantics rebuilt in a different construction order.
    rebuilt = record_identity_hash(
        payload={
            "tags": CanonicalSet(["table-cell", "finance"]),
            "amount": DecimalValue("1000.50"),
            "polygon": CanonicalPolygon.from_coordinates(
                [[110.0, 200.25], [220.75, 200.25], [220.75, 240.0], [110.0, 240.0]]
            ),
            "bbox": CanonicalBox.from_bbox([110.0, 200.25, 220.75, 240.0]),
            "page": 2,
            "text": "1 000,50 €",
            "cell_id": "r0c1",
        },
        record_type="marker.table_cell_identity.v1",
        schema_version="marker.table_cell_identity.v1",
    )
    assert first == rebuilt
    assert first.startswith("sha256:")


def test_legacy_stable_chunk_ids_are_unchanged() -> None:
    # Committed constant proving the legacy marker.chunks.v1 scheme is
    # untouched by the new identity layer.
    block = MarkdownBlock(
        text="Hello chunk",
        start_line=1,
        end_line=1,
        char_start=0,
        char_end=11,
        kind="text",
    )
    assert _stable_chunk_id("a" * 64, block, "Hello chunk") == "stable_ff75e14b994bb826"
    # The new layer is an additive helper with a different output format;
    # it does not touch the legacy scheme.
    assert record_identity_hash(
        record_type="marker.chunks.v1",
        schema_version="marker.chunks.v1",
        payload={"text": "Hello chunk"},
    ).startswith("sha256:")
