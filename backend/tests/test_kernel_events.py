"""Durable semantic event sequencing, replay, and subscriber independence
tests (V3.2 PR67A).

The behavioral contract under test (matrices E and F):

* the semantic sequence is unique and monotonic per scope under
  concurrent producers — it cannot fork or regress;
* replay order is the sequence, never timestamps;
* replay from sequence K returns exactly the durable events after K;
* restart between append and replay changes neither order nor
  membership;
* terminal events remain replayable when no client was connected;
* a progress flood coalesces to one durable snapshot row and never
  forces one row per tick, and durable events are never dropped as a
  consequence of progress backpressure;
* a slow or disconnected consumer cannot block execution; reconnect
  resumes from the durable sequence.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel import events, scheduler
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InvalidEventError
from app.kernel.models import KernelEvent, KernelProgress
from app.kernel.outbox import OUTBOX_STATE_DONE, OutboxIntent, list_outbox
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

_seq = iter(range(10_000))


async def _new_work(payload_env, *, workspace_id: str = "ws-ev") -> int:
    _factory, _store, service = payload_env
    n = next(_seq)
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ClaimAssertionRecord(
                    claim_key=f"ev-{n}", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"n": n}),),
        )
    )
    rows = await list_outbox(payload_env[0])
    return rows[-1].id


async def _event_count(factory) -> int:
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(KernelEvent))
        ).scalar_one()


async def _progress_count(factory) -> int:
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(KernelProgress))
        ).scalar_one()


# ---------------------------------------------------------------------------
# E1: concurrency cannot fork the sequence
# ---------------------------------------------------------------------------


async def test_concurrent_producers_never_fork_the_sequence(payload_env) -> None:
    factory = payload_env[0]
    producers = 8
    per_task = 25

    async def produce(task_id: int) -> None:
        for i in range(per_task):
            await events.append(
                factory,
                workspace_id="ws-ev",
                event_type="probe.tick",
                payload={"task": task_id, "i": i},
            )

    await asyncio.gather(*(produce(t) for t in range(producers)))

    replayed = await events.replay(factory, workspace_id="ws-ev")
    assert len(replayed) == producers * per_task
    sequences = [e.semantic_sequence for e in replayed]
    assert sequences == list(range(1, producers * per_task + 1))
    assert len(set(sequences)) == len(sequences)  # unique — no fork, no duplicate


# ---------------------------------------------------------------------------
# E2: replay from a cursor, bounded
# ---------------------------------------------------------------------------


async def test_replay_from_sequence_returns_remaining_events_in_order(
    payload_env,
) -> None:
    factory = payload_env[0]
    for i in range(10):
        await events.append(
            factory, workspace_id="ws-ev", event_type="probe.tick", payload={"i": i}
        )

    after_three = await events.replay(factory, workspace_id="ws-ev", after_sequence=3)
    assert [e.semantic_sequence for e in after_three] == list(range(4, 11))

    limited = await events.replay(
        factory, workspace_id="ws-ev", after_sequence=3, limit=2
    )
    assert [e.semantic_sequence for e in limited] == [4, 5]

    with pytest.raises(InvalidEventError):
        await events.replay(factory, workspace_id="ws-ev", limit=0)
    assert await events.get_latest_sequence(factory, workspace_id="ws-ev") == 10


# ---------------------------------------------------------------------------
# E3: restart between append and replay
# ---------------------------------------------------------------------------


async def test_restart_preserves_order_and_membership(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'events.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        for i in range(6):
            await events.append(
                factory, workspace_id="ws", event_type="probe.tick", payload={"i": i}
            )
        before = await events.replay(factory, workspace_id="ws")
    finally:
        await engine.dispose()

    # "Restart": a fresh engine over the same durable file.
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        after = await events.replay(factory2, workspace_id="ws")
        assert [(e.semantic_sequence, e.event_type, e.payload) for e in after] == [
            (e.semantic_sequence, e.event_type, e.payload) for e in before
        ]
        assert await events.get_latest_sequence(factory2, workspace_id="ws") == 6
        resume = await events.append(
            factory2, workspace_id="ws", event_type="probe.tick", payload={"i": 6}
        )
        assert resume.semantic_sequence == 7  # the sequence continues, no reset
    finally:
        await engine2.dispose()


# ---------------------------------------------------------------------------
# E4: terminal truth with nobody listening
# ---------------------------------------------------------------------------


async def test_terminal_events_survive_with_no_subscriber(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env)

    # No follow(), no consumer anywhere: plain execution lifecycle.
    claimed = await scheduler.claim_fair(factory, owner_id="worker-a")
    assert claimed is not None and claimed.work_id == work_id
    await scheduler.accept_work(
        factory,
        work_id=work_id,
        fencing_token=claimed.lease.fencing_token,
        result={"done": True},
    )
    from app.kernel import fencing

    assert await fencing.complete_work(
        factory, work_id=work_id, fencing_token=claimed.lease.fencing_token
    )

    replayed = await events.replay(factory, workspace_id="ws-ev")
    assert [e.event_type for e in replayed] == ["work.claimed", "work.accepted"]
    accepted = replayed[-1]
    assert accepted.payload["work_id"] == work_id


# ---------------------------------------------------------------------------
# E5: progress coalesces; E6: flood cannot drop durable truth
# ---------------------------------------------------------------------------


async def test_progress_flood_coalesces_to_one_row(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env)
    for tick in range(150):
        await events.append_progress(
            factory,
            workspace_id="ws-ev",
            work_id=work_id,
            counter=tick,
            payload={"tick": tick},
        )

    assert await _progress_count(factory) == 1
    assert await _event_count(factory) == 0  # no per-tick durable rows

    snapshot = await events.get_progress(factory, workspace_id="ws-ev", work_id=work_id)
    assert snapshot is not None
    assert snapshot.counter == 149
    assert snapshot.payload["tick"] == 149


async def test_flood_never_drops_durable_events(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env)

    async def flood() -> None:
        for tick in range(100):
            await events.append_progress(
                factory, workspace_id="ws-ev", work_id=work_id, counter=tick
            )

    async def durable() -> None:
        for i in range(10):
            await events.append(
                factory,
                workspace_id="ws-ev",
                event_type="probe.control",
                payload={"i": i},
            )

    await asyncio.gather(flood(), durable())

    replayed = await events.replay(factory, workspace_id="ws-ev")
    assert [e.semantic_sequence for e in replayed] == list(range(1, 11))
    assert all(e.event_type == "probe.control" for e in replayed)
    assert await _progress_count(factory) == 1


# ---------------------------------------------------------------------------
# F: slow and disconnected subscribers
# ---------------------------------------------------------------------------


async def test_slow_consumer_does_not_block_execution(payload_env) -> None:
    """A consumer reading at a crawl gets every event eventually, while
    the executor completes the same amount of work it would with no
    consumer attached."""
    factory = payload_env[0]

    # Control: drain three jobs with no consumer at all.
    for _ in range(3):
        await _new_work(payload_env)
    baseline = time.perf_counter()
    for _ in range(3):
        claimed = await scheduler.claim_fair(factory, owner_id="solo")
        assert claimed is not None
        await scheduler.accept_work(
            factory,
            work_id=claimed.work_id,
            fencing_token=claimed.lease.fencing_token,
            result={"ok": True},
        )
        from app.kernel import fencing

        await fencing.complete_work(
            factory, work_id=claimed.work_id, fencing_token=claimed.lease.fencing_token
        )
    solo_duration = time.perf_counter() - baseline
    assert {r.state for r in await list_outbox(factory)} == {OUTBOX_STATE_DONE}

    # Now a deliberately slow follower trails a fresh workspace's
    # stream while the executor runs the same shaped workload.
    for _ in range(3):
        await _new_work(payload_env, workspace_id="ws-slow")

    async def execute() -> float:
        started = time.perf_counter()
        for _ in range(3):
            claimed = await scheduler.claim_fair(
                factory, owner_id="solo", workspace_id="ws-slow"
            )
            assert claimed is not None
            await scheduler.accept_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
                result={"ok": True},
            )
            from app.kernel import fencing

            await fencing.complete_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
            )
        return time.perf_counter() - started

    received: list[events.SemanticEvent] = []

    async def slow_consume() -> None:
        async for event in events.follow(
            factory,
            workspace_id="ws-slow",
            poll_interval=0.01,
            max_idle_seconds=2.0,
        ):
            received.append(event)
            await asyncio.sleep(0.05)  # crawl

    consumer = asyncio.create_task(slow_consume())
    followed_duration = await execute()
    await asyncio.wait_for(consumer, timeout=15)

    # Execution stayed cheap: the trailing reader never gated it.
    assert followed_duration < max(solo_duration * 3, solo_duration + 1.0)
    kinds = [e.event_type for e in received]
    assert kinds.count("work.accepted") == 3  # every terminal event arrived
    seqs = [e.semantic_sequence for e in received]
    assert seqs == sorted(seqs)  # authoritative order even for a crawler


async def test_disconnect_and_reconnect_resume_from_durable_sequence(
    payload_env,
) -> None:
    factory = payload_env[0]
    for i in range(3):
        await events.append(
            factory, workspace_id="ws-ev", event_type="probe.tick", payload={"i": i}
        )

    # Consume three events, then disconnect (break the iteration).
    seen_first: list[int] = []
    async for event in events.follow(
        factory, workspace_id="ws-ev", poll_interval=0.01, max_idle_seconds=0.2
    ):
        seen_first.append(event.semantic_sequence)
        if len(seen_first) == 3:
            break

    # Work continues with nobody connected.
    for i in range(3, 6):
        await events.append(
            factory, workspace_id="ws-ev", event_type="probe.tick", payload={"i": i}
        )

    # Reconnect with the last delivered sequence as the cursor.
    resumed: list[int] = []
    async for event in events.follow(
        factory,
        workspace_id="ws-ev",
        after_sequence=seen_first[-1],
        poll_interval=0.01,
        max_idle_seconds=0.3,
    ):
        resumed.append(event.semantic_sequence)

    assert seen_first == [1, 2, 3]
    assert resumed == [4, 5, 6]  # exactly the missed tail, in order


# ---------------------------------------------------------------------------
# boundary validation
# ---------------------------------------------------------------------------


async def test_event_boundary_validation(payload_env) -> None:
    factory = payload_env[0]
    with pytest.raises(InvalidEventError):
        await events.append(
            factory, workspace_id="ws", event_type="Bad Type", payload={}
        )
    with pytest.raises(InvalidEventError):
        await events.append(
            factory, workspace_id="ws", event_type="probe.tick", payload={"x": 1},
            durability="ephemeral",
        )
    with pytest.raises(InvalidEventError):
        await events.append(
            factory, workspace_id="ws", event_type="probe.tick", payload={1, 2}
        )
    with pytest.raises(InvalidEventError):
        await events.append_progress(
            factory, workspace_id="ws", work_id=0, counter=1
        )
    with pytest.raises(InvalidEventError):
        await events.append_progress(
            factory, workspace_id="ws", work_id=1, counter=-1
        )
    assert await _event_count(factory) == 0  # nothing half-written
