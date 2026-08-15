"""Kernel concurrency and ordering tests (V3.2 PR63A, workstream E).

Concurrent same-workspace commits must serialize into one contiguous
parent-linked chain: unique ids, no forks, no out-of-order visibility,
returned ids backed by durable manifests. Independent workspaces keep
independent heads. Concurrent duplicate submissions collapse to one
accepted commit.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import DuplicateRecordIdentityError
from app.kernel.models import KernelCommitManifest
from app.kernel.records import ClaimAssertionRecord, ObservationRecord
from app.kernel.replay import read_head, verify_history

pytestmark = pytest.mark.asyncio

CONCURRENT_COMMITS = 12


def _batch_for(index: int, rng: random.Random) -> KernelCommitBatch:
    records = []
    for j in range(rng.randint(1, 4)):
        if j % 2 == 0:
            records.append(
                ClaimAssertionRecord(
                    claim_key=f"claim-{index}-{j}",
                    subject="doc:report.pdf",
                    predicate="p",
                    value=j,
                )
            )
        else:
            records.append(
                ObservationRecord(
                    observer="obs", derivation={"batch": index, "seq": j}
                )
            )
    return KernelCommitBatch(
        workspace_id="ws-a", records=tuple(records), producer={"batch": index}
    )


async def test_concurrent_same_workspace_commits_serialize(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    rng = random.Random(20260815)
    batches = [_batch_for(i, rng) for i in range(CONCURRENT_COMMITS)]

    receipts = await asyncio.gather(
        *(service.commit(batch) for batch in batches), return_exceptions=True
    )
    failures = [r for r in receipts if isinstance(r, BaseException)]
    assert failures == [], [repr(f) for f in failures]

    ids = sorted(r.kernel_commit_id for r in receipts)
    assert ids == list(range(1, CONCURRENT_COMMITS + 1))  # unique, contiguous

    by_id = {r.kernel_commit_id: r for r in receipts}
    for commit_id in ids:
        receipt = by_id[commit_id]
        expected_parent = commit_id - 1
        assert receipt.parent_kernel_commit_id == expected_parent

    head = await read_head(kernel_env, "ws-a")
    assert head == CONCURRENT_COMMITS

    async with kernel_env() as session:
        manifests = (
            (
                await session.execute(
                    select(KernelCommitManifest)
                    .where(KernelCommitManifest.workspace_id == "ws-a")
                    .order_by(KernelCommitManifest.kernel_commit_id.asc())
                )
            )
            .scalars()
            .all()
        )
    assert [m.kernel_commit_id for m in manifests] == ids
    assert [m.parent_kernel_commit_id for m in manifests] == list(range(head))
    # every returned id corresponds to a durable manifest with matching counts
    for receipt in receipts:
        manifest = next(
            m for m in manifests if m.kernel_commit_id == receipt.kernel_commit_id
        )
        assert manifest.record_count == receipt.record_count
        assert manifest.edge_count == receipt.edge_count
        assert manifest.manifest_identity_hash == receipt.manifest_identity_hash

    result = await verify_history(kernel_env, "ws-a")
    assert result.ok, result.problems


async def test_concurrent_independent_workspaces_do_not_serialize_together(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)

    async def run_workspace(ws: str, count: int) -> None:
        for i in range(count):
            await service.commit(
                KernelCommitBatch(
                    workspace_id=ws,
                    records=(
                        ClaimAssertionRecord(
                            claim_key=f"{ws}-{i}",
                            subject="doc:x.pdf",
                            predicate="p",
                            value=i,
                        ),
                    ),
                )
            )

    await asyncio.gather(run_workspace("ws-a", 6), run_workspace("ws-b", 6))

    assert await read_head(kernel_env, "ws-a") == 6
    assert await read_head(kernel_env, "ws-b") == 6
    for ws in ("ws-a", "ws-b"):
        result = await verify_history(kernel_env, ws)
        assert result.ok, result.problems
        # every manifest of one workspace names only that workspace's chain
        async with kernel_env() as session:
            rows = (
                (
                    await session.execute(
                        select(KernelCommitManifest.kernel_commit_id).where(
                            KernelCommitManifest.workspace_id == ws
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows == list(range(1, 7))


async def test_concurrent_duplicate_identity_collapses_to_one_commit(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    # same semantic record submitted twice concurrently
    def _same_record() -> ClaimAssertionRecord:
        return ClaimAssertionRecord(
            claim_key="dup", subject="doc:x.pdf", predicate="p", value=1
        )

    results = await asyncio.gather(
        service.commit(KernelCommitBatch(workspace_id="ws-a", records=(_same_record(),))),
        service.commit(KernelCommitBatch(workspace_id="ws-a", records=(_same_record(),))),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], DuplicateRecordIdentityError)
    assert successes[0].kernel_commit_id == 1
    assert await read_head(kernel_env, "ws-a") == 1
    result = await verify_history(kernel_env, "ws-a")
    assert result.ok, result.problems
