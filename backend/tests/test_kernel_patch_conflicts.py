"""PR73 conflict matrix: stale base, before-hash, source mismatch,
overlapping non-commutative changes, the split-vs-replace adversary,
disjoint composition via the tested rebase rule, order-independence of
disjoint pairs, and real concurrent submission races."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.kernel.commit import KernelCommitService
from app.kernel.errors import (
    BeforeHashMismatchError,
    MissingViewTargetError,
    SourceRevisionMismatchError,
    StaleBaseRevisionError,
)
from app.kernel.models import KernelRecord
from app.kernel.patches import (
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    view_text_hash,
)
from app.kernel.patching import (
    initialize_view,
    read_current_view,
    rebase_proposal,
    submit_patch,
)
from app.kernel.reading_order import OrderEdge, OrderNode, ReadingOrderGraph, order_confidence

CONF = order_confidence("1.0")


def base_graph() -> ReadingOrderGraph:
    return ReadingOrderGraph.build(
        [
            OrderNode(node_id="run-a", anchor_ref="anchor-a"),
            OrderNode(node_id="run-b", anchor_ref="anchor-b"),
            OrderNode(node_id="run-c", anchor_ref="anchor-c"),
        ],
        [
            OrderEdge(kind="before", source_id="run-a", target_id="run-b", producer="t", confidence=CONF),
            OrderEdge(kind="before", source_id="run-b", target_id="run-c", producer="t", confidence=CONF),
        ],
    )


GENESIS_TEXTS = {"run-a": "Alpha", "run-b": "Beta", "run-c": "Gamma"}


async def make_workspace(kernel_env, service=None, workspace_id="ws-conflict"):
    service = service or KernelCommitService(kernel_env)
    genesis = await initialize_view(
        kernel_env,
        service,
        workspace_id=workspace_id,
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts=GENESIS_TEXTS,
    )
    return kernel_env, service, genesis


def replace_proposal(
    base_revision: str,
    *,
    node_id: str,
    before_text: str,
    after_text: str,
    record_id: str,
    source_refs: tuple[str, ...] = ("rev-s1",),
) -> PatchProposalRecord:
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=base_revision,
            target_checks=(
                TargetCheck(node_id=node_id, before_hash=view_text_hash(before_text)),
            ),
            required_source_revision_refs=source_refs,
        ),
        operations=(PatchOperation.replace_text(node_id=node_id, after_text=after_text),),
    )


def split_proposal(
    base_revision: str,
    *,
    node_id: str,
    before_text: str,
    record_id: str,
) -> PatchProposalRecord:
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=base_revision,
            target_checks=(
                TargetCheck(node_id=node_id, before_hash=view_text_hash(before_text)),
            ),
            required_source_revision_refs=("rev-s1",),
        ),
        operations=(
            PatchOperation.split_node(
                node_id=node_id,
                children=[
                    {"node_id": f"{node_id}-s1", "text": before_text[:1]},
                    {"node_id": f"{node_id}-s2", "text": before_text[1:]},
                ],
                child_order=[f"{node_id}-s1", f"{node_id}-s2"],
            ),
        ),
    )


async def record_count(kernel_env, workspace_id) -> int:
    async with kernel_env() as session:
        return await session.scalar(
            select(func.count()).select_from(KernelRecord).where(
                KernelRecord.workspace_id == workspace_id
            )
        )


# ---------------------------------------------------------------------------
# B1 / B2 / B3: precondition truth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_stale_base_cannot_silently_apply(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    winner = replace_proposal(
        genesis.revision_id, node_id="run-a", before_text="Alpha", after_text="A1", record_id="p-winner"
    )
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=winner)
    count = await record_count(factory, "ws-conflict")

    stale = replace_proposal(
        genesis.revision_id, node_id="run-c", before_text="Gamma", after_text="G1", record_id="p-stale"
    )
    with pytest.raises(StaleBaseRevisionError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=stale)
    assert await record_count(factory, "ws-conflict") == count


@pytest.mark.asyncio
async def test_b2_changed_before_value_conflicts_even_with_fresh_base(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    first = replace_proposal(
        genesis.revision_id, node_id="run-b", before_text="Beta", after_text="B1", record_id="p-b1"
    )
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=first)
    current = await read_current_view(factory, "ws-conflict")

    # Node id still exists — that must NOT be enough to apply.
    stale_intent = replace_proposal(
        current.revision_id, node_id="run-b", before_text="Beta", after_text="B2", record_id="p-b2"
    )
    with pytest.raises(BeforeHashMismatchError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=stale_intent)
    assert (await read_current_view(factory, "ws-conflict")).revision_id == current.revision_id


@pytest.mark.asyncio
async def test_b3_source_revision_mismatch(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    wrong_source = replace_proposal(
        genesis.revision_id,
        node_id="run-a",
        before_text="Alpha",
        after_text="A1",
        record_id="p-src",
        source_refs=("rev-s2",),
    )
    with pytest.raises(SourceRevisionMismatchError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=wrong_source)
    assert (await read_current_view(factory, "ws-conflict")).revision_id == genesis.revision_id


@pytest.mark.asyncio
async def test_missing_target_fails_closed(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    ghost = replace_proposal(
        genesis.revision_id, node_id="run-ghost", before_text="Boo", after_text="X", record_id="p-ghost"
    )
    with pytest.raises(MissingViewTargetError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=ghost)
    assert await record_count(factory, "ws-conflict") == 1


# ---------------------------------------------------------------------------
# B4: overlapping non-commutative changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b4_overlapping_replaces_conflict_by_value_not_arrival(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    first = replace_proposal(
        genesis.revision_id, node_id="run-a", before_text="Alpha", after_text="First!", record_id="p-a1"
    )
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=first)
    current = await read_current_view(factory, "ws-conflict")

    # The loser re-proposes the same node with its original intent; the
    # before-value it observed is gone, so only a conflict — or an
    # explicit clobber decision — can follow.
    second = replace_proposal(
        current.revision_id, node_id="run-a", before_text="Alpha", after_text="Second!", record_id="p-a2"
    )
    with pytest.raises(BeforeHashMismatchError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=second)
    assert (await read_current_view(factory, "ws-conflict")).view.texts["run-a"] == "First!"


# ---------------------------------------------------------------------------
# B5: the PR72 canonical adversary — split races replace on the same node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b5_split_vs_replace_conflict_sequential_both_orders(kernel_env):
    """Whichever lands first, the other cannot land from the same base;
    rebased, one direction loses its target and the other its value."""
    factory, service, genesis = await make_workspace(kernel_env)

    # Order 1: split lands, replace rebases -> target node is gone.
    split = split_proposal(genesis.revision_id, node_id="run-b", before_text="Beta", record_id="p-split")
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=split)
    current = await read_current_view(factory, "ws-conflict")
    replace = replace_proposal(
        genesis.revision_id, node_id="run-b", before_text="Beta", after_text="Replaced", record_id="p-replace"
    )
    with pytest.raises(StaleBaseRevisionError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=replace)
    assert rebase_proposal(replace, current, record_id="p-replace-rb") is None
    assert current.view.texts == {
        "run-a": "Alpha",
        "run-c": "Gamma",
        "run-b-s1": "B",
        "run-b-s2": "eta",
    }

    # Order 2 (fresh workspace): replace lands, split rebases -> value moved.
    factory2, service2, genesis2 = await make_workspace(
        kernel_env, workspace_id="ws-conflict-2"
    )
    replace2 = replace_proposal(
        genesis2.revision_id, node_id="run-b", before_text="Beta", after_text="Replaced", record_id="p-replace2"
    )
    await submit_patch(factory2, service2, workspace_id="ws-conflict-2", proposal=replace2)
    current2 = await read_current_view(factory2, "ws-conflict-2")
    split2 = split_proposal(genesis2.revision_id, node_id="run-b", before_text="Beta", record_id="p-split2")
    with pytest.raises(StaleBaseRevisionError):
        await submit_patch(factory2, service2, workspace_id="ws-conflict-2", proposal=split2)
    assert rebase_proposal(split2, current2, record_id="p-split2-rb") is None


@pytest.mark.asyncio
async def test_b5_split_vs_replace_real_concurrent_race(kernel_env):
    """Two submissions race from the same base on the SAME node: the
    writer-lock serialization plus in-transaction precondition check
    must accept exactly one and typed-conflict the other — arrival
    order can never produce two accepted truths."""
    factory, service, genesis = await make_workspace(kernel_env)
    split = split_proposal(genesis.revision_id, node_id="run-c", before_text="Gamma", record_id="p-race-split")
    replace = replace_proposal(
        genesis.revision_id, node_id="run-c", before_text="Gamma", after_text="Race!", record_id="p-race-replace"
    )

    results = await asyncio.gather(
        submit_patch(factory, service, workspace_id="ws-conflict", proposal=split),
        submit_patch(factory, service, workspace_id="ws-conflict", proposal=replace),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, BaseException)]
    assert len(accepted) == 1
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], StaleBaseRevisionError)

    current = await read_current_view(factory, "ws-conflict")
    final_texts = sorted(current.view.texts.items())
    if "run-c" in current.view.texts:
        assert current.view.texts["run-c"] == "Race!"  # replace won
        assert current.view.view_revision_id() == accepted[0].result.revision_id
    else:
        assert set(current.view.texts) == {"run-a", "run-b", "run-c-s1", "run-c-s2"}
    # Exactly the genesis + one patch batch committed: no partial state.
    assert await record_count(factory, "ws-conflict") == 4  # view + proposal + outcome + view


@pytest.mark.asyncio
async def test_b5_three_way_same_target_race(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    proposals = [
        replace_proposal(
            genesis.revision_id,
            node_id="run-a",
            before_text="Alpha",
            after_text=f"Winner-{i}",
            record_id=f"p-race-{i}",
        )
        for i in range(3)
    ]
    results = await asyncio.gather(
        *(
            submit_patch(factory, service, workspace_id="ws-conflict", proposal=p)
            for p in proposals
        ),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    assert len(accepted) == 1
    assert all(isinstance(r, StaleBaseRevisionError) for r in results if isinstance(r, BaseException))
    current = await read_current_view(factory, "ws-conflict")
    assert current.view.texts["run-a"].startswith("Winner-")


# ---------------------------------------------------------------------------
# B6: disjoint changes compose through the tested rebase rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b6_disjoint_patches_compose_via_rebase(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    left = replace_proposal(
        genesis.revision_id, node_id="run-a", before_text="Alpha", after_text="A-left", record_id="p-left"
    )
    right = replace_proposal(
        genesis.revision_id, node_id="run-c", before_text="Gamma", after_text="C-right", record_id="p-right"
    )
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=left)
    current = await read_current_view(factory, "ws-conflict")

    rebased = rebase_proposal(right, current, record_id="p-right-rb")
    assert rebased is not None  # disjoint target untouched -> rebase possible
    acceptance = await submit_patch(factory, service, workspace_id="ws-conflict", proposal=rebased)
    assert acceptance.result.view.texts == {
        "run-a": "A-left",
        "run-b": "Beta",
        "run-c": "C-right",
    }

    # A rebase across a source change is refused for source-bound patches.
    other_source = replace_proposal(
        current.revision_id,
        node_id="run-b",
        before_text="Beta",
        after_text="B-x",
        record_id="p-srcbound",
        source_refs=("rev-s9",),
    )
    assert rebase_proposal(other_source, acceptance.result, record_id="p-srcbound-rb") is None


# ---------------------------------------------------------------------------
# B7: order-independence of the claimed-composable disjoint pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b7_disjoint_pair_is_order_independent(kernel_env):
    """For the disjoint replace pair, both application orders produce the
    same declared canonical view (identity). No stronger commutativity
    claim is made anywhere in the system."""

    async def run_order(ws: str, first_op: PatchOperation, second_op: PatchOperation, second_id: str):
        service = KernelCommitService(kernel_env)
        genesis = await initialize_view(
            kernel_env, service, workspace_id=ws,
            content_revision_ref="rev-s1", graph=base_graph(), texts=GENESIS_TEXTS,
        )

        def proposal(op: PatchOperation, record_id: str) -> PatchProposalRecord:
            return PatchProposalRecord(
                record_id=record_id,
                preconditions=PatchPreconditions(
                    base_revision_id=genesis.revision_id,
                    target_checks=(
                        TargetCheck(
                            node_id=op.params["node_id"],
                            before_hash=view_text_hash(GENESIS_TEXTS[op.params["node_id"]]),
                        ),
                    ),
                ),
                operations=(op,),
            )

        await submit_patch(kernel_env, service, workspace_id=ws, proposal=proposal(first_op, f"{ws}-first"))
        current = await read_current_view(kernel_env, ws)
        rebased = rebase_proposal(
            proposal(second_op, f"{ws}-second"), current, record_id=second_id
        )
        assert rebased is not None
        await submit_patch(kernel_env, service, workspace_id=ws, proposal=rebased)
        return (await read_current_view(kernel_env, ws)).revision_id

    left_op = PatchOperation.replace_text(node_id="run-a", after_text="A9")
    right_op = PatchOperation.replace_text(node_id="run-c", after_text="C9")
    forward = await run_order("ws-order-1", left_op, right_op, "ws-order-1-second-rb")
    reverse = await run_order("ws-order-2", right_op, left_op, "ws-order-2-second-rb")
    assert forward == reverse


@pytest.mark.asyncio
async def test_b7_overlapping_pair_is_not_order_independent_and_conflicts(kernel_env):
    """The falsification side: overlapping replaces are NOT commutative —
    the second application order yields a different final value, so the
    system must (and does) conflict instead of merging."""
    factory, service, genesis = await make_workspace(kernel_env)
    first = replace_proposal(
        genesis.revision_id, node_id="run-b", before_text="Beta", after_text="Order-1", record_id="p-o1"
    )
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=first)
    current = await read_current_view(factory, "ws-conflict")
    second = replace_proposal(
        current.revision_id, node_id="run-b", before_text="Beta", after_text="Order-2", record_id="p-o2"
    )
    with pytest.raises(BeforeHashMismatchError):
        await submit_patch(factory, service, workspace_id="ws-conflict", proposal=second)
    assert current.view.texts["run-b"] == "Order-1"


# ---------------------------------------------------------------------------
# Concurrency: loser retries through the rebase rule after the race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_race_then_honest_rebase_lands_both_disjoint_changes(kernel_env):
    factory, service, genesis = await make_workspace(kernel_env)
    left = replace_proposal(
        genesis.revision_id, node_id="run-a", before_text="Alpha", after_text="A-race", record_id="p-cr-a"
    )
    right = replace_proposal(
        genesis.revision_id, node_id="run-c", before_text="Gamma", after_text="C-race", record_id="p-cr-c"
    )
    results = await asyncio.gather(
        submit_patch(factory, service, workspace_id="ws-conflict", proposal=left),
        submit_patch(factory, service, workspace_id="ws-conflict", proposal=right),
        return_exceptions=True,
    )
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    loser = next(
        r for r in results if isinstance(r, BaseException)
    )
    assert isinstance(loser, StaleBaseRevisionError)

    current = await read_current_view(factory, "ws-conflict")
    left_won = current.view.texts["run-a"] == "A-race"
    loser_proposal = right if left_won else left
    rebased = rebase_proposal(loser_proposal, current, record_id=loser_proposal.record_id + "-rb")
    assert rebased is not None
    await submit_patch(factory, service, workspace_id="ws-conflict", proposal=rebased)
    final = await read_current_view(factory, "ws-conflict")
    assert final.view.texts["run-a"] == "A-race"
    assert final.view.texts["run-c"] == "C-race"
