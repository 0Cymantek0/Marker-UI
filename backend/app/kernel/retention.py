"""Durable retention roots and reader pins (V3.2 PR65B).

This module is the attachment contract between the kernel's garbage
collector and every current or future producer of retention
obligations. It answers exactly one question:

    What currently requires a kernel cut (and its payload bytes) to
    remain inspectable or replayable?

Two root families exist and the collector treats them identically:

* **Intrinsic roots** — the current generation of each workspace, read
  directly from ``kernel_generation_heads``. Structural, always live,
  never stored as rows.
* **Declared roots** — rows in ``kernel_retention_roots``. Today the
  contract offers ``generation_hold`` (protect one materialized
  generation) and ``snapshot_hold`` (protect a committed cut without a
  materialized generation). Future subsystems — jobs, reviews, cursors,
  exports, legal holds, claim proof closures, PublicationSets — attach
  by inserting rows with their own ``root_kind`` and producer context;
  the collector never needs to know them.

Reader pins (``kernel_reader_pins``) are bounded wall-clock leases over
one generation. An unexpired pin is an active root for that generation
and its required payload class, so collection cannot retire data
underneath a reader that validly acquired protection. A crashed
reader's pin lapses when the lease expires; expired rows are inert and
purged by collection. Safety across restart therefore never depends on
process memory.

Wall-clock use is deliberately confined to lease/hold expiry — the same
audit-metadata discipline as the outbox timestamps. Causal order of
kernel truth remains ``kernel_commit_id``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import validate_workspace_id
from app.kernel.errors import (
    RetentionContractError,
    UnknownGenerationError,
    UnknownReaderPinError,
    UnknownRetentionRootError,
)
from app.kernel.models import (
    KernelGeneration,
    KernelReaderPin,
    KernelRetentionRoot,
)
from app.kernel.snapshots import (
    PAYLOAD_REQUIREMENTS,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
)
from app.utils.canonical import canonical_json_str, record_identity_hash, to_json_ready

__all__ = [
    "DEFAULT_PIN_LEASE_SECONDS",
    "ReaderPinView",
    "RETENTION_ROOT_KINDS",
    "RETENTION_ROOT_RECORD_TYPE",
    "RETENTION_ROOT_SCHEMA_VERSION",
    "RetentionHoldView",
    "ROOT_KIND_GENERATION_HOLD",
    "ROOT_KIND_SNAPSHOT_HOLD",
    "ROOT_STATE_ACTIVE",
    "ROOT_STATE_RELEASED",
    "acquire_reader_pin",
    "active_reader_pins",
    "compute_hold_identity",
    "declare_hold",
    "get_hold",
    "list_holds",
    "purge_expired_pins",
    "release_hold",
    "release_reader_pin",
    "renew_reader_pin",
]

#: Framing domain separating retention-root identity from other hashes.
RETENTION_ROOT_RECORD_TYPE = "marker.kernel.retention_root.v1"
RETENTION_ROOT_SCHEMA_VERSION = "1.0.0"

ROOT_KIND_GENERATION_HOLD = "generation_hold"
ROOT_KIND_SNAPSHOT_HOLD = "snapshot_hold"
#: Kinds the declaration boundary validates today. The column itself is
#: open-ended: later PRs register new producers without touching GC.
RETENTION_ROOT_KINDS = frozenset({ROOT_KIND_GENERATION_HOLD, ROOT_KIND_SNAPSHOT_HOLD})

ROOT_STATE_ACTIVE = "active"
ROOT_STATE_RELEASED = "released"

#: Default reader lease. Long reads renew; a crashed reader's pin lapses
#: after this window, bounding how long orphaned protection delays GC.
DEFAULT_PIN_LEASE_SECONDS = 300.0


@dataclass(frozen=True)
class RetentionHoldView:
    """Read-side view of one declared retention root."""

    root_id: str
    workspace_id: str
    root_kind: str
    target_generation_id: str | None
    kernel_commit_id: int
    required_payload_state: str
    producer: Mapping
    state: str
    created_at: datetime | None
    expires_at: datetime | None

    @property
    def active(self) -> bool:
        """Active now: not released and not past an declared expiry."""
        if self.state != ROOT_STATE_ACTIVE:
            return False
        if self.expires_at is None:
            return True
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReaderPinView:
    """Read-side view of one reader pin (a bounded lease)."""

    pin_id: str
    generation_id: str
    workspace_id: str
    created_at: datetime | None
    expires_at: datetime

    @property
    def active(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def compute_hold_identity(
    *,
    workspace_id: str,
    root_kind: str,
    kernel_commit_id: int,
    target_generation_id: str | None = None,
    required_payload_state: str = PAYLOAD_REQUIREMENT_METADATA_ONLY,
    producer: Mapping | None = None,
) -> str:
    """Deterministic hold identity over the declared protection.

    Re-declaring the same protection resolves to the same root row —
    idempotent by construction, never a duplicate obligation.
    """
    return record_identity_hash(
        record_type=RETENTION_ROOT_RECORD_TYPE,
        schema_version=RETENTION_ROOT_SCHEMA_VERSION,
        payload={
            "workspace_id": workspace_id,
            "root_kind": root_kind,
            "kernel_commit_id": kernel_commit_id,
            "target_generation_id": target_generation_id,
            "required_payload_state": required_payload_state,
            "producer": to_json_ready(dict(producer or {})),
        },
    )


def _hold_view(row: KernelRetentionRoot) -> RetentionHoldView:
    try:
        producer = json.loads(row.producer_json)
    except ValueError:
        producer = {}
    return RetentionHoldView(
        root_id=row.root_id,
        workspace_id=row.workspace_id,
        root_kind=row.root_kind,
        target_generation_id=row.target_generation_id,
        kernel_commit_id=row.kernel_commit_id,
        required_payload_state=row.required_payload_state,
        producer=producer if isinstance(producer, dict) else {},
        state=row.state,
        created_at=_as_utc(row.created_at),
        expires_at=_as_utc(row.expires_at),
    )


def _pin_view(row: KernelReaderPin) -> ReaderPinView:
    return ReaderPinView(
        pin_id=row.pin_id,
        generation_id=row.generation_id,
        workspace_id=row.workspace_id,
        created_at=_as_utc(row.created_at),
        expires_at=_as_utc(row.expires_at) or _utcnow(),
    )


# ---------------------------------------------------------------------------
# declared retention holds
# ---------------------------------------------------------------------------


async def declare_hold(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str,
    root_kind: str,
    kernel_commit_id: int,
    target_generation_id: str | None = None,
    required_payload_state: str = PAYLOAD_REQUIREMENT_METADATA_ONLY,
    expires_at: datetime | None = None,
    producer: Mapping | None = None,
) -> RetentionHoldView:
    """Declare (or idempotently re-affirm) one durable retention hold.

    ``generation_hold`` requires ``target_generation_id`` and the cut is
    taken from that generation's manifest row (the caller's
    ``kernel_commit_id`` must agree). ``snapshot_hold`` protects the
    given cut directly and takes no target. Re-declaring a released or
    expired hold with the same identity re-activates it with the new
    expiry — a hold is a standing obligation, not an event log.
    """
    validate_workspace_id(workspace_id)
    if root_kind not in RETENTION_ROOT_KINDS:
        raise RetentionContractError(
            f"unknown root kind {root_kind!r}; allowed: {sorted(RETENTION_ROOT_KINDS)}"
        )
    if required_payload_state not in PAYLOAD_REQUIREMENTS:
        raise RetentionContractError(
            f"unknown payload requirement {required_payload_state!r}; "
            f"allowed: {sorted(PAYLOAD_REQUIREMENTS)}"
        )
    if expires_at is not None and _as_utc(expires_at) <= _utcnow():
        raise RetentionContractError("expires_at must be in the future")
    producer_json = canonical_json_str(to_json_ready(dict(producer or {})))

    cut = kernel_commit_id
    async with session_factory() as session:
        if root_kind == ROOT_KIND_GENERATION_HOLD:
            if target_generation_id is None:
                raise RetentionContractError(
                    "generation_hold requires target_generation_id"
                )
            gen = await session.get(KernelGeneration, target_generation_id)
            if gen is None:
                raise UnknownGenerationError(
                    f"generation={target_generation_id}: no such generation"
                )
            if gen.workspace_id != workspace_id:
                raise RetentionContractError(
                    f"generation={target_generation_id}: belongs to workspace "
                    f"{gen.workspace_id!r}, not {workspace_id!r}"
                )
            if gen.kernel_commit_id != kernel_commit_id:
                raise RetentionContractError(
                    f"generation={target_generation_id}: cut is "
                    f"{gen.kernel_commit_id}, not {kernel_commit_id}"
                )
        elif target_generation_id is not None:
            raise RetentionContractError(
                "snapshot_hold protects a cut directly; target_generation_id "
                "is not part of its identity"
            )

    root_id = compute_hold_identity(
        workspace_id=workspace_id,
        root_kind=root_kind,
        kernel_commit_id=cut,
        target_generation_id=target_generation_id,
        required_payload_state=required_payload_state,
        producer=producer,
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                sqlite_insert(KernelRetentionRoot)
                .values(
                    root_id=root_id,
                    workspace_id=workspace_id,
                    root_kind=root_kind,
                    target_generation_id=target_generation_id,
                    kernel_commit_id=cut,
                    required_payload_state=required_payload_state,
                    producer_json=producer_json,
                    state=ROOT_STATE_ACTIVE,
                    created_at=_utcnow(),
                    expires_at=_as_utc(expires_at),
                )
                .on_conflict_do_nothing(index_elements=[KernelRetentionRoot.root_id])
            )
            # Re-declaration re-activates a released/expired hold and
            # refreshes its expiry without duplicating the obligation.
            existing = await session.get(KernelRetentionRoot, root_id)
            assert existing is not None
            if existing.state != ROOT_STATE_ACTIVE or existing.expires_at != _as_utc(
                expires_at
            ):
                existing.state = ROOT_STATE_ACTIVE
                existing.expires_at = _as_utc(expires_at)
                session.add(existing)
        row = await session.get(KernelRetentionRoot, root_id)
    assert row is not None
    return _hold_view(row)


async def release_hold(session_factory: async_sessionmaker, root_id: str) -> bool:
    """Release one hold; returns False when it was already released.

    The row is kept (state ``released``) as retention-history evidence;
    release stops protecting data, it never deletes anything.
    """
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(KernelRetentionRoot, root_id)
            if row is None:
                raise UnknownRetentionRootError(
                    f"retention root {root_id!r}: no such root"
                )
            if row.state == ROOT_STATE_RELEASED:
                return False
            row.state = ROOT_STATE_RELEASED
            session.add(row)
    return True


async def get_hold(
    session_factory: async_sessionmaker, root_id: str
) -> RetentionHoldView | None:
    async with session_factory() as session:
        row = await session.get(KernelRetentionRoot, root_id)
    return None if row is None else _hold_view(row)


async def list_holds(
    session_factory: async_sessionmaker,
    *,
    workspace_id: str | None = None,
    state: str | None = None,
) -> tuple[RetentionHoldView, ...]:
    stmt = select(KernelRetentionRoot).order_by(KernelRetentionRoot.created_at.asc())
    if workspace_id is not None:
        stmt = stmt.where(KernelRetentionRoot.workspace_id == workspace_id)
    if state is not None:
        stmt = stmt.where(KernelRetentionRoot.state == state)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return tuple(_hold_view(row) for row in rows)


# ---------------------------------------------------------------------------
# reader pins (bounded leases)
# ---------------------------------------------------------------------------


async def acquire_reader_pin(
    session_factory: async_sessionmaker,
    generation_id: str,
    *,
    lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS,
) -> ReaderPinView:
    """Acquire a durable read lease over one generation.

    The pin is an active retention root until it expires, is released,
    or is renewed. Long reads renew before the lease lapses; a reader
    that lets the lease lapse has no protection claim.
    """
    if lease_seconds <= 0:
        raise RetentionContractError("lease_seconds must be positive")
    now = _utcnow()
    expires = now + timedelta(seconds=lease_seconds)
    async with session_factory() as session:
        async with session.begin():
            gen = await session.get(KernelGeneration, generation_id)
            if gen is None:
                raise UnknownGenerationError(
                    f"generation={generation_id}: no such generation"
                )
            pin = KernelReaderPin(
                pin_id=str(uuid.uuid4()),
                generation_id=generation_id,
                workspace_id=gen.workspace_id,
                created_at=now,
                expires_at=expires,
            )
            session.add(pin)
    return _pin_view(pin)


async def renew_reader_pin(
    session_factory: async_sessionmaker,
    pin_id: str,
    *,
    lease_seconds: float = DEFAULT_PIN_LEASE_SECONDS,
) -> ReaderPinView:
    """Extend one pin's lease from now; expired pins cannot be revived."""
    if lease_seconds <= 0:
        raise RetentionContractError("lease_seconds must be positive")
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(KernelReaderPin, pin_id)
            if row is None or not _pin_view(row).active:
                raise UnknownReaderPinError(
                    f"reader pin {pin_id!r}: no such active pin (released, "
                    "purged, or the lease expired)"
                )
            row.expires_at = _utcnow() + timedelta(seconds=lease_seconds)
            session.add(row)
        refreshed = await session.get(KernelReaderPin, pin_id)
    assert refreshed is not None
    return _pin_view(refreshed)


async def release_reader_pin(session_factory: async_sessionmaker, pin_id: str) -> bool:
    """Release one pin (row deleted); False when it no longer exists."""
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(KernelReaderPin).where(KernelReaderPin.pin_id == pin_id)
            )
            released = result.rowcount == 1
    return released


async def active_reader_pins(
    session_factory: async_sessionmaker,
    *,
    generation_id: str | None = None,
) -> tuple[ReaderPinView, ...]:
    """All currently active (unexpired) pins, optionally for one generation."""
    stmt = select(KernelReaderPin).where(
        KernelReaderPin.expires_at > _utcnow()
    )
    if generation_id is not None:
        stmt = stmt.where(KernelReaderPin.generation_id == generation_id)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return tuple(_pin_view(row) for row in rows)


async def purge_expired_pins(session_factory: async_sessionmaker) -> int:
    """Delete lapsed pin rows; returns how many were purged.

    Called by collection; lapsed pins are inert either way — this only
    keeps the table bounded.
    """
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(KernelReaderPin).where(
                    KernelReaderPin.expires_at <= _utcnow()
                )
            )
            purged = result.rowcount
    return purged
