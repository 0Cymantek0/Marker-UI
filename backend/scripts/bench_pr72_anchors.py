"""Operational characterization for PR72 anchors + reading order.

Measures the costs the slice must not hide (plan §12):

* canonical bytes and identity time per anchor, and per order edge;
* batch canonicalization/serialization time for a representative page;
* local split/restitch cost versus full graph reconstruction on a
  1000-node document graph (neighborhood scaling evidence);
* confirmation that no path performs document-wide all-pairs work.

Run:  python scripts/bench_pr72_anchors.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.kernel.anchors import (  # noqa: E402
    COORDINATE_SPACE_PDF_PAGE_POINTS,
    GeometrySelector,
    NativeSelector,
    SourceAnchorRecord,
    TextQuoteSelector,
)
from app.kernel.reading_order import (  # noqa: E402
    NODE_KIND_CONTENT,
    NODE_KIND_REGION,
    ORDER_EDGE_BEFORE,
    ORDER_EDGE_CONTAINS,
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    order_confidence,
    split_node,
)
from app.utils.canonical import CanonicalPoint, canonical_json_bytes, to_json_ready  # noqa: E402

CONF = order_confidence("1.0")
MEASUREMENTS_PATH = Path(__file__).resolve().parents[2] / "docs" / "reference" / "measurements"


def make_anchor(i: int) -> SourceAnchorRecord:
    return SourceAnchorRecord(
        record_id=f"bench-anchor-{i}",
        content_revision_ref="rev-bench",
        locator="pdf:page:1",
        selectors={
            "native": NativeSelector("ooxml", "bookmark", str(i), "word/document.xml"),
            "quote": TextQuoteSelector(f"Anchor sentence number {i}.", "Context before ", " after"),
            "geometry": GeometrySelector(
                geometry=CanonicalPoint.from_coordinates(72 + i, 720 - i),
                space=COORDINATE_SPACE_PDF_PAGE_POINTS,
                boundary_convention="origin_point",
            ),
        },
        evidence={"producer": "bench"},
    )


def make_document_graph(node_count: int) -> ReadingOrderGraph:
    nodes = [OrderNode("page", NODE_KIND_REGION)]
    edges = []
    for i in range(node_count):
        node_id = f"n{i:05d}"
        nodes.append(OrderNode(node_id, NODE_KIND_CONTENT, anchor_ref=f"bench-anchor-{i}"))
        edges.append(
            OrderEdge(ORDER_EDGE_CONTAINS, "page", node_id, "layout", CONF)
        )
        if i > 0:
            edges.append(
                OrderEdge(ORDER_EDGE_BEFORE, f"n{i - 1:05d}", node_id, "layout", CONF)
            )
    return ReadingOrderGraph.build(nodes, edges)


def timed(fn, *, repeat: int = 3):
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def main() -> int:
    results: dict[str, object] = {}

    # --- anchor batch characterization -------------------------------
    anchors = [make_anchor(i) for i in range(1000)]

    anchor_payload_bytes = [
        canonical_json_bytes(to_json_ready(a.identity_payload())) for a in anchors[:100]
    ]
    results["anchor_payload_bytes_avg"] = round(
        sum(len(b) for b in anchor_payload_bytes) / len(anchor_payload_bytes), 1
    )

    start = time.perf_counter()
    anchor_ids = [a.anchor_id() for a in anchors]
    results["anchor_identity_1000_s"] = round(time.perf_counter() - start, 4)
    results["anchor_identity_us_each"] = round(
        results["anchor_identity_1000_s"] * 1_000_000 / len(anchors), 1
    )
    results["anchor_id_example"] = anchor_ids[0]

    # --- graph serialization ------------------------------------------
    graph = make_document_graph(1000)
    graph_payload = canonical_json_bytes(to_json_ready(graph.canonical_payload()))
    results["graph_nodes"] = 1000
    results["graph_edges"] = len(graph.edges)
    results["graph_payload_bytes"] = len(graph_payload)
    results["graph_bytes_per_edge"] = round(len(graph_payload) / len(graph.edges), 1)
    results["graph_serialize_s"] = round(timed(lambda: graph.canonical_payload()), 4)

    # --- local split vs full reconstruction ---------------------------
    def do_split():
        return split_node(
            graph,
            "n00500",
            [OrderNode("n00500a", NODE_KIND_CONTENT), OrderNode("n00500b", NODE_KIND_CONTENT)],
            child_order=["n00500a", "n00500b"],
            producer="specialist",
        )

    split_result = do_split()
    results["split_neighborhood_nodes"] = len(split_result.neighborhood)
    results["split_edges_rewritten"] = split_result.rewritten_edge_count
    results["split_edges_preserved"] = split_result.preserved_edge_count
    results["split_s"] = round(timed(do_split), 6)

    def full_reconstruction():
        nodes = [n for n in graph.nodes if n.node_id != "n00500"] + [
            OrderNode("n00500a", NODE_KIND_CONTENT),
            OrderNode("n00500b", NODE_KIND_CONTENT),
        ]
        rebuilt_edges = [e for e in graph.edges
                         if "n00500" not in (e.source_id, e.target_id)]
        rebuilt_edges += [
            OrderEdge(ORDER_EDGE_CONTAINS, "page", "n00500a", "specialist", CONF),
            OrderEdge(ORDER_EDGE_CONTAINS, "page", "n00500b", "specialist", CONF),
            OrderEdge(ORDER_EDGE_BEFORE, "n00499", "n00500a", "specialist", CONF),
            OrderEdge(ORDER_EDGE_BEFORE, "n00500a", "n00500b", "specialist", CONF),
            OrderEdge(ORDER_EDGE_BEFORE, "n00500b", "n00501", "specialist", CONF),
        ]
        return ReadingOrderGraph.build(nodes, rebuilt_edges)

    results["full_rebuild_s"] = round(timed(full_reconstruction), 6)
    results["split_vs_rebuild_ratio"] = round(
        results["split_s"] / results["full_rebuild_s"], 3
    )

    # Neighborhood is degree-bounded, not document-bounded: prove the
    # same split on a 10x graph touches the same node count.
    bigger = make_document_graph(10000)
    bigger_result = split_node(
        bigger,
        "n05000",
        [OrderNode("a", NODE_KIND_CONTENT), OrderNode("b", NODE_KIND_CONTENT)],
        producer="specialist",
    )
    results["split_neighborhood_nodes_10x_graph"] = len(bigger_result.neighborhood)
    results["neighborhood_is_graph_size_independent"] = (
        len(bigger_result.neighborhood) == len(split_result.neighborhood)
    )

    MEASUREMENTS_PATH.mkdir(parents=True, exist_ok=True)
    output = MEASUREMENTS_PATH / "pr72-anchor-reading-order.json"
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
