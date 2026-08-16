"""Challenge-backed lease liveness contract tests (V3.2 PR67A).

The behavioral contract under test (matrix D):

* current owner + current fence + responsive challenge evidence
  (rotating nonce, strictly advancing progress, coherent active
  request) renews the lease without moving the fence;
* a superseded owner (post-takeover) cannot renew — stale fence;
* topology-generation mismatch is rejected;
* a detached timer holding a cached nonce, or replaying flat
  progress, cannot renew;
* a wedged worker stops renewing and becomes takeover-eligible;
* a long external wait stays valid only while its control loop
  responds and the referenced request is still known active;
* durably observed cancellation defeats any later liveness evidence;
* after takeover, the old owner's late result cannot publish (PR66)
  and the old owner cannot acknowledge.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.kernel import fencing, liveness, scheduler
from app.kernel.commit import KernelCommitBatch
from app.kernel.errors import (
    InvalidChallengeError,
    ProgressNotAdvancingError,
    RequestNotActiveError,
    StaleFenceError,
    TopologyMismatchError,
    WorkCancelledError,
)
from app.kernel.events import replay
from app.kernel.liveness import LivenessView
from app.kernel.outbox import OutboxIntent, list_outbox, reset_in_flight
from app.kernel.records import ClaimAssertionRecord

pytestmark = pytest.mark.asyncio

_seq = iter(range(10_000))


async def _new_work(payload_env, *, workspace_id: str = "ws-live") -> int:
    _factory, _store, service = payload_env
    n = next(_seq)
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(
                ClaimAssertionRecord(
                    claim_key=f"live-{n}", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"n": n}),),
        )
    )
    rows = await list_outbox(payload_env[0])
    return rows[-1].id


async def _claim(payload_env, *, owner_id: str, lease_seconds: float = 5.0, **kwargs):
    await _new_work(payload_env)
    return await scheduler.claim_fair(
        payload_env[0], owner_id=owner_id, lease_seconds=lease_seconds, **kwargs
    )


# ---------------------------------------------------------------------------
# D1: healthy renewal
# ---------------------------------------------------------------------------


async def test_healthy_owner_renews_without_moving_fence(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a", lease_seconds=0.5)
    assert claimed is not None

    outcome = await liveness.renew_lease(
        factory,
        work_id=claimed.work_id,
        owner_id="worker-a",
        fencing_token=claimed.lease.fencing_token,
        challenge_nonce=claimed.challenge_nonce,
        progress=1,
        active_request_id="stage-parse",
    )
    assert outcome.lease.fencing_token == 1  # renewal never moves the fence
    assert outcome.next_challenge_nonce != claimed.challenge_nonce
    assert outcome.renew_count == 1 and outcome.progress_high_water == 1

    # The nonce rotates: the previous one is dead, the new one works.
    with pytest.raises(InvalidChallengeError):
        await liveness.renew_lease(
            factory,
            work_id=claimed.work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=claimed.challenge_nonce,
            progress=2,
            active_request_id="stage-parse",
        )
    second = await liveness.renew_lease(
        factory,
        work_id=claimed.work_id,
        owner_id="worker-a",
        fencing_token=1,
        challenge_nonce=outcome.next_challenge_nonce,
        progress=2,
        active_request_id="stage-parse",
    )
    assert second.renew_count == 2

    view = await liveness.get_liveness(factory, claimed.work_id)
    assert view is not None
    assert view.active_request_id == "stage-parse"
    assert view.progress_high_water == 2
    # The read view deliberately cannot leak renewal material.
    assert "challenge_nonce" not in LivenessView.__dataclass_fields__
    assert "challenge_nonce" not in view.__dict__


# ---------------------------------------------------------------------------
# D2: stale fence after takeover
# ---------------------------------------------------------------------------


async def test_old_fence_cannot_renew_after_takeover(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a", lease_seconds=0.3)
    await asyncio.sleep(0.4)

    # Takeover through the fair claim path (reseeds challenge evidence).
    await reset_in_flight(factory)
    takeover = await scheduler.claim_fair(factory, owner_id="worker-b")
    assert takeover is not None and takeover.lease.fencing_token == 2

    with pytest.raises(StaleFenceError):
        await liveness.renew_lease(
            factory,
            work_id=claimed.work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=claimed.challenge_nonce,
            progress=9,
            active_request_id="stage-parse",
        )


# ---------------------------------------------------------------------------
# D3: topology generation mismatch
# ---------------------------------------------------------------------------


async def test_topology_generation_mismatch_rejected(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a", topology_generation=7)
    assert claimed is not None

    for wrong in (8, None):
        with pytest.raises(TopologyMismatchError):
            await liveness.renew_lease(
                factory,
                work_id=claimed.work_id,
                owner_id="worker-a",
                fencing_token=1,
                challenge_nonce=claimed.challenge_nonce,
                progress=1,
                active_request_id="stage-parse",
                topology_generation=wrong,
            )
    outcome = await liveness.renew_lease(
        factory,
        work_id=claimed.work_id,
        owner_id="worker-a",
        fencing_token=1,
        challenge_nonce=claimed.challenge_nonce,
        progress=1,
        active_request_id="stage-parse",
        topology_generation=7,
    )
    assert outcome.renew_count == 1


# ---------------------------------------------------------------------------
# D4: the detached timer
# ---------------------------------------------------------------------------


async def test_detached_timer_cannot_renew(payload_env) -> None:
    """A renewal path that is not the live control loop fails every
    evidence axis: no nonce, a cached (rotated-away) nonce, or fresh
    nonce with non-advancing progress."""
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a")
    work_id = claimed.work_id

    # Timer that never saw the claim response: no nonce at all.
    with pytest.raises(InvalidChallengeError):
        await liveness.renew_lease(
            factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce="",
            progress=1,
            active_request_id="stage-parse",
        )

    # The control loop renews first, rotating the nonce...
    outcome = await liveness.renew_lease(
        factory,
        work_id=work_id,
        owner_id="worker-a",
        fencing_token=1,
        challenge_nonce=claimed.challenge_nonce,
        progress=5,
        active_request_id="stage-parse",
    )
    # ...so the timer's cached copy of the original nonce is dead.
    with pytest.raises(InvalidChallengeError):
        await liveness.renew_lease(
            factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=claimed.challenge_nonce,
            progress=6,
            active_request_id="stage-parse",
        )
    # Even a timer that somehow observed the fresh nonce cannot show
    # advancing progress — its counter is frozen at a replayed value.
    with pytest.raises(ProgressNotAdvancingError):
        await liveness.renew_lease(
            factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=outcome.next_challenge_nonce,
            progress=5,
            active_request_id="stage-parse",
        )
    with pytest.raises(ProgressNotAdvancingError):
        await liveness.renew_lease(
            factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=outcome.next_challenge_nonce,
            progress=4,
            active_request_id="stage-parse",
        )


# ---------------------------------------------------------------------------
# D5: wedged worker becomes takeover-eligible
# ---------------------------------------------------------------------------


async def test_wedged_worker_lapses_and_is_taken_over(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a", lease_seconds=0.3)
    # The worker wedges: no renewal ever happens.
    await asyncio.sleep(0.4)

    lease = await fencing.get_lease(factory, claimed.work_id)
    assert lease is not None and lease.fencing_token == 1  # authority intact...

    await reset_in_flight(factory)
    takeover = await scheduler.claim_fair(factory, owner_id="worker-b")
    assert takeover is not None and takeover.work_id == claimed.work_id
    assert takeover.lease.fencing_token == 2  # ...until takeover advances it

    # The successor holds fresh evidence; the wedged worker is rejected
    # at the fence first (authority precedes evidence — its cached
    # nonce would have been dead anyway).
    view = await liveness.get_liveness(factory, claimed.work_id)
    assert view is not None and view.renew_count == 0  # reseeded, not revived
    with pytest.raises(StaleFenceError):
        await liveness.renew_lease(
            factory,
            work_id=claimed.work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=claimed.challenge_nonce,
            progress=3,
            active_request_id="stage-parse",
        )


# ---------------------------------------------------------------------------
# D6: long external request liveness
# ---------------------------------------------------------------------------


async def test_external_request_liveness_is_bounded_by_request_activity(
    payload_env,
) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a", lease_seconds=5.0)

    async def renew(nonce, progress, request_id, expires_in):
        return await liveness.renew_lease(
            factory,
            work_id=claimed.work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=nonce,
            progress=progress,
            active_request_id=request_id,
            request_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )

    first = await renew(claimed.challenge_nonce, 1, "infer-42", 0.3)
    second = await renew(first.next_challenge_nonce, 2, "infer-42", 0.3)
    assert second.renew_count == 2

    await asyncio.sleep(0.4)  # the referenced request lapses

    # Same request id: dead — liveness cannot outlive the request.
    with pytest.raises(RequestNotActiveError):
        await renew(second.next_challenge_nonce, 3, "infer-42", 0.3)
    # A different stage/request id is an honest transition and renews.
    third = await renew(second.next_challenge_nonce, 3, "infer-43", 60.0)
    assert third.renew_count == 3

    # While the request is active, serving a *different* id is not a
    # transition and is rejected.
    with pytest.raises(RequestNotActiveError):
        await renew(third.next_challenge_nonce, 4, "infer-99", 60.0)


# ---------------------------------------------------------------------------
# D7: cancellation observation
# ---------------------------------------------------------------------------


async def test_cancellation_observation_defeats_stale_liveness(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a")
    work_id = claimed.work_id

    outcome = await liveness.renew_lease(
        factory,
        work_id=work_id,
        owner_id="worker-a",
        fencing_token=1,
        challenge_nonce=claimed.challenge_nonce,
        progress=1,
        active_request_id="stage-render",
    )

    # Only the current fence may record the observation.
    with pytest.raises(StaleFenceError):
        await liveness.report_cancellation(
            factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=99,
            reason="user",
        )
    assert await liveness.report_cancellation(
        factory,
        work_id=work_id,
        owner_id="worker-a",
        fencing_token=1,
        reason="user requested",
    )
    # Idempotent: no duplicate observation or event.
    assert not await liveness.report_cancellation(
        factory,
        work_id=work_id,
        owner_id="worker-a",
        fencing_token=1,
        reason="user requested",
    )

    # Even perfectly fresh, advancing evidence is now dead.
    with pytest.raises(WorkCancelledError):
        await liveness.renew_lease(
            factory,
            work_id=work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=outcome.next_challenge_nonce,
            progress=2,
            active_request_id="stage-render",
        )

    events = await replay(factory, workspace_id="ws-live")
    cancels = [e for e in events if e.event_type == "work.cancel_requested"]
    assert len(cancels) == 1 and cancels[0].payload["reason"] == "user requested"


# ---------------------------------------------------------------------------
# D8: late output from a superseded owner
# ---------------------------------------------------------------------------


async def test_superseded_owner_cannot_accept_or_acknowledge(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a", lease_seconds=0.3)
    await asyncio.sleep(0.4)
    await reset_in_flight(factory)
    takeover = await scheduler.claim_fair(factory, owner_id="worker-b")
    assert takeover is not None and takeover.lease.fencing_token == 2

    with pytest.raises(StaleFenceError):
        await fencing.accept(
            factory, work_id=claimed.work_id, fencing_token=1, result={"late": True}
        )
    assert not await fencing.complete_work(
        factory, work_id=claimed.work_id, fencing_token=1
    )
    outcome, _ = await scheduler.accept_work(
        factory,
        work_id=takeover.work_id,
        fencing_token=2,
        result={"fresh": True},
    )
    assert not outcome.already_accepted
    assert await fencing.complete_work(factory, work_id=takeover.work_id, fencing_token=2)


# ---------------------------------------------------------------------------
# renewal events are opt-in (write-amplification control)
# ---------------------------------------------------------------------------


async def test_renewal_events_are_opt_in(payload_env) -> None:
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a")
    nonce = claimed.challenge_nonce

    first = await liveness.renew_lease(
        factory,
        work_id=claimed.work_id,
        owner_id="worker-a",
        fencing_token=1,
        challenge_nonce=nonce,
        progress=1,
        active_request_id="stage-a",
    )
    events = await replay(factory, workspace_id="ws-live")
    assert [e.event_type for e in events] == ["work.claimed"]  # default: no renew event

    second = await liveness.renew_lease(
        factory,
        work_id=claimed.work_id,
        owner_id="worker-a",
        fencing_token=1,
        challenge_nonce=first.next_challenge_nonce,
        progress=2,
        active_request_id="stage-a",
        emit_event=True,
    )
    assert second.renew_count == 2
    events = await replay(factory, workspace_id="ws-live")
    assert [e.event_type for e in events] == ["work.claimed", "lease.renewed"]


async def test_renewal_nonce_chain_is_single_threaded_evidence(payload_env) -> None:
    """Only the responder of the previous renewal can renew again — the
    nonce chain is exclusive by construction (explicit chaining)."""
    factory = payload_env[0]
    claimed = await _claim(payload_env, owner_id="worker-a")
    nonce = claimed.challenge_nonce
    for progress in (1, 2, 3):
        outcome = await liveness.renew_lease(
            factory,
            work_id=claimed.work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=nonce,
            progress=progress,
            active_request_id="stage-a",
        )
        nonce = outcome.next_challenge_nonce
    view = await liveness.get_liveness(factory, claimed.work_id)
    assert view.progress_high_water == 3 and view.renew_count == 3
