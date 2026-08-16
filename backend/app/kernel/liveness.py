"""Challenge-backed lease liveness (V3.2 PR67A).

PR66 made lease expiry *takeover eligibility*, deliberately refusing to
let a wall-clock timestamp prove anything. This module supplies the
other half: renewal that is **evidence-bearing**, not timer-bearing.

The contract: a healthy worker renews by presenting, in one
transaction —

* its identity (``owner_id``) and the **current fencing token** (PR66
  authority: a superseded worker fails here forever);
* the **current challenge nonce** — issued to the claimer inside the
  claim transaction and rotated on every successful renewal, handed
  only to the responder whose evidence just passed;
* a **progress counter strictly advancing** the durable high-water
  mark — a replayed or frozen snapshot is not a responsive control
  loop;
* the **active request/stage identity** the control loop is serving,
  coherent with the request bound to the lease (a stage switch is
  allowed only when the previously bound request is no longer active);
* optionally the **topology generation** the fence was issued under.

Failure semantics, each a distinct observable rejection:

* superseded owner (takeover happened) → :class:`StaleFenceError`;
* cached/replayed nonce (detached timer) → :class:`InvalidChallengeError`;
* non-advancing progress → :class:`ProgressNotAdvancingError`;
* unknown/expired/incoherent active request → :class:`RequestNotActiveError`;
* topology mismatch → :class:`TopologyMismatchError`;
* durably observed cancellation → :class:`WorkCancelledError`.

A wedged worker simply stops renewing; its lease lapses and PR66
takeover eligibility takes over. A long external/inference wait stays
valid only while its control loop keeps answering with fresh evidence
**and** the referenced request is still known active
(``request_expires_at``). Renewal never advances the fencing token —
authority still moves only through PR66 ownership transitions.

The challenge nonce is deliberately **not** part of any read view: it
exists only in the claim/renew responses handed to the legitimate
owner, so a component that merely reads the database cannot forge
renewal evidence.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    InvalidChallengeError,
    InvalidEventError,
    KernelBusyError,
    ProgressNotAdvancingError,
    RequestNotActiveError,
    StaleFenceError,
    TopologyMismatchError,
    UnknownWorkLeaseError,
    WorkCancelledError,
)
from app.kernel.events import DURABILITY_DURABLE, _append_in_session
from app.kernel.fencing import (
    DEFAULT_WORK_LEASE_SECONDS,
    LEASE_STATE_LEASED,
    WorkLease,
    _iso,
    _lease_view,
    validate_owner_id,
)

__all__ = [
    "LivenessView",
    "RenewOutcome",
    "EVENT_RENEWED",
    "EVENT_CANCEL_REQUESTED",
    "get_liveness",
    "new_challenge_nonce",
    "renew_lease",
    "report_cancellation",
]

EVENT_RENEWED = "lease.renewed"
EVENT_CANCEL_REQUESTED = "work.cancel_requested"

DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
_MAX_RETRY_DELAY = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime) -> datetime:
    """Normalize to naive-UTC for comparison against SQLite values."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _is_busy(exc: OperationalError) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
        )
    )


def _retry_delay(base: float, attempt: int) -> float:
    return min(base * (2**attempt), _MAX_RETRY_DELAY)


async def _run_with_busy_retry(operation, *, busy_retry_attempts, busy_retry_base_delay):
    attempts = busy_retry_attempts or DEFAULT_BUSY_RETRY_ATTEMPTS
    base_delay = busy_retry_base_delay or DEFAULT_BUSY_RETRY_BASE_DELAY
    last_error: OperationalError | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except OperationalError as exc:
            if not _is_busy(exc):
                raise
            last_error = exc
            await asyncio.sleep(_retry_delay(base_delay, attempt))
    raise KernelBusyError(
        f"liveness operation still busy after {attempts} attempts: {last_error}"
    )


def new_challenge_nonce() -> str:
    """Fresh unguessable challenge material (rotated every renewal)."""
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivenessView:
    """Durable liveness evidence snapshot — deliberately excludes the
    challenge nonce, which is only ever handed to the legitimate owner
    inside claim/renew responses."""

    work_id: int
    active_request_id: str
    progress_high_water: int
    topology_generation: int | None
    request_expires_at: str | None
    cancelled_at: str | None
    renew_count: int
    last_activity_at: str | None


@dataclass(frozen=True)
class RenewOutcome:
    """Result of one successful evidence-bearing renewal."""

    lease: WorkLease
    next_challenge_nonce: str
    renew_count: int
    progress_high_water: int


def _liveness_view(row) -> LivenessView:
    return LivenessView(
        work_id=row.work_id,
        active_request_id=row.active_request_id,
        progress_high_water=row.progress_high_water,
        topology_generation=row.topology_generation,
        request_expires_at=_iso(row.request_expires_at),
        cancelled_at=_iso(row.cancelled_at),
        renew_count=row.renew_count,
        last_activity_at=_iso(row.last_activity_at),
    )


# ---------------------------------------------------------------------------
# seeding (called inside the claim bookkeeping transaction)
# ---------------------------------------------------------------------------


async def seed_challenge_in_session(
    session,
    *,
    work_id: int,
    topology_generation: int | None = None,
) -> str:
    """Reset challenge evidence for a fresh fenced claim and return the
    new nonce (handed only to the claimer).

    Takeover must invalidate the previous owner's evidence: the upsert
    replaces the nonce, zeroes the progress high-water mark and renewal
    count, clears cancellation and the request binding, and rebinds the
    topology generation to the claiming worker's declaration."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import KernelLiveness

    nonce = new_challenge_nonce()
    now = _utcnow()
    stmt = (
        sqlite_insert(KernelLiveness)
        .values(
            work_id=work_id,
            challenge_nonce=nonce,
            progress_high_water=0,
            active_request_id="",
            topology_generation=topology_generation,
            request_expires_at=None,
            cancelled_at=None,
            renew_count=0,
            last_activity_at=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["work_id"],
            set_={
                "challenge_nonce": nonce,
                "progress_high_water": 0,
                "active_request_id": "",
                "topology_generation": topology_generation,
                "request_expires_at": None,
                "cancelled_at": None,
                "renew_count": 0,
                "last_activity_at": now,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)
    return nonce


# ---------------------------------------------------------------------------
# evidence-bearing renewal
# ---------------------------------------------------------------------------


async def renew_lease(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    owner_id: str,
    fencing_token: int,
    challenge_nonce: str,
    progress: int,
    active_request_id: str,
    topology_generation: int | None = None,
    request_expires_at: datetime | None = None,
    extend_seconds: float = DEFAULT_WORK_LEASE_SECONDS,
    emit_event: bool = False,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> RenewOutcome:
    """Extend the lease only behind coherent, advancing control-loop
    evidence (see module docstring). The fencing token never moves
    here; renewal is same-owner by construction.

    ``request_expires_at`` bounds the referenced request: while set and
    in the future, renewal is valid; once passed, the request identity
    is no longer accepted and the control loop must present a new
    active request (or let the lease lapse). ``None`` clears the bound
    (deadline enforcement is opt-in per external wait)."""
    from app.kernel.models import KernelLiveness, KernelWorkLease

    validate_owner_id(owner_id)
    if extend_seconds <= 0:
        from app.kernel.errors import InvalidWorkLeaseError

        raise InvalidWorkLeaseError("extend_seconds must be positive")
    if not isinstance(progress, int) or isinstance(progress, bool):
        raise ProgressNotAdvancingError(
            f"progress must be an integer counter, got {progress!r}"
        )
    if not isinstance(active_request_id, str) or not active_request_id:
        raise RequestNotActiveError("renewal must name the active request/stage")

    async def _operation() -> RenewOutcome:
        async with session_factory() as session:
            async with session.begin():
                lease = await session.get(KernelWorkLease, work_id)
                if lease is None:
                    raise UnknownWorkLeaseError(
                        f"work {work_id!r} was never acquired through the "
                        "fencing boundary"
                    )
                if (
                    lease.fencing_token != fencing_token
                    or lease.owner_id != owner_id
                    or lease.state != LEASE_STATE_LEASED
                ):
                    raise StaleFenceError(
                        submitted_token=fencing_token,
                        current_token=lease.fencing_token,
                    )

                liveness = await session.get(KernelLiveness, work_id)
                if liveness is None:
                    raise InvalidChallengeError(
                        "no challenge evidence exists for this lease"
                    )
                if liveness.challenge_nonce != challenge_nonce:
                    raise InvalidChallengeError(
                        "challenge nonce is not current (rotated, replayed, "
                        "or never issued to this renewal path)"
                    )
                if liveness.cancelled_at is not None:
                    raise WorkCancelledError(
                        "cancellation was durably observed; liveness evidence "
                        "can no longer extend this lease"
                    )
                if progress <= liveness.progress_high_water:
                    raise ProgressNotAdvancingError(
                        f"progress {progress} does not advance the durable "
                        f"high-water mark {liveness.progress_high_water}"
                    )
                if liveness.topology_generation is not None and (
                    topology_generation != liveness.topology_generation
                ):
                    raise TopologyMismatchError(
                        submitted_generation=topology_generation,
                        current_generation=liveness.topology_generation,
                    )

                now = _utcnow()
                bound_expiry = (
                    _naive(liveness.request_expires_at)
                    if liveness.request_expires_at is not None
                    else None
                )
                if liveness.active_request_id and bound_expiry is not None:
                    # A bound request exists: renewal must serve that same
                    # request while it is still active. Serving a different
                    # id is a transition and is only honest once the bound
                    # request has lapsed.
                    if active_request_id == liveness.active_request_id:
                        if bound_expiry <= _naive(now):
                            raise RequestNotActiveError(
                                f"bound request {liveness.active_request_id!r} "
                                "is no longer active (expired)"
                            )
                    elif bound_expiry > _naive(now):
                        raise RequestNotActiveError(
                            f"renewal names request {active_request_id!r} but "
                            f"the lease is bound to active request "
                            f"{liveness.active_request_id!r}"
                        )

                next_nonce = new_challenge_nonce()
                extended = await session.execute(
                    update(KernelWorkLease)
                    .where(
                        KernelWorkLease.work_id == work_id,
                        KernelWorkLease.fencing_token == fencing_token,
                        KernelWorkLease.owner_id == owner_id,
                        KernelWorkLease.state == LEASE_STATE_LEASED,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=extend_seconds),
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if extended.rowcount != 1:
                    raise StaleFenceError(
                        submitted_token=fencing_token,
                        current_token=fencing_token + 1,
                    )

                await session.execute(
                    update(KernelLiveness)
                    .where(KernelLiveness.work_id == work_id)
                    .values(
                        challenge_nonce=next_nonce,
                        progress_high_water=progress,
                        active_request_id=active_request_id,
                        request_expires_at=request_expires_at,
                        renew_count=liveness.renew_count + 1,
                        last_activity_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )

                if emit_event:
                    await _append_in_session(
                        session,
                        workspace_id=lease.workspace_id,
                        stream="work",
                        event_type=EVENT_RENEWED,
                        payload_json=_canonical_payload(
                            work_id=work_id,
                            owner_id=owner_id,
                            fencing_token=fencing_token,
                            progress=progress,
                            active_request_id=active_request_id,
                        ),
                        durability=DURABILITY_DURABLE,
                    )

                session.expire(lease)
                refreshed = await session.get(KernelWorkLease, work_id)
                return RenewOutcome(
                    lease=_lease_view(refreshed),
                    next_challenge_nonce=next_nonce,
                    renew_count=liveness.renew_count + 1,
                    progress_high_water=progress,
                )

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


def _canonical_payload(**fields: Any) -> str:
    from app.utils.canonical import canonical_json_str, to_json_ready

    try:
        return canonical_json_str(to_json_ready(dict(fields)))
    except Exception as exc:  # payload is plain scalars; defensive
        raise InvalidEventError(f"liveness event payload rejected: {exc}") from exc


# ---------------------------------------------------------------------------
# cancellation observation
# ---------------------------------------------------------------------------


async def report_cancellation(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    owner_id: str,
    fencing_token: int,
    reason: str,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> bool:
    """Durably observe cancellation behind the current fence.

    Once observed, no liveness evidence can extend the lease — a stale
    heartbeat response cannot resurrect cancelled work. The observation
    itself is authority-gated (current owner + token), and the
    ``work.cancel_requested`` semantic event lands in the same
    transaction. Idempotent: a second report changes nothing and writes
    no second event (returns False). Executing the actual cancellation
    remains the runtime's job; this records the observation."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import KernelLiveness, KernelWorkLease

    validate_owner_id(owner_id)

    async def _operation() -> bool:
        async with session_factory() as session:
            async with session.begin():
                lease = await session.get(KernelWorkLease, work_id)
                if lease is None:
                    raise UnknownWorkLeaseError(
                        f"work {work_id!r} was never acquired through the "
                        "fencing boundary"
                    )
                if (
                    lease.fencing_token != fencing_token
                    or lease.owner_id != owner_id
                    or lease.state != LEASE_STATE_LEASED
                ):
                    raise StaleFenceError(
                        submitted_token=fencing_token,
                        current_token=lease.fencing_token,
                    )

                liveness = await session.get(KernelLiveness, work_id)
                now = _utcnow()
                if liveness is None:
                    # Fence exists but evidence seeding was lost to a crash;
                    # record the observation on a fresh evidence row.
                    await session.execute(
                        sqlite_insert(KernelLiveness)
                        .values(
                            work_id=work_id,
                            challenge_nonce=new_challenge_nonce(),
                            progress_high_water=0,
                            active_request_id="",
                            topology_generation=None,
                            request_expires_at=None,
                            cancelled_at=now,
                            renew_count=0,
                            last_activity_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=["work_id"])
                    )
                elif liveness.cancelled_at is not None:
                    return False  # already observed; no duplicate event

                await session.execute(
                    update(KernelLiveness)
                    .where(
                        KernelLiveness.work_id == work_id,
                        KernelLiveness.cancelled_at.is_(None),
                    )
                    .values(cancelled_at=now, updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                await _append_in_session(
                    session,
                    workspace_id=lease.workspace_id,
                    stream="work",
                    event_type=EVENT_CANCEL_REQUESTED,
                    payload_json=_canonical_payload(
                        work_id=work_id,
                        owner_id=owner_id,
                        fencing_token=fencing_token,
                        reason=reason,
                    ),
                    durability=DURABILITY_DURABLE,
                )
                return True

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


# ---------------------------------------------------------------------------
# read view
# ---------------------------------------------------------------------------


async def get_liveness(
    session_factory: async_sessionmaker, work_id: int
) -> LivenessView | None:
    """Durable liveness evidence without the challenge nonce."""
    from app.kernel.models import KernelLiveness

    async with session_factory() as session:
        row = await session.get(KernelLiveness, work_id)
    return _liveness_view(row) if row is not None else None
