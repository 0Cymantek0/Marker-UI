"""PR73 service-level tests: initialize/submit lineage, restart
durability, clean-rebuild oracle over committed history."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitService
from app.kernel.errors import (
    BeforeHashMismatchError,
    DuplicateRecordIdentityError,
    StaleBaseRevisionError,
)
from app.kernel.models import KernelRecord, KernelRecordEdge
from app.kernel.patches import (
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    ViewDocumentRecord,
    view_text_hash,
)
from app.kernel.patching import (
    clean_rebuild_view,
    initialize_view,
    load_view_history,
    read_current_view,
    submit_patch,
)
from app.kernel.reading_order import OrderEdge, OrderNode, ReadingOrderGraph, order_confidence
from app.kernel.replay import verify_history

CONF = order_confidence("1.0")
WS = "ws-patch-svc"


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


def replace_proposal(
    base_revision: str,
    source_ref: str,
    *,
    node_id: str,
    before_text: str,
    after_text: str,
    record_id: str,
) -> PatchProposalRecord:
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=base_revision,
            target_checks=(
                TargetCheck(node_id=node_id, before_hash=view_text_hash(before_text)),
            ),
            required_source_revision_refs=(source_ref,),
        ),
        operations=(PatchOperation.replace_text(node_id=node_id, after_text=after_text),),
    )


@pytest_asyncio.fixture
async def env(kernel_env, tmp_path):
    service = KernelCommitService(kernel_env)
    genesis = await initialize_view(
        kernel_env,
        service,
        workspace_id=WS,
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts=GENESIS_TEXTS,
    )
    return kernel_env, service, genesis


@pytest.mark.asyncio
async def test_initialize_and_read_current(env):
    factory, _service, genesis = env
    current = await read_current_view(factory, WS)
    assert current is not None
    assert current.revision_id == genesis.revision_id
    assert current.kernel_commit_id == 1
    assert current.view.texts == GENESIS_TEXTS


@pytest.mark.asyncio
async def test_submit_patch_creates_distinct_immutable_revision(env):
    factory, service, genesis = env
    proposal = replace_proposal(
        genesis.revision_id,
        "rev-s1",
        node_id="run-b",
        before_text="Beta",
        after_text="Bravo",
        record_id="proposal-fix-b",
    )
    acceptance = await submit_patch(factory, service, workspace_id=WS, proposal=proposal)
    assert acceptance.result.revision_id != genesis.revision_id
    assert acceptance.result.kernel_commit_id == acceptance.previous.kernel_commit_id + 1
    assert acceptance.result.view.texts["run-b"] == "Bravo"

    current = await read_current_view(factory, WS)
    assert current.revision_id == acceptance.result.revision_id

    # Base revision remains readable and digest-stable.
    history = await load_view_history(factory, WS)
    assert [entry.kernel_commit_id for entry in history] == [1, 2]
    assert history[0].view.view_revision_id() == genesis.revision_id
    assert history[0].view.texts["run-b"] == "Beta"
    assert history[1].proposal is not None
    assert history[1].proposal.proposal_id() == proposal.proposal_id()
    assert history[1].outcome is not None
    assert history[1].outcome.resulting_revision_id == acceptance.result.revision_id


@pytest.mark.asyncio
async def test_lineage_edges_link_proposal_outcome_and_revisions(env):
    factory, service, genesis = env
    proposal = replace_proposal(
        genesis.revision_id,
        "rev-s1",
        node_id="run-a",
        before_text="Alpha",
        after_text="Alpha!",
        record_id="proposal-fix-a",
    )
    acceptance = await submit_patch(factory, service, workspace_id=WS, proposal=proposal)
    async with factory() as session:
        edges = (
            (
                await session.execute(
                    select(KernelRecordEdge.edge_kind, KernelRecordEdge.source_record_id, KernelRecordEdge.target_record_id).where(
                        KernelRecordEdge.workspace_id == WS
                    )
                )
            ).all()
        )
    triples = {(e.edge_kind, e.source_record_id, e.target_record_id) for e in edges}
    assert ("depends_on", proposal.record_id, genesis.record_id) in triples
    assert ("derived_from", acceptance.result.record_id, genesis.record_id) in triples
    assert (
        "evidence_for",
        f"outcome-{proposal.record_id}",
        acceptance.result.record_id,
    ) in triples


@pytest.mark.asyncio
async def test_stale_submission_rejected_before_commit(env):
    factory, service, genesis = env
    first = replace_proposal(
        genesis.revision_id,
        "rev-s1",
        node_id="run-a",
        before_text="Alpha",
        after_text="First",
        record_id="proposal-1",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=first)

    async with factory() as session:
        count_before = await session.scalar(
            select(func.count()).select_from(KernelRecord).where(KernelRecord.workspace_id == WS)
        )

    stale = replace_proposal(
        genesis.revision_id,  # still asserts the ORIGINAL base
        "rev-s1",
        node_id="run-c",
        before_text="Gamma",
        after_text="Stale",
        record_id="proposal-2",
    )
    with pytest.raises(StaleBaseRevisionError):
        await submit_patch(factory, service, workspace_id=WS, proposal=stale)

    async with factory() as session:
        count_after = await session.scalar(
            select(func.count()).select_from(KernelRecord).where(KernelRecord.workspace_id == WS)
        )
    assert count_after == count_before  # no accepted partial state


@pytest.mark.asyncio
async def test_before_hash_rejection(env):
    factory, service, genesis = env
    lying = replace_proposal(
        genesis.revision_id,
        "rev-s1",
        node_id="run-a",
        before_text="Wrong prior",
        after_text="X",
        record_id="proposal-lying",
    )
    with pytest.raises(BeforeHashMismatchError):
        await submit_patch(factory, service, workspace_id=WS, proposal=lying)
    current = await read_current_view(factory, WS)
    assert current.revision_id == genesis.revision_id


@pytest.mark.asyncio
async def test_sequential_patches_chain_via_fresh_bases(env):
    factory, service, genesis = env
    p1 = replace_proposal(
        genesis.revision_id, "rev-s1", node_id="run-a", before_text="Alpha", after_text="A1", record_id="p1"
    )
    a1 = await submit_patch(factory, service, workspace_id=WS, proposal=p1)
    p2 = replace_proposal(
        a1.result.revision_id, "rev-s1", node_id="run-b", before_text="Beta", after_text="B1", record_id="p2"
    )
    a2 = await submit_patch(factory, service, workspace_id=WS, proposal=p2)
    current = await read_current_view(factory, WS)
    assert current.revision_id == a2.result.revision_id
    assert current.view.texts == {"run-a": "A1", "run-b": "B1", "run-c": "Gamma"}


@pytest.mark.asyncio
async def test_duplicate_and_resubmission_rejections(env):
    factory, service, genesis = env
    proposal = replace_proposal(
        genesis.revision_id, "rev-s1", node_id="run-a", before_text="Alpha", after_text="A2", record_id="p-dup"
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=proposal)
    current = await read_current_view(factory, WS)

    # Exact resubmission still asserts the superseded base: the base
    # precondition is the first truthful rejection.
    with pytest.raises(StaleBaseRevisionError):
        await submit_patch(factory, service, workspace_id=WS, proposal=proposal)

    # Re-targeting the same intent at the current revision keeps the
    # original before-value claim, which no longer holds.
    with pytest.raises(BeforeHashMismatchError):
        await submit_patch(
            factory,
            service,
            workspace_id=WS,
            proposal=replace_proposal(
                current.revision_id,
                "rev-s1",
                node_id="run-a",
                before_text="Alpha",
                after_text="A2",
                record_id="p-retarget",
            ),
        )

    # A semantically no-op patch (after == before) against the CURRENT
    # revision reproduces that revision's identity and is rejected as a
    # duplicate identity rather than fabricating a new revision.
    no_op = replace_proposal(
        current.revision_id, "rev-s1", node_id="run-b", before_text="Beta", after_text="Beta", record_id="p-noop"
    )
    with pytest.raises(DuplicateRecordIdentityError):
        await submit_patch(factory, service, workspace_id=WS, proposal=no_op)


@pytest.mark.asyncio
async def test_submit_without_initialized_view_is_stale(kernel_env):
    service = KernelCommitService(kernel_env)
    proposal = replace_proposal(
        view_text_hash("nowhere"), "rev-s1", node_id="run-a", before_text="Alpha", after_text="X", record_id="p-void"
    )
    with pytest.raises(StaleBaseRevisionError):
        await submit_patch(kernel_env, service, workspace_id=WS, proposal=proposal)


@pytest.mark.asyncio
async def test_clean_rebuild_oracle_matches_incremental_history(env):
    factory, service, genesis = env
    r1 = replace_proposal(genesis.revision_id, "rev-s1", node_id="run-a", before_text="Alpha", after_text="A9", record_id="r1")
    await submit_patch(factory, service, workspace_id=WS, proposal=r1)
    current = await read_current_view(factory, WS)
    r2 = replace_proposal(current.revision_id, "rev-s1", node_id="run-c", before_text="Gamma", after_text="G9", record_id="r2")
    await submit_patch(factory, service, workspace_id=WS, proposal=r2)

    rebuilt = await clean_rebuild_view(factory, WS)
    final = await read_current_view(factory, WS)
    assert rebuilt.view_revision_id() == final.revision_id
    # bounded replay also reproduces the intermediate revision
    mid = await clean_rebuild_view(factory, WS, upto_commit=2)
    assert mid.view_revision_id() == current.revision_id


@pytest.mark.asyncio
async def test_clean_rebuild_detects_tampered_history(env):
    """If a committed view revision disagrees with replaying its own
    proposal, the oracle refuses — divergence can never pass silently."""
    factory, service, genesis = env
    proposal = replace_proposal(
        genesis.revision_id, "rev-s1", node_id="run-a", before_text="Alpha", after_text="A3", record_id="p-ok"
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=proposal)

    # Tamper directly with the committed result record's stored payload:
    # same identity row, different declared texts than replay yields.
    async with factory() as session:
        row = (
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == "view_document",
                    KernelRecord.kernel_commit_id == 2,
                )
            )
        ).scalar_one()
        payload = json.loads(row.payload_json)
        payload["texts"]["run-b"] = "Tampered"
        from app.utils.canonical import canonical_json_str

        row.payload_json = canonical_json_str(payload)
        await session.commit()

    from app.kernel.errors import InvalidViewAdvancementError

    with pytest.raises(InvalidViewAdvancementError, match="diverged"):
        await clean_rebuild_view(factory, WS)


@pytest.mark.asyncio
async def test_accepted_lineage_survives_restart(env, tmp_path):
    factory, service, genesis = env
    proposal = replace_proposal(
        genesis.revision_id, "rev-s1", node_id="run-b", before_text="Beta", after_text="B!", record_id="p-restart"
    )
    acceptance = await submit_patch(factory, service, workspace_id=WS, proposal=proposal)
    db_path = tmp_path / "kernel.db"

    # New process: fresh engine + services over the same file.
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        current = await read_current_view(factory2, WS)
        assert current is not None
        assert current.revision_id == acceptance.result.revision_id
        assert current.kernel_commit_id == acceptance.receipt.kernel_commit_id
        history = await load_view_history(factory2, WS)
        assert len(history) == 2
        verification = await verify_history(factory2, WS)
        assert verification.ok
        rebuilt = await clean_rebuild_view(factory2, WS)
        assert rebuilt.view_revision_id() == current.revision_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verify_history_ok_after_patches(env):
    factory, _service, _genesis = env
    verification = await verify_history(factory, WS)
    assert verification.ok
    assert not verification.problems
