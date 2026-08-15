"""Generation concurrency tests (V3.2 PR65A, matrix 10.3).

Builds racing kernel commits, duplicate/competing builds, readers
resolving during activation, activation retries, and independent
workspaces. No mixed-generation or ambiguous-current state may occur.
"""

from __future__ import annotations

import asyncio

import pytest

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import KernelError
from app.kernel.generations import (
    GENERATION_STATE_ACTIVE,
    GENERATION_STATE_SUPERSEDED,
    GenerationReader,
    GenerationService,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.records import ClaimAssertionRecord
from app.kernel.snapshots import resolve_snapshot

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


async def _commit(service: KernelCommitService, workspace: str, key: str) -> None:
    await service.commit(
        KernelCommitBatch(workspace_id=workspace, records=(_assertion(key),))
    )


async def test_build_racing_kernel_commits_pins_its_cut(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "ws-a", "base")
    gen_service = GenerationService(factory)

    snapshot = await resolve_snapshot(factory, "ws-a")  # pin at K=1

    async def commits() -> None:
        for i in range(6):
            await _commit(service, "ws-a", f"racing-{i}")

    async def build() -> None:
        await asyncio.sleep(0)  # interleave with the writers
        ref = await gen_service.build_and_activate(snapshot)
        assert ref.kernel_commit_id == 1
        assert ref.record_count == 1

    await asyncio.gather(commits(), build())

    current = await resolve_current_generation(factory, "ws-a")
    assert current.kernel_commit_id == 1  # the pinned cut, not the new head
    assert current.record_count == 1
    assert (await verify_generation(factory, current.generation_id)).ok
    # the kernel chain itself stayed coherent
    from app.kernel.replay import verify_history

    verification = await verify_history(factory, "ws-a")
    assert verification.ok and verification.head_kernel_commit_id == 7


async def test_two_identical_builds_converge_idempotently(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "ws-a", "base")
    await _commit(service, "ws-a", "more")
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, "ws-a")

    first, second = await asyncio.gather(
        gen_service.build_and_activate(snapshot),
        gen_service.build_and_activate(snapshot),
    )
    assert first.generation_id == second.generation_id
    assert first.content_digest == second.content_digest
    reader = GenerationReader(factory, first.generation_id)
    assert await reader.count_records() == 2  # no duplicated materialized rows
    assert (await verify_generation(factory, first.generation_id)).ok
    assert (await resolve_current_generation(factory, "ws-a")).generation_id == (
        first.generation_id
    )


async def test_competing_snapshots_single_active_winner(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "ws-a", "one")
    cut_one = await resolve_snapshot(factory, "ws-a", at_commit=1)
    await _commit(service, "ws-a", "two")
    cut_two = await resolve_snapshot(factory, "ws-a")

    gen_service = GenerationService(factory)
    ref_one, ref_two = await asyncio.gather(
        gen_service.build_and_activate(cut_one),
        gen_service.build_and_activate(cut_two),
    )
    assert ref_one.generation_id != ref_two.generation_id

    states = {
        ref.generation_id: (await gen_service.get_generation(ref.generation_id)).state
        for ref in (ref_one, ref_two)
    }
    active = [gid for gid, state in states.items() if state == GENERATION_STATE_ACTIVE]
    superseded = [gid for gid, state in states.items() if state == GENERATION_STATE_SUPERSEDED]
    assert len(active) == 1 and len(superseded) == 1  # exactly one current truth

    current = await resolve_current_generation(factory, "ws-a")
    assert current.generation_id == active[0]
    assert (await verify_generation(factory, ref_one.generation_id)).ok
    assert (await verify_generation(factory, ref_two.generation_id)).ok


async def test_readers_during_activation_stay_pinned(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "ws-a", "base")
    gen_service = GenerationService(factory)
    gen_a = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a", at_commit=1)
    )
    reader = await asyncio.to_thread(
        lambda: GenerationReader(factory, gen_a.generation_id)
    )

    async def churn_reads(stop: asyncio.Event) -> list[str]:
        seen: list[str] = []
        while not stop.is_set():
            summary = await reader.summary()
            seen.append(summary.generation_id)
            await reader.count_records()
            await reader.list_records(limit=2)
            await asyncio.sleep(0)
        return seen

    stop = asyncio.Event()
    reads = asyncio.create_task(churn_reads(stop))
    await _commit(service, "ws-a", "next")
    gen_b = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    await asyncio.sleep(0)
    stop.set()
    seen = await reads

    assert set(seen) == {gen_a.generation_id}  # never mixed with B
    assert gen_b.generation_id != gen_a.generation_id
    assert (
        await resolve_current_generation(factory, "ws-a")
    ).generation_id == gen_b.generation_id


async def test_concurrent_activations_of_same_generation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "ws-a", "base")
    gen_service = GenerationService(factory)
    ref = await gen_service.build(await resolve_snapshot(factory, "ws-a"))

    results = await asyncio.gather(
        gen_service.activate(ref.generation_id),
        gen_service.activate(ref.generation_id),
        gen_service.activate(ref.generation_id),
    )
    assert all(r.state == GENERATION_STATE_ACTIVE for r in results)
    assert (
        await resolve_current_generation(factory, "ws-a")
    ).generation_id == ref.generation_id
    states = [g.state for g in await gen_service.list_generations()]
    assert states.count(GENERATION_STATE_ACTIVE) == 1


async def test_independent_workspaces_build_concurrently(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "ws-a", "a1")
    await _commit(service, "ws-a", "a2")
    await _commit(service, "ws-b", "b1")
    gen_service = GenerationService(factory)

    snap_a, snap_b = await asyncio.gather(
        resolve_snapshot(factory, "ws-a"),
        resolve_snapshot(factory, "ws-b"),
    )
    ref_a, ref_b = await asyncio.gather(
        gen_service.build_and_activate(snap_a),
        gen_service.build_and_activate(snap_b),
    )

    assert ref_a.workspace_id == "ws-a" and ref_b.workspace_id == "ws-b"
    assert (await resolve_current_generation(factory, "ws-a")).generation_id == (
        ref_a.generation_id
    )
    assert (await resolve_current_generation(factory, "ws-b")).generation_id == (
        ref_b.generation_id
    )

    reader_a = GenerationReader(factory, ref_a.generation_id, workspace_id="ws-a")
    reader_b = GenerationReader(factory, ref_b.generation_id, workspace_id="ws-b")
    assert await reader_a.count_records() == 2
    assert await reader_b.count_records() == 1

    # workspace guard: a generation of ws-b cannot pose as ws-a state
    with pytest.raises(KernelError):
        await GenerationReader(
            factory, ref_b.generation_id, workspace_id="ws-a"
        ).summary()
