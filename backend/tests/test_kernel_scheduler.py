"""Fair bounded scheduling contract tests (V3.2 PR67A).

The behavioral contract under test — observable service properties,
never a specific algorithm:

* mixed long/small load: a small/interactive item is served while a
  large backlog still exists (oldest-first would make it wait for the
  entire drain);
* equal-weight groups interleave — no eligible group starves and no
  service-gap runaway appears;
* configured weights steer long-run service shares without allowing
  starvation;
* age and deadline pressure rescue old eligible work from perpetual
  displacement;
* resource classes separate capacity: one class never serves the
  other's work;
* the per-group in-flight window bounds outstanding fan-out and
  applies backpressure instead of unbounded queueing;
* PR66 authority underneath is untouched: every claim still fences,
  accepts exactly-once, and acknowledges only behind accepted truth.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.kernel import fencing, scheduler
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import (
    InvalidGroupPolicyError,
    StaleFenceError,
    UnknownWorkError,
)
from app.kernel.events import replay
from app.kernel.outbox import OUTBOX_STATE_DONE, OutboxIntent, list_outbox
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

SHORT_LEASE = 0.05

_seq = iter(range(10_000))


async def _new_work(payload_env, *, workspace_id: str, tag: str | None = None) -> int:
    """Commit one outbox intent and return its durable work id."""
    _factory, _store, service = payload_env
    n = next(_seq)
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ClaimAssertionRecord(
                    claim_key=f"sched-{n}",
                    subject="doc:x.pdf",
                    predicate="p",
                    value=1,
                ),
            ),
            outbox=(
                OutboxIntent(work_kind="materialize", payload={"tag": tag or f"w{n}"}),
            ),
        )
    )
    rows = await list_outbox(payload_env[0])
    return rows[-1].id


async def _serve(factory, claimed) -> None:
    """Complete one claimed item through the full PR66 lifecycle."""
    await scheduler.accept_work(
        factory,
        work_id=claimed.work_id,
        fencing_token=claimed.lease.fencing_token,
        result={"ok": True, "work": claimed.work_id},
    )
    assert await fencing.complete_work(
        factory, work_id=claimed.work_id, fencing_token=claimed.lease.fencing_token
    )


async def _drain(factory, *, owner_id: str = "dispatcher") -> list[str]:
    """Serve until no eligible work remains; returns group service order."""
    order: list[str] = []
    while True:
        claimed = await scheduler.claim_fair(factory, owner_id=owner_id)
        if claimed is None:
            return order
        await _serve(factory, claimed)
        order.append(claimed.group_id)


async def _outbox_states(factory) -> dict[int, str]:
    return {row.id: row.state for row in await list_outbox(factory)}


async def _seed_served(factory, *, resource_class: str, **served_by_group: int) -> None:
    """Seed the non-authoritative service bookkeeping the way real
    history would have left it, so scoring tests start from a
    deterministic virtual-finish state (the counter is policy
    bookkeeping — authority lives in the fence/publication tables)."""
    from sqlalchemy import update

    from app.kernel.models import KernelSchedulingGroup

    for group_id in served_by_group:
        # The row must exist before it can carry history (group rows
        # otherwise materialize at the first dispatch backfill).
        await scheduler.set_group_policy(
            factory,
            resource_class=resource_class,
            group_id=group_id,
            policy=scheduler.GroupPolicy(),
        )
    async with factory() as session:
        async with session.begin():
            for group_id, served in served_by_group.items():
                result = await session.execute(
                    update(KernelSchedulingGroup)
                    .where(
                        KernelSchedulingGroup.resource_class == resource_class,
                        KernelSchedulingGroup.group_id == group_id,
                    )
                    .values(served_count=served)
                    .execution_options(synchronize_session=False)
                )
                assert result.rowcount == 1, f"group row missing for {group_id!r}"


# ---------------------------------------------------------------------------
# registration and policy surface
# ---------------------------------------------------------------------------


async def test_register_work_validates_and_defaults_group(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env, workspace_id="ws-reg")

    await scheduler.register_work(factory, work_id=work_id)
    view = await scheduler.get_group_policy(
        factory, resource_class="default", group_id="ws-reg"
    )
    assert view is not None
    assert view.policy.weight == scheduler.DEFAULT_WEIGHT
    assert view.policy.max_in_flight == scheduler.DEFAULT_MAX_IN_FLIGHT
    assert view.served_count == 0

    # Re-registration moves policy metadata but keeps registration age.
    await scheduler.register_work(
        factory, work_id=work_id, resource_class="cpu", group_id="ws-reg:doc-1"
    )
    with pytest.raises(UnknownWorkError):
        await scheduler.register_work(factory, work_id=999_999)


async def test_group_policy_validation(payload_env) -> None:
    factory = payload_env[0]
    with pytest.raises(InvalidGroupPolicyError):
        await scheduler.set_group_policy(
            factory,
            resource_class="default",
            group_id="g",
            policy=scheduler.GroupPolicy(weight=0),
        )
    with pytest.raises(InvalidGroupPolicyError):
        await scheduler.set_group_policy(
            factory,
            resource_class="default",
            group_id="g",
            policy=scheduler.GroupPolicy(max_in_flight=0),
        )
    with pytest.raises(InvalidGroupPolicyError):
        await scheduler.set_group_policy(
            factory, resource_class="Bad Class", group_id="g", policy=scheduler.GroupPolicy()
        )
    with pytest.raises(InvalidGroupPolicyError):
        await scheduler.set_group_policy(
            factory,
            resource_class="default",
            group_id="no good",
            policy=scheduler.GroupPolicy(),
        )

    await scheduler.set_group_policy(
        factory,
        resource_class="cpu",
        group_id="g",
        policy=scheduler.GroupPolicy(weight=2.5, max_in_flight=7),
    )
    view = await scheduler.get_group_policy(factory, resource_class="cpu", group_id="g")
    assert view.policy.weight == 2.5 and view.policy.max_in_flight == 7
    # Policy update preserves served bookkeeping.
    await scheduler.set_group_policy(
        factory,
        resource_class="cpu",
        group_id="g",
        policy=scheduler.GroupPolicy(weight=1.0, max_in_flight=2),
    )
    refreshed = await scheduler.get_group_policy(factory, resource_class="cpu", group_id="g")
    assert refreshed.served_count == view.served_count


# ---------------------------------------------------------------------------
# mixed long/small load (matrix B)
# ---------------------------------------------------------------------------


async def test_small_work_served_while_large_backlog_remains(payload_env) -> None:
    """The core fairness tracer bullet: two groups with deep backlogs,
    one late interactive item — the interactive item must receive
    service after a handful of dispatch decisions, not after the drain.
    Oldest-first (the PR66 ``claim_next`` baseline) serves it dead
    last."""
    factory = payload_env[0]
    for _ in range(10):
        await _new_work(payload_env, workspace_id="ws-big-a")
    for _ in range(10):
        await _new_work(payload_env, workspace_id="ws-big-b")

    service_order: list[str] = []
    small_served_at: int | None = None
    for i in range(40):
        if i == 2:  # small interactive item arrives mid-backlog
            await _new_work(payload_env, workspace_id="ws-small")
        claimed = await scheduler.claim_fair(factory, owner_id="dispatcher")
        if claimed is None:
            break
        await _serve(factory, claimed)
        service_order.append(claimed.group_id)
        if claimed.group_id == "ws-small":
            small_served_at = len(service_order)

    assert small_served_at is not None
    assert small_served_at <= 6, (
        f"small item waited {small_served_at} service decisions — fair "
        "dispatch must interleave it while the backlog remains"
    )
    counts = {g: service_order.count(g) for g in set(service_order)}
    assert counts == {"ws-big-a": 10, "ws-big-b": 10, "ws-small": 1}
    assert set((await _outbox_states(factory)).values()) == {OUTBOX_STATE_DONE}


async def test_oldest_first_baseline_serves_small_item_last(payload_env) -> None:
    """Control for the fairness test: the PR66 policy-light dispatch
    seam makes the late small item wait for the whole backlog. This is
    the baseline PR67A replaces, documented by contrast."""
    factory = payload_env[0]
    for _ in range(6):
        await _new_work(payload_env, workspace_id="ws-big-a")
    for _ in range(6):
        await _new_work(payload_env, workspace_id="ws-big-b")
    await _new_work(payload_env, workspace_id="ws-small")

    order: list[str] = []
    while True:
        claimed = await fencing.claim_next(factory, owner_id="dispatcher")
        if claimed is None:
            break
        await fencing.accept(
            factory,
            work_id=claimed.work_id,
            fencing_token=claimed.lease.fencing_token,
            result={"ok": True},
        )
        await fencing.complete_work(
            factory, work_id=claimed.work_id, fencing_token=claimed.lease.fencing_token
        )
        order.append(claimed.workspace_id)
    assert order[-1] == "ws-small"


async def test_equal_weight_groups_interleave_without_starvation(payload_env) -> None:
    """Equal weights must produce bounded service interleaving: at any
    prefix, no continuously eligible group falls behind by more than a
    small bound (the explicit measured service-gap guarantee)."""
    factory = payload_env[0]
    for _ in range(12):
        await _new_work(payload_env, workspace_id="ws-even-a")
    for _ in range(12):
        await _new_work(payload_env, workspace_id="ws-even-b")

    order = await _drain(factory)
    assert len(order) == 24
    served = {"a": 0, "b": 0}
    max_gap = 0
    for group in order:
        if group == "ws-even-a":
            served["a"] += 1
        else:
            served["b"] += 1
        max_gap = max(max_gap, abs(served["a"] - served["b"]))
    assert served == {"a": 12, "b": 12}
    assert max_gap <= 2, f"service gap {max_gap} exceeds the fairness bound"


async def test_weights_steer_long_run_share_without_starvation(payload_env) -> None:
    """A 2:1 weight ratio over inventories in the same 2:1 proportion
    must make the groups *finish together* (service tracks weight), and
    the lighter group is never shut out at any prefix."""
    factory = payload_env[0]
    for _ in range(24):
        await _new_work(payload_env, workspace_id="ws-heavy")
    for _ in range(12):
        await _new_work(payload_env, workspace_id="ws-light")
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-heavy",
        policy=scheduler.GroupPolicy(weight=2.0),
    )

    order = await _drain(factory)
    assert order.count("ws-heavy") == 24 and order.count("ws-light") == 12
    last_heavy = max(i for i, g in enumerate(order) if g == "ws-heavy")
    last_light = max(i for i, g in enumerate(order) if g == "ws-light")
    # Under 1:1 service the light group would finish ~12 claims before
    # the heavy one; weight-tracked service finishes them together.
    assert abs(last_heavy - last_light) <= 3, (
        f"weights did not steer long-run service: heavy finished at "
        f"{last_heavy}, light at {last_light}"
    )
    # The lighter group was never shut out: it receives service from
    # the very start of the drain, not only after heavy exhausts.
    first_light = order.index("ws-light")
    assert first_light <= 2

    order = await _drain(factory)  # already drained; sanity no-op
    assert order == []


async def test_age_boost_rescues_old_eligible_work(payload_env) -> None:
    """A group whose oldest item has waited past the boost window must
    outrank a lower-virtual-finish competitor; without the boost the
    same construction leaves it displaced (control case)."""
    factory = payload_env[0]
    for _ in range(2):
        await _new_work(payload_env, workspace_id="ws-young")
    for _ in range(2):
        await _new_work(payload_env, workspace_id="ws-old")
    # Deterministic scoring start: ws-young has less historical service
    # (virtual finish 2) than ws-old (virtual finish 4).
    await _seed_served(
        factory, resource_class="default", **{"ws-young": 2, "ws-old": 4}
    )

    # Control: no boost configured -> the younger group keeps winning.
    claimed = await scheduler.claim_fair(factory, owner_id="d")
    assert claimed.group_id == "ws-young"
    await _serve(factory, claimed)

    # Boost window is tiny for ws-old: its remaining items are aged.
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-old",
        policy=scheduler.GroupPolicy(age_boost_after_seconds=0.01),
    )
    await asyncio.sleep(0.05)
    claimed = await scheduler.claim_fair(factory, owner_id="d")
    assert claimed.group_id == "ws-old", (
        "aged eligible work must outrank a lower-virtual-finish group"
    )


async def test_deadline_pressure_promotes_late_work(payload_env) -> None:
    factory = payload_env[0]
    for _ in range(2):
        await _new_work(payload_env, workspace_id="ws-calm")
    urgent_work = await _new_work(payload_env, workspace_id="ws-urgent")
    await _new_work(payload_env, workspace_id="ws-urgent")
    # ws-calm holds the lower virtual finish (1 vs 2).
    await _seed_served(factory, resource_class="default", **{"ws-calm": 1, "ws-urgent": 2})

    # Control: no deadline -> the calm group wins.
    claimed = await scheduler.claim_fair(factory, owner_id="d")
    assert claimed.group_id == "ws-calm"
    await _serve(factory, claimed)

    # A passed deadline on the urgent group's oldest item quadruples
    # its boost: 2 / 4 = 0.5 < 1.
    await scheduler.register_work(
        factory,
        work_id=urgent_work,
        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    claimed = await scheduler.claim_fair(factory, owner_id="d")
    assert claimed.group_id == "ws-urgent", (
        "a passed deadline must promote its item over lower virtual finish"
    )
    await _serve(factory, claimed)


# ---------------------------------------------------------------------------
# resource classes and bounded fan-out (matrix B/C)
# ---------------------------------------------------------------------------


async def test_resource_classes_separate_capacity(payload_env) -> None:
    factory = payload_env[0]
    cpu_ids = [await _new_work(payload_env, workspace_id="ws-cpu") for _ in range(4)]
    marker_ids = [
        await _new_work(payload_env, workspace_id="ws-marker") for _ in range(4)
    ]
    for wid in cpu_ids:
        await scheduler.register_work(factory, work_id=wid, resource_class="cpu")
    for wid in marker_ids:
        await scheduler.register_work(factory, work_id=wid, resource_class="marker")

    served: list[str] = []
    while True:
        claimed = await scheduler.claim_fair(factory, owner_id="cpu-worker", resource_class="cpu")
        if claimed is None:
            break
        await _serve(factory, claimed)
        served.append(claimed.workspace_id)
    assert served == ["ws-cpu"] * 4  # never touched the marker class

    states = await _outbox_states(factory)
    assert {states[wid] for wid in marker_ids} == {"pending"}

    # And the marker class drains independently.
    while True:
        claimed = await scheduler.claim_fair(
            factory, owner_id="marker-worker", resource_class="marker"
        )
        if claimed is None:
            break
        await _serve(factory, claimed)
    assert {states2 for states2 in (await _outbox_states(factory)).values()} == {
        OUTBOX_STATE_DONE
    }


async def test_in_flight_window_bounds_fan_out_and_applies_backpressure(
    payload_env,
) -> None:
    """A group at its fan-out window is skipped: further outstanding
    work is refused (backpressure) while an unrelated group still gets
    service — the bounded-hierarchy tracer bullet."""
    factory = payload_env[0]
    wide_ids = [await _new_work(payload_env, workspace_id="ws-wide") for _ in range(6)]
    await _new_work(payload_env, workspace_id="ws-other")
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-wide",
        policy=scheduler.GroupPolicy(max_in_flight=2),
    )
    # ws-wide holds the lower virtual finish so the first two dispatch
    # decisions target it deterministically.
    await _seed_served(factory, resource_class="default", **{"ws-other": 5})

    first = await scheduler.claim_fair(factory, owner_id="worker-1")
    second = await scheduler.claim_fair(factory, owner_id="worker-2")
    assert first.group_id == second.group_id == "ws-wide"

    # Window full: the dispatcher gets the unrelated group's work, not
    # a third outstanding item of ws-wide.
    third = await scheduler.claim_fair(factory, owner_id="worker-3")
    assert third is not None and third.group_id == "ws-other"

    # Still full and nothing else eligible.
    fourth = await scheduler.claim_fair(factory, owner_id="worker-4")
    assert fourth is None

    # Completing one wide item releases one window slot.
    await _serve(factory, first)
    fifth = await scheduler.claim_fair(factory, owner_id="worker-5")
    assert fifth is not None and fifth.group_id == "ws-wide"

    # The remaining backlog drains only as slots free.
    for claimed in (second, third, fifth):
        await _serve(factory, claimed)
    order = await _drain(factory)
    assert order.count("ws-wide") == 3  # 6 total, 3 served above
    assert len(wide_ids) == 6  # readability anchor: full backlog existed


async def test_expired_leases_do_not_hold_the_window(payload_env) -> None:
    """A wedged owner's lapsed lease must not keep the group's window
    occupied forever — expired leases free scheduling slots (takeover
    remains the PR66 eligibility path)."""
    factory = payload_env[0]
    for _ in range(4):
        await _new_work(payload_env, workspace_id="ws-wedge")
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-wedge",
        policy=scheduler.GroupPolicy(max_in_flight=2),
    )

    # Long enough that the window-full check below cannot race the
    # expiry; short enough that the takeover wait stays test-sized.
    lease = 0.8
    first = await scheduler.claim_fair(factory, owner_id="w1", lease_seconds=lease)
    second = await scheduler.claim_fair(factory, owner_id="w2", lease_seconds=lease)
    assert first is not None and second is not None
    assert await scheduler.claim_fair(factory, owner_id="w3") is None

    await asyncio.sleep(lease + 0.1)
    # Both leases lapsed: takeover is eligible and the window no longer
    # blocks recovery. The lapsed deliveries are still in_flight rows —
    # crash-style reset makes them claimable again, and the fence
    # acquire performs the takeover (token 2, never a revival).
    from app.kernel.outbox import reset_in_flight

    assert await reset_in_flight(factory) == 2
    third = await scheduler.claim_fair(factory, owner_id="w3")
    assert third is not None and third.group_id == "ws-wedge"
    assert third.lease.fencing_token == 2  # PR66 takeover, not revival


# ---------------------------------------------------------------------------
# authority underneath (matrix A regression at the new seam)
# ---------------------------------------------------------------------------


async def test_claim_fair_appends_claimed_event_and_guards_redelivery(
    payload_env,
) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env, workspace_id="ws-ev")

    claimed = await scheduler.claim_fair(
        factory, owner_id="worker-a", lease_seconds=0.5
    )
    assert claimed.work_id == work_id
    events = await replay(factory, workspace_id="ws-ev")
    assert [e.event_type for e in events] == ["work.claimed"]
    assert events[0].payload["work_id"] == work_id
    assert events[0].payload["owner_id"] == "worker-a"

    # Redelivery while the fence is valid: the scheduler refuses to
    # hand the same work to another owner (PR66 semantics at the new
    # seam) and returns the delivery row to pending.
    from app.kernel.outbox import reset_in_flight

    await reset_in_flight(factory)
    assert await scheduler.claim_fair(factory, owner_id="worker-b") is None
    states = await _outbox_states(factory)
    assert states[work_id] == "pending"

    # After the fence lapses, takeover proceeds and the old owner's
    # late acceptance is rejected by PR66.
    await asyncio.sleep(0.6)
    takeover = await scheduler.claim_fair(factory, owner_id="worker-b")
    assert takeover is not None and takeover.lease.fencing_token == 2
    with pytest.raises(StaleFenceError):
        await fencing.accept(
            factory, work_id=work_id, fencing_token=1, result={"late": True}
        )


async def test_accept_work_records_accepted_event_idempotently(payload_env) -> None:
    factory = payload_env[0]
    work_id = await _new_work(payload_env, workspace_id="ws-acc")
    claimed = await scheduler.claim_fair(factory, owner_id="worker-a")

    outcome, appended = await scheduler.accept_work(
        factory,
        work_id=work_id,
        fencing_token=claimed.lease.fencing_token,
        result={"value": 42},
    )
    assert not outcome.already_accepted and appended

    # Same-result redelivery converges through PR66 and does not
    # duplicate the semantic event.
    again, appended_again = await scheduler.accept_work(
        factory,
        work_id=work_id,
        fencing_token=claimed.lease.fencing_token,
        result={"value": 42},
    )
    assert again.already_accepted and not appended_again

    events = await replay(factory, workspace_id="ws-acc")
    accepted = [e for e in events if e.event_type == "work.accepted"]
    assert len(accepted) == 1
    assert accepted[0].payload["result_hash"] == outcome.publication.result_hash


# ---------------------------------------------------------------------------
# hard fan-out cap under concurrent dispatchers (PR67 hardening)
# ---------------------------------------------------------------------------


async def _live_leases(factory, *, group_id: str) -> int:
    """Committed live (leased, unexpired) leases in one group — the
    capacity truth the cap must bound."""
    from sqlalchemy import func, select

    from app.kernel.models import KernelSchedulingEntry, KernelWorkLease

    async with factory() as session:
        value = (
            await session.execute(
                select(func.count())
                .select_from(KernelWorkLease)
                .join(
                    KernelSchedulingEntry,
                    KernelWorkLease.work_id == KernelSchedulingEntry.work_id,
                )
                .where(
                    KernelSchedulingEntry.resource_class == "default",
                    KernelSchedulingEntry.group_id == group_id,
                    KernelWorkLease.state == "leased",
                    KernelWorkLease.lease_expires_at > datetime.now(timezone.utc),
                )
            )
        ).scalar_one()
    return int(value)


async def test_cap_one_never_oversubscribes_under_concurrent_dispatchers(
    payload_env,
) -> None:
    """N simultaneous dispatchers against a cap-1 group: exactly one
    committed live lease may exist — the check-and-claim window is
    closed by the write-serialized claim transaction."""
    factory = payload_env[0]
    for _ in range(10):
        await _new_work(payload_env, workspace_id="ws-cap1")
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-cap1",
        policy=scheduler.GroupPolicy(max_in_flight=1),
    )

    results = await asyncio.gather(
        *(scheduler.claim_fair(factory, owner_id=f"racer-{i}") for i in range(6))
    )
    claims = [r for r in results if r is not None]
    assert len(claims) == 1
    assert await _live_leases(factory, group_id="ws-cap1") == 1

    # Losers left no partial state behind: their candidates stay
    # pending (nothing in_flight without a lease, no served-count
    # inflation beyond the single winner).
    states = await _outbox_states(factory)
    assert list(states.values()).count("in_flight") == 1
    view = await scheduler.get_group_policy(
        factory, resource_class="default", group_id="ws-cap1"
    )
    assert view is not None and view.served_count == 1

    # Serving the winner frees the only slot: capacity is reclaimable.
    await _serve(factory, claims[0])
    assert await _live_leases(factory, group_id="ws-cap1") == 0
    nxt = await scheduler.claim_fair(factory, owner_id="racer-relief")
    assert nxt is not None and nxt.group_id == "ws-cap1"


async def test_cap_k_bounds_concurrent_dispatchers(payload_env) -> None:
    """Cap K with more concurrent dispatchers than slots: committed
    live leases never exceed K, and an unrelated group still claims."""
    factory = payload_env[0]
    for _ in range(12):
        await _new_work(payload_env, workspace_id="ws-capk")
    await _new_work(payload_env, workspace_id="ws-free")
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-capk",
        policy=scheduler.GroupPolicy(max_in_flight=3),
    )
    # Deterministic preference: ws-capk holds the lower virtual finish
    # while any of its backlog remains, so racers and the drain loop
    # always target it; ws-free is reserved for the saturated check.
    await _seed_served(factory, resource_class="default", **{"ws-free": 5})

    results = await asyncio.gather(
        *(scheduler.claim_fair(factory, owner_id=f"racer-{i}") for i in range(8))
    )
    capped = [r for r in results if r is not None and r.group_id == "ws-capk"]
    # Concurrent racers may converge on the same oldest candidate (one
    # dispatch per call); losers retry. Drain to saturation: the
    # committed total converges to exactly the cap, never past it.
    while len(capped) < 3:
        nxt = await scheduler.claim_fair(factory, owner_id="drain")
        if nxt is None:
            break
        assert nxt.group_id == "ws-capk"
        capped.append(nxt)
    assert len(capped) == 3
    assert await _live_leases(factory, group_id="ws-capk") == 3

    # Unrelated group progress under saturation: a racer that found
    # ws-capk already full claimed ws-free instead, or it is claimable
    # now — either way the capped group refuses further work.
    unrelated = [r for r in results if r is not None and r.group_id != "ws-capk"]
    if not unrelated:
        further = await scheduler.claim_fair(factory, owner_id="outsider")
        assert further is not None and further.group_id == "ws-free"

    # Completing one capped item reopens exactly one slot.
    await _serve(factory, capped[0])
    assert await _live_leases(factory, group_id="ws-capk") == 2
    refill = await scheduler.claim_fair(factory, owner_id="refill")
    assert refill is not None and refill.group_id == "ws-capk"
    assert await _live_leases(factory, group_id="ws-capk") == 3


async def test_lease_expiry_frees_capacity_under_concurrency(
    payload_env, monkeypatch
) -> None:
    """Wedged owners' lapsed leases must not consume capacity: after
    expiry the group admits fresh claims (takeover path)."""
    factory = payload_env[0]
    for _ in range(6):
        await _new_work(payload_env, workspace_id="ws-exp")
    await scheduler.set_group_policy(
        factory,
        resource_class="default",
        group_id="ws-exp",
        policy=scheduler.GroupPolicy(max_in_flight=2),
    )

    clock = [datetime.now(timezone.utc)]
    from app.kernel import fencing

    monkeypatch.setattr(scheduler, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(fencing, "_utcnow", lambda: clock[0])
    lease_seconds = 300.0
    holders = [
        await scheduler.claim_fair(factory, owner_id=f"w{i}", lease_seconds=lease_seconds)
        for i in range(2)
    ]
    assert all(h is not None for h in holders)
    assert await scheduler.claim_fair(factory, owner_id="w2") is None

    # Advance scheduler time beyond real lease deadlines. No direct row edits:
    # claim eligibility and capacity accounting must both honor clock expiry.
    clock[0] += timedelta(seconds=lease_seconds + 1)

    from app.kernel.outbox import reset_in_flight

    await reset_in_flight(factory)
    results = await asyncio.gather(
        *(scheduler.claim_fair(factory, owner_id=f"t{i}") for i in range(4))
    )
    takeovers = [r for r in results if r is not None]
    while len(takeovers) < 2:
        nxt = await scheduler.claim_fair(factory, owner_id="t-late")
        if nxt is None:
            break
        takeovers.append(nxt)
    assert len(takeovers) == 2  # exactly the freed capacity, not more
    assert await _live_leases(factory, group_id="ws-exp") == 2
    assert all(r.lease.fencing_token == 2 for r in takeovers)  # takeover, not revival
