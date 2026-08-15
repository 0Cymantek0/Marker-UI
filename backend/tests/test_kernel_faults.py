"""Kernel commit fault-injection matrix (V3.2 PR63A, workstream E).

For every deterministic commit-protocol phase, an injected failure inside
the transaction must leave: the previous head current, no manifest for
the rejected commit, no subset of the rejected batch visible as committed
state, and a later clean retry succeeding without manual repair.

This is database-transaction PR63A fault coverage only. It deliberately
does not claim PR64 filesystem/object-store crash behavior.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import (
    FAULT_PHASES,
    PHASE_BEGIN,
    KernelCommitBatch,
    KernelCommitService,
)
from app.kernel.errors import InjectedFaultError
from app.kernel.models import (
    KernelCommitHead,
    KernelCommitManifest,
    KernelRecord,
    KernelRecordEdge,
)
from app.kernel.records import (
    EDGE_KIND_EVIDENCE_FOR,
    ClaimAssertionRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import read_head

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


def _second_batch() -> KernelCommitBatch:
    a = _assertion("second-commit")
    o = ObservationRecord(observer="obs", derivation={"pass": 2})
    return KernelCommitBatch(
        workspace_id="ws-a",
        records=(a, o),
        edges=(
            KernelEdge(
                edge_kind=EDGE_KIND_EVIDENCE_FOR,
                source_ref=o.record_id,
                target_ref=a.record_id,
            ),
        ),
    )


async def _counts(factory: async_sessionmaker) -> tuple[int, int, int, int, int]:
    """(head, manifests, records, edges, rejected-record-ids present)."""
    async with factory() as session:
        head = (
            await session.execute(
                select(func.count()).select_from(KernelCommitManifest).where(
                    KernelCommitManifest.workspace_id == "ws-a"
                )
            )
        ).scalar_one()
        manifests = head  # alias for readability below
        records = (
            await session.execute(
                select(func.count()).select_from(KernelRecord).where(
                    KernelRecord.workspace_id == "ws-a"
                )
            )
        ).scalar_one()
        edges = (
            await session.execute(
                select(func.count()).select_from(KernelRecordEdge).where(
                    KernelRecordEdge.workspace_id == "ws-a"
                )
            )
        ).scalar_one()
        head_value = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == "ws-a"
            )
        )
    return manifests, records, edges, head_value or 0, 0


async def _visible_records_for_commit(factory, commit_id: int) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(KernelRecord).where(
                    KernelRecord.workspace_id == "ws-a",
                    KernelRecord.kernel_commit_id == commit_id,
                )
            )
        ).scalar_one()


async def test_all_fault_phases_leave_no_partial_state(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("first"),))
    )
    before = await _counts(kernel_env)

    for phase in sorted(FAULT_PHASES):
        batch = _second_batch()
        with pytest.raises(InjectedFaultError):
            await service.commit(batch, _inject_fault_at=phase)

        after = await _counts(kernel_env)
        assert after[:4] == before[:4], f"phase {phase}: durable state changed"
        assert await _visible_records_for_commit(kernel_env, 2) == 0, (
            f"phase {phase}: rejected batch members are visible"
        )

    # after every injected failure, a clean retry succeeds unchanged
    retry = await service.commit(_second_batch())
    assert retry.kernel_commit_id == 2
    assert retry.parent_kernel_commit_id == 1
    assert await read_head(kernel_env, "ws-a") == 2


async def test_fault_on_first_commit_ever_leaves_initial_state(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(InjectedFaultError):
        await service.commit(_second_batch(), _inject_fault_at=PHASE_BEGIN)

    manifests, records, edges, head, _ = await _counts(kernel_env)
    assert (manifests, records, edges) == (0, 0, 0)
    assert head == 0

    clean = await service.commit(_second_batch())
    assert clean.kernel_commit_id == 1
    assert clean.parent_kernel_commit_id == 0


async def test_unknown_fault_phase_rejected(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    with pytest.raises(Exception, match="unknown fault phase"):
        await service.commit(_second_batch(), _inject_fault_at="not-a-phase")
