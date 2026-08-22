"""Connector stream checkpoint + inbox effects inside the kernel commit
(PR71B, amendment 16B.7).

This module is the checked-and-applied-in-transaction contract the
single kernel commit authority (:mod:`app.kernel.commit`) uses for
connector convergence batches, mirroring the PR73 view-advancement
seam: the batch carries a :class:`ConnectorEffects` bundle, the commit
transaction validates it against current durable stream state under the
writer lock it already holds (step 2.75), applies the inbox rows and the
conditional cursor flip right before the head advance (step 5.8), and
the database commit makes source truth, successor-work intent, receipt
evidence, and the checkpoint visible atomically — or none of them.

Fixed properties enforced here:

* the durable cursor only ever names provider progress whose consuming
  state was accepted in the same database transaction;
* a cursor advancement is conditional on the exact previously-applied
  token (compare-and-set); a stale advancement rolls the whole commit
  back rather than forking connector truth;
* provider event identity is deduplicated durably: a batch carrying an
  already-recorded event is refused before any insert, so redelivery
  can only converge, never duplicate;
* a stream awaiting reconciliation refuses normal checkpoint movement —
  only reconciliation itself (page-wise restartable progress, or the
  final completion transition) may move that cursor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import select, update

from app.kernel.errors import (
    ConnectorStreamStateError,
    DuplicateConnectorEventError,
    InvalidConnectorEffectsError,
    StaleCursorError,
)
from app.kernel.models import KernelConnectorInbox, KernelConnectorStream
from app.utils.canonical import canonical_json_str, to_json_ready

__all__ = [
    "CONNECTOR_STREAM_CONSUMING",
    "CONNECTOR_STREAM_RECONCILIATION_REQUIRED",
    "CONNECTOR_STREAM_STATES",
    "CONNECTOR_APPLIED_STATES",
    "CONNECTOR_EVENT_KINDS",
    "ConnectorInboxEntry",
    "ConnectorCursorAdvancement",
    "ConnectorEffects",
    "ConnectorFlip",
    "check_connector_effects",
    "apply_connector_effects",
]

CONNECTOR_STREAM_CONSUMING = "consuming"
CONNECTOR_STREAM_RECONCILIATION_REQUIRED = "reconciliation_required"
CONNECTOR_STREAM_STATES = frozenset(
    {CONNECTOR_STREAM_CONSUMING, CONNECTOR_STREAM_RECONCILIATION_REQUIRED}
)

#: Application classification vocabulary for one provider event receipt.
CONNECTOR_APPLIED_APPLIED = "applied"
CONNECTOR_APPLIED_DUPLICATE = "duplicate"
CONNECTOR_APPLIED_STALE = "stale"
CONNECTOR_APPLIED_DEFERRED = "deferred_reconciliation"
CONNECTOR_APPLIED_REJECTED = "rejected"
CONNECTOR_APPLIED_STATES = frozenset(
    {
        CONNECTOR_APPLIED_APPLIED,
        CONNECTOR_APPLIED_DUPLICATE,
        CONNECTOR_APPLIED_STALE,
        CONNECTOR_APPLIED_DEFERRED,
        CONNECTOR_APPLIED_REJECTED,
    }
)

#: Provider-neutral change vocabulary. Provider adapters map their
#: native change shapes onto these classes; the shared core never sees
#: vendor pagination or notification vocabulary.
CONNECTOR_EVENT_CONTENT_CHANGED = "content_changed"
CONNECTOR_EVENT_POLICY_CHANGED = "policy_changed"
CONNECTOR_EVENT_REMOVED = "removed"
CONNECTOR_EVENT_RESTORED = "restored"
CONNECTOR_EVENT_MOVED = "moved"
CONNECTOR_EVENT_KINDS = frozenset(
    {
        CONNECTOR_EVENT_CONTENT_CHANGED,
        CONNECTOR_EVENT_POLICY_CHANGED,
        CONNECTOR_EVENT_REMOVED,
        CONNECTOR_EVENT_RESTORED,
        CONNECTOR_EVENT_MOVED,
    }
)

STREAM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,127}$")
PROVIDER_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
PROVIDER_ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
#: ``""`` is the fresh-stream checkpoint: no provider progress consumed.
CURSOR_TOKEN_PATTERN = re.compile(r"^$|^[A-Za-z0-9][A-Za-z0-9._:=+-]{0,511}$")


@dataclass(frozen=True)
class ConnectorInboxEntry:
    """Receipt + classification for one provider event in this commit."""

    provider_event_id: str
    event_kind: str
    applied_state: str
    provider_item_id: str | None = None
    provider_revision: str | None = None
    provider_seq: int | None = None
    result: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.provider_event_id, str) or not PROVIDER_EVENT_ID_PATTERN.match(
            self.provider_event_id
        ):
            raise InvalidConnectorEffectsError(
                f"invalid provider_event_id: {self.provider_event_id!r}"
            )
        if self.event_kind not in CONNECTOR_EVENT_KINDS:
            raise InvalidConnectorEffectsError(
                f"invalid event_kind: {self.event_kind!r}; allowed: "
                f"{sorted(CONNECTOR_EVENT_KINDS)}"
            )
        if self.applied_state not in CONNECTOR_APPLIED_STATES:
            raise InvalidConnectorEffectsError(
                f"invalid applied_state: {self.applied_state!r}; allowed: "
                f"{sorted(CONNECTOR_APPLIED_STATES)}"
            )
        if self.provider_item_id is not None and (
            not isinstance(self.provider_item_id, str)
            or not PROVIDER_ITEM_ID_PATTERN.match(self.provider_item_id)
        ):
            raise InvalidConnectorEffectsError(
                f"invalid provider_item_id: {self.provider_item_id!r}"
            )
        if self.provider_revision is not None and (
            not isinstance(self.provider_revision, str) or not self.provider_revision
        ):
            raise InvalidConnectorEffectsError(
                f"invalid provider_revision: {self.provider_revision!r}"
            )
        if self.provider_seq is not None and (
            not isinstance(self.provider_seq, int)
            or isinstance(self.provider_seq, bool)
            or self.provider_seq < 0
        ):
            raise InvalidConnectorEffectsError(
                f"invalid provider_seq: {self.provider_seq!r}"
            )
        if self.event_kind == CONNECTOR_EVENT_MOVED and not self.provider_item_id:
            raise InvalidConnectorEffectsError(
                f"moved event {self.provider_event_id!r} requires provider_item_id"
            )


@dataclass(frozen=True)
class ConnectorCursorAdvancement:
    """Conditional checkpoint movement for one connector stream.

    ``expected_cursor_token`` is the exact currently-durably-applied
    token the advancement is based on (``None`` only when creating the
    stream row). The kernel applies the movement as a compare-and-set:
    anything else is a :class:`StaleCursorError` that rolls the whole
    commit back.

    ``new_state``/``reconciliation_reason`` carry the stream health
    transition. ``completes_reconciliation`` is the only way a
    reconciliation-required stream may return to ``consuming``: it names
    the transition as the durable completion of a reconciliation scan,
    not an ordinary poll advancement.
    """

    expected_cursor_token: str | None
    new_cursor_token: str
    new_state: str = CONNECTOR_STREAM_CONSUMING
    new_cursor_seq: int | None = None
    reconciliation_reason: str | None = None
    completes_reconciliation: bool = False

    def __post_init__(self) -> None:
        if self.expected_cursor_token is not None and (
            not isinstance(self.expected_cursor_token, str)
            or not CURSOR_TOKEN_PATTERN.match(self.expected_cursor_token)
        ):
            raise InvalidConnectorEffectsError(
                f"invalid expected_cursor_token: {self.expected_cursor_token!r}"
            )
        if (
            not isinstance(self.new_cursor_token, str)
            or not CURSOR_TOKEN_PATTERN.match(self.new_cursor_token)
        ):
            raise InvalidConnectorEffectsError(
                f"invalid new_cursor_token: {self.new_cursor_token!r}"
            )
        if self.new_state not in CONNECTOR_STREAM_STATES:
            raise InvalidConnectorEffectsError(
                f"invalid new_state: {self.new_state!r}; allowed: "
                f"{sorted(CONNECTOR_STREAM_STATES)}"
            )
        if self.new_cursor_seq is not None and (
            not isinstance(self.new_cursor_seq, int)
            or isinstance(self.new_cursor_seq, bool)
            or self.new_cursor_seq < 0
        ):
            raise InvalidConnectorEffectsError(
                f"invalid new_cursor_seq: {self.new_cursor_seq!r}"
            )
        if self.new_state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED:
            if not isinstance(self.reconciliation_reason, str) or not (
                self.reconciliation_reason.strip()
            ):
                raise InvalidConnectorEffectsError(
                    "entering reconciliation_required requires a reason"
                )
        elif self.reconciliation_reason is not None:
            raise InvalidConnectorEffectsError(
                "reconciliation_reason is only valid while entering "
                "reconciliation_required"
            )


@dataclass(frozen=True)
class ConnectorEffects:
    """One connector stream's durable effects for one kernel commit."""

    workspace_id: str
    stream_id: str
    inbox: tuple[ConnectorInboxEntry, ...] = ()
    cursor: ConnectorCursorAdvancement | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, str) or not STREAM_ID_PATTERN.match(
            self.stream_id
        ):
            raise InvalidConnectorEffectsError(
                f"invalid stream_id: {self.stream_id!r} must match "
                f"{STREAM_ID_PATTERN.pattern}"
            )
        if self.cursor is None and not self.inbox:
            raise InvalidConnectorEffectsError(
                "connector effects carry neither inbox rows nor a cursor advancement"
            )
        seen: set[str] = set()
        for entry in self.inbox:
            if entry.provider_event_id in seen:
                raise InvalidConnectorEffectsError(
                    f"batch carries provider event {entry.provider_event_id!r} twice"
                )
            seen.add(entry.provider_event_id)


@dataclass(frozen=True)
class ConnectorFlip:
    """Durable stream-row mutation plan validated under the writer lock."""

    kind: str  # "insert" | "update"
    workspace_id: str
    stream_id: str
    expected_cursor_token: str | None
    new_cursor_token: str
    new_cursor_seq: int | None
    new_state: str
    reconciliation_reason: str | None


async def check_connector_effects(
    session,
    *,
    workspace_id: str,
    effects: ConnectorEffects,
) -> ConnectorFlip:
    """Validate connector effects against current durable stream state.

    Runs inside the kernel commit transaction, after the head row lock
    is held: the pre-checks below and any concurrent stream movement
    cannot interleave. A violation raises a typed conflict that rolls
    the whole batch back — all-or-conflict, no partial application.
    """
    if effects.workspace_id != workspace_id:
        raise InvalidConnectorEffectsError(
            f"connector effects workspace {effects.workspace_id!r} does not "
            f"match the committing workspace {workspace_id!r}"
        )

    row = await session.get(KernelConnectorStream, effects.stream_id)

    cursor = effects.cursor
    if cursor is not None:
        if row is None:
            if cursor.expected_cursor_token is not None:
                raise StaleCursorError(
                    expected_cursor_token=cursor.expected_cursor_token,
                    observed_cursor=None,
                )
            if cursor.new_state != CONNECTOR_STREAM_CONSUMING:
                raise ConnectorStreamStateError(
                    f"stream {effects.stream_id!r} does not exist; only its "
                    "creation (state consuming) may initialize it"
                )
            flip = ConnectorFlip(
                kind="insert",
                workspace_id=workspace_id,
                stream_id=effects.stream_id,
                expected_cursor_token=None,
                new_cursor_token=cursor.new_cursor_token,
                new_cursor_seq=cursor.new_cursor_seq,
                new_state=cursor.new_state,
                reconciliation_reason=None,
            )
        else:
            if row.workspace_id != workspace_id:
                raise InvalidConnectorEffectsError(
                    f"stream {effects.stream_id!r} belongs to workspace "
                    f"{row.workspace_id!r}, not {workspace_id!r}"
                )
            if row.cursor_token != (cursor.expected_cursor_token or ""):
                raise StaleCursorError(
                    expected_cursor_token=cursor.expected_cursor_token,
                    observed_cursor=row.cursor_token,
                )
            if (
                row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
                and cursor.new_state == CONNECTOR_STREAM_CONSUMING
                and not cursor.completes_reconciliation
            ):
                raise ConnectorStreamStateError(
                    f"stream {effects.stream_id!r} awaits reconciliation; a "
                    "normal checkpoint advancement cannot leave that state"
                )
            if (
                row.state == CONNECTOR_STREAM_CONSUMING
                and cursor.new_state == CONNECTOR_STREAM_CONSUMING
                and cursor.completes_reconciliation
            ):
                raise ConnectorStreamStateError(
                    f"stream {effects.stream_id!r} is consuming; "
                    "completes_reconciliation names a reconciliation exit, "
                    "not an ordinary advancement"
                )
            flip = ConnectorFlip(
                kind="update",
                workspace_id=workspace_id,
                stream_id=effects.stream_id,
                expected_cursor_token=cursor.expected_cursor_token,
                new_cursor_token=cursor.new_cursor_token,
                new_cursor_seq=cursor.new_cursor_seq,
                new_state=cursor.new_state,
                reconciliation_reason=cursor.reconciliation_reason,
            )
    else:
        if row is None:
            raise InvalidConnectorEffectsError(
                f"inbox-only effects reference stream {effects.stream_id!r} "
                "which does not exist; create it with a cursor advancement first"
            )
        if row.workspace_id != workspace_id:
            raise InvalidConnectorEffectsError(
                f"stream {effects.stream_id!r} belongs to workspace "
                f"{row.workspace_id!r}, not {workspace_id!r}"
            )
        flip = ConnectorFlip(
            kind="none",
            workspace_id=workspace_id,
            stream_id=effects.stream_id,
            expected_cursor_token=row.cursor_token,
            new_cursor_token=row.cursor_token,
            new_cursor_seq=row.cursor_seq,
            new_state=row.state,
            reconciliation_reason=None,
        )

    # Durable event dedupe: a provider event recorded once can never be
    # semantically applied by a second commit — refuse the whole batch
    # before anything is inserted.
    if effects.inbox:
        event_ids = [entry.provider_event_id for entry in effects.inbox]
        existing = (
            (
                await session.execute(
                    select(KernelConnectorInbox.provider_event_id).where(
                        KernelConnectorInbox.stream_id == effects.stream_id,
                        KernelConnectorInbox.provider_event_id.in_(event_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            raise DuplicateConnectorEventError(
                f"stream {effects.stream_id!r} already durably recorded provider "
                f"event(s): {sorted(existing)}"
            )
    return flip


async def apply_connector_effects(
    session,
    *,
    flip: ConnectorFlip,
    effects: ConnectorEffects,
    next_commit_id: int,
) -> None:
    """Insert inbox rows and flip the stream row inside this commit.

    Both under the writer lock the commit already holds and validated by
    :func:`check_connector_effects` in the same transaction, so the
    compare-and-set below cannot lose an update silently.
    """
    from datetime import datetime, timezone

    for entry in effects.inbox:
        session.add(
            KernelConnectorInbox(
                workspace_id=effects.workspace_id,
                stream_id=effects.stream_id,
                provider_event_id=entry.provider_event_id,
                event_kind=entry.event_kind,
                provider_item_id=entry.provider_item_id,
                provider_revision=entry.provider_revision,
                provider_seq=entry.provider_seq,
                applied_state=entry.applied_state,
                applied_kernel_commit_id=next_commit_id,
                result_json=canonical_json_str(to_json_ready(dict(entry.result))),
            )
        )

    if flip.kind == "insert":
        session.add(
            KernelConnectorStream(
                stream_id=effects.stream_id,
                workspace_id=flip.workspace_id,
                cursor_token=flip.new_cursor_token,
                cursor_seq=flip.new_cursor_seq,
                state=flip.new_state,
                reconciliation_reason=flip.reconciliation_reason,
                applied_kernel_commit_id=next_commit_id,
            )
        )
    elif flip.kind == "update":
        conditions = [KernelConnectorStream.stream_id == flip.stream_id]
        if flip.expected_cursor_token is not None:
            conditions.append(
                KernelConnectorStream.cursor_token == flip.expected_cursor_token
            )
        result = await session.execute(
            update(KernelConnectorStream)
            .where(*conditions)
            .values(
                cursor_token=flip.new_cursor_token,
                cursor_seq=flip.new_cursor_seq,
                state=flip.new_state,
                reconciliation_reason=flip.reconciliation_reason,
                applied_kernel_commit_id=next_commit_id,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount != 1:
            raise StaleCursorError(
                expected_cursor_token=flip.expected_cursor_token,
                observed_cursor=None,
            )
