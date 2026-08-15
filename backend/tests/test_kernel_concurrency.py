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
from app.kernel.models import KernelCommitManifest, KernelRecord
from app.kernel.outbox import OutboxIntent, list_outbox
from app.kernel.reconcile import verify_payload_availability
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


# ---------------------------------------------------------------------------
# V3.2 PR64: concurrency across the payload/storage boundary (plan 7.5)
# ---------------------------------------------------------------------------


def _payload_obs(index: int, payload: bytes) -> ObservationRecord:
    return ObservationRecord(
        observer=f"obs-{index}",
        derivation={"race": index},
        payload_bytes=payload,
    )


async def test_concurrent_distinct_payload_commits_stay_linear_and_complete(
    payload_env,
) -> None:
    factory, store, service = payload_env
    payloads = [f"distinct payload {i}".encode() for i in range(10)]

    receipts = await asyncio.gather(
        *(
            service.commit(
                KernelCommitBatch(
                    workspace_id="ws-race",
                    records=(_payload_obs(i, payloads[i]),),
                    outbox=(OutboxIntent(work_kind="materialize", payload={"i": i}),),
                )
            )
            for i in range(len(payloads))
        ),
        return_exceptions=True,
    )
    failures = [r for r in receipts if isinstance(r, BaseException)]
    assert failures == [], [repr(f) for f in failures]

    ids = sorted(r.kernel_commit_id for r in receipts)
    assert ids == list(range(1, len(payloads) + 1))  # linear chain
    history = await verify_history(factory, "ws-race")
    assert history.ok, history.problems

    availability = await verify_payload_availability(
        factory, store, workspace_id="ws-race"
    )
    assert availability.payload_backed_complete is True
    assert len(availability.orphan_objects) == 0

    # Exactly one outbox intent per commit — no duplicates, none lost.
    pending = await list_outbox(factory, workspace_id="ws-race")
    assert len(pending) == len(payloads)
    assert len({p.dedupe_key for p in pending}) == len(payloads)


async def test_concurrent_same_bytes_races_publish_one_object(payload_env) -> None:
    factory, store, service = payload_env
    shared = b"contended content bytes"

    receipts = await asyncio.gather(
        *(
            service.commit(
                KernelCommitBatch(
                    workspace_id="ws-same",
                    records=(
                        ObservationRecord(
                            observer=f"obs-{i}",
                            derivation={"witness": i},  # distinct evidence
                            payload_bytes=shared,
                        ),
                    ),
                )
            )
            for i in range(8)
        ),
        return_exceptions=True,
    )
    failures = [r for r in receipts if isinstance(r, BaseException)]
    assert failures == [], [repr(f) for f in failures]

    keys = await store.list_objects()
    assert len(keys) == 1  # one immutable object, byte-identical publishers
    check = await store.check_object(keys[0], expected_length=len(shared))
    assert check.available

    # Evidence did not collapse: 8 distinct records share the bytes.
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KernelRecord).where(KernelRecord.workspace_id == "ws-same")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 8
    assert len({r.identity_hash for r in rows}) == 8
    assert {r.payload_byte_hash for r in rows} == set(keys)

    history = await verify_history(factory, "ws-same")
    assert history.ok, history.problems


async def test_store_level_same_content_race_is_safe(payload_env) -> None:
    _factory, store, _service = payload_env
    payload = b"raw store race payload"

    staged = await asyncio.gather(
        *(store.stage(payload) for _ in range(6)),
        return_exceptions=True,
    )
    failures = [s for s in staged if isinstance(s, BaseException)]
    assert failures == [], [repr(f) for f in failures]

    keys = {s.blob_key for s in staged}
    assert len(keys) == 1
    assert await store.read(keys.pop()) == payload
    # At most one physical write happened; racers reused the result.
    assert store.bytes_written == len(payload)


async def test_db_contention_with_payload_staging_stays_bounded(payload_env) -> None:
    """Payload staging concurrent with DB writer contention: retries stay
    bounded, immutable content survives, chain stays linear."""
    factory, store, service = payload_env

    async def commit_many(prefix: str, base: int) -> list:
        out = []
        for i in range(6):
            receipt = await service.commit(
                KernelCommitBatch(
                    workspace_id="ws-busy",
                    records=(
                        _payload_obs(base + i, f"{prefix} bytes {i}".encode()),
                    ),
                )
            )
            out.append(receipt)
        return out

    results = await asyncio.gather(commit_many("left", 0), commit_many("right", 100))
    assert sum(len(r) for r in results) == 12
    assert await read_head(factory, "ws-busy") == 12
    history = await verify_history(factory, "ws-busy")
    assert history.ok, history.problems
    availability = await verify_payload_availability(factory, store, workspace_id="ws-busy")
    assert availability.payload_backed_complete is True
    assert service.busy_retries + service.head_retries <= 12 * 4  # bounded
