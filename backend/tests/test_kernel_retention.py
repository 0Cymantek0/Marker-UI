"""Retention contract tests (V3.2 PR65B): declared holds and reader pins.

Proves the attachment contract a skeptical GC can rely on: hold identity
is deterministic and idempotent, release/expiry stop protection without
deleting anything, generation holds validate their target, reader pins
are bounded leases with honest renewal/lapse semantics, and protection
is visible to a fresh process (no process memory).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    RetentionContractError,
    UnknownGenerationError,
    UnknownReaderPinError,
    UnknownRetentionRootError,
)
from app.kernel.generations import GenerationService
from app.kernel.records import ClaimAssertionRecord
from app.kernel.retention import (
    DEFAULT_PIN_LEASE_SECONDS,
    ROOT_KIND_GENERATION_HOLD,
    ROOT_KIND_SNAPSHOT_HOLD,
    ROOT_STATE_RELEASED,
    acquire_reader_pin,
    active_reader_pins,
    compute_hold_identity,
    declare_hold,
    get_hold,
    list_holds,
    purge_expired_pins,
    release_hold,
    release_reader_pin,
    renew_reader_pin,
)
from app.kernel.snapshots import (
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
    resolve_snapshot,
)

pytestmark = pytest.mark.asyncio


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


async def _committed_cut(factory: async_sessionmaker, service: KernelCommitService) -> int:
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                ClaimAssertionRecord(
                    claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
        )
    )
    from app.kernel.replay import read_head

    return await read_head(factory, "ws-a")


async def _active_generation(factory: async_sessionmaker) -> str:
    gen_service = GenerationService(factory)
    ref = await gen_service.build_and_activate(await resolve_snapshot(factory, "ws-a"))
    return ref.generation_id


# ---------------------------------------------------------------------------
# snapshot holds
# ---------------------------------------------------------------------------


async def test_snapshot_hold_declare_is_idempotent_and_active(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    cut = await _committed_cut(factory, service)

    hold = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        producer={"subsystem": "legal", "matter": "m-1"},
    )
    assert hold.active
    assert hold.kernel_commit_id == cut
    assert hold.required_payload_state == PAYLOAD_REQUIREMENT_INSPECTABLE
    assert hold.producer == {"subsystem": "legal", "matter": "m-1"}

    again = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        producer={"subsystem": "legal", "matter": "m-1"},
    )
    assert again.root_id == hold.root_id  # deterministic identity
    assert len(await list_holds(factory, workspace_id="ws-a")) == 1

    # identity is over the declared protection: a different producer
    # context is a different root
    other = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        producer={"subsystem": "export"},
    )
    assert other.root_id != hold.root_id


async def test_hold_identity_is_pure(payload_env: tuple) -> None:
    a = compute_hold_identity(
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=3,
        required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY,
    )
    b = compute_hold_identity(
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=3,
        required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY,
    )
    c = compute_hold_identity(
        workspace_id="ws-b",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=3,
        required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY,
    )
    assert a == b and a != c


async def test_hold_release_keeps_row_and_stops_protection(payload_env: tuple) -> None:
    factory, store, service = payload_env
    cut = await _committed_cut(factory, service)
    hold = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
    )

    assert await release_hold(factory, hold.root_id) is True
    view = await get_hold(factory, hold.root_id)
    assert view is not None  # history kept
    assert view.state == ROOT_STATE_RELEASED
    assert not view.active
    assert await release_hold(factory, hold.root_id) is False  # idempotent

    with pytest.raises(UnknownRetentionRootError):
        await release_hold(factory, "sha256:" + "0" * 64)

    # re-declaring the same protection re-activates the standing hold
    revived = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
    )
    assert revived.root_id == hold.root_id
    assert revived.active


async def test_hold_contract_validation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    cut = await _committed_cut(factory, service)

    with pytest.raises(RetentionContractError):  # unknown kind
        await declare_hold(
            factory, workspace_id="ws-a", root_kind="legal_hold", kernel_commit_id=cut
        )
    with pytest.raises(RetentionContractError):  # unknown payload class
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=cut,
            required_payload_state="auditable",
        )
    with pytest.raises(RetentionContractError):  # expiry in the past
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=cut,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    with pytest.raises(RetentionContractError):  # snapshot hold takes no target
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=cut,
            target_generation_id="sha256:" + "1" * 64,
        )
    with pytest.raises(UnknownGenerationError):  # generation hold needs a real target
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_GENERATION_HOLD,
            kernel_commit_id=cut,
            target_generation_id="sha256:" + "2" * 64,
        )
    assert await list_holds(factory) == ()


# ---------------------------------------------------------------------------
# generation holds
# ---------------------------------------------------------------------------


async def test_generation_hold_validates_target_cut_and_workspace(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _committed_cut(factory, service)
    generation_id = await _active_generation(factory)

    hold = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_GENERATION_HOLD,
        kernel_commit_id=1,
        target_generation_id=generation_id,
    )
    assert hold.active
    assert hold.target_generation_id == generation_id

    with pytest.raises(RetentionContractError):  # wrong cut
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_GENERATION_HOLD,
            kernel_commit_id=2,
            target_generation_id=generation_id,
        )
    with pytest.raises(RetentionContractError):  # wrong workspace
        await declare_hold(
            factory,
            workspace_id="ws-b",
            root_kind=ROOT_KIND_GENERATION_HOLD,
            kernel_commit_id=1,
            target_generation_id=generation_id,
        )
    with pytest.raises(RetentionContractError):  # missing target
        await declare_hold(
            factory,
            workspace_id="ws-a",
            root_kind=ROOT_KIND_GENERATION_HOLD,
            kernel_commit_id=1,
        )


async def test_expired_hold_is_inert_not_destructive(payload_env: tuple) -> None:
    """Expiry only stops protection; it never deletes anything and a
    lapsed hold is simply no longer an active root."""
    factory, store, service = payload_env
    cut = await _committed_cut(factory, service)
    hold = await declare_hold(
        factory,
        workspace_id="ws-a",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=cut,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert hold.active

    # simulate the window passing: expiry is a wall-clock fact (written
    # in the SQLite datetime storage format the dialect uses)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(
        tzinfo=None
    )
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_retention_roots SET expires_at = ? WHERE root_id = ?",
            (expired.isoformat(sep=" "), hold.root_id),
        )
        conn.commit()
    lapsed = await get_hold(factory, hold.root_id)
    assert lapsed is not None and not lapsed.active
    # the row itself survives as retention history; the state column
    # records the declaration, effective activity is computed
    assert all(not h.active for h in await list_holds(factory))


# ---------------------------------------------------------------------------
# reader pins
# ---------------------------------------------------------------------------


async def test_reader_pin_lifecycle(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _committed_cut(factory, service)
    generation_id = await _active_generation(factory)

    pin = await acquire_reader_pin(factory, generation_id, lease_seconds=60)
    assert pin.active
    assert pin.generation_id == generation_id
    assert len(await active_reader_pins(factory, generation_id=generation_id)) == 1

    renewed = await renew_reader_pin(factory, pin.pin_id, lease_seconds=120)
    assert renewed.pin_id == pin.pin_id
    assert renewed.expires_at > pin.expires_at

    assert await release_reader_pin(factory, pin.pin_id) is True
    assert await active_reader_pins(factory, generation_id=generation_id) == ()
    assert await release_reader_pin(factory, pin.pin_id) is False  # idempotent
    with pytest.raises(UnknownReaderPinError):
        await renew_reader_pin(factory, pin.pin_id)


async def test_pin_for_unknown_generation_rejected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    with pytest.raises(UnknownGenerationError):
        await acquire_reader_pin(factory, "sha256:" + "3" * 64)
    with pytest.raises(RetentionContractError):
        await acquire_reader_pin(
            factory, "sha256:" + "3" * 64, lease_seconds=0
        )


async def test_expired_pin_lapses_and_is_purged(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _committed_cut(factory, service)
    generation_id = await _active_generation(factory)

    pin = await acquire_reader_pin(
        factory, generation_id, lease_seconds=DEFAULT_PIN_LEASE_SECONDS
    )
    assert pin.active
    # simulate the lease lapsing (a crashed reader never releases)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(tzinfo=None)
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_reader_pins SET expires_at = ? WHERE pin_id = ?",
            (past.isoformat(sep=" "), pin.pin_id),
        )
        conn.commit()
    lapsed = (await active_reader_pins(factory, generation_id=generation_id))
    assert lapsed == ()
    with pytest.raises(UnknownReaderPinError):  # a lapsed pin cannot be revived
        await renew_reader_pin(factory, pin.pin_id)

    assert await purge_expired_pins(factory) == 1  # inert rows are bounded
    assert await purge_expired_pins(factory) == 0


async def test_pins_survive_restart_from_durable_state(payload_env: tuple) -> None:
    """Protection must not depend on process memory: a fresh engine over
    the same database still sees the active pin (matrix 23)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    factory, store, service = payload_env
    await _committed_cut(factory, service)
    generation_id = await _active_generation(factory)
    pin = await acquire_reader_pin(factory, generation_id, lease_seconds=3600)

    url = f"sqlite+aiosqlite:///{_db_path(factory).as_posix()}"
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    fresh = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    seen = await active_reader_pins(fresh, generation_id=generation_id)
    assert [p.pin_id for p in seen] == [pin.pin_id]
    await engine2.dispose()
