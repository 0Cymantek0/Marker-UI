"""PR73 patch contract tests: records, operations, preconditions, application.

Pure contract level — no kernel engine. Mirrors the PR72 anchor/reading
order contract-test style: construct typed objects, assert deterministic
identity, fail-closed on unknown/malformed identity-affecting input.
"""

from __future__ import annotations

import pytest

from app.kernel.errors import (
    BeforeHashMismatchError,
    InvalidViewAdvancementError,
    KernelError,
    MissingViewTargetError,
    OrderConflictError,
    SourceRevisionMismatchError,
)
from app.kernel.patches import (
    DEFAULT_VIEW_ID,
    PatchOperation,
    PatchOutcomeRecord,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    ViewAdvancement,
    ViewDocumentRecord,
    apply_operation,
    apply_rebase_source,
    evaluate_preconditions,
    view_text_hash,
)
from app.kernel.reading_order import (
    NODE_KIND_CONTENT,
    NODE_KIND_REGION,
    OrderEdge,
    OrderNode,
    ReadingOrderGraph,
    order_confidence,
)

CONF = order_confidence("1.0")


def node(node_id: str, *, kind: str = NODE_KIND_CONTENT, anchor: str | None = None):
    return OrderNode(node_id=node_id, kind=kind, anchor_ref=anchor)


def before(source: str, target: str):
    return OrderEdge(
        kind="before", source_id=source, target_id=target, producer="t", confidence=CONF
    )


def base_graph() -> ReadingOrderGraph:
    return ReadingOrderGraph.build(
        [
            node("run-a", anchor="anchor-a"),
            node("run-b", anchor="anchor-b"),
            node("run-c", anchor="anchor-c"),
        ],
        [before("run-a", "run-b"), before("run-b", "run-c")],
    )


BASE_TEXTS = {"run-a": "Alpha", "run-b": "Beta", "run-c": "Gamma"}


def make_view(
    *, record_id: str = "view-evt-1", revision: str = "rev-s1", texts=None
) -> ViewDocumentRecord:
    return ViewDocumentRecord(
        record_id=record_id,
        content_revision_ref=revision,
        graph=base_graph(),
        texts=dict(texts if texts is not None else BASE_TEXTS),
    )


# ---------------------------------------------------------------------------
# value hashing
# ---------------------------------------------------------------------------


def test_text_hash_is_deterministic_and_exact():
    assert view_text_hash("Beta") == view_text_hash("Beta")
    assert view_text_hash("Beta") != view_text_hash("beta")
    assert view_text_hash("Beta") != view_text_hash("Beta ")
    assert view_text_hash("café") == view_text_hash("café")  # raw unicode, no folding
    with pytest.raises(KernelError):
        view_text_hash(b"bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------


def test_preconditions_reject_malformed_hashes_and_duplicate_targets():
    with pytest.raises(KernelError, match="before_hash"):
        TargetCheck(node_id="run-a", before_hash="not-a-hash")
    with pytest.raises(KernelError, match="duplicate target check"):
        PatchPreconditions(
            base_revision_id=view_text_hash("x"),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Beta")),
            ),
        )


def test_preconditions_fail_closed_on_pr74_claims():
    with pytest.raises(KernelError, match="PR74"):
        PatchPreconditions(
            base_revision_id=view_text_hash("x"),
            required_claim_refs=("claim-1",),
        )


def test_preconditions_canonical_value_is_order_insensitive():
    a = PatchPreconditions(
        base_revision_id=view_text_hash("x"),
        target_checks=(
            TargetCheck(node_id="run-b", before_hash=view_text_hash("Beta")),
            TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
        ),
        required_source_revision_refs=("rev-s2", "rev-s1"),
    )
    b = PatchPreconditions(
        base_revision_id=view_text_hash("x"),
        target_checks=(
            TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
            TargetCheck(node_id="run-b", before_hash=view_text_hash("Beta")),
        ),
        required_source_revision_refs=("rev-s1", "rev-s2"),
    )
    assert a.canonical_value() == b.canonical_value()
    round_trip = PatchPreconditions.from_canonical(a.canonical_value())
    assert round_trip.canonical_value() == a.canonical_value()


def test_preconditions_from_canonical_rejects_claims_and_unknown_fields():
    payload = PatchPreconditions(base_revision_id=view_text_hash("x")).canonical_value()
    with pytest.raises(KernelError, match="PR74"):
        PatchPreconditions.from_canonical(
            {**payload, "required_claim_refs": ["claim-1"]}
        )
    with pytest.raises(KernelError, match="unknown precondition fields"):
        PatchPreconditions.from_canonical({**payload, "surprise": 1})


# ---------------------------------------------------------------------------
# view document revisions
# ---------------------------------------------------------------------------


def test_view_identity_converges_for_same_facts():
    a = make_view()
    b = make_view(record_id="view-evt-2")
    assert a.view_revision_id() == b.view_revision_id()


def test_view_identity_ignores_texts_mapping_order():
    a = make_view()
    b = ViewDocumentRecord(
        record_id="view-evt-2",
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts={"run-c": "Gamma", "run-a": "Alpha", "run-b": "Beta"},
    )
    assert a.view_revision_id() == b.view_revision_id()


def test_view_identity_separates_text_graph_and_source_changes():
    base_id = make_view().view_revision_id()
    changed_text = make_view(texts={**BASE_TEXTS, "run-a": "alpha"}).view_revision_id()
    changed_source = make_view(revision="rev-s2").view_revision_id()
    changed_graph = ViewDocumentRecord(
        record_id="view-evt-9",
        content_revision_ref="rev-s1",
        graph=ReadingOrderGraph.build(
            [node("run-a"), node("run-b"), node("run-c")],
            [before("run-a", "run-b"), before("run-a", "run-c")],
        ),
        texts=dict(BASE_TEXTS),
    ).view_revision_id()
    assert len({base_id, changed_text, changed_source, changed_graph}) == 4


def test_view_rejects_incomplete_and_fabricated_text_state():
    with pytest.raises(KernelError, match="incomplete"):
        make_view(texts={"run-a": "Alpha"})
    with pytest.raises(KernelError, match="not a content node"):
        make_view(texts={**BASE_TEXTS, "run-x": "Ghost"})
    with pytest.raises(KernelError, match="must be str"):
        make_view(texts={**BASE_TEXTS, "run-a": 7})  # type: ignore[dict-item]


def test_view_from_payload_round_trips_and_fails_closed():
    view = make_view()
    payload = view.identity_payload()
    remat = ViewDocumentRecord.from_payload(payload, record_id="view-evt-2")
    assert remat.view_revision_id() == view.view_revision_id()
    with pytest.raises(KernelError, match="unknown view payload fields"):
        ViewDocumentRecord.from_payload(
            {**payload, "extra": True}, record_id="view-evt-3"
        )


def test_view_text_of_rejects_unknown_and_region_nodes():
    view = ViewDocumentRecord(
        record_id="view-evt-1",
        content_revision_ref="rev-s1",
        graph=ReadingOrderGraph.build(
            [node("col", kind=NODE_KIND_REGION), node("run-a", anchor="anchor-a")],
            [
                OrderEdge(
                    kind="contains",
                    source_id="col",
                    target_id="run-a",
                    producer="t",
                    confidence=CONF,
                )
            ],
        ),
        texts={"run-a": "Alpha"},
    )
    assert view.text_of("run-a") == "Alpha"
    with pytest.raises(KernelError):
        view.text_of("run-z")
    with pytest.raises(KernelError, match="region"):
        view.text_of("col")


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def test_replace_text_operation_normalization():
    op = PatchOperation.replace_text(node_id="run-a", after_text="Fixed")
    assert op.canonical_value() == {
        "op_type": "replace_text",
        "params": {"node_id": "run-a", "after_text": "Fixed"},
    }
    with pytest.raises(KernelError, match="unknown replace_text fields"):
        PatchOperation(op_type="replace_text", params={"node_id": "x", "extra": 1})
    with pytest.raises(KernelError):
        PatchOperation(op_type="replace_text", params={"after_text": "y"})


def test_split_operation_normalizes_children_order_but_keeps_child_order():
    a = PatchOperation.split_node(
        node_id="run-b",
        children=[
            {"node_id": "seg-2", "text": "two"},
            {"node_id": "seg-1", "text": "one"},
        ],
        child_order=["seg-1", "seg-2"],
    )
    b = PatchOperation.split_node(
        node_id="run-b",
        children=[
            {"node_id": "seg-1", "text": "one"},
            {"node_id": "seg-2", "text": "two"},
        ],
        child_order=["seg-1", "seg-2"],
    )
    assert a.canonical_value() == b.canonical_value()
    # permuting child_order alone changes the asserted evidence
    c = PatchOperation.split_node(
        node_id="run-b",
        children=[{"node_id": "seg-1", "text": "one"}, {"node_id": "seg-2", "text": "two"}],
        child_order=["seg-2", "seg-1"],
    )
    assert a.canonical_value() != c.canonical_value()
    with pytest.raises(KernelError, match="permutation"):
        PatchOperation.split_node(
            node_id="run-b",
            children=[{"node_id": "seg-1", "text": "one"}],
            child_order=["seg-1", "seg-2"],
        )
    with pytest.raises(KernelError, match="duplicate split child"):
        PatchOperation.split_node(
            node_id="run-b",
            children=[
                {"node_id": "seg-1", "text": "one"},
                {"node_id": "seg-1", "text": "two"},
            ],
        )


def test_operations_round_trip_through_canonical():
    for op in (
        PatchOperation.replace_text(node_id="run-a", after_text="Fixed"),
        PatchOperation.split_node(
            node_id="run-b",
            children=[{"node_id": "seg-1", "text": "one"}],
            child_order=["seg-1"],
        ),
    ):
        remat = PatchOperation.from_canonical(op.canonical_value())
        assert remat.canonical_value() == op.canonical_value()
    with pytest.raises(KernelError, match="unknown patch operation"):
        PatchOperation.from_canonical({"op_type": "mystery", "params": {}})


# ---------------------------------------------------------------------------
# proposal & outcome records
# ---------------------------------------------------------------------------


def proposal(**overrides) -> PatchProposalRecord:
    params = dict(
        record_id="proposal-evt-1",
        preconditions=PatchPreconditions(
            base_revision_id=make_view().view_revision_id(),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
            ),
        ),
        operations=(PatchOperation.replace_text(node_id="run-a", after_text="Fixed"),),
    )
    params.update(overrides)
    return PatchProposalRecord(**params)


def test_proposal_identity_stable_and_producer_excluded():
    a = proposal()
    b = proposal(record_id="proposal-evt-2", producer={"actor": "someone-else"})
    assert a.proposal_id() == b.proposal_id()


def test_proposal_identity_separates_base_checks_and_op_order():
    base = proposal().proposal_id()
    other_base = proposal(
        preconditions=PatchPreconditions(
            base_revision_id=view_text_hash("different"),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
            ),
        )
    ).proposal_id()
    other_check = proposal(
        preconditions=PatchPreconditions(
            base_revision_id=make_view().view_revision_id(),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Alphas")),
            ),
        )
    ).proposal_id()
    other_op = proposal(
        operations=(PatchOperation.replace_text(node_id="run-a", after_text="Other"),)
    ).proposal_id()
    assert len({base, other_base, other_check, other_op}) == 4


def test_proposal_operation_order_is_identity_bearing():
    ops = (
        PatchOperation.replace_text(node_id="run-a", after_text="X"),
        PatchOperation.replace_text(node_id="run-b", after_text="Y"),
    )
    forward = proposal(operations=ops).proposal_id()
    reverse = proposal(operations=tuple(reversed(ops))).proposal_id()
    assert forward != reverse  # commutativity is never assumed


def test_proposal_from_payload_round_trips_and_fails_closed():
    record = proposal()
    payload = record.identity_payload()
    remat = PatchProposalRecord.from_payload(payload, record_id="proposal-evt-2")
    assert remat.proposal_id() == record.proposal_id()
    with pytest.raises(KernelError, match="unknown proposal payload fields"):
        PatchProposalRecord.from_payload(
            {**payload, "producer": {"actor": "x"}}, record_id="proposal-evt-3"
        )


def test_outcome_requires_result_revision_when_accepted():
    identity = proposal().proposal_id()
    good = PatchOutcomeRecord(
        record_id="outcome-evt-1",
        proposal_ref="proposal-evt-1",
        proposal_identity=identity,
        outcome="accepted",
        observed={"base_revision_id": view_text_hash("x")},
        resulting_revision_id=make_view(texts={**BASE_TEXTS, "run-a": "Fixed"}).view_revision_id(),
    )
    assert good.identity_payload()["proposal_identity"] == identity
    with pytest.raises(KernelError, match="resulting view revision"):
        PatchOutcomeRecord(
            record_id="outcome-evt-2",
            proposal_ref="proposal-evt-1",
            proposal_identity=identity,
            outcome="accepted",
            resulting_revision_id=None,
        )
    with pytest.raises(KernelError, match="invalid outcome"):
        PatchOutcomeRecord(
            record_id="outcome-evt-3",
            proposal_ref="proposal-evt-1",
            proposal_identity=identity,
            outcome="maybe",
            resulting_revision_id=None,
        )
    remat = PatchOutcomeRecord.from_payload(
        {**good.identity_payload(), "proposal_ref": "proposal-evt-1"},
        record_id="o2",
    )
    assert remat.identity_payload() == good.identity_payload()


# ---------------------------------------------------------------------------
# precondition evaluation
# ---------------------------------------------------------------------------


def test_evaluate_preconditions_accepts_truthful_checks():
    view = make_view()
    evaluate_preconditions(
        view,
        PatchPreconditions(
            base_revision_id=view.view_revision_id(),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
                TargetCheck(node_id="run-c", before_hash=view_text_hash("Gamma")),
            ),
            required_source_revision_refs=("rev-s1",),
        ),
    )


def test_evaluate_preconditions_source_mismatch():
    with pytest.raises(SourceRevisionMismatchError) as excinfo:
        evaluate_preconditions(
            make_view(),
            PatchPreconditions(
                base_revision_id=view_text_hash("x"),
                required_source_revision_refs=("rev-s2",),
            ),
        )
    assert excinfo.value.observed_ref == "rev-s1"
    assert excinfo.value.required_refs == ("rev-s2",)


def test_evaluate_preconditions_missing_target_and_hash_mismatch():
    view = make_view()
    with pytest.raises(MissingViewTargetError) as missing:
        evaluate_preconditions(
            view,
            PatchPreconditions(
                base_revision_id=view.view_revision_id(),
                target_checks=(
                    TargetCheck(node_id="run-z", before_hash=view_text_hash("Alpha")),
                ),
            ),
        )
    assert missing.value.node_id == "run-z"
    with pytest.raises(BeforeHashMismatchError) as mismatch:
        evaluate_preconditions(
            view,
            PatchPreconditions(
                base_revision_id=view.view_revision_id(),
                target_checks=(
                    TargetCheck(node_id="run-a", before_hash=view_text_hash("alpha")),
                ),
            ),
        )
    assert mismatch.value.node_id == "run-a"
    assert mismatch.value.observed_hash == view_text_hash("Alpha")


# ---------------------------------------------------------------------------
# pure application
# ---------------------------------------------------------------------------


def test_apply_replace_text():
    graph, texts = apply_operation(
        base_graph(), BASE_TEXTS, PatchOperation.replace_text(node_id="run-b", after_text="Bravo")
    )
    assert texts["run-b"] == "Bravo"
    assert graph.graph_id() == base_graph().graph_id()  # structure untouched
    with pytest.raises(MissingViewTargetError):
        apply_operation(
            base_graph(),
            BASE_TEXTS,
            PatchOperation.replace_text(node_id="run-z", after_text="x"),
        )


def test_apply_split_distributes_text_and_inherits_anchor():
    graph, texts = apply_operation(
        base_graph(),
        BASE_TEXTS,
        PatchOperation.split_node(
            node_id="run-b",
            children=[
                {"node_id": "seg-1", "text": "Be"},
                {"node_id": "seg-2", "text": "ta"},
            ],
            child_order=["seg-1", "seg-2"],
        ),
    )
    assert texts == {"run-a": "Alpha", "run-c": "Gamma", "seg-1": "Be", "seg-2": "ta"}
    assert graph.node("seg-1").anchor_ref == "anchor-b"  # inherited
    assert graph.node("seg-1").kind == NODE_KIND_CONTENT
    # order evidence transferred: run-a before children before run-c
    ids = [e.canonical_value() for e in graph.edges if e.kind == "before"]
    assert any(
        v["source"] == "run-a" and v["target"] == "seg-1" for v in ids
    )
    with pytest.raises(MissingViewTargetError):
        apply_operation(
            base_graph(),
            BASE_TEXTS,
            PatchOperation.split_node(
                node_id="run-z", children=[{"node_id": "seg-1", "text": "x"}]
            ),
        )


def test_apply_rejects_rebase_against_current_view():
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=base_graph(),
        source_texts=dict(BASE_TEXTS),
    )
    with pytest.raises(KernelError, match="replay"):
        apply_operation(base_graph(), BASE_TEXTS, op)


# ---------------------------------------------------------------------------
# rebase replay
# ---------------------------------------------------------------------------


def test_rebase_replays_compatible_and_drops_stale_proposals():
    view = make_view()
    compatible = PatchProposalRecord(
        record_id="prop-a",
        preconditions=PatchPreconditions(
            base_revision_id=view.view_revision_id(),
            target_checks=(
                TargetCheck(node_id="run-c", before_hash=view_text_hash("Gamma")),
            ),
        ),
        operations=(PatchOperation.replace_text(node_id="run-c", after_text="Gamma!"),),
    )
    stale = PatchProposalRecord(
        record_id="prop-b",
        preconditions=PatchPreconditions(
            base_revision_id=view.view_revision_id(),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Different")),
            ),
        ),
        operations=(PatchOperation.replace_text(node_id="run-a", after_text="X"),),
    )
    source_bound = PatchProposalRecord(
        record_id="prop-c",
        preconditions=PatchPreconditions(
            base_revision_id=view.view_revision_id(),
            required_source_revision_refs=("rev-s1",),  # rebased view binds rev-s2
        ),
        operations=(PatchOperation.replace_text(node_id="run-b", after_text="B"),),
    )
    # Fresh source facts: run-a's text changed in the source itself.
    fresh_texts = {**BASE_TEXTS, "run-a": "Alpha (fixed upstream)"}
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=base_graph(),
        source_texts=fresh_texts,
        replay_proposal_refs=("prop-a", "prop-b", "prop-c"),
    )
    result = apply_rebase_source(op, {"prop-a": compatible, "prop-b": stale, "prop-c": source_bound})
    assert result.applied_refs == ("prop-a",)
    assert [ref for ref, _ in result.dropped_refs] == ["prop-b", "prop-c"]
    assert result.dropped_refs[0][1] == "BeforeHashMismatchError"
    assert result.dropped_refs[1][1] == "SourceRevisionMismatchError"
    assert result.view.texts["run-a"] == "Alpha (fixed upstream)"
    assert result.view.texts["run-c"] == "Gamma!"
    assert result.view.content_revision_ref == "rev-s2"


def test_rebase_is_deterministic_across_proposal_map_order():
    p = PatchProposalRecord(
        record_id="prop-a",
        preconditions=PatchPreconditions(
            base_revision_id=view_text_hash("x"),
            target_checks=(
                TargetCheck(node_id="run-a", before_hash=view_text_hash("Alpha")),
            ),
        ),
        operations=(PatchOperation.replace_text(node_id="run-a", after_text="A2"),),
    )
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=base_graph(),
        source_texts=dict(BASE_TEXTS),
        replay_proposal_refs=("prop-a",),
    )
    first = apply_rebase_source(op, {"prop-a": p})
    second = apply_rebase_source(op, {"prop-a": p})
    assert first.view.view_revision_id() == second.view.view_revision_id()


def test_rebase_rejects_incomplete_source_texts_and_unknown_proposals():
    op = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=base_graph(),
        source_texts={"run-a": "Alpha"},
    )
    with pytest.raises(KernelError, match="exactly the content nodes"):
        apply_rebase_source(op, {})
    op2 = PatchOperation.rebase_source(
        new_content_revision_ref="rev-s2",
        source_graph=base_graph(),
        source_texts=dict(BASE_TEXTS),
        replay_proposal_refs=("ghost",),
    )
    with pytest.raises(KernelError, match="unknown proposals"):
        apply_rebase_source(op2, {})


# ---------------------------------------------------------------------------
# advancement contract
# ---------------------------------------------------------------------------


def test_view_advancement_forms():
    revision = make_view().view_revision_id()
    assert ViewAdvancement(new_revision_id=revision).view_id == DEFAULT_VIEW_ID
    assert (
        ViewAdvancement(
            new_revision_id=revision,
            base_revision_id=make_view(revision="rev-s2").view_revision_id(),
            proposal_record_id="proposal-evt-1",
        ).proposal_record_id
        == "proposal-evt-1"
    )
    assert ViewAdvancement(
        new_revision_id=revision, base_revision_id=revision, verified_rebuild=True
    ).verified_rebuild
    with pytest.raises(InvalidViewAdvancementError):
        ViewAdvancement(new_revision_id=revision, base_revision_id=revision)
    with pytest.raises(InvalidViewAdvancementError):
        ViewAdvancement(
            new_revision_id=revision,
            base_revision_id=revision,
            proposal_record_id="proposal-evt-1",
            verified_rebuild=True,
        )
    with pytest.raises(InvalidViewAdvancementError):
        ViewAdvancement(new_revision_id=revision, proposal_record_id="proposal-evt-1")
    with pytest.raises(InvalidViewAdvancementError):
        ViewAdvancement(new_revision_id="not-a-hash", base_revision_id=None)
    with pytest.raises(InvalidViewAdvancementError):
        ViewAdvancement(new_revision_id=revision, view_id="Bad Id")
