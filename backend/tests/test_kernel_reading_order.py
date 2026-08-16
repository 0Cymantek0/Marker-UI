"""Partial-order reading graph + bounded restitch tests (PR72 §9.3/§9.4).

Adversarial fixtures: single-column order, two-column pages with no
justified cross-column order, unresolved side notes, contradictory
constraints, deterministic serialization under shuffled insertion, and
specialist splits that must stay inside a declared neighborhood.
"""

from __future__ import annotations

import random

import pytest

from app.kernel.errors import KernelError, OrderConflictError
from app.kernel.reading_order import (
    EVIDENCE_STATE_ASSERTED,
    EVIDENCE_STATE_UNRESOLVED,
    NODE_KIND_CONTENT,
    NODE_KIND_REGION,
    ORDER_EDGE_BEFORE,
    ORDER_EDGE_CONTAINS,
    ORDER_EDGE_CONTINUES,
    ORDER_EDGE_MEMBER_OF,
    LinearizationView,
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    ReadingOrderRecord,
    linearize,
    order_confidence,
    split_node,
)
from app.utils.canonical import canonical_json_str, to_json_ready

CONF = order_confidence("1.0")


def node(node_id: str, kind: str = NODE_KIND_CONTENT, anchor_ref: str | None = None):
    return OrderNode(node_id=node_id, kind=kind, anchor_ref=anchor_ref)


def before(a: str, b: str, producer: str = "layout", state: str = EVIDENCE_STATE_ASSERTED):
    return OrderEdge(
        kind=ORDER_EDGE_BEFORE, source_id=a, target_id=b, producer=producer,
        confidence=CONF, state=state,
    )


def contains(parent: str, child: str, producer: str = "layout"):
    return OrderEdge(
        kind=ORDER_EDGE_CONTAINS, source_id=parent, target_id=child,
        producer=producer, confidence=CONF,
    )


def member_of(member: str, region: str, producer: str = "layout"):
    return OrderEdge(
        kind=ORDER_EDGE_MEMBER_OF, source_id=member, target_id=region,
        producer=producer, confidence=CONF,
    )


class TestPartialOrderHonesty:
    def test_single_column_order_is_fully_constrained(self):
        graph = ReadingOrderGraph.build(
            [node("p1"), node("p2"), node("p3")],
            [before("p1", "p2"), before("p2", "p3")],
        )
        view = linearize(graph)
        assert view.sequence == ("p1", "p2", "p3")
        assert view.ambiguous_groups == ()

    def test_two_columns_stay_partially_ordered(self):
        graph = ReadingOrderGraph.build(
            [
                node("col-a", NODE_KIND_REGION),
                node("col-b", NODE_KIND_REGION),
                node("a1"), node("a2"), node("b1"), node("b2"),
            ],
            [
                member_of("a1", "col-a"), member_of("a2", "col-a"),
                member_of("b1", "col-b"), member_of("b2", "col-b"),
                before("a1", "a2"), before("b1", "b2"),
            ],
        )
        view = linearize(graph)
        # Within-column order is preserved everywhere.
        assert view.sequence.index("a1") < view.sequence.index("a2")
        assert view.sequence.index("b1") < view.sequence.index("b2")
        # Cross-column order was fabricated by nobody: every cross-column
        # pair shows up together in some ambiguity group, and the graph
        # itself gained no edges (linearize is a pure view).
        flattened_ambiguity = {n for group in view.ambiguous_groups for n in group}
        for a in ("a1", "a2"):
            for b in ("b1", "b2"):
                assert a in flattened_ambiguity and b in flattened_ambiguity
        assert all(e.kind != ORDER_EDGE_BEFORE or {e.source_id, e.target_id} <= {"a1", "a2", "b1", "b2"}
                   for e in graph.edges)

    def test_unresolved_side_note_is_representable_not_promoted(self):
        graph = ReadingOrderGraph.build(
            [node("para-1"), node("para-2"), node("side-note")],
            [before("para-1", "para-2"),
             before("side-note", "para-2", state=EVIDENCE_STATE_UNRESOLVED)],
        )
        # The unresolved edge does not order anything by itself.
        view = linearize(graph)
        assert view.sequence.index("para-1") < view.sequence.index("para-2")
        assert any("side-note" in group for group in view.ambiguous_groups)

    def test_continues_edge_is_a_hypothesis(self):
        graph = ReadingOrderGraph.build(
            [node("p1-tail"), node("p2-head")],
            [OrderEdge(kind=ORDER_EDGE_CONTINUES, source_id="p1-tail", target_id="p2-head",
                       producer="paginator", confidence=order_confidence("0.7"),
                       state=EVIDENCE_STATE_UNRESOLVED)],
        )
        view = linearize(graph)
        # Unresolved continuation does not fabricate order.
        assert view.ambiguous_groups == (("p1-tail", "p2-head"),)

    def test_containment_is_not_ordering(self):
        graph = ReadingOrderGraph.build(
            [node("page-1", NODE_KIND_REGION), node("p1"), node("p2")],
            [contains("page-1", "p1"), contains("page-1", "p2"), before("p1", "p2")],
        )
        assert len(graph.parents_of("p1")) == 1
        view = linearize(graph)
        # Only the before-edge orders content; the container itself is
        # unordered relative to its children and stays in an ambiguity
        # group instead of being silently placed.
        assert view.sequence.index("p1") < view.sequence.index("p2")
        assert any("page-1" in group for group in view.ambiguous_groups)

    def test_ordering_ancestor_against_descendant_rejected(self):
        with pytest.raises(KernelError, match="ancestor"):
            ReadingOrderGraph.build(
                [node("page-1", NODE_KIND_REGION), node("p1")],
                [contains("page-1", "p1"), before("page-1", "p1")],
            )


class TestContradictions:
    def test_asserted_cycle_rejected(self):
        with pytest.raises(OrderConflictError, match="cycle"):
            ReadingOrderGraph.build(
                [node("a"), node("b"), node("c")],
                [before("a", "b"), before("b", "c"), before("c", "a")],
            )

    def test_both_directions_asserted_rejected(self):
        with pytest.raises(OrderConflictError, match="contradictory"):
            ReadingOrderGraph.build(
                [node("a"), node("b")],
                [before("a", "b"), before("b", "a")],
            )

    def test_asserted_plus_unresolved_opposite_is_ambiguity(self):
        # One direction asserted, the reverse merely hypothesized: the
        # graph keeps both instead of silently dropping evidence.
        graph = ReadingOrderGraph.build(
            [node("a"), node("b")],
            [before("a", "b"), before("b", "a", producer="ocr", state=EVIDENCE_STATE_UNRESOLVED)],
        )
        assert len(graph.edges) == 2

    def test_duplicate_edge_never_last_write_wins(self):
        with pytest.raises(KernelError, match="duplicate"):
            ReadingOrderGraph.build(
                [node("a"), node("b")],
                [before("a", "b", producer="layout"), before("a", "b", producer="other")],
            )

    def test_overlapping_containment_rejected(self):
        with pytest.raises(KernelError, match="contained by both"):
            ReadingOrderGraph.build(
                [node("r1", NODE_KIND_REGION), node("r2", NODE_KIND_REGION), node("p1")],
                [contains("r1", "p1"), contains("r2", "p1")],
            )

    def test_containment_cycle_rejected(self):
        with pytest.raises(KernelError, match="containment cycle"):
            ReadingOrderGraph.build(
                [node("r1", NODE_KIND_REGION), node("r2", NODE_KIND_REGION)],
                [contains("r1", "r2"), contains("r2", "r1")],
            )

    def test_unknown_edge_kind_and_unknown_node_rejected(self):
        with pytest.raises(KernelError, match="unknown order edge kind"):
            OrderEdge(kind="sorta_before", source_id="a", target_id="b",
                      producer="x", confidence=CONF)
        with pytest.raises(KernelError, match="unknown node"):
            ReadingOrderGraph.build([node("a")], [before("a", "ghost")])

    def test_member_of_requires_region_target(self):
        with pytest.raises(KernelError, match="region"):
            ReadingOrderGraph.build(
                [node("a"), node("b")], [member_of("a", "b")]
            )

    def test_confidence_bounds_enforced(self):
        with pytest.raises(KernelError, match="confidence"):
            order_confidence("1.5")
        with pytest.raises(KernelError, match="confidence"):
            order_confidence("high")


class TestDeterministicSerialization:
    def test_shuffled_insertion_produces_identical_bytes(self):
        nodes = [node("p1"), node("p2"), node("p3"), node("r", NODE_KIND_REGION)]
        edges = [contains("r", "p1"), contains("r", "p2"), contains("r", "p3"),
                 before("p1", "p2"), before("p2", "p3")]
        reference = ReadingOrderGraph.build(nodes, edges)
        rng = random.Random(72)
        for _ in range(5):
            shuffled_nodes = nodes[:]
            shuffled_edges = edges[:]
            rng.shuffle(shuffled_nodes)
            rng.shuffle(shuffled_edges)
            candidate = ReadingOrderGraph.build(shuffled_nodes, shuffled_edges)
            assert canonical_json_str(
                to_json_ready(candidate.canonical_payload())
            ) == canonical_json_str(to_json_ready(reference.canonical_payload()))
            assert candidate.graph_id() == reference.graph_id()

    def test_producer_confidence_state_participate_in_identity(self):
        base = ReadingOrderGraph.build(
            [node("a"), node("b")], [before("a", "b", producer="layout")]
        )
        other_producer = ReadingOrderGraph.build(
            [node("a"), node("b")], [before("a", "b", producer="ocr")]
        )
        other_confidence = ReadingOrderGraph.build(
            [node("a"), node("b")],
            [OrderEdge(kind=ORDER_EDGE_BEFORE, source_id="a", target_id="b",
                       producer="layout", confidence=order_confidence("0.6"))],
        )
        assert len({base.graph_id(), other_producer.graph_id(), other_confidence.graph_id()}) == 3

    def test_from_payload_round_trips_and_fails_closed(self):
        graph = ReadingOrderGraph.build(
            [node("r", NODE_KIND_REGION), node("a", anchor_ref="anchor-1"), node("b")],
            [contains("r", "a"), contains("r", "b"), before("a", "b")],
        )
        payload = graph.canonical_payload()
        assert ReadingOrderGraph.from_payload(payload).graph_id() == graph.graph_id()

        bad_schema = dict(payload, schema="9.9.9")
        with pytest.raises(KernelError, match="unsupported reading-order schema"):
            ReadingOrderGraph.from_payload(bad_schema)

        bad_edge = dict(payload)
        bad_edge["edges"] = [dict(payload["edges"][0], similarity="0.99")]
        with pytest.raises(KernelError, match="unknown order edge fields"):
            ReadingOrderGraph.from_payload(bad_edge)

    def test_reading_order_record_identity(self):
        graph = ReadingOrderGraph.build([node("a"), node("b")], [before("a", "b")])
        base = ReadingOrderRecord(
            record_id="ro-1", content_revision_ref="rev-0001", graph=graph
        )
        same = ReadingOrderRecord(
            record_id="ro-2",
            content_revision_ref="rev-0001",
            graph=ReadingOrderGraph.build(
                [node("a"), node("b")], [before("a", "b")]
            ),
            evidence={"producer": "layout-engine"},
        )
        other_revision = ReadingOrderRecord(
            record_id="ro-3", content_revision_ref="rev-0002", graph=graph
        )
        from app.utils.canonical import record_identity_hash

        def identity_of(record):
            return record_identity_hash(
                record_type=record.record_type,
                schema_version=record.schema_version,
                payload=to_json_ready(record.identity_payload()),
            )

        assert identity_of(base) == identity_of(same)  # evidence-only excluded
        assert identity_of(base) != identity_of(other_revision)
        rebuilt = ReadingOrderRecord.from_payload(
            base.identity_payload(), record_id="ro-1"
        )
        assert rebuilt.graph.graph_id() == graph.graph_id()


# ---------------------------------------------------------------------------
# Specialist split / bounded restitch (§9.4)
# ---------------------------------------------------------------------------


def make_page_graph():
    """Two regions on one page; para-a is the split target."""
    return ReadingOrderGraph.build(
        [
            node("page-1", NODE_KIND_REGION),
            node("col-left", NODE_KIND_REGION),
            node("col-right", NODE_KIND_REGION),
            node("title"),
            node("para-a"),
            node("para-b"),
            node("para-c"),
        ],
        [
            contains("page-1", "title"),
            contains("page-1", "col-left"),
            contains("page-1", "col-right"),
            contains("col-left", "para-a"),
            contains("col-left", "para-b"),
            contains("col-right", "para-c"),
            before("title", "para-a"),
            before("para-a", "para-b"),
        ],
    )


class TestSpecialistSplit:
    def test_split_updates_neighborhood_and_reconnects_soundly(self):
        graph = make_page_graph()
        result = split_node(
            graph,
            "para-a",
            [node("para-a1", anchor_ref="anchor-a1"), node("para-a2", anchor_ref="anchor-a2")],
            child_order=["para-a1", "para-a2"],
            producer="table-specialist",
        )
        new_graph = result.graph

        # Containment re-parented to the same parent.
        assert {e.source_id for e in new_graph.parents_of("para-a1")} == {"col-left"}
        assert {e.source_id for e in new_graph.parents_of("para-a2")} == {"col-left"}
        assert "para-a" not in {n.node_id for n in new_graph.nodes}

        # External constraints transferred to every child (partition soundness).
        assert before("title", "para-a1", producer="table-specialist") in new_graph.edges
        assert before("title", "para-a2", producer="table-specialist") in new_graph.edges
        assert before("para-a1", "para-b", producer="table-specialist") in new_graph.edges
        assert before("para-a2", "para-b", producer="table-specialist") in new_graph.edges
        # Specialist-only internal order is asserted evidence.
        assert before("para-a1", "para-a2", producer="table-specialist") in new_graph.edges

        # Unrelated structure is byte-identical.
        for preserved_edge in (
            contains("page-1", "title"),
            contains("page-1", "col-left"),
            contains("page-1", "col-right"),
            contains("col-left", "para-b"),
            contains("col-right", "para-c"),
        ):
            assert preserved_edge in new_graph.edges

        assert result.neighborhood == frozenset(
            {"para-a", "para-a1", "para-a2", "col-left", "title", "para-b"}
        )
        assert result.preserved_edge_count == 5
        assert result.rewritten_edge_count == 7

    def test_split_without_local_order_leaves_children_unordered(self):
        graph = make_page_graph()
        result = split_node(
            graph, "para-a", [node("para-a1"), node("para-a2")], producer="specialist"
        )
        edge_pairs = {
            (e.source_id, e.target_id)
            for e in result.graph.edges
            if e.kind == ORDER_EDGE_BEFORE
        }
        assert ("para-a1", "para-a2") not in edge_pairs
        # External transfer still sound.
        assert ("title", "para-a1") in edge_pairs
        assert ("para-a2", "para-b") in edge_pairs

    def test_unrelated_nodes_keep_identity_and_constraints(self):
        graph = make_page_graph()
        result = split_node(
            graph, "para-a", [node("para-a1"), node("para-a2")], producer="specialist"
        )
        before_ids = {n.node_id for n in graph.nodes} - {"para-a"}
        after_ids = {n.node_id for n in result.graph.nodes}
        assert before_ids <= after_ids
        # para-c/col-right untouched: identical incident edges.
        assert result.graph.regions_of("para-c") == graph.regions_of("para-c")
        assert result.graph.parents_of("para-c") == graph.parents_of("para-c")

    def test_neighborhood_is_bounded_not_document_wide(self):
        rng = random.Random(7)
        nodes = [node("page", NODE_KIND_REGION), node("target")]
        edges = [contains("page", "target")]
        for i in range(200):
            far_id = f"far-{i:03d}"
            nodes.append(node(far_id))
            edges.append(contains("page", far_id))
            if i > 0:
                edges.append(before(f"far-{i - 1:03d}", far_id))
        rng.shuffle(nodes)
        rng.shuffle(edges)
        graph = ReadingOrderGraph.build(nodes, edges)

        result = split_node(
            graph, "target", [node("target-a"), node("target-b")], producer="specialist"
        )
        # 203 nodes in the document, 3 in the affected neighborhood.
        assert result.neighborhood == frozenset({"target", "target-a", "target-b", "page"})
        assert result.preserved_edge_count == len(graph.edges) - 1  # only contains(page,target) dropped

    def test_split_of_container_conservatively_abstains(self):
        # A content node that itself contains children (e.g. a paragraph
        # holding lines) cannot be re-split without an explicit
        # disposition for those children.
        graph = ReadingOrderGraph.build(
            [node("para-x"), node("line-1"), node("line-2")],
            [contains("para-x", "line-1"), contains("para-x", "line-2")],
        )
        with pytest.raises(KernelError, match="re-parenting"):
            split_node(graph, "para-x", [node("para-x1"), node("para-x2")], producer="specialist")

    def test_splitting_a_region_is_rejected(self):
        graph = ReadingOrderGraph.build(
            [node("region-1", NODE_KIND_REGION), node("p1")],
            [contains("region-1", "p1")],
        )
        with pytest.raises(KernelError, match="only content nodes"):
            split_node(graph, "region-1", [node("r1a"), node("r1b")], producer="specialist")

    def test_child_id_collision_rejected(self):
        graph = make_page_graph()
        with pytest.raises(KernelError, match="collides"):
            split_node(graph, "para-a", [node("para-b"), node("para-a1")], producer="specialist")

    def test_bad_child_order_rejected(self):
        graph = make_page_graph()
        with pytest.raises(KernelError, match="permutation"):
            split_node(
                graph, "para-a", [node("para-a1"), node("para-a2")],
                child_order=["para-a1", "ghost"], producer="specialist",
            )

    def test_region_child_rejected(self):
        graph = make_page_graph()
        with pytest.raises(KernelError, match="content nodes"):
            split_node(
                graph, "para-a", [node("para-a1", NODE_KIND_REGION)], producer="specialist"
            )

    def test_sound_transfer_cannot_manufacture_a_cycle(self):
        # Soundness property: transferring external constraints to every
        # child of a valid graph cannot create an asserted cycle, for
        # ANY child_order permutation.
        graph = ReadingOrderGraph.build(
            [node("r", NODE_KIND_REGION), node("x"), node("target"), node("y")],
            [
                contains("r", "x"), contains("r", "target"), contains("r", "y"),
                before("x", "target"), before("target", "y"), before("x", "y"),
            ],
        )
        result = split_node(
            graph, "target", [node("t1"), node("t2"), node("t3")],
            child_order=["t3", "t1", "t2"], producer="specialist",
        )
        view = linearize(result.graph)
        assert view.sequence.index("x") < view.sequence.index("t1")
        assert view.sequence.index("t3") < view.sequence.index("t1")
        assert view.sequence.index("t2") < view.sequence.index("y")

    def test_failed_split_leaves_original_graph_unchanged(self):
        graph = make_page_graph()
        original_id = graph.graph_id()
        with pytest.raises(KernelError):
            split_node(graph, "para-a", [node("para-b")], producer="specialist")
        assert graph.graph_id() == original_id

    def test_split_result_is_a_new_graph_not_a_mutation(self):
        graph = make_page_graph()
        original_id = graph.graph_id()
        split_node(graph, "para-a", [node("para-a1"), node("para-a2")], producer="specialist")
        assert graph.graph_id() == original_id
        assert "para-a" in {n.node_id for n in graph.nodes}


class TestLinearizationView:
    def test_view_is_derived_not_stored(self):
        graph = ReadingOrderGraph.build(
            [node("a"), node("b")], [before("a", "b")]
        )
        view = linearize(graph)
        assert isinstance(view, LinearizationView)
        assert view.policy == "canonical_id"
        # No graph mutation, no new edges.
        assert len(graph.edges) == 1

    def test_unknown_policy_rejected(self):
        graph = ReadingOrderGraph.build([node("a")], [])
        with pytest.raises(KernelError, match="unknown linearization policy"):
            linearize(graph, policy="semantic_similarity")
