"""Fair bounded dispatch over the fenced work authority (V3.2 PR67A).

PR66 left ``fencing.claim_next`` deliberately policy-light: oldest-
first, no fairness, no heartbeat. This module replaces the *policy*
while keeping the authority underneath untouched: every claim still
goes through the PR66 outbox claim + fence acquire, acceptance stays
exactly-once in ``kernel_publications``, and acknowledgement still
happens only behind accepted truth.

Policy (weighted fair queuing with aging):

* work is partitioned into **resource classes** (capacity separation:
  e.g. ``cpu`` vs ``marker`` never contend for the same dispatch) and,
  inside a class, into **scheduling groups** — the fair-share entity,
  at least as fine as one workspace or document/run flow;
* a group's priority is its **virtual finish** ``served / weight``
  (exact rational arithmetic — no float drift across restarts); each
  successful claim increments ``served``, so equal weights interleave
  strictly and a 2:1 weight ratio converges to a 2:1 long-run service
  share without letting anyone starve;
* **age boost**: a group whose oldest eligible item has waited beyond
  ``age_boost_after_seconds`` has its virtual finish divided by
  ``age_boost_factor`` — old eligible work cannot be perpetually
  displaced; **deadline pressure** doubles (near) and quadruples
  (passed) the boost;
* **bounded fan-out**: a group with ``max_in_flight`` currently-leased
  items is skipped — a parent flow with a huge child backlog cannot
  monopolize the class, and backpressure reduces further fan-out
  instead of queueing unbounded work. A coordinator waiting on
  children holds no lease and consumes no slot. The window is a
  backpressure bound, not a hard invariant: concurrent dispatchers may
  overshoot it transiently by their own count;
* **bounded look-ahead**: each pass scores at most ``lookahead``
  oldest pending items *per group* (a window, never a global id-ordered
  scan that would keep late-arriving groups invisible behind an older
  backlog) and dispatches exactly one item per call.

``served_count`` is deliberately **non-authoritative bookkeeping** —
ownership, acceptance, and acknowledgement are decided solely by the
PR66 fence and publication tables. Losing or rebuilding the counter
changes interleaving, never truth.

Crash windows are explicit and repairable: a crash between the outbox
claim and the fence acquire leaves an orphan delivery (no lease) that
:func:`reconcile_dispatch` returns to pending; a crash between the
acquire and the bookkeeping transaction leaves the ``work.claimed``
semantic event missing while the claim itself stands — repaired by
deterministic derivation from the lease authority, never invention.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    InvalidEventError,
    InvalidGroupPolicyError,
    KernelBusyError,
    UnknownWorkError,
)
from app.kernel.events import DURABILITY_DURABLE, _append_in_session
from app.kernel.fencing import (
    DEFAULT_WORK_LEASE_SECONDS,
    LEASE_STATE_LEASED,
    WorkLease,
    _iso,
    acquire,
    validate_owner_id,
)
from app.kernel.liveness import seed_challenge_in_session
from app.kernel.outbox import (
    OUTBOX_STATE_IN_FLIGHT,
    OUTBOX_STATE_PENDING,
    claim as _outbox_claim,
    release as _outbox_release,
)

__all__ = [
    "ClaimFairResult",
    "DEFAULT_AGE_BOOST_AFTER_SECONDS",
    "DEFAULT_AGE_BOOST_FACTOR",
    "DEFAULT_LOOKAHEAD",
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_RESOURCE_CLASS",
    "DEFAULT_WEIGHT",
    "EVENT_CLAIMED",
    "GroupPolicy",
    "GroupView",
    "RESOURCE_CLASS_PATTERN",
    "accept_work",
    "claim_fair",
    "get_group_policy",
    "group_stats",
    "reconcile_dispatch",
    "register_work",
    "set_group_policy",
    "validate_group_id",
    "validate_resource_class",
]

EVENT_CLAIMED = "work.claimed"

DEFAULT_RESOURCE_CLASS = "default"
RESOURCE_CLASS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
#: Group ids may carry one more path segment than a class (e.g.
#: ``"<workspace>:<document>"``), hence the wider bound.
_FULL_GROUP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,191}$")

DEFAULT_WEIGHT = 1.0
DEFAULT_MAX_IN_FLIGHT = 4
DEFAULT_AGE_BOOST_AFTER_SECONDS = 30.0
DEFAULT_AGE_BOOST_FACTOR = 4.0
DEFAULT_LOOKAHEAD = 16

DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
_MAX_RETRY_DELAY = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
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
        f"scheduler operation still busy after {attempts} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# validation, policy, and views
# ---------------------------------------------------------------------------


def validate_resource_class(resource_class: str) -> str:
    if not isinstance(resource_class, str) or not RESOURCE_CLASS_PATTERN.match(
        resource_class
    ):
        raise InvalidGroupPolicyError(
            f"invalid resource class: {resource_class!r} must match "
            f"{RESOURCE_CLASS_PATTERN.pattern}"
        )
    return resource_class


def validate_group_id(group_id: str) -> str:
    if not isinstance(group_id, str) or not _FULL_GROUP_ID_PATTERN.match(group_id):
        raise InvalidGroupPolicyError(
            f"invalid scheduling group id: {group_id!r} must match "
            f"{_FULL_GROUP_ID_PATTERN.pattern}"
        )
    return group_id


@dataclass(frozen=True)
class GroupPolicy:
    """Fair-share policy for one (resource class, group)."""

    weight: float = DEFAULT_WEIGHT
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    age_boost_after_seconds: float = DEFAULT_AGE_BOOST_AFTER_SECONDS
    age_boost_factor: float = DEFAULT_AGE_BOOST_FACTOR


@dataclass(frozen=True)
class GroupView:
    """Group policy plus current (non-authoritative) bookkeeping."""

    resource_class: str
    group_id: str
    policy: GroupPolicy
    served_count: int
    updated_at: str | None


@dataclass(frozen=True)
class ClaimFairResult:
    """One work item bound to a fresh fence, plus the challenge
    evidence material handed only to the claimer."""

    work_id: int
    workspace_id: str
    work_kind: str
    payload: dict
    lease: WorkLease
    challenge_nonce: str
    resource_class: str
    group_id: str


_GROUP_POLICY_COLUMNS = (
    "weight",
    "max_in_flight",
    "age_boost_after_seconds",
    "age_boost_factor",
)


def _validated_policy(policy: GroupPolicy) -> GroupPolicy:
    if policy.weight <= 0:
        raise InvalidGroupPolicyError(f"weight must be positive: {policy.weight!r}")
    if policy.max_in_flight < 1:
        raise InvalidGroupPolicyError(
            f"max_in_flight must be >= 1: {policy.max_in_flight!r}"
        )
    if policy.age_boost_after_seconds <= 0:
        raise InvalidGroupPolicyError(
            f"age_boost_after_seconds must be positive: "
            f"{policy.age_boost_after_seconds!r}"
        )
    if policy.age_boost_factor < 1:
        raise InvalidGroupPolicyError(
            f"age_boost_factor must be >= 1: {policy.age_boost_factor!r}"
        )
    return policy


def _group_view(row) -> GroupView:
    return GroupView(
        resource_class=row.resource_class,
        group_id=row.group_id,
        policy=GroupPolicy(
            weight=row.weight,
            max_in_flight=row.max_in_flight,
            age_boost_after_seconds=row.age_boost_after_seconds,
            age_boost_factor=row.age_boost_factor,
        ),
        served_count=row.served_count,
        updated_at=_iso(row.updated_at),
    )


# ---------------------------------------------------------------------------
# policy and registration surface
# ---------------------------------------------------------------------------


async def set_group_policy(
    session_factory: async_sessionmaker,
    *,
    resource_class: str,
    group_id: str,
    policy: GroupPolicy,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> GroupView:
    """Create or update the fair-share policy for one group. The
    served-count bookkeeping is preserved across policy changes —
    fairness accounting continuity, never reset by configuration."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import KernelSchedulingGroup

    validate_resource_class(resource_class)
    validate_group_id(group_id)
    _validated_policy(policy)

    async def _operation() -> GroupView:
        async with session_factory() as session:
            async with session.begin():
                now = _utcnow()
                stmt = (
                    sqlite_insert(KernelSchedulingGroup)
                    .values(
                        resource_class=resource_class,
                        group_id=group_id,
                        weight=policy.weight,
                        max_in_flight=policy.max_in_flight,
                        age_boost_after_seconds=policy.age_boost_after_seconds,
                        age_boost_factor=policy.age_boost_factor,
                        served_count=0,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["resource_class", "group_id"],
                        set_={
                            **{name: getattr(policy, name) for name in _GROUP_POLICY_COLUMNS},
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(stmt)
                row = await session.get(
                    KernelSchedulingGroup,
                    {"resource_class": resource_class, "group_id": group_id},
                )
                return _group_view(row)

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


async def get_group_policy(
    session_factory: async_sessionmaker,
    *,
    resource_class: str,
    group_id: str,
) -> GroupView | None:
    from app.kernel.models import KernelSchedulingGroup

    async with session_factory() as session:
        row = await session.get(
            KernelSchedulingGroup,
            {"resource_class": resource_class, "group_id": group_id},
        )
    return _group_view(row) if row is not None else None


async def group_stats(
    session_factory: async_sessionmaker,
    *,
    resource_class: str,
) -> list[GroupView]:
    """Current policy + bookkeeping for every group in a class — the
    fairness measurement surface."""
    from app.kernel.models import KernelSchedulingGroup

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(KernelSchedulingGroup)
                .where(KernelSchedulingGroup.resource_class == resource_class)
                .order_by(KernelSchedulingGroup.group_id.asc())
            )
        ).scalars().all()
    return [_group_view(row) for row in rows]


async def register_work(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    resource_class: str = DEFAULT_RESOURCE_CLASS,
    group_id: str | None = None,
    deadline_at: datetime | None = None,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> None:
    """Attach scheduling metadata to an existing outbox work item.

    Policy data only — never a claim of runnability or ownership.
    Re-registration updates class/group/deadline and preserves the
    original registration time so aging stays honest. Work registered
    with no explicit group schedules under its workspace id."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import (
        KernelOutbox,
        KernelSchedulingEntry,
        KernelSchedulingGroup,
    )

    validate_resource_class(resource_class)

    async def _operation() -> None:
        async with session_factory() as session:
            async with session.begin():
                outbox_row = await session.get(KernelOutbox, work_id)
                if outbox_row is None:
                    raise UnknownWorkError(f"no outbox work item {work_id!r}")
                resolved_group = group_id if group_id is not None else outbox_row.workspace_id
                validate_group_id(resolved_group)
                now = _utcnow()
                stmt = (
                    sqlite_insert(KernelSchedulingEntry)
                    .values(
                        work_id=work_id,
                        workspace_id=outbox_row.workspace_id,
                        resource_class=resource_class,
                        group_id=resolved_group,
                        deadline_at=deadline_at,
                        created_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["work_id"],
                        set_={
                            "resource_class": resource_class,
                            "group_id": resolved_group,
                            "deadline_at": deadline_at,
                        },
                    )
                )
                await session.execute(stmt)
                # The group must be schedulable under default policy even
                # when nothing else ever touched it.
                await session.execute(
                    sqlite_insert(KernelSchedulingGroup)
                    .values(
                        resource_class=resource_class,
                        group_id=resolved_group,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["resource_class", "group_id"]
                    )
                )

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


# ---------------------------------------------------------------------------
# fair claim selection
# ---------------------------------------------------------------------------


async def _backfill_missing_entries(
    session_factory: async_sessionmaker,
    *,
    bound: int,
) -> None:
    """Give unregistered pending work its default scheduling metadata
    (default class, workspace group) and make sure the involved group
    policy rows exist. Idempotent; one bounded pass."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import KernelOutbox, KernelSchedulingEntry, KernelSchedulingGroup

    async with session_factory() as session:
        async with session.begin():
            missing = (
                await session.execute(
                    select(KernelOutbox)
                    .outerjoin(KernelSchedulingEntry, KernelOutbox.id == KernelSchedulingEntry.work_id)
                    .where(
                        KernelOutbox.state == OUTBOX_STATE_PENDING,
                        KernelSchedulingEntry.work_id.is_(None),
                    )
                    .order_by(KernelOutbox.id.asc())
                    .limit(bound)
                )
            ).scalars().all()
            if not missing:
                return
            now = _utcnow()
            for row in missing:
                validate_group_id(row.workspace_id)
                await session.execute(
                    sqlite_insert(KernelSchedulingEntry)
                    .values(
                        work_id=row.id,
                        workspace_id=row.workspace_id,
                        resource_class=DEFAULT_RESOURCE_CLASS,
                        group_id=row.workspace_id,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["work_id"])
                )
                await session.execute(
                    sqlite_insert(KernelSchedulingGroup)
                    .values(
                        resource_class=DEFAULT_RESOURCE_CLASS,
                        group_id=row.workspace_id,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["resource_class", "group_id"])
                )


async def claim_fair(
    session_factory: async_sessionmaker,
    *,
    owner_id: str,
    resource_class: str = DEFAULT_RESOURCE_CLASS,
    workspace_id: str | None = None,
    lease_seconds: float = DEFAULT_WORK_LEASE_SECONDS,
    topology_generation: int | None = None,
    lookahead: int = DEFAULT_LOOKAHEAD,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> ClaimFairResult | None:
    """Claim the next work item under weighted-fair selection and fence
    it to ``owner_id``.

    Returns ``None`` when no eligible work remains: every candidate is
    claimed, fenced elsewhere, accepted, or held back by its group's
    fan-out window. The claim path is the PR66 path — selection only
    changes *which* item is attempted, never how authority is taken or
    recorded. On success the bookkeeping transaction (same commit)
    increments the group's served count, seeds the challenge evidence,
    and appends the ``work.claimed`` semantic event; the returned
    ``challenge_nonce`` is handed only to this claimer. ``lookahead``
    bounds the scored window per scheduling group."""
    from app.kernel.models import (
        KernelOutbox,
        KernelSchedulingEntry,
        KernelSchedulingGroup,
        KernelWorkLease,
    )

    validate_owner_id(owner_id)
    validate_resource_class(resource_class)

    await _backfill_missing_entries(
        session_factory,
        bound=lookahead * 4,
    )

    # Bounded look-ahead, per group: the K oldest pending items of each
    # scheduling group (never a global id-ordered window, which would
    # keep late-arriving groups invisible behind an older backlog).
    # Items currently fenced by a *valid* lease are unavailable — a
    # redelivered-but-actively-leased item must not shadow its group's
    # claimable work.
    async with session_factory() as session:
        now_aware = _utcnow()
        subq = (
            select(
                KernelOutbox.id.label("work_id"),
                KernelOutbox.workspace_id.label("workspace_id"),
                KernelOutbox.work_kind.label("work_kind"),
                KernelOutbox.payload_json.label("payload_json"),
                KernelSchedulingEntry.group_id.label("group_id"),
                KernelSchedulingEntry.deadline_at.label("deadline_at"),
                KernelSchedulingEntry.created_at.label("entry_created_at"),
                func.row_number()
                .over(
                    partition_by=KernelSchedulingEntry.group_id,
                    order_by=KernelOutbox.id.asc(),
                )
                .label("rn"),
            )
            .join(
                KernelSchedulingEntry,
                KernelOutbox.id == KernelSchedulingEntry.work_id,
            )
            .outerjoin(
                KernelWorkLease, KernelOutbox.id == KernelWorkLease.work_id
            )
            .where(
                KernelOutbox.state == OUTBOX_STATE_PENDING,
                KernelSchedulingEntry.resource_class == resource_class,
                or_(
                    KernelWorkLease.work_id.is_(None),
                    KernelWorkLease.state != LEASE_STATE_LEASED,
                    KernelWorkLease.lease_expires_at <= now_aware,
                ),
            )
        )
        if workspace_id is not None:
            subq = subq.where(KernelOutbox.workspace_id == workspace_id)
        subq = subq.subquery()
        rows = (
            await session.execute(
                select(subq).where(subq.c.rn <= lookahead).order_by(subq.c.work_id.asc())
            )
        ).all()
        if not rows:
            return None

        group_rows = {
            row.group_id: row
            for row in (
                await session.execute(
                    select(KernelSchedulingGroup).where(
                        KernelSchedulingGroup.resource_class == resource_class
                    )
                )
            ).scalars().all()
        }

        now = _naive(_utcnow())
        by_group: dict[str, list] = {}
        for row in rows:
            by_group.setdefault(row.group_id, []).append(row)

        in_flight: dict[str, int] = {}
        for group_id in by_group:
            value = (
                await session.execute(
                    select(func.count())
                    .select_from(KernelWorkLease)
                    .join(
                        KernelSchedulingEntry,
                        KernelWorkLease.work_id == KernelSchedulingEntry.work_id,
                    )
                    .where(
                        KernelSchedulingEntry.resource_class == resource_class,
                        KernelSchedulingEntry.group_id == group_id,
                        KernelWorkLease.state == LEASE_STATE_LEASED,
                        KernelWorkLease.lease_expires_at > _utcnow(),
                    )
                )
            ).scalar_one()
            in_flight[group_id] = int(value)

    scored: list[tuple[Fraction, int, str, Any, Any]] = []
    for group_id, items in by_group.items():
        group = group_rows.get(group_id)
        if group is None:
            # Registered entry without a policy row cannot normally
            # happen (backfill/registration create both); skip honestly
            # rather than inventing policy at dispatch time.
            continue
        if in_flight.get(group_id, 0) >= group.max_in_flight:
            continue  # fan-out backpressure: bounded outstanding window
        oldest = items[0]

        virtual_finish = Fraction(group.served_count, 1) / Fraction(group.weight)
        boost = Fraction(1)
        age_seconds = (now - _naive(oldest.entry_created_at)).total_seconds()
        if age_seconds > group.age_boost_after_seconds:
            boost *= Fraction(group.age_boost_factor).limit_denominator(10**9)
        deadline = _naive(oldest.deadline_at)
        if deadline is not None:
            seconds_left = (deadline - now).total_seconds()
            if seconds_left <= 0:
                boost *= 4
            elif seconds_left <= group.age_boost_after_seconds:
                boost *= 2
        scored.append((virtual_finish / boost, oldest.work_id, group_id, group, oldest))

    scored.sort(key=lambda item: (item[0], item[1]))

    for _effective, work_id, group_id, _group, oldest in scored:
        claimed = await _outbox_claim(session_factory, work_id)
        if claimed is None:
            continue  # another dispatcher moved first
        lease = await acquire(
            session_factory,
            work_id=work_id,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            busy_retry_attempts=busy_retry_attempts,
            busy_retry_base_delay=busy_retry_base_delay,
        )
        if lease is None:
            # Fence lost after claiming (e.g. redelivery while a valid
            # lease exists): unstuck the delivery row, PR66 pattern.
            await _outbox_release(session_factory, work_id)
            continue

        nonce = await _record_claim(
            session_factory,
            work_id=work_id,
            workspace_id=oldest.workspace_id,
            work_kind=oldest.work_kind,
            owner_id=owner_id,
            lease=lease,
            resource_class=resource_class,
            group_id=group_id,
            topology_generation=topology_generation,
            busy_retry_attempts=busy_retry_attempts,
            busy_retry_base_delay=busy_retry_base_delay,
        )
        return ClaimFairResult(
            work_id=work_id,
            workspace_id=oldest.workspace_id,
            work_kind=oldest.work_kind,
            payload=json.loads(oldest.payload_json),
            lease=lease,
            challenge_nonce=nonce,
            resource_class=resource_class,
            group_id=group_id,
        )
    return None


async def _record_claim(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    workspace_id: str,
    work_kind: str,
    owner_id: str,
    lease: WorkLease,
    resource_class: str,
    group_id: str,
    topology_generation: int | None,
    busy_retry_attempts: int | None,
    busy_retry_base_delay: float | None,
) -> str:
    """Post-acquire bookkeeping in one transaction: served-count
    increment (non-authoritative), challenge evidence seed, and the
    ``work.claimed`` semantic event. A crash before this transaction
    leaves a repairable gap (see :func:`reconcile_dispatch`)."""
    from app.kernel.models import KernelSchedulingGroup
    from app.utils.canonical import canonical_json_str, to_json_ready

    async def _operation() -> str:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(KernelSchedulingGroup)
                    .where(
                        KernelSchedulingGroup.resource_class == resource_class,
                        KernelSchedulingGroup.group_id == group_id,
                    )
                    .values(
                        served_count=KernelSchedulingGroup.served_count + 1,
                        updated_at=_utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                nonce = await seed_challenge_in_session(
                    session, work_id=work_id, topology_generation=topology_generation
                )
                try:
                    payload_json = canonical_json_str(
                        to_json_ready(
                            {
                                "work_id": work_id,
                                "owner_id": owner_id,
                                "fencing_token": lease.fencing_token,
                                "work_kind": work_kind,
                                "resource_class": resource_class,
                                "group_id": group_id,
                            }
                        )
                    )
                except Exception as exc:
                    raise InvalidEventError(f"claim event payload rejected: {exc}") from exc
                await _append_in_session(
                    session,
                    workspace_id=workspace_id,
                    stream="work",
                    event_type=EVENT_CLAIMED,
                    payload_json=payload_json,
                    durability=DURABILITY_DURABLE,
                )
                return nonce

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


# ---------------------------------------------------------------------------
# accepted publication with durable semantic record
# ---------------------------------------------------------------------------


async def accept_work(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    fencing_token: int,
    result: dict,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
):
    """Accept an executed result through the PR66 authority, then record
    the ``work.accepted`` semantic event.

    Authority order is preserved exactly: :func:`app.kernel.fencing.accept`
    runs first and alone decides acceptance (stale fences never reach
    result comparison; the unique scope keeps one publication). The
    event transaction afterwards can only describe what already
    committed — a crash between the two leaves a repairable gap that
    :func:`reconcile_dispatch` derives from the publication authority.
    Idempotent: a converged re-acceptance does not append a second
    event."""
    import json as _json

    from app.kernel import fencing as _fencing
    from app.kernel.models import KernelEvent

    outcome = await _fencing.accept(
        session_factory,
        work_id=work_id,
        fencing_token=fencing_token,
        result=result,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )
    publication = outcome.publication

    async def _operation() -> bool:
        async with session_factory() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(KernelEvent.payload_json).where(
                            KernelEvent.workspace_id == publication.workspace_id,
                            KernelEvent.stream == "work",
                            KernelEvent.event_type == "work.accepted",
                        )
                    )
                ).all()
                for (payload_json,) in rows:
                    try:
                        if _json.loads(payload_json).get("work_id") == work_id:
                            return False  # already recorded (converged retry)
                    except (ValueError, AttributeError):  # pragma: no cover
                        continue
                await _append_in_session(
                    session,
                    workspace_id=publication.workspace_id,
                    stream="work",
                    event_type="work.accepted",
                    payload_json=_canonical_event_payload(
                        work_id=work_id,
                        publication_id=publication.publication_id,
                        result_hash=publication.result_hash,
                        fencing_token=publication.fencing_token,
                        owner_id=publication.owner_id,
                    ),
                    durability=DURABILITY_DURABLE,
                )
                return True

    appended = await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )
    return outcome, appended


def _canonical_event_payload(**fields: Any) -> str:
    from app.utils.canonical import canonical_json_str, to_json_ready

    try:
        return canonical_json_str(to_json_ready(dict(fields)))
    except Exception as exc:
        raise InvalidEventError(f"event payload rejected: {exc}") from exc


# ---------------------------------------------------------------------------
# deterministic crash repair
# ---------------------------------------------------------------------------


async def reconcile_dispatch(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str | None = None,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> dict[str, Any]:
    """Converge dispatch state after crashes — repairs, never invention.

    * an outbox delivery stuck ``in_flight`` with no lease row (crash
      between the delivery claim and the fence acquire) is returned to
      ``pending`` through the outbox's own at-least-once release;
    * a committed ownership or acceptance whose ``work.claimed`` /
      ``work.accepted`` semantic event was lost (crash between the
      authority commit and the bookkeeping commit) is re-derived from
      the lease/publication authorities by
      :func:`app.kernel.events.reconcile_from_authority`.

    Allowed end states after any dispatch crash: work pending again
    (at-least-once), work leased under its current fence with the event
    repaired, or work accepted with the event repaired. Nothing here
    can fabricate a claim, publication, or event that never committed."""
    from app.kernel.models import KernelOutbox, KernelWorkLease

    from app.kernel import events as _events

    async def _operation() -> int:
        async with session_factory() as session:
            async with session.begin():
                leased_ids = select(KernelWorkLease.work_id).scalar_subquery()
                result = await session.execute(
                    update(KernelOutbox)
                    .where(
                        KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT,
                        KernelOutbox.id.not_in(leased_ids),
                    )
                    .values(state=OUTBOX_STATE_PENDING, claimed_at=None)
                    .execution_options(synchronize_session=False)
                )
                return result.rowcount

    released = await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )

    async with session_factory() as session:
        workspace_rows = (
            await session.execute(
                select(KernelWorkLease.workspace_id).distinct()
            )
        ).scalars().all()
    workspaces = (
        [workspace_id] if workspace_id is not None else list(workspace_rows)
    )

    repaired: list = []
    for ws in workspaces:
        repaired.extend(
            await _events.reconcile_from_authority(session_factory, workspace_id=ws)
        )
    return {"orphaned_deliveries_released": released, "events_repaired": repaired}
