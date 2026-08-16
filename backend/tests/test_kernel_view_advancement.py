"""PR73 in-commit view advancement tests: genesis, conditional flip,
all-or-conflict rollback, and fault injection at the new phases.

These tests drive KernelCommitService directly with view batches — the
service layer lands in the next commit; what is proven here is the
commit-transaction seam itself.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import (
    KernelCommitBatch,
    KernelCommitService,
    PHASE_VIEW_ADVANCED,
    PHASE_VIEW_CHECKED,
)
from app.kernel.errors import (
    BeforeHashMismatchError,
    DuplicateRecordIdentityError,
    InjectedFaultError,
    InvalidViewAdvancementError,
    StaleBaseRevisionError,
)
from app.kernel.models import KernelRecord, KernelViewHead
from app.kernel.patches import (
    PatchOperation,
    PatchOutcomeRecord,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    ViewAdvancement,
    ViewDocumentRecord,
    apply_operation,
    view_text_hash,
)
from app.kernel.reading_order import OrderEdge, OrderNode, ReadingOrderGraph, order_confidence

CONF = order_confidence("1.0")
WS = "ws-adv"


def base_graph() -> ReadingOrderGraph:
    return ReadingOrderGraph.build(
        [
            OrderNode(node_id="run-a", anchor_ref="anchor-a"),
            OrderNode(node_id="run-b", anchor_ref="anchor-b"),
        ],
        [
            OrderEdge(
                kind="before",
                source_id="run-a",
                target_id="run-b",
                producer="t",
                confidence=CONF,
            )
        ],
    )


def genesis_view() -> ViewDocumentRecord:
    return ViewDocumentRecord(
        record_id="view-genesis",
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts={"run-a": "Alpha", "run-b": "Beta"},
    )


def replacement_proposal(
    base: ViewDocumentRecord,
    *,
    node_id: str = "run-a",
    after_text: str = "Fixed",
    before_text: str | None = None,
    record_id: str = "proposal-1",
) -> PatchProposalRecord:
    before = before_text if before_text is not None else base.texts[node_id]
    return PatchProposalRecord(
        record_id=record_id,
        preconditions=PatchPreconditions(
            base_revision_id=base.view_revision_id(),
            target_checks=(
                TargetCheck(node_id=node_id, before_hash=view_text_hash(before)),
            ),
            required_source_revision_refs=(base.content_revision_ref,),
        ),
        operations=(PatchOperation.replace_text(node_id=node_id, after_text=after_text),),
    )


def patched_view(
    base: ViewDocumentRecord, proposal: PatchProposalRecord
) -> ViewDocumentRecord:
    graph, texts = base.graph, dict(base.texts)
    for op in proposal.operations:
        graph, texts = apply_operation(graph, texts, op)
    return ViewDocumentRecord(
        record_id="view-next",
        content_revision_ref=base.content_revision_ref,
        graph=graph,
        texts=texts,
    )


async def read_head(factory: async_sessionmaker) -> KernelViewHead | None:
    async with factory() as session:
        return (
            await session.execute(
                select(KernelViewHead).where(
                    KernelViewHead.workspace_id == WS,
                    KernelViewHead.view_id == "document",
                )
            )
        ).scalar_one_or_none()


async def record_count(factory: async_sessionmaker) -> int:
    async with factory() as session:
        return await session.scalar(
            select(func.count()).select_from(KernelRecord).where(
                KernelRecord.workspace_id == WS
            )
        )


@pytest_asyncio.fixture
async def service(kernel_env) -> KernelCommitService:
    return KernelCommitService(kernel_env)


@pytest.mark.asyncio
async def test_genesis_advancement_inserts_head(kernel_env, service):
    view = genesis_view()
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(view,),
            view_advancement=ViewAdvancement(new_revision_id=view.view_revision_id()),
        )
    )
    head = await read_head(kernel_env)
    assert head is not None
    assert head.current_revision_id == view.view_revision_id()
    assert head.kernel_commit_id == receipt.kernel_commit_id
    assert await record_count(kernel_env) == 1


@pytest.mark.asyncio
async def test_second_genesis_is_stale_conflict(kernel_env, service):
    view = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(view,),
            view_advancement=ViewAdvancement(new_revision_id=view.view_revision_id()),
        )
    )
    other = ViewDocumentRecord(
        record_id="view-other",
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts={"run-a": "Different", "run-b": "Beta"},
    )
    with pytest.raises(StaleBaseRevisionError) as excinfo:
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(other,),
                view_advancement=ViewAdvancement(
                    new_revision_id=other.view_revision_id()
                ),
            )
        )
    assert excinfo.value.expected_base_revision_id is None
    assert excinfo.value.observed_base_revision_id == view.view_revision_id()
    head = await read_head(kernel_env)
    assert head.current_revision_id == view.view_revision_id()  # unchanged
    assert await record_count(kernel_env) == 1  # nothing partially applied


@pytest.mark.asyncio
async def test_accepted_patch_flips_head_and_keeps_base_immutable(kernel_env, service):
    base = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(base,),
            view_advancement=ViewAdvancement(new_revision_id=base.view_revision_id()),
        )
    )
    base_revision = base.view_revision_id()

    proposal = replacement_proposal(base)
    next_view = patched_view(base, proposal)
    outcome = PatchOutcomeRecord(
        record_id="outcome-1",
        proposal_identity=proposal.proposal_id(),
        outcome="accepted",
        observed={
            "base_revision_id": base_revision,
            "source_revision": base.content_revision_ref,
        },
        resulting_revision_id=next_view.view_revision_id(),
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(proposal, outcome, next_view),
            view_advancement=ViewAdvancement(
                new_revision_id=next_view.view_revision_id(),
                base_revision_id=base_revision,
                proposal_record_id=proposal.record_id,
            ),
        )
    )
    head = await read_head(kernel_env)
    assert head.current_revision_id == next_view.view_revision_id()
    assert head.kernel_commit_id == receipt.kernel_commit_id

    # Base revision bytes/digest remain stable and reconstructable.
    async with kernel_env() as session:
        row = (
            await session.execute(
                select(KernelRecord.payload_json).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.identity_hash == base_revision,
                )
            )
        ).scalar_one()
    remat = ViewDocumentRecord.from_payload(__import__("json").loads(row), record_id="r")
    assert remat.view_revision_id() == base_revision
    assert remat.texts["run-a"] == "Alpha"


@pytest.mark.asyncio
async def test_stale_base_patch_rolls_back_everything(kernel_env, service):
    base = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(base,),
            view_advancement=ViewAdvancement(new_revision_id=base.view_revision_id()),
        )
    )
    # Accepted change advances the view.
    first = replacement_proposal(base, after_text="First")
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(first, patched_view(base, first)),
            view_advancement=ViewAdvancement(
                new_revision_id=patched_view(base, first).view_revision_id(),
                base_revision_id=base.view_revision_id(),
                proposal_record_id=first.record_id,
            ),
        )
    )
    current = await read_head(kernel_env)
    records_before = await record_count(kernel_env)

    # A second patch still asserting the ORIGINAL base must conflict and
    # leave no accepted partial state.
    stale = replacement_proposal(base, node_id="run-b", after_text="Stale")
    with pytest.raises(StaleBaseRevisionError) as excinfo:
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(stale, patched_view(base, stale)),
                view_advancement=ViewAdvancement(
                    new_revision_id=patched_view(base, stale).view_revision_id(),
                    base_revision_id=base.view_revision_id(),
                    proposal_record_id=stale.record_id,
                ),
            )
        )
    assert excinfo.value.observed_base_revision_id == current.current_revision_id
    after = await read_head(kernel_env)
    assert after.current_revision_id == current.current_revision_id
    assert after.kernel_commit_id == current.kernel_commit_id
    assert await record_count(kernel_env) == records_before


@pytest.mark.asyncio
async def test_before_hash_mismatch_inside_transaction(kernel_env, service):
    base = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(base,),
            view_advancement=ViewAdvancement(new_revision_id=base.view_revision_id()),
        )
    )
    # Patch asserts a before value that is not true even against its own
    # declared base — the transactional evaluation must reject it.
    lying = replacement_proposal(base, before_text="Not The Value")
    with pytest.raises(BeforeHashMismatchError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(lying, patched_view(base, lying)),
                view_advancement=ViewAdvancement(
                    new_revision_id=patched_view(base, lying).view_revision_id(),
                    base_revision_id=base.view_revision_id(),
                    proposal_record_id=lying.record_id,
                ),
            )
        )
    head = await read_head(kernel_env)
    assert head.current_revision_id == base.view_revision_id()
    assert await record_count(kernel_env) == 1


@pytest.mark.asyncio
async def test_advancement_requires_batch_view_record_and_proposal(kernel_env, service):
    base = genesis_view()
    # new_revision_id not present in the batch
    with pytest.raises(InvalidViewAdvancementError, match="same batch"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(base,),
                view_advancement=ViewAdvancement(
                    new_revision_id=view_text_hash("not-in-batch")
                ),
            )
        )
    # proposal_record_id not present in the batch
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(base,),
            view_advancement=ViewAdvancement(new_revision_id=base.view_revision_id()),
        )
    )
    proposal = replacement_proposal(base)
    next_view = patched_view(base, proposal)
    with pytest.raises(InvalidViewAdvancementError, match="not part of this batch"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(next_view,),
                view_advancement=ViewAdvancement(
                    new_revision_id=next_view.view_revision_id(),
                    base_revision_id=base.view_revision_id(),
                    proposal_record_id="proposal-missing",
                ),
            )
        )
    assert await record_count(kernel_env) == 1


@pytest.mark.asyncio
async def test_tampered_result_revision_is_rejected(kernel_env, service):
    """The commit independently recomputes the patch result; a view
    record that disagrees with the proposal's operations can never be
    advanced to, even with a self-consistent-looking batch."""
    base = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(base,),
            view_advancement=ViewAdvancement(new_revision_id=base.view_revision_id()),
        )
    )
    proposal = replacement_proposal(base, after_text="Fixed")
    tampered = ViewDocumentRecord(
        record_id="view-tampered",
        content_revision_ref=base.content_revision_ref,
        graph=base.graph,
        texts={"run-a": "Totally Different", "run-b": "Beta"},
    )
    with pytest.raises(InvalidViewAdvancementError, match="does not equal"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(proposal, tampered),
                view_advancement=ViewAdvancement(
                    new_revision_id=tampered.view_revision_id(),
                    base_revision_id=base.view_revision_id(),
                    proposal_record_id=proposal.record_id,
                ),
            )
        )
    head = await read_head(kernel_env)
    assert head.current_revision_id == base.view_revision_id()


@pytest.mark.asyncio
async def test_duplicate_view_identity_rejected(kernel_env, service):
    view = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(view,),
            view_advancement=ViewAdvancement(new_revision_id=view.view_revision_id()),
        )
    )
    clone = ViewDocumentRecord(
        record_id="view-clone",
        content_revision_ref=view.content_revision_ref,
        graph=view.graph,
        texts=dict(view.texts),
    )
    with pytest.raises(DuplicateRecordIdentityError):
        await service.commit(KernelCommitBatch(workspace_id=WS, records=(clone,)))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [PHASE_VIEW_CHECKED, PHASE_VIEW_ADVANCED])
async def test_view_fault_injection_rolls_back_head_and_records(
    kernel_env, service, phase
):
    base = genesis_view()
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(base,),
            view_advancement=ViewAdvancement(new_revision_id=base.view_revision_id()),
        )
    )
    proposal = replacement_proposal(base)
    next_view = patched_view(base, proposal)
    with pytest.raises(InjectedFaultError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(proposal, next_view),
                view_advancement=ViewAdvancement(
                    new_revision_id=next_view.view_revision_id(),
                    base_revision_id=base.view_revision_id(),
                    proposal_record_id=proposal.record_id,
                ),
            ),
            _inject_fault_at=phase,
        )
    # Old-valid current revision, no mixed state, no orphan records.
    head = await read_head(kernel_env)
    assert head.current_revision_id == base.view_revision_id()
    assert head.kernel_commit_id == 1
    assert await record_count(kernel_env) == 1
