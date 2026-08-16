"""Transactional kernel outbox (V3.2 PR64).

Durable successor-work intent for accepted kernel commits. Rows are
created exclusively inside the commit transaction (see
:mod:`app.kernel.commit`) so commit and intent become visible together
or not at all. This module owns the read/claim/acknowledge surface.

Delivery semantics at this stage are honestly **at-least-once**:

* ``claim`` atomically moves one pending item to ``in_flight``;
* ``ack`` completes it; ``release`` returns a claimed item to pending
  and increments its attempt counter;
* after a process crash, ``reset_in_flight`` returns every stuck
  ``in_flight`` item to pending — redelivery is expected and consumers
  must be idempotent.

The PR66 fencing layer (:mod:`app.kernel.fencing`) builds the durable
authority boundary on top of this seam: only the current fenced
ownership generation may turn a result into the one accepted
publication, and fenced acknowledgement (``complete_work``) replaces
bare ``ack`` on the dispatch path. This module remains the honest
lower-level at-least-once surface it always was.

Identity: ``dedupe_key`` deterministically derives from the authorizing
commit and the intent content, so a retried commit protocol cannot
enqueue the same intent twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.errors import InvalidOutboxIntentError
from app.utils.canonical import (
    CanonicalValueError,
    canonical_json_str,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "OutboxIntent",
    "OutboxView",
    "OUTBOX_STATE_PENDING",
    "OUTBOX_STATE_IN_FLIGHT",
    "OUTBOX_STATE_DONE",
    "WORK_KIND_PATTERN",
    "ack",
    "claim",
    "compute_dedupe_key",
    "list_outbox",
    "release",
    "reset_in_flight",
]

OUTBOX_STATE_PENDING = "pending"
OUTBOX_STATE_IN_FLIGHT = "in_flight"
OUTBOX_STATE_DONE = "done"

WORK_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

#: Framing domain separating outbox dedupe keys from other kernel hashes.
_OUTBOX_FRAMING_RECORD_TYPE = "marker.kernel.outbox_intent.v1"
_OUTBOX_FRAMING_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class OutboxIntent:
    """Caller-declared successor work for one commit.

    ``work_kind`` names the future work class (e.g. ``materialize``,
    ``index``, ``publish``); ``payload`` is canonicalizable metadata the
    future consumer needs. Neither is dispatched by PR64.
    """

    work_kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class OutboxView:
    id: int
    workspace_id: str
    kernel_commit_id: int
    work_kind: str
    payload: dict
    dedupe_key: str
    state: str
    attempts: int
    created_at: str | None
    claimed_at: str | None
    completed_at: str | None


def validate_intent(intent: OutboxIntent) -> OutboxIntent:
    if not isinstance(intent.work_kind, str) or not WORK_KIND_PATTERN.match(
        intent.work_kind
    ):
        raise InvalidOutboxIntentError(
            f"invalid outbox work_kind: {intent.work_kind!r} must match "
            f"{WORK_KIND_PATTERN.pattern}"
        )
    try:
        canonical_json_str(to_json_ready(dict(intent.payload)))
    except CanonicalValueError as exc:
        raise InvalidOutboxIntentError(
            f"outbox intent {intent.work_kind!r} payload rejected: {exc}"
        ) from exc
    return intent


def intent_payload_json(intent: OutboxIntent) -> str:
    return canonical_json_str(to_json_ready(dict(intent.payload)))


def compute_dedupe_key(
    *, workspace_id: str, kernel_commit_id: int, work_kind: str, payload_json: str
) -> str:
    """Deterministic identity of one intent within one authorizing commit."""
    framed = {
        "workspace_id": workspace_id,
        "kernel_commit_id": kernel_commit_id,
        "work_kind": work_kind,
        "payload": json.loads(payload_json),
    }
    return record_identity_hash(
        record_type=_OUTBOX_FRAMING_RECORD_TYPE,
        schema_version=_OUTBOX_FRAMING_SCHEMA_VERSION,
        payload=framed,
    )


# ---------------------------------------------------------------------------
# read/claim/acknowledge surface (at-least-once)
# ---------------------------------------------------------------------------


def _view(row) -> OutboxView:
    return OutboxView(
        id=row.id,
        workspace_id=row.workspace_id,
        kernel_commit_id=row.kernel_commit_id,
        work_kind=row.work_kind,
        payload=json.loads(row.payload_json),
        dedupe_key=row.dedupe_key,
        state=row.state,
        attempts=row.attempts,
        created_at=row.created_at.isoformat() if row.created_at else None,
        claimed_at=row.claimed_at.isoformat() if row.claimed_at else None,
        completed_at=row.completed_at.isoformat() if row.completed_at else None,
    )


async def list_outbox(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str | None = None,
    state: str | None = None,
) -> list[OutboxView]:
    """Pending/claimed/done views, oldest first (audit order, not scheduling)."""
    from app.kernel.models import KernelOutbox

    stmt = select(KernelOutbox).order_by(KernelOutbox.id.asc())
    if workspace_id is not None:
        stmt = stmt.where(KernelOutbox.workspace_id == workspace_id)
    if state is not None:
        stmt = stmt.where(KernelOutbox.state == state)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [_view(row) for row in rows]


async def claim(session_factory: async_sessionmaker, work_id: int) -> OutboxView | None:
    """Atomically move one pending item to in_flight; None if not claimable."""
    from app.kernel.models import KernelOutbox

    async with session_factory() as session:
        async with session.begin():
            claimed = await _claim_in_session(session, work_id)
            if not claimed:
                return None
            row = await session.get(KernelOutbox, work_id)
            return _view(row)


async def _claim_in_session(session, work_id: int) -> bool:
    """Transactional claim core for callers holding an open write
    transaction (internal transactional API; the scheduler claims the
    delivery inside its serialized capacity/ownership decision)."""
    from app.kernel.models import KernelOutbox

    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(KernelOutbox)
        .where(KernelOutbox.id == work_id, KernelOutbox.state == OUTBOX_STATE_PENDING)
        .values(state=OUTBOX_STATE_IN_FLIGHT, claimed_at=now)
    )
    return result.rowcount == 1


async def ack(session_factory: async_sessionmaker, work_id: int) -> bool:
    """Complete one in_flight item; False if it was not in_flight."""
    from app.kernel.models import KernelOutbox

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(KernelOutbox)
                .where(
                    KernelOutbox.id == work_id,
                    KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT,
                )
                .values(
                    state=OUTBOX_STATE_DONE,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            return result.rowcount == 1


async def release(session_factory: async_sessionmaker, work_id: int) -> bool:
    """Return one in_flight item to pending, incrementing its attempts."""
    from app.kernel.models import KernelOutbox

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(KernelOutbox, work_id)
            if row is None or row.state != OUTBOX_STATE_IN_FLIGHT:
                return False
            row.state = OUTBOX_STATE_PENDING
            row.attempts += 1
            row.claimed_at = None
            return True


async def reset_in_flight(session_factory: async_sessionmaker) -> int:
    """After a crash/restart: return every in_flight item to pending.

    Honest at-least-once recovery — items may be redelivered. Returns
    the number of items reset.
    """
    from app.kernel.models import KernelOutbox

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(KernelOutbox)
                .where(KernelOutbox.state == OUTBOX_STATE_IN_FLIGHT)
                .values(state=OUTBOX_STATE_PENDING, claimed_at=None)
            )
            return result.rowcount
