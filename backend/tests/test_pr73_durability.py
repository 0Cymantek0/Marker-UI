"""PR73 durability: deterministic reversal, restart survival, and
retention/GC honesty around the revision lineage."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitService
from app.kernel.errors import KernelError
from app.kernel.gc import collect
from app.kernel.generations import GenerationService
from app.kernel.patches import (
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    view_text_hash,
)
from app.kernel.patching import (
    clean_rebuild_view,
    initialize_view,
    load_view_history,
    read_current_view,
    reverse_patch,
    submit_patch,
)
from app.kernel.reading_order import OrderEdge, OrderNode, ReadingOrderGraph, order_confidence
from app.kernel.replay import verify_history
from app.kernel.retention import ROOT_KIND_GENERATION_HOLD, declare_hold, release_hold
from app.kernel.snapshots import resolve_snapshot

CONF = order_confidence("1.0")
WS = "ws-pr73-dur"


def base_graph() -> ReadingOrderGraph:
    return ReadingOrderGraph.build(
        [
            OrderNode(node_id="run-a", anchor_ref="anchor-a"),
            OrderNode(node_id="run-b", anchor_ref="anchor-b"),
        ],
        [
            OrderEdge(
                kind="before", source_id="run-a", target_id="run-b",
                producer="t", confidence=CONF,
            )
        ],
    )


def replace_proposal(base_revision, *, node_id, before_text, after_text, record_id):
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=base_revision,
            target_checks=(
                TargetCheck(node_id=node_id, before_hash=view_text_hash(before_text)),
            ),
            required_source_revision_refs=("rev-s1",),
        ),
        operations=(PatchOperation.replace_text(node_id=node_id, after_text=after_text),),
    )


@pytest_asyncio.fixture
async def env(kernel_env):
    service = KernelCommitService(kernel_env)
    genesis = await initialize_view(
        kernel_env,
        service,
        workspace_id=WS,
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts={"run-a": "Alpha", "run-b": "Beta"},
    )
    return kernel_env, service, genesis


@pytest.mark.asyncio
async def test_reversal_restores_prior_revision_exactly_and_keeps_history(env):
    factory, service, genesis = env
    patch = replace_proposal(
        genesis.revision_id,
        node_id="run-a",
        before_text="Alpha",
        after_text="Repaired",
        record_id="p-repair",
    )
    acceptance = await submit_patch(factory, service, workspace_id=WS, proposal=patch)

    reversal = await reverse_patch(
        factory, service, workspace_id=WS, proposal_record_id="p-repair"
    )
    # Content-digest-for-content-digest: the head names the prior
    # revision's exact identity, without rewriting any history.
    assert reversal.result.revision_id == genesis.revision_id
    current = await read_current_view(factory, WS)
    assert current.revision_id == genesis.revision_id
    assert current.view.texts["run-a"] == "Alpha"

    history = await load_view_history(factory, WS)
    # The lineage keeps genesis, the patch revision, AND the reversal
    # commit (a head movement with no new view record) — the original
    # patch event survives its own reversal.
    assert [entry.kernel_commit_id for entry in history] == [1, 2, 3]
    assert history[2].view is None  # reversal carries no new revision
    assert history[2].outcome is not None
    assert history[2].outcome.resulting_revision_id == genesis.revision_id
    rebuilt = await clean_rebuild_view(factory, WS)
    assert rebuilt.view_revision_id() == genesis.revision_id

    # Post-reversal patches chain from the restored revision.
    follow = replace_proposal(
        genesis.revision_id,
        node_id="run-b",
        before_text="Beta",
        after_text="Bravo",
        record_id="p-after-reverse",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=follow)
    final = await read_current_view(factory, WS)
    assert final.view.texts == {"run-a": "Alpha", "run-b": "Bravo"}


@pytest.mark.asyncio
async def test_reversal_after_intervening_change_conflicts(env):
    factory, service, genesis = env
    patch = replace_proposal(
        genesis.revision_id,
        node_id="run-a",
        before_text="Alpha",
        after_text="Repaired",
        record_id="p-repair-2",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=patch)
    current = await read_current_view(factory, WS)
    intervening = replace_proposal(
        current.revision_id,
        node_id="run-a",
        before_text="Repaired",
        after_text="Repaired again",
        record_id="p-intervening",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=intervening)

    # The reversal's before-value claim (current == "Repaired") no
    # longer holds; it must conflict rather than clobber.
    with pytest.raises(KernelError):
        await reverse_patch(
            factory, service, workspace_id=WS, proposal_record_id="p-repair-2"
        )
    final = await read_current_view(factory, WS)
    assert final.view.texts["run-a"] == "Repaired again"


@pytest.mark.asyncio
async def test_split_is_not_declared_reversible(env):
    factory, service, genesis = env
    split = PatchProposalRecord(
        record_id="p-split-r",
        preconditions=PatchPreconditions(
            base_revision_id=genesis.revision_id,
            target_checks=(
                TargetCheck(node_id="run-b", before_hash=view_text_hash("Beta")),
            ),
        ),
        operations=(
            PatchOperation.split_node(
                node_id="run-b",
                children=[
                    {"node_id": "run-b-s1", "text": "Be"},
                    {"node_id": "run-b-s2", "text": "ta"},
                ],
                child_order=["run-b-s1", "run-b-s2"],
            ),
        ),
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=split)
    with pytest.raises(KernelError, match="replace_text"):
        await reverse_patch(
            factory, service, workspace_id=WS, proposal_record_id="p-split-r"
        )


@pytest.mark.asyncio
async def test_accepted_lineage_and_reversal_survive_restart(env, tmp_path):
    factory, service, genesis = env
    patch = replace_proposal(
        genesis.revision_id,
        node_id="run-b",
        before_text="Beta",
        after_text="Bravo",
        record_id="p-restart",
    )
    await submit_patch(factory, service, workspace_id=WS, proposal=patch)
    await reverse_patch(
        factory, service, workspace_id=WS, proposal_record_id="p-restart"
    )

    url = f"sqlite+aiosqlite:///{(tmp_path / 'kernel.db').as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        current = await read_current_view(factory2, WS)
        assert current.revision_id == genesis.revision_id
        verification = await verify_history(factory2, WS)
        assert verification.ok and not verification.problems
        rebuilt = await clean_rebuild_view(factory2, WS)
        assert rebuilt.view_revision_id() == genesis.revision_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_keeps_prior_revision_materialization_alive(env, tmp_path):
    factory, service, genesis = env
    patch = replace_proposal(
        genesis.revision_id,
        node_id="run-a",
        before_text="Alpha",
        after_text="Repaired",
        record_id="p-gc",
    )
    acceptance = await submit_patch(factory, service, workspace_id=WS, proposal=patch)

    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, WS)
    generation = await gen_service.build_and_activate(snapshot)

    # A generation hold on the current generation protects its
    # materialization from collection while reversal/audit needs it.
    from app.kernel.generations import open_pinned_generation, resolve_current_generation
    from app.kernel.payloads import LocalPayloadStore

    hold = await declare_hold(
        factory,
        workspace_id=WS,
        root_kind=ROOT_KIND_GENERATION_HOLD,
        kernel_commit_id=acceptance.result.kernel_commit_id,
        target_generation_id=generation.generation_id,
    )
    assert hold.active

    store = LocalPayloadStore(tmp_path / "payloads")
    await collect(factory, store, workspace_id=WS, grace_seconds=0.0)
    # The pinned generation survived collection.
    current_gen = await resolve_current_generation(factory, WS)
    assert current_gen is not None and current_gen.generation_id == generation.generation_id
    reader = await open_pinned_generation(factory, current_gen.generation_id)
    assert await reader.count_records() > 0
    await reader.close()

    # Releasing the hold lets a later collection retire unprotected
    # generations — but committed kernel truth (the revision lineage)
    # is never deleted, and history stays reconstructable.
    await release_hold(factory, hold.root_id)
    await collect(factory, store, workspace_id=WS, grace_seconds=0.0)
    history = await load_view_history(factory, WS)
    assert [entry.kernel_commit_id for entry in history] == [1, 2]
    current = await read_current_view(factory, WS)
    assert current.revision_id == acceptance.result.revision_id
    rebuilt = await clean_rebuild_view(factory, WS)
    assert rebuilt.view_revision_id() == current.revision_id
