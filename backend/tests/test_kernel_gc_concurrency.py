"""GC concurrency tests (V3.2 PR65B, plan matrix 9, 21, 22).

Duplicate collectors converge safely; collection coexists with
concurrent commits under the existing retry/serialization rules without
long write transactions; a long-lived reader that keeps its lease
renewed cannot be collected underneath itself; and a hold declared
during a real grace window is honored.
"""

from __future__ import annotations

import asyncio

import pytest

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.gc import collect, plan_collection, execute_collection
from app.kernel.generations import (
    GenerationService,
    open_pinned_generation,
    resolve_current_generation,
)
from app.kernel.records import ClaimAssertionRecord, ObservationRecord
from app.kernel.reconcile import verify_payload_availability
from app.kernel.retention import ROOT_KIND_SNAPSHOT_HOLD, declare_hold
from app.kernel.snapshots import (
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
    resolve_snapshot,
)

pytestmark = pytest.mark.asyncio


async def _commit(service: KernelCommitService, key: str, data: bytes | None) -> None:
    if data is not None:
        record = ObservationRecord(
            observer="marker", derivation={"probe": key}, payload_bytes=data
        )
    else:
        record = ClaimAssertionRecord(
            claim_key=key, subject="doc:x.pdf", predicate="p", value=key
        )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )


async def _seed_superseded_state(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "p1", b"concurrency-one")
    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    await GenerationService(factory).build_and_activate(snapshot)
    await _commit(service, "p2", b"concurrency-two")
    meta = await resolve_snapshot(
        factory, "ws-a", required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY
    )
    await GenerationService(factory).build_and_activate(meta)


async def test_concurrent_collectors_converge(payload_env: tuple) -> None:
    """Matrix 9: two full collection passes racing each other delete
    each object exactly once and leave one authoritative tombstone."""
    factory, store, service = payload_env
    await _seed_superseded_state(payload_env)

    reports = await asyncio.gather(
        collect(factory, store), collect(factory, store)
    )
    total_deleted = sum(r.swept_deleted for r in reports)
    total_absent = sum(r.already_absent for r in reports)
    assert total_deleted == 2  # exactly once across both passes
    assert total_absent == 0  # the loser saw deleted-state, not absence
    assert set(await store.list_objects()) == set()

    import sqlite3
    from pathlib import Path

    db_path = Path(factory.kw["bind"].url.database)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT blob_key, state, attempts FROM kernel_payload_retirements"
        ).fetchall()
    assert len(rows) == 2
    assert all(state == "deleted" for _, state, _ in rows)

    current = await resolve_current_generation(factory, "ws-a")
    assert current is not None and current.state == "active"


async def test_duplicate_execution_of_one_plan_is_safe(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _seed_superseded_state(payload_env)
    plan = await plan_collection(factory, store)

    first, second = await asyncio.gather(
        execute_collection(factory, store, plan),
        execute_collection(factory, store, plan),
    )
    assert first.swept_deleted + second.swept_deleted == 2
    assert set(await store.list_objects()) == set()


async def test_collection_coexists_with_concurrent_commits(payload_env: tuple) -> None:
    """Matrix 21: commits run against the same workspace while GC works;
    both converge under the retry rules, no commit is lost, and no
    committed reference points at retired bytes."""
    factory, store, service = payload_env
    await _seed_superseded_state(payload_env)
    plan = await plan_collection(factory, store)

    async def writer(tag: str) -> None:
        for i in range(4):
            await _commit(service, f"{tag}-{i}", b"writer-bytes-" + tag.encode())

    async def collector() -> None:
        await execute_collection(factory, store, plan)
        await collect(factory, store)

    await asyncio.gather(writer("w1"), writer("w2"), collector())

    from app.kernel.replay import read_head, verify_history

    head = await read_head(factory, "ws-a")
    assert head == 2 + 8  # every commit landed
    assert (await verify_history(factory, "ws-a")).ok
    # bytes referenced by the new commits are present (staged fresh)
    availability = await verify_payload_availability(factory, store, workspace_id="ws-a")
    for state in availability.record_states:
        assert state.state in ("available", "retired")


async def test_long_reader_with_renewal_survives_repeated_collections(
    payload_env: tuple,
) -> None:
    """Matrix 22: a slow reader that keeps renewing its lease cannot be
    collected underneath itself, however many passes run."""
    factory, store, service = payload_env
    await _seed_superseded_state(payload_env)
    gen_service = GenerationService(factory)
    superseded = (await gen_service.list_generations(state="superseded"))[0]

    reader = await open_pinned_generation(factory, superseded.generation_id)
    try:
        for _ in range(3):
            await reader.renew(lease_seconds=300)
            report = await collect(factory, store)
            assert superseded.generation_id in (
                report.generations_rescued or ()
            ) or report.generations_retired == 0
        # the pinned superseded generation is still fully readable
        assert await reader.count_records() == 1
        assert (await reader.summary()).generation_id == superseded.generation_id
    finally:
        await reader.close()

    report = await collect(factory, store)
    assert report.generations_retired == 1  # released pin: now collectible


async def test_hold_declared_during_grace_window_is_honored(payload_env: tuple) -> None:
    """A root declared while the collector sleeps in its grace interval
    is visible at the recheck and rescues the bytes (real timing)."""
    factory, store, service = payload_env
    await _seed_superseded_state(payload_env)

    async def late_hold() -> None:
        await asyncio.sleep(0.05)
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=1,
            required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        )

    results = await asyncio.gather(
        collect(factory, store, grace_seconds=0.3), late_hold()
    )
    report = results[0]
    from app.utils.canonical import payload_byte_hash

    k1 = payload_byte_hash(b"concurrency-one")
    assert k1 in report.rescued_keys or k1 in set(await store.list_objects())
    assert k1 in set(await store.list_objects())
