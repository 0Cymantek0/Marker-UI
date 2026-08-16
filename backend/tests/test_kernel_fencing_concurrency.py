"""Fencing concurrency, redelivery, restart, and contention tests
(V3.2 PR66).

The adversarial matrix covered here:

* T2 — concurrent claim races produce exactly one current authority;
* T5 — a 100-round publication race stress: stale accept vs takeover
  vs concurrent accept under varying interleavings never yields more
  (or fewer) than one accepted publication per work;
* T11 — outbox redelivery after a lost acknowledgement: duplicate
  execution is fenced, delivery state stays reconcilable;
* T12 — authority and accepted truth reconstruct from the database
  after engine/process restart, including mid-race;
* T16 — SQLite ``BUSY`` is retried with a bounded budget and surfaces
  truthfully as :class:`KernelBusyError` when exhausted.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel import fencing
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import (
    KernelBusyError,
    PublicationConflictError,
    StaleFenceError,
)
from app.kernel.models import KernelPublication
from app.kernel.outbox import (
    OUTBOX_STATE_DONE,
    OutboxIntent,
    claim as outbox_claim,
    list_outbox,
    reset_in_flight,
)
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

SHORT_LEASE = 0.05
STRESS_ROUNDS = 100


async def _new_work(payload_env, *, tag: str, workspace_id: str = "ws") -> int:
    _factory, _store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ClaimAssertionRecord(
                    claim_key=f"race-{tag}",
                    subject="doc:x.pdf",
                    predicate="p",
                    value=1,
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"tag": tag}),),
        )
    )
    rows = [
        r for r in await list_outbox(payload_env[0]) if r.payload.get("tag") == tag
    ]
    assert len(rows) == 1
    return rows[0].id


async def _publication_count(factory) -> int:
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(KernelPublication))
        ).scalar_one()


# ---------------------------------------------------------------------------
# T2: concurrent claim race
# ---------------------------------------------------------------------------


async def test_concurrent_claims_yield_exactly_one_authority(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="claim-race")

    results = await asyncio.gather(
        *(fencing.claim_next(factory, owner_id=f"worker-{i}") for i in range(8))
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].work_id == work_id
    assert winners[0].lease.fencing_token == 1
    # No forked current authority exists in the durable state.
    lease = await fencing.get_lease(factory, work_id)
    assert lease.owner_id == winners[0].lease.owner_id


async def test_concurrent_first_acquires_yield_exactly_one_lease(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="acquire-race")

    results = await asyncio.gather(
        *(
            fencing.acquire(factory, work_id=work_id, owner_id=f"worker-{i}")
            for i in range(8)
        )
    )
    leases = [r for r in results if r is not None]
    assert len(leases) == 1 and leases[0].fencing_token == 1
    assert await _publication_count(factory) == 0


# ---------------------------------------------------------------------------
# T5: repeated publication race stress
# ---------------------------------------------------------------------------


async def _race_round(payload_env, work_id: int, pattern: int) -> None:
    factory = payload_env[0]
    stale = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    assert stale is not None and stale.fencing_token == 1

    async def stale_accept() -> None:
        if pattern == 0:
            await asyncio.sleep(0)  # accept while still current
        elif pattern == 1:
            await asyncio.sleep(SHORT_LEASE + 0.06)  # long after takeover
        else:
            await asyncio.sleep(SHORT_LEASE + 0.03)  # across the boundary
        await fencing.accept(
            factory, work_id=work_id, fencing_token=1, result={"who": "a"}
        )

    async def takeover_and_accept() -> None:
        await asyncio.sleep(SHORT_LEASE + 0.01)
        current = await fencing.acquire(
            factory, work_id=work_id, owner_id="worker-b", lease_seconds=60.0
        )
        if current is None:
            # The stale worker accepted first; the accepted lease blocks
            # any further ownership move. Legitimate race outcome.
            return
        assert current.fencing_token == 2
        await fencing.accept(
            factory, work_id=work_id, fencing_token=2, result={"who": "b"}
        )

    outcomes = await asyncio.gather(
        stale_accept(), takeover_and_accept(), return_exceptions=True
    )
    for outcome in outcomes:
        assert outcome is None or isinstance(
            outcome,
            (fencing.AcceptOutcome, StaleFenceError, PublicationConflictError),
        ), outcome

    publication = await fencing.get_publication(factory, work_id=work_id)
    assert publication is not None  # exactly one accepted winner exists
    lease = await fencing.get_lease(factory, work_id)
    # Either the accepting fence stayed final, or a successor moved past
    # the accepted-but-late original — never a second publication.
    assert lease.state == fencing.LEASE_STATE_ACCEPTED or lease.fencing_token >= 2


async def test_publication_race_stress_100_rounds(payload_env) -> None:
    """100 repeated races across three interleaving patterns (accept-
    first, takeover-first, simultaneous). Every round must settle with
    exactly one accepted publication; every loser is classified."""
    _factory, _store, service = payload_env
    # Sequential setup keeps commit-head contention out of the race.
    work_ids = []
    for i in range(STRESS_ROUNDS):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    ClaimAssertionRecord(
                        claim_key=f"stress-{i}",
                        subject="doc:x.pdf",
                        predicate="p",
                        value=i,
                    ),
                ),
                outbox=(
                    OutboxIntent(work_kind="materialize", payload={"round": i}),
                ),
            )
        )
    by_tag = {r.payload["round"]: r.id for r in await list_outbox(_factory)}
    work_ids = [by_tag[i] for i in range(STRESS_ROUNDS)]

    await asyncio.gather(
        *(
            _race_round(payload_env, work_id, i % 3)
            for i, work_id in enumerate(work_ids)
        )
    )

    assert await _publication_count(_factory) == STRESS_ROUNDS


# ---------------------------------------------------------------------------
# T11: outbox redelivery after lost acknowledgement
# ---------------------------------------------------------------------------


async def test_redelivery_after_lost_ack_is_fenced_and_reconcilable(payload_env) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="redeliver")

    first = await fencing.claim_next(
        factory, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    assert first is not None and first.lease.fencing_token == 1

    # Process state is lost: the delivery is reset for redelivery.
    assert await reset_in_flight(factory) == 1

    # While worker A's fence is still valid, redelivery is refused —
    # duplicate execution cannot fork authority.
    again = await fencing.claim_next(factory, owner_id="worker-b")
    assert again is None
    rows = {r.id: r.state for r in await list_outbox(factory)}
    assert rows[work_id] == "pending"

    # After the fence lapses, worker B takes over from durable state.
    await asyncio.sleep(SHORT_LEASE + 0.02)
    second = await fencing.claim_next(factory, owner_id="worker-b")
    assert second is not None and second.work_id == work_id
    assert second.lease.fencing_token == 2

    # The still-late original worker cannot accept or acknowledge.
    with pytest.raises(StaleFenceError):
        await fencing.accept(factory, work_id=work_id, fencing_token=1, result={"a": 1})
    assert not await fencing.complete_work(factory, work_id=work_id, fencing_token=1)

    outcome = await fencing.accept(
        factory, work_id=work_id, fencing_token=2, result={"b": 1}
    )
    assert not outcome.already_accepted
    assert await fencing.complete_work(factory, work_id=work_id, fencing_token=2)
    rows = {r.id: r.state for r in await list_outbox(factory)}
    assert rows[work_id] == OUTBOX_STATE_DONE
    assert await _publication_count(factory) == 1


# ---------------------------------------------------------------------------
# T12: restart reconstructs authority and accepted truth
# ---------------------------------------------------------------------------


async def test_authority_and_publication_survive_restart_mid_race(
    payload_env, tmp_path
) -> None:
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="restart")
    assert await outbox_claim(factory, work_id) is not None
    stale = await fencing.acquire(
        factory, work_id=work_id, owner_id="worker-a", lease_seconds=SHORT_LEASE
    )
    await asyncio.sleep(SHORT_LEASE + 0.02)  # "process" dies; lease lapses

    db_file = tmp_path / "kernel.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        # Reconstructed authority is the durable row, not memory.
        reopened = await fencing.get_lease(factory2, work_id=work_id)
        assert reopened.fencing_token == stale.fencing_token
        current = await fencing.acquire(factory2, work_id=work_id, owner_id="worker-b")
        assert current.fencing_token == 2
        with pytest.raises(StaleFenceError):
            await fencing.accept(
                factory2, work_id=work_id, fencing_token=stale.fencing_token, result={}
            )
        outcome = await fencing.accept(
            factory2, work_id=work_id, fencing_token=2, result={"restarted": True}
        )
        assert not outcome.already_accepted
        assert await fencing.complete_work(factory2, work_id=work_id, fencing_token=2)
        rows = {r.id: r.state for r in await list_outbox(factory2)}
        assert rows[work_id] == OUTBOX_STATE_DONE
    finally:
        await engine2.dispose()


# ---------------------------------------------------------------------------
# T16: SQLite busy behavior
# ---------------------------------------------------------------------------


async def test_busy_contention_is_absorbed_and_correct(payload_env) -> None:
    """Real overlapping writers: every distinct work item still settles
    with one accepted publication and a completed delivery."""
    factory, _store, service = payload_env
    for i in range(6):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    ClaimAssertionRecord(
                        claim_key=f"busy-{i}",
                        subject="doc:x.pdf",
                        predicate="p",
                        value=i,
                    ),
                ),
                outbox=(OutboxIntent(work_kind="index", payload={"i": i}),),
            )
        )

    async def worker(i: int) -> None:
        claimed = None
        while claimed is None:
            claimed = await fencing.claim_next(factory, owner_id=f"worker-{i}")
        outcome = await fencing.accept(
            factory,
            work_id=claimed.work_id,
            fencing_token=claimed.lease.fencing_token,
            result={"worker": i},
        )
        assert await fencing.complete_work(
            factory, work_id=claimed.work_id, fencing_token=claimed.lease.fencing_token
        )

    await asyncio.gather(*(worker(i) for i in range(6)))

    assert await _publication_count(factory) == 6
    states = {r.state for r in await list_outbox(factory)}
    assert states == {OUTBOX_STATE_DONE}


async def test_exhausted_busy_budget_surfaces_truthfully(payload_env, tmp_path) -> None:
    """A writer holding the database lock past the retry budget raises
    KernelBusyError — never a false success — and a clean retry after
    the lock clears converges."""
    factory, _store, _service = payload_env
    work_id = await _new_work(payload_env, tag="busy-exhaust")
    lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")

    db_file = tmp_path / "kernel.db"
    engine2 = create_async_engine(
        f"sqlite+aiosqlite:///{db_file.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 0.02},
    )
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    try:
        def _hold_write_lock() -> None:
            with closing(sqlite3.connect(db_file)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE kernel_outbox SET attempts = attempts WHERE id = ?",
                    (work_id,),
                )
                hold.set()
                cleared.wait(timeout=10.0)

        hold = threading.Event()
        cleared = threading.Event()
        holder = asyncio.create_task(asyncio.to_thread(_hold_write_lock))
        await asyncio.to_thread(hold.wait)

        with pytest.raises(KernelBusyError):
            await fencing.accept(
                factory2,
                work_id=work_id,
                fencing_token=lease.fencing_token,
                result={"r": 1},
                busy_retry_attempts=2,
                busy_retry_base_delay=0.001,
            )
        # Nothing was written by the failed attempt.
        assert await _publication_count(factory2) == 0

        cleared.set()
        await holder
        retry = await fencing.accept(
            factory2,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result={"r": 1},
        )
        assert not retry.already_accepted
    finally:
        cleared.set()
        await engine2.dispose()
