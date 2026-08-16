"""Fenced work ownership and exactly-once accepted publication (V3.2
PR66).

The PR64 outbox is honestly at-least-once: duplicate execution,
redelivery, and failover are expected. This module adds the durable
authority boundary that turns that honesty into a guarantee — for one
outbox work item, the SQLite database can always answer *which
ownership generation may turn an executed result into accepted state*.

Two durable primitives (revision ``20260816_0008``):

* ``kernel_work_leases`` — the fencing authority. A monotonically
  increasing ``fencing_token`` advances inside every ownership
  transition transaction (first acquire → 1, takeover → +1, vacate →
  +1). Authority is the stored token, never wall-clock time: lease
  expiry only makes takeover *eligible*; it can neither revive a stale
  token nor authorize an old worker that wakes up late.
* ``kernel_publications`` — the accepted-result truth. The acceptance
  linearization point is the single transaction that (1) verifies the
  submitted token is the current lease authority, then (2) inserts the
  publication row and flips the lease to ``accepted``. The unique
  ``(workspace_id, work_id)`` scope makes "at most one accepted
  publication" a database-enforced fact.

Concurrency model: every transition is a conditional ``UPDATE`` (or
guarded ``INSERT``) checked via ``rowcount`` inside one transaction, so
racing contenders cannot both win; SQLite writer serialization plus a
bounded busy-retry loop handles ``SQLITE_BUSY``.

Acceptance outcomes:

* same result retried after an ambiguous caller outcome → converges to
  the existing publication (``already_accepted=True``);
* materially different result for the same work →
  :class:`PublicationConflictError`, accepted state unchanged;
* stale fence (superseded, vacated, or never-acquired work) →
  :class:`StaleFenceError`, nothing written.

External effects are NOT covered: this module is exactly-once for local
database state only. Any notification, webhook, or remote upload
triggered downstream remains at-least-once unless the destination
itself supplies a real idempotency primitive.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    InjectedFaultError,
    InvalidOwnerIdError,
    InvalidWorkLeaseError,
    InvalidWorkResultError,
    KernelBusyError,
    PublicationConflictError,
    StaleFenceError,
    UnknownWorkError,
    UnknownWorkLeaseError,
)
from app.kernel.outbox import (
    OUTBOX_STATE_DONE,
    OUTBOX_STATE_IN_FLIGHT,
    OUTBOX_STATE_PENDING,
    claim as _outbox_claim,
    release as _outbox_release,
)
from app.utils.canonical import (
    CanonicalValueError,
    canonical_json_str,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "AcceptOutcome",
    "ClaimedWork",
    "FAULT_PHASES",
    "LEASE_STATE_ACCEPTED",
    "LEASE_STATE_LEASED",
    "LEASE_STATE_RELEASED",
    "PHASE_FENCE_VALIDATED",
    "PHASE_POST_COMMIT",
    "PHASE_PUBLICATION_INSERTED",
    "Publication",
    "WorkLease",
    "DEFAULT_WORK_LEASE_SECONDS",
    "OWNER_ID_PATTERN",
    "accept",
    "acquire",
    "claim_next",
    "complete_work",
    "compute_publication_id",
    "compute_result_hash",
    "get_lease",
    "get_publication",
    "release",
    "validate_owner_id",
    "validate_result",
]

LEASE_STATE_LEASED = "leased"
LEASE_STATE_RELEASED = "released"
LEASE_STATE_ACCEPTED = "accepted"

#: Wall-clock lease length. Expiry makes takeover eligible; it never
#: rescinds a token that is still current (see module docstring).
DEFAULT_WORK_LEASE_SECONDS = 300.0

OWNER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
_MAX_RETRY_DELAY = 0.5

#: Framing domains separating work results and publications from other
#: kernel hashes.
_RESULT_FRAMING_RECORD_TYPE = "marker.kernel.work_result.v1"
_PUBLICATION_FRAMING_RECORD_TYPE = "marker.kernel.publication.v1"
_FRAMING_SCHEMA_VERSION = "1.0.0"

# Deterministic fault-injection phases for the acceptance protocol.
PHASE_FENCE_VALIDATED = "fence_validated"
PHASE_PUBLICATION_INSERTED = "publication_inserted"
PHASE_POST_COMMIT = "post_commit"

FAULT_PHASES = frozenset(
    {
        PHASE_FENCE_VALIDATED,
        PHASE_PUBLICATION_INSERTED,
        PHASE_POST_COMMIT,
    }
)

_PUBLICATION_SCOPE_MARKER = "uq_kernel_publications_scope"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Naive-UTC isoformat so views are stable across reopen (SQLite
    returns tz-naive datetimes; in-session rows may be tz-aware)."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


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


def _maybe_inject(phase: str | None, expected: str) -> None:
    if phase == expected:
        raise InjectedFaultError(phase)


async def _run_with_busy_retry(
    operation,
    *,
    busy_retry_attempts: int | None,
    busy_retry_base_delay: float | None,
) -> Any:
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
        f"work fencing operation still busy after {attempts} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# identity and validation
# ---------------------------------------------------------------------------


def validate_owner_id(owner_id: str) -> str:
    if not isinstance(owner_id, str) or not OWNER_ID_PATTERN.match(owner_id):
        raise InvalidOwnerIdError(
            f"invalid owner_id: {owner_id!r} must match {OWNER_ID_PATTERN.pattern}"
        )
    return owner_id


def validate_result(result: Mapping[str, Any]) -> str:
    """Canonical JSON for a work result; rejects non-canonical values."""
    try:
        return canonical_json_str(to_json_ready(dict(result)))
    except CanonicalValueError as exc:
        raise InvalidWorkResultError(f"work result rejected: {exc}") from exc


def compute_result_hash(result_json: str) -> str:
    """Deterministic framed identity of one executed result."""
    return record_identity_hash(
        record_type=_RESULT_FRAMING_RECORD_TYPE,
        schema_version=_FRAMING_SCHEMA_VERSION,
        payload=json.loads(result_json),
    )


def compute_publication_id(*, workspace_id: str, work_id: int, result_hash: str) -> str:
    """Deterministic publication identity over its acceptance scope."""
    framed = {
        "workspace_id": workspace_id,
        "work_id": work_id,
        "result_hash": result_hash,
    }
    return record_identity_hash(
        record_type=_PUBLICATION_FRAMING_RECORD_TYPE,
        schema_version=_FRAMING_SCHEMA_VERSION,
        payload=framed,
    )


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkLease:
    """Durable fenced ownership snapshot for one work item."""

    work_id: int
    workspace_id: str
    work_kind: str
    fencing_token: int
    owner_id: str
    state: str
    lease_expires_at: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class Publication:
    """The accepted result for one work identity."""

    publication_id: str
    workspace_id: str
    work_id: int
    work_kind: str
    result: dict
    result_hash: str
    fencing_token: int
    owner_id: str
    accepted_at: str | None


@dataclass(frozen=True)
class AcceptOutcome:
    """Result of one acceptance attempt.

    ``already_accepted`` is True when the attempt converged to an
    existing publication (idempotent retry) rather than creating one.
    """

    publication: Publication
    already_accepted: bool


@dataclass(frozen=True)
class ClaimedWork:
    """One outbox item bound to a freshly acquired fence."""

    work_id: int
    workspace_id: str
    work_kind: str
    payload: dict
    lease: WorkLease


def _lease_view(row) -> WorkLease:
    return WorkLease(
        work_id=row.work_id,
        workspace_id=row.workspace_id,
        work_kind=row.work_kind,
        fencing_token=row.fencing_token,
        owner_id=row.owner_id,
        state=row.state,
        lease_expires_at=_iso(row.lease_expires_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _publication_view(row) -> Publication:
    return Publication(
        publication_id=row.publication_id,
        workspace_id=row.workspace_id,
        work_id=row.work_id,
        work_kind=row.work_kind,
        result=json.loads(row.result_json),
        result_hash=row.result_hash,
        fencing_token=row.fencing_token,
        owner_id=row.owner_id,
        accepted_at=_iso(row.accepted_at),
    )


# ---------------------------------------------------------------------------
# fenced ownership
# ---------------------------------------------------------------------------


async def acquire(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    owner_id: str,
    lease_seconds: float = DEFAULT_WORK_LEASE_SECONDS,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> WorkLease | None:
    """Acquire or take over fenced ownership of one outbox work item.

    Returns the current lease, or ``None`` when the caller lost the
    claim race, the work is already accepted, or another owner holds a
    valid lease. Transitions, each atomic and token-advancing:

    * no lease row yet → first acquire creates it with token 1;
    * same current owner, state ``leased`` → renewal (expiry extended,
      token unchanged — idempotent for at-least-once redelivery);
    * lease vacated (``released``) or expired → takeover (token +1);
    * anything else → ``None`` (not eligible).
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import KernelOutbox, KernelWorkLease

    validate_owner_id(owner_id)
    if lease_seconds <= 0:
        raise InvalidWorkLeaseError("lease_seconds must be positive")

    async def _operation() -> WorkLease | None:
        async with session_factory() as session:
            async with session.begin():
                outbox_row = await session.get(KernelOutbox, work_id)
                if outbox_row is None:
                    raise UnknownWorkError(f"no outbox work item {work_id!r}")
                if outbox_row.state == OUTBOX_STATE_DONE:
                    return None

                now = _utcnow()
                expires = now + timedelta(seconds=lease_seconds)
                lease = await session.get(KernelWorkLease, work_id)

                if lease is None:
                    # Guarded insert: a concurrent first-acquire that wins
                    # the primary key makes this contender cleanly lose
                    # (T2) instead of failing the transaction.
                    inserted = await session.execute(
                        sqlite_insert(KernelWorkLease)
                        .values(
                            work_id=work_id,
                            workspace_id=outbox_row.workspace_id,
                            work_kind=outbox_row.work_kind,
                            fencing_token=1,
                            owner_id=owner_id,
                            state=LEASE_STATE_LEASED,
                            lease_expires_at=expires,
                        )
                        .on_conflict_do_nothing(index_elements=["work_id"])
                    )
                    if inserted.rowcount != 1:
                        return None
                    created = await session.get(KernelWorkLease, work_id)
                    return _lease_view(created)

                if lease.state == LEASE_STATE_ACCEPTED:
                    return None

                if lease.owner_id == owner_id and lease.state == LEASE_STATE_LEASED:
                    # Renewal by the still-current owner: token must not
                    # move just because delivery duplicated the acquire.
                    result = await session.execute(
                        update(KernelWorkLease)
                        .where(
                            KernelWorkLease.work_id == work_id,
                            KernelWorkLease.fencing_token == lease.fencing_token,
                            KernelWorkLease.owner_id == owner_id,
                            KernelWorkLease.state == LEASE_STATE_LEASED,
                        )
                        .values(lease_expires_at=expires, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        return None  # ownership moved mid-renewal
                    session.expire(lease)  # Core update bypassed the instance
                    refreshed = await session.get(KernelWorkLease, work_id)
                    return _lease_view(refreshed)

                # Takeover: only when vacated or the lease has lapsed.
                result = await session.execute(
                    update(KernelWorkLease)
                    .where(
                        KernelWorkLease.work_id == work_id,
                        KernelWorkLease.fencing_token == lease.fencing_token,
                    )
                    .where(
                        (KernelWorkLease.state == LEASE_STATE_RELEASED)
                        | (KernelWorkLease.lease_expires_at <= now)
                    )
                    .values(
                        fencing_token=lease.fencing_token + 1,
                        owner_id=owner_id,
                        state=LEASE_STATE_LEASED,
                        lease_expires_at=expires,
                        updated_at=now,
                    )
                    # The identity-mapped row may hold tz-aware values the
                    # SQL-side expiry compare must not re-evaluate in Python.
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    return None  # lost the takeover race
                session.expire(lease)  # Core update bypassed the instance
                refreshed = await session.get(KernelWorkLease, work_id)
                return _lease_view(refreshed)

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


async def release(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    owner_id: str,
    fencing_token: int,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> bool:
    """Vacate ownership: state ``released`` and token advanced, so the
    releasing owner is immediately stale. False if the fence did not
    match (nothing is written)."""
    from app.kernel.models import KernelWorkLease

    validate_owner_id(owner_id)

    async def _operation() -> bool:
        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelWorkLease)
                    .where(
                        KernelWorkLease.work_id == work_id,
                        KernelWorkLease.fencing_token == fencing_token,
                        KernelWorkLease.owner_id == owner_id,
                        KernelWorkLease.state == LEASE_STATE_LEASED,
                    )
                    .values(
                        state=LEASE_STATE_RELEASED,
                        fencing_token=fencing_token + 1,
                        updated_at=_utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                return result.rowcount == 1

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


async def get_lease(session_factory: async_sessionmaker, work_id: int) -> WorkLease | None:
    """Current durable ownership, reconstructed from the database."""
    from app.kernel.models import KernelWorkLease

    async with session_factory() as session:
        row = await session.get(KernelWorkLease, work_id)
    return _lease_view(row) if row is not None else None


# ---------------------------------------------------------------------------
# exactly-once accepted publication
# ---------------------------------------------------------------------------


async def accept(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    fencing_token: int,
    result: Mapping[str, Any],
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
    _inject_fault_at: str | None = None,
) -> AcceptOutcome:
    """Submit an executed result as the accepted publication for work.

    Linearization point: the commit of the single transaction that
    verified the fence, inserted ``kernel_publications``, and flipped
    the lease to ``accepted``. Outcomes, in authority order:

    * token not the current authority (superseded, vacated, or never
      acquired) → :class:`StaleFenceError`, nothing written — an
      unauthorized submission is never even compared against accepted
      state;
    * current fence, no publication yet → creates the one accepted
      publication (``already_accepted=False``);
    * current fence, publication exists with the same result hash →
      converges to it (``already_accepted=True``) — the ambiguous-
      outcome retry never creates a second authoritative state;
    * current fence, publication exists with a different result →
      :class:`PublicationConflictError`, accepted state unchanged.
    """
    from app.kernel.models import KernelPublication, KernelWorkLease

    if _inject_fault_at is not None and _inject_fault_at not in FAULT_PHASES:
        raise InjectedFaultError(_inject_fault_at)
    result_json = validate_result(result)
    result_hash = compute_result_hash(result_json)
    # One extra pass is enough to converge a unique-scope race (which
    # SQLite writer serialization already prevents; kept as a guard).
    converged_race = False

    async def _operation() -> AcceptOutcome:
        nonlocal converged_race
        async with session_factory() as session:
            async with session.begin():
                # Authority first: an unauthorized submission is stale
                # regardless of what it carries and must never even be
                # compared against accepted state.
                lease = await session.get(KernelWorkLease, work_id)
                if lease is None:
                    raise UnknownWorkLeaseError(
                        f"work {work_id!r} was never acquired through the "
                        "fencing boundary"
                    )
                if lease.fencing_token != fencing_token or lease.state not in (
                    LEASE_STATE_LEASED,
                    LEASE_STATE_ACCEPTED,
                ):
                    raise StaleFenceError(
                        submitted_token=fencing_token,
                        current_token=lease.fencing_token,
                    )

                publication = (
                    await session.execute(
                        select(KernelPublication).where(
                            KernelPublication.work_id == work_id
                        )
                    )
                ).scalar_one_or_none()
                if publication is not None:
                    if publication.result_hash == result_hash:
                        return AcceptOutcome(
                            publication=_publication_view(publication),
                            already_accepted=True,
                        )
                    raise PublicationConflictError(
                        existing_result_hash=publication.result_hash,
                        submitted_result_hash=result_hash,
                    )
                if lease.state == LEASE_STATE_ACCEPTED:
                    # Accepted without a publication row cannot happen
                    # (same transaction); fail closed if it ever does.
                    raise StaleFenceError(
                        submitted_token=fencing_token,
                        current_token=lease.fencing_token,
                    )

                _maybe_inject(_inject_fault_at, PHASE_FENCE_VALIDATED)

                row = KernelPublication(
                    publication_id=compute_publication_id(
                        workspace_id=lease.workspace_id,
                        work_id=work_id,
                        result_hash=result_hash,
                    ),
                    workspace_id=lease.workspace_id,
                    work_id=work_id,
                    work_kind=lease.work_kind,
                    result_json=result_json,
                    result_hash=result_hash,
                    fencing_token=fencing_token,
                    owner_id=lease.owner_id,
                )
                session.add(row)
                await session.flush()

                flipped = await session.execute(
                    update(KernelWorkLease)
                    .where(
                        KernelWorkLease.work_id == work_id,
                        KernelWorkLease.fencing_token == fencing_token,
                        KernelWorkLease.state == LEASE_STATE_LEASED,
                    )
                    .values(state=LEASE_STATE_ACCEPTED, updated_at=_utcnow())
                    .execution_options(synchronize_session=False)
                )
                if flipped.rowcount != 1:
                    raise StaleFenceError(
                        submitted_token=fencing_token,
                        current_token=fencing_token + 1,
                    )

                _maybe_inject(_inject_fault_at, PHASE_PUBLICATION_INSERTED)
                view = _publication_view(row)

            # Transaction committed: the acceptance linearization point.
            _maybe_inject(_inject_fault_at, PHASE_POST_COMMIT)
            return AcceptOutcome(publication=view, already_accepted=False)

    try:
        return await _run_with_busy_retry(
            _operation,
            busy_retry_attempts=busy_retry_attempts,
            busy_retry_base_delay=busy_retry_base_delay,
        )
    except IntegrityError as exc:
        # SQLite's unique-violation text names the table/columns, not
        # the constraint, so match either form.
        text = str(exc)
        if ("kernel_publications" in text or _PUBLICATION_SCOPE_MARKER in text) and (
            not converged_race
        ):
            converged_race = True
            return await _run_with_busy_retry(
                _operation,
                busy_retry_attempts=busy_retry_attempts,
                busy_retry_base_delay=busy_retry_base_delay,
            )
        raise


async def get_publication(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    workspace_id: str | None = None,
) -> Publication | None:
    """The accepted publication for one work identity, if any.

    Workspace is derived from the publication row itself; an explicit
    ``workspace_id`` filter must agree with it (isolation guard)."""
    from app.kernel.models import KernelPublication

    async with session_factory() as session:
        stmt = select(KernelPublication).where(KernelPublication.work_id == work_id)
        if workspace_id is not None:
            stmt = stmt.where(KernelPublication.workspace_id == workspace_id)
        row = (await session.execute(stmt)).scalar_one_or_none()
    return _publication_view(row) if row is not None else None


async def complete_work(
    session_factory: async_sessionmaker,
    *,
    work_id: int,
    fencing_token: int,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> bool:
    """Acknowledge outbox delivery, but only behind accepted truth.

    The outbox row may move to ``done`` solely inside a transaction
    that also observes (1) the lease is still under the submitting,
    accepted fence and (2) the accepted publication exists. A stale
    worker therefore cannot acknowledge work, and acknowledgement can
    never become durable before the accepted result it represents.
    False if the fence did not match or the work is not accepted yet.
    """
    from app.kernel.models import KernelOutbox, KernelPublication, KernelWorkLease

    async def _operation() -> bool:
        async with session_factory() as session:
            async with session.begin():
                lease = await session.get(KernelWorkLease, work_id)
                if (
                    lease is None
                    or lease.fencing_token != fencing_token
                    or lease.state != LEASE_STATE_ACCEPTED
                ):
                    return False
                publication = (
                    await session.execute(
                        select(KernelPublication).where(
                            KernelPublication.work_id == work_id,
                            KernelPublication.workspace_id == lease.workspace_id,
                        )
                    )
                ).scalar_one_or_none()
                if publication is None:
                    return False
                result = await session.execute(
                    update(KernelOutbox)
                    .where(
                        KernelOutbox.id == work_id,
                        KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT,
                    )
                    .values(state=OUTBOX_STATE_DONE, completed_at=_utcnow())
                )
                return result.rowcount == 1

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


# ---------------------------------------------------------------------------
# minimal dispatch seam (policy-light on purpose — PR67 owns scheduling)
# ---------------------------------------------------------------------------


async def claim_next(
    session_factory: async_sessionmaker,
    *,
    owner_id: str,
    workspace_id: str | None = None,
    lease_seconds: float = DEFAULT_WORK_LEASE_SECONDS,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> ClaimedWork | None:
    """Claim the oldest pending outbox item and fence it to ``owner_id``.

    Deliberately minimal: oldest-first selection, no fairness, no
    heartbeat. Loses cleanly (``None``) when every candidate is already
    claimed, fenced elsewhere, or accepted. A claimed-but-unfenceable
    item is returned to pending so no state gets stuck.
    """
    from app.kernel.models import KernelOutbox

    validate_owner_id(owner_id)

    async with session_factory() as session:
        stmt = (
            select(KernelOutbox.id)
            .where(KernelOutbox.state == OUTBOX_STATE_PENDING)
            .order_by(KernelOutbox.id.asc())
            .limit(8)
        )
        if workspace_id is not None:
            stmt = stmt.where(KernelOutbox.workspace_id == workspace_id)
        candidates = (await session.execute(stmt)).scalars().all()

    for work_id in candidates:
        claimed = await _outbox_claim(session_factory, work_id)
        if claimed is None:
            continue  # another dispatcher moved it first
        lease = await acquire(
            session_factory,
            work_id=work_id,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
            busy_retry_attempts=busy_retry_attempts,
            busy_retry_base_delay=busy_retry_base_delay,
        )
        if lease is None:
            # Fence lost after claiming: unstuck the delivery row so the
            # winner (or a later retry) can proceed honestly.
            await _outbox_release(session_factory, work_id)
            continue
        return ClaimedWork(
            work_id=work_id,
            workspace_id=claimed.workspace_id,
            work_kind=claimed.work_kind,
            payload=claimed.payload,
            lease=lease,
        )
    return None
