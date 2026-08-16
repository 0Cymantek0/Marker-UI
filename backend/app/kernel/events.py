"""Durable semantic events and coalescible progress (V3.2 PR67A).

Execution correctness must not depend on any subscriber's speed. This
module owns the durable side of that contract:

* ``kernel_events`` — append-only semantic/control events with an
  authoritative per-(workspace, stream) ``semantic_sequence``. The
  sequence is allocated inside the append transaction (writer-serialized
  ``MAX+1``), so within one scope it can neither fork nor regress, and
  replay order never depends on wall-clock timestamps. Terminal and
  control events land here and remain replayable even when nobody was
  connected when they happened.
* ``kernel_progress`` — coalescible progress snapshots: exactly one row
  per (workspace, work), updated in place. A progress flood converges to
  the latest snapshot instead of one durable row per tick, and dropping
  or lagging progress never loses semantic truth.

Classification is the caller's contract, enforced by surface: semantic
events go through :func:`append` (always durable rows), progress goes
through :func:`append_progress` (always coalesced). Bounded diagnostics
(e.g. selected warnings) may be appended as events with
``durability="diagnostic"``; the marker records the class so retention
policy can later bound them without conflating them with control truth.

Reading is strictly pull-based: :func:`replay` answers "events after
sequence K" from the database, and :func:`follow` is a polling cursor
adapter that opens a fresh short session per batch — a slow consumer
slows only itself; it can never hold a write transaction or block
dispatch, renewal, publication, or terminal state.

This module is not a second work authority: events describe transitions
that already happened elsewhere (outbox, fences, publications). Repair
of crash-induced gaps is deterministic derivation from those authorities
(:func:`reconcile_from_authority`), never invention.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import (
    InvalidEventError,
    KernelBusyError,
)
from app.utils.canonical import CanonicalValueError, canonical_json_str, to_json_ready

__all__ = [
    "DEFAULT_STREAM",
    "DURABILITY_DIAGNOSTIC",
    "DURABILITY_DURABLE",
    "EVENT_STREAM_PATTERN",
    "EVENT_TYPE_PATTERN",
    "ProgressSnapshot",
    "SemanticEvent",
    "append",
    "append_progress",
    "follow",
    "get_latest_sequence",
    "get_progress",
    "reconcile_from_authority",
    "replay",
    "validate_event_type",
    "validate_stream",
]

DEFAULT_STREAM = "work"

DURABILITY_DURABLE = "durable"
DURABILITY_DIAGNOSTIC = "diagnostic"
_DURABILITY_VALUES = frozenset({DURABILITY_DURABLE, DURABILITY_DIAGNOSTIC})

EVENT_STREAM_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")

DEFAULT_BUSY_RETRY_ATTEMPTS = 8
DEFAULT_BUSY_RETRY_BASE_DELAY = 0.02
_MAX_RETRY_DELAY = 0.5

_EVENT_CLAIMED = "work.claimed"
_EVENT_ACCEPTED = "work.accepted"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
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
        f"event operation still busy after {attempts} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# validation and views
# ---------------------------------------------------------------------------


def validate_stream(stream: str) -> str:
    if not isinstance(stream, str) or not EVENT_STREAM_PATTERN.match(stream):
        raise InvalidEventError(
            f"invalid event stream: {stream!r} must match "
            f"{EVENT_STREAM_PATTERN.pattern}"
        )
    return stream


def validate_event_type(event_type: str) -> str:
    if not isinstance(event_type, str) or not EVENT_TYPE_PATTERN.match(event_type):
        raise InvalidEventError(
            f"invalid event type: {event_type!r} must match "
            f"{EVENT_TYPE_PATTERN.pattern}"
        )
    return event_type


def _validate_payload(payload: Mapping[str, Any]) -> str:
    try:
        return canonical_json_str(to_json_ready(dict(payload)))
    except (CanonicalValueError, TypeError, ValueError) as exc:
        raise InvalidEventError(f"event payload rejected: {exc}") from exc


@dataclass(frozen=True)
class SemanticEvent:
    """One durable semantic event at its authoritative sequence."""

    workspace_id: str
    stream: str
    semantic_sequence: int
    event_type: str
    durability: str
    payload: dict
    created_at: str | None


@dataclass(frozen=True)
class ProgressSnapshot:
    """Latest coalesced progress for one work item."""

    workspace_id: str
    work_id: int
    counter: int
    payload: dict
    updated_at: str | None


def _event_view(row) -> SemanticEvent:
    return SemanticEvent(
        workspace_id=row.workspace_id,
        stream=row.stream,
        semantic_sequence=row.semantic_sequence,
        event_type=row.event_type,
        durability=row.durability,
        payload=json.loads(row.payload_json),
        created_at=_iso(row.created_at),
    )


def _progress_view(row) -> ProgressSnapshot:
    return ProgressSnapshot(
        workspace_id=row.workspace_id,
        work_id=row.work_id,
        counter=row.counter,
        payload=json.loads(row.payload_json),
        updated_at=_iso(row.updated_at),
    )


# ---------------------------------------------------------------------------
# in-session append (shared with scheduler/liveness transactions)
# ---------------------------------------------------------------------------


async def _append_in_session(
    session,
    *,
    workspace_id: str,
    stream: str,
    event_type: str,
    payload_json: str,
    durability: str,
) -> SemanticEvent:
    """Append inside a caller-owned transaction; the caller's commit is
    the sequence linearization point. Writer serialization makes the
    MAX+1 allocation race-free; a unique-violation can only mean the
    caller raced outside a transaction, so it fails loudly rather than
    silently forking the sequence."""
    from app.kernel.models import KernelEvent

    next_seq = (
        select(func.coalesce(func.max(KernelEvent.semantic_sequence), 0) + 1)
        .where(
            KernelEvent.workspace_id == workspace_id,
            KernelEvent.stream == stream,
        )
        .scalar_subquery()
    )
    row = KernelEvent(
        workspace_id=workspace_id,
        stream=stream,
        semantic_sequence=next_seq,
        event_type=event_type,
        durability=durability,
        payload_json=payload_json,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:  # pragma: no cover - writer serialization guard
        raise KernelBusyError(
            "semantic sequence allocation raced outside writer "
            "serialization; retry the append inside one transaction"
        ) from exc
    return SemanticEvent(
        workspace_id=workspace_id,
        stream=stream,
        semantic_sequence=row.semantic_sequence,
        event_type=event_type,
        durability=durability,
        payload=json.loads(payload_json),
        created_at=_iso(row.created_at),
    )


# ---------------------------------------------------------------------------
# public append surface
# ---------------------------------------------------------------------------


async def append(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    stream: str = DEFAULT_STREAM,
    event_type: str,
    payload: Mapping[str, Any],
    durability: str = DURABILITY_DURABLE,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> SemanticEvent:
    """Append one durable semantic event; returns it with its assigned
    authoritative sequence. The commit of the append transaction is the
    linearization point — an event that never committed is never
    replayable, and no gap is left behind by a rolled-back append."""
    validate_stream(stream)
    validate_event_type(event_type)
    if durability not in _DURABILITY_VALUES:
        raise InvalidEventError(
            f"invalid durability {durability!r}; expected one of "
            f"{sorted(_DURABILITY_VALUES)}"
        )
    payload_json = _validate_payload(payload)

    async def _operation() -> SemanticEvent:
        async with session_factory() as session:
            async with session.begin():
                return await _append_in_session(
                    session,
                    workspace_id=workspace_id,
                    stream=stream,
                    event_type=event_type,
                    payload_json=payload_json,
                    durability=durability,
                )

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


async def append_progress(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    work_id: int,
    counter: int,
    payload: Mapping[str, Any] | None = None,
    busy_retry_attempts: int | None = None,
    busy_retry_base_delay: float | None = None,
) -> ProgressSnapshot:
    """Coalesce one progress tick onto the single durable snapshot row.

    Best-effort by contract: the row always holds the latest accepted
    tick, repeated or lagging ticks overwrite in place, and nothing here
    can block or lose durable semantic events. ``counter`` is the
    caller's monotonic progress measure; it is stored, not enforced."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from app.kernel.models import KernelProgress

    if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0:
        raise InvalidEventError(f"invalid work_id: {work_id!r}")
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
        raise InvalidEventError(f"invalid progress counter: {counter!r}")
    payload_json = _validate_payload(payload or {})

    async def _operation() -> ProgressSnapshot:
        async with session_factory() as session:
            async with session.begin():
                now = _utcnow()
                stmt = (
                    sqlite_insert(KernelProgress)
                    .values(
                        workspace_id=workspace_id,
                        work_id=work_id,
                        counter=counter,
                        payload_json=payload_json,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "work_id"],
                        set_={
                            "counter": counter,
                            "payload_json": payload_json,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(stmt)
                row = await session.get(
                    KernelProgress, {"workspace_id": workspace_id, "work_id": work_id}
                )
                return _progress_view(row)

    return await _run_with_busy_retry(
        _operation,
        busy_retry_attempts=busy_retry_attempts,
        busy_retry_base_delay=busy_retry_base_delay,
    )


# ---------------------------------------------------------------------------
# replay surface
# ---------------------------------------------------------------------------


async def replay(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    stream: str = DEFAULT_STREAM,
    after_sequence: int = 0,
    limit: int | None = None,
) -> list[SemanticEvent]:
    """Durable events after ``after_sequence``, in authoritative
    sequence order. Independent of any live subscriber, in-memory
    buffer, or timestamp."""
    from app.kernel.models import KernelEvent

    validate_stream(stream)
    async with session_factory() as session:
        stmt = (
            select(KernelEvent)
            .where(
                KernelEvent.workspace_id == workspace_id,
                KernelEvent.stream == stream,
                KernelEvent.semantic_sequence > after_sequence,
            )
            .order_by(KernelEvent.semantic_sequence.asc())
        )
        if limit is not None:
            if limit <= 0:
                raise InvalidEventError(f"invalid replay limit: {limit!r}")
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
    return [_event_view(row) for row in rows]


async def get_latest_sequence(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    stream: str = DEFAULT_STREAM,
) -> int:
    """The highest assigned semantic sequence in this scope (0 when
    empty). Reconnect cursors start after this value."""
    from app.kernel.models import KernelEvent

    async with session_factory() as session:
        value = (
            await session.execute(
                select(func.max(KernelEvent.semantic_sequence)).where(
                    KernelEvent.workspace_id == workspace_id,
                    KernelEvent.stream == stream,
                )
            )
        ).scalar_one_or_none()
    return int(value or 0)


async def follow(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    stream: str = DEFAULT_STREAM,
    after_sequence: int = 0,
    poll_interval: float = 0.05,
    max_idle_seconds: float | None = None,
) -> AsyncIterator[SemanticEvent]:
    """Pull-based live cursor over the durable sequence.

    Transport-independent adapter, not an authority: each poll opens a
    fresh short read session, so a slow consumer only slows itself and
    can never hold a write transaction open. Disconnecting simply ends
    the iteration — work continues, and a reconnect resumes from the
    last delivered sequence. ``max_idle_seconds`` stops the cursor after
    that long with no new events (test/ops convenience; ``None`` waits
    forever)."""
    validate_stream(stream)
    cursor = after_sequence
    idle_deadline: datetime | None = (
        _utcnow() + timedelta(seconds=max_idle_seconds)
        if max_idle_seconds is not None
        else None
    )
    while True:
        batch = await replay(
            session_factory,
            workspace_id=workspace_id,
            stream=stream,
            after_sequence=cursor,
            limit=256,
        )
        if batch:
            for event in batch:
                yield event
                cursor = event.semantic_sequence
            if idle_deadline is not None:
                idle_deadline = _utcnow() + timedelta(seconds=max_idle_seconds)
            continue
        if idle_deadline is not None and _utcnow() >= idle_deadline:
            return
        await asyncio.sleep(poll_interval)


async def get_progress(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    work_id: int,
) -> ProgressSnapshot | None:
    """The latest coalesced progress snapshot, if any tick landed."""
    from app.kernel.models import KernelProgress

    async with session_factory() as session:
        row = await session.get(
            KernelProgress, {"workspace_id": workspace_id, "work_id": work_id}
        )
    return _progress_view(row) if row is not None else None


# ---------------------------------------------------------------------------
# deterministic authority repair
# ---------------------------------------------------------------------------


async def reconcile_from_authority(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    stream: str = DEFAULT_STREAM,
) -> list[SemanticEvent]:
    """Repair semantic-log gaps by deriving missing events from the
    durable authorities (leases, publications) — never inventing history.

    A crash between an authority transition and its event append leaves
    the event absent while the transition itself stands committed. This
    pass walks the authorities in causal order (claim before accept),
    finds work identities lacking their ``work.claimed`` /
    ``work.accepted`` events, and appends the missing event carrying the
    authority's own facts plus ``repair: true``. Derived events state
    what the authority already proves; they never claim a transition
    that did not happen. Idempotent: a second run appends nothing."""

    from app.kernel.models import KernelEvent, KernelPublication, KernelWorkLease

    async with session_factory() as session:
        claimed_ids: set[int] = set()
        accepted_ids: set[int] = set()
        event_rows = (
            await session.execute(
                select(KernelEvent.event_type, KernelEvent.payload_json).where(
                    KernelEvent.workspace_id == workspace_id,
                    KernelEvent.stream == stream,
                    KernelEvent.event_type.in_([_EVENT_CLAIMED, _EVENT_ACCEPTED]),
                )
            )
        ).all()
        for event_type, payload_json in event_rows:
            target = accepted_ids if event_type == _EVENT_ACCEPTED else claimed_ids
            try:
                target.add(json.loads(payload_json).get("work_id"))
            except (ValueError, AttributeError):  # pragma: no cover
                continue

        lease_rows = (
            await session.execute(
                select(KernelWorkLease)
                .where(KernelWorkLease.workspace_id == workspace_id)
                .order_by(KernelWorkLease.work_id.asc())
            )
        ).scalars().all()
        publication_rows = (
            await session.execute(
                select(KernelPublication)
                .where(KernelPublication.workspace_id == workspace_id)
                .order_by(KernelPublication.work_id.asc())
            )
        ).scalars().all()

    repaired: list[SemanticEvent] = []
    for lease in lease_rows:
        if lease.work_id in claimed_ids:
            continue
        event = await append(
            session_factory,
            workspace_id=workspace_id,
            stream=stream,
            event_type=_EVENT_CLAIMED,
            payload={
                "work_id": lease.work_id,
                "owner_id": lease.owner_id,
                "fencing_token": lease.fencing_token,
                "state": lease.state,
                "repair": True,
            },
        )
        repaired.append(event)
        claimed_ids.add(lease.work_id)
    for publication in publication_rows:
        if publication.work_id in accepted_ids:
            continue
        event = await append(
            session_factory,
            workspace_id=workspace_id,
            stream=stream,
            event_type=_EVENT_ACCEPTED,
            payload={
                "work_id": publication.work_id,
                "publication_id": publication.publication_id,
                "result_hash": publication.result_hash,
                "fencing_token": publication.fencing_token,
                "owner_id": publication.owner_id,
                "repair": True,
            },
        )
        repaired.append(event)
        accepted_ids.add(publication.work_id)
    return repaired
