"""Crash, race, and contention evidence for the PR67A scheduler
(matrices A/G).

Every fault injected here must converge to a small, documented state
set — never a fabricated claim, publication, or event:

* crash between the outbox delivery claim and the fence acquire →
  orphan delivery returned to pending by reconciliation (at-least-once);
* crash between the fence acquire and the bookkeeping commit → the
  lease stands, the ``work.claimed`` event is re-derived from the
  authority, and renewal fails closed until fresh evidence is seeded;
* crash between acceptance and the event append → ``work.accepted``
  re-derived from the publication authority;
* concurrent dispatchers over shared and disjoint groups → each work
  item served exactly once, publications unique, bookkeeping consistent;
* concurrent event producers alongside dispatchers → sequence stays
  linear, no busy exhaustion.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel import events, fencing, outbox, scheduler
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import InvalidChallengeError
from app.kernel.models import KernelPublication
from app.kernel.outbox import (
    OUTBOX_STATE_DONE,
    OUTBOX_STATE_IN_FLIGHT,
    OutboxIntent,
    list_outbox,
)
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

_seq = iter(range(10_000))


async def _new_work(payload_env, *, workspace_id: str = "ws-g") -> int:
    _factory, _store, service = payload_env
    n = next(_seq)
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ClaimAssertionRecord(
                    claim_key=f"fault-{n}", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"n": n}),),
        )
    )
    rows = await list_outbox(payload_env[0])
    return rows[-1].id


async def _publication_count(factory) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(KernelPublication)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# G: crash after the delivery claim, before the fence
# ---------------------------------------------------------------------------


async def test_orphan_delivery_is_reconciled_to_pending(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env)

    # Crash window: the delivery moved in_flight, no lease was taken.
    assert await outbox.claim(factory, work_id) is not None
    assert (await list_outbox(factory))[-1].state == OUTBOX_STATE_IN_FLIGHT

    report = await scheduler.reconcile_dispatch(factory)
    assert report["orphaned_deliveries_released"] == 1
    assert report["events_repaired"] == []
    assert (await list_outbox(factory))[-1].state == "pending"

    # The work is claimable again through the fair path.
    claimed = await scheduler.claim_fair(factory, owner_id="worker-a")
    assert claimed is not None and claimed.work_id == work_id
    await scheduler.accept_work(
        factory,
        work_id=work_id,
        fencing_token=claimed.lease.fencing_token,
        result={"ok": True},
    )


# ---------------------------------------------------------------------------
# G: crash after the fence, before the bookkeeping commit
# ---------------------------------------------------------------------------


async def test_lease_without_event_is_repaired_and_liveness_fails_closed(
    payload_env,
) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env)

    # Crash window: PR66 claim primitives ran, but the scheduler's
    # bookkeeping transaction (served count, liveness seed, event) did
    # not — a takeover-styled raw acquire models it exactly.
    assert await outbox.claim(factory, work_id) is not None
    lease = await fencing.acquire(factory, work_id=work_id, owner_id="worker-a")
    assert lease is not None and lease.fencing_token == 1

    # No challenge evidence exists: renewal must fail closed, not
    # accept timer-shaped guesses.
    with pytest.raises(InvalidChallengeError):
        await liveness_renew(factory, work_id, "worker-a", 1, "guessed", 1)

    report = await scheduler.reconcile_dispatch(factory)
    repaired_types = [e.event_type for e in report["events_repaired"]]
    assert repaired_types == ["work.claimed"]
    repaired = report["events_repaired"][0]
    assert repaired.payload["repair"] is True
    assert repaired.payload["fencing_token"] == 1

    # The claim itself stands (it committed); the delivery is not
    # orphaned, and a second reconciliation is a no-op.
    second = await scheduler.reconcile_dispatch(factory)
    assert second["orphaned_deliveries_released"] == 0
    assert second["events_repaired"] == []
    states = {row.id: row.state for row in await list_outbox(factory)}
    assert states[work_id] == OUTBOX_STATE_IN_FLIGHT


async def liveness_renew(factory, work_id, owner, token, nonce, progress):
    from app.kernel import liveness

    return await liveness.renew_lease(
        factory,
        work_id=work_id,
        owner_id=owner,
        fencing_token=token,
        challenge_nonce=nonce,
        progress=progress,
        active_request_id="stage-x",
    )


# ---------------------------------------------------------------------------
# G: acceptance whose event append was lost
# ---------------------------------------------------------------------------


async def test_accepted_publication_without_event_is_derived(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env)

    claimed = await scheduler.claim_fair(factory, owner_id="worker-a")
    assert claimed is not None and claimed.work_id == work_id

    # Raw PR66 acceptance: the event append that accept_work would
    # have done was lost to a crash.
    outcome = await fencing.accept(
        factory,
        work_id=work_id,
        fencing_token=claimed.lease.fencing_token,
        result={"raw": True},
    )
    assert not outcome.already_accepted

    report = await scheduler.reconcile_dispatch(factory)
    repaired_types = [e.event_type for e in report["events_repaired"]]
    assert repaired_types == ["work.accepted"]
    event = report["events_repaired"][0]
    assert event.payload["publication_id"] == outcome.publication.publication_id
    assert event.payload["repair"] is True

    # Idempotent derivation; no duplicate accepted event ever.
    again = await scheduler.reconcile_dispatch(factory)
    assert again["events_repaired"] == []
    replayed = await events.replay(factory, workspace_id="ws-g")
    assert [e.event_type for e in replayed] == ["work.claimed", "work.accepted"]


# ---------------------------------------------------------------------------
# G: concurrent dispatchers over shared groups
# ---------------------------------------------------------------------------


async def test_concurrent_dispatchers_serve_each_item_exactly_once(
    payload_env,
) -> None:
    factory = payload_env[0]
    for group in ("ws-r1", "ws-r2", "ws-r3"):
        for _ in range(8):
            await _new_work(payload_env, workspace_id=group)
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-r2",
        policy=scheduler.GroupPolicy(weight=2.0),
    )

    async def dispatcher(worker: str) -> list[int]:
        served: list[int] = []
        while True:
            claimed = await scheduler.claim_fair(factory, owner_id=worker)
            if claimed is None:
                # Another worker may still be mid-flight; poll briefly
                # for reclaimed/redelivered work before giving up.
                await asyncio.sleep(0.05)
                retry = await scheduler.claim_fair(factory, owner_id=worker)
                if retry is None:
                    return served
                claimed = retry
            await scheduler.accept_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
                result={"worker": worker},
            )
            assert await fencing.complete_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
            )
            served.append(claimed.work_id)

    workers = [f"worker-{i}" for i in range(4)]
    served_lists = await asyncio.gather(*(dispatcher(w) for w in workers))
    all_served = [wid for lst in served_lists for wid in lst]

    assert len(all_served) == 24  # every item served
    assert len(set(all_served)) == 24  # ... exactly once
    assert {r.state for r in await list_outbox(factory)} == {OUTBOX_STATE_DONE}
    assert await _publication_count(factory) == 24

    stats = {
        view.group_id: view.served_count
        for view in await scheduler.group_stats(factory, resource_class="default")
    }
    assert sum(stats.values()) == 24  # bookkeeping stayed consistent
    assert stats["ws-r1"] > 0 and stats["ws-r2"] > 0 and stats["ws-r3"] > 0


# ---------------------------------------------------------------------------
# G: event producers racing the dispatchers
# ---------------------------------------------------------------------------


async def test_event_storm_alongside_dispatch_stays_linear(payload_env) -> None:
    factory = payload_env[0]
    for _ in range(9):
        await _new_work(payload_env)

    async def producer() -> None:
        for i in range(30):
            await events.append(
                factory,
                workspace_id="ws-g",
                event_type="probe.tick",
                payload={"i": i},
            )

    async def dispatcher() -> None:
        while True:
            claimed = await scheduler.claim_fair(factory, owner_id="storm-worker")
            if claimed is None:
                return
            await scheduler.accept_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
                result={"ok": True},
            )
            await fencing.complete_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
            )

    await asyncio.gather(producer(), dispatcher())

    replayed = await events.replay(factory, workspace_id="ws-g")
    sequences = [e.semantic_sequence for e in replayed]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert {r.state for r in await list_outbox(factory)} == {OUTBOX_STATE_DONE}
