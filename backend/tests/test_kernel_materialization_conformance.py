"""Dual-backend materialization/retention conformance (PR83B1 Gate 4).

Materialized generations and reader-retention metadata — build, verify,
activate, pin, hold — run against both first-class database profiles
through the same public services. The activation linearization point
(one conditional pointer move per workspace) and pin/hold expiry rules
are exactly the invariants SQLite's single-writer model used to make
trivially true; here they are exercised on real PostgreSQL
concurrency, with raw durable-state audits for the head/pin tables.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel import retention
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import (
    GENERATION_STATE_ACTIVE,
    GENERATION_STATE_SUPERSEDED,
    PHASE_GEN_BUILD_BEGIN,
    PHASE_GEN_RECORDS_MATERIALIZED,
    PHASE_GEN_SOURCE_READ,
    PHASE_GEN_STAGED,
    GenerationReader,
    GenerationService,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.models import (
    KernelGeneration,
    KernelGenerationHead,
    KernelGenerationRecord,
    KernelReaderPin,
    KernelRetentionRoot,
)
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ClaimAssertionRecord, ObservationRecord
from app.kernel.retention import ROOT_KIND_GENERATION_HOLD
from app.kernel.snapshots import resolve_snapshot
from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


def _observation(tag: str, payload: bytes | None = None) -> ObservationRecord:
    return ObservationRecord(
        observer="obs", derivation={"tag": tag}, payload_bytes=payload
    )


async def _seed_two_cuts(service: KernelCommitService) -> None:
    """Two commits: the second advances the workspace cut."""
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a1"),))
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a2"),))
    )


@pytest_asyncio.fixture(params=BACKENDS, ids=BACKENDS)
async def mat_env(request, tmp_path: pathlib.Path):
    backend = request.param
    async with provisioned_database(
        backend, (tmp_path / "kernel.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        engine = create_async_engine(prov.url, **engine_kwargs_for(backend))
        assert engine.dialect.name == backend
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = LocalPayloadStore(tmp_path / "payloads")
        service = KernelCommitService(factory, payload_store=store)
        try:
            yield {
                "backend": backend,
                "url": prov.url,
                "engine": engine,
                "factory": factory,
                "store": store,
                "service": service,
            }
        finally:
            await engine.dispose()


def _new_engine(env):
    engine = create_async_engine(env["url"], **engine_kwargs_for(env["backend"]))
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )


def _hold_kwargs(gen, producer: str):
    return {
        "workspace_id": "ws-a",
        "root_kind": ROOT_KIND_GENERATION_HOLD,
        "kernel_commit_id": gen.kernel_commit_id,
        "target_generation_id": gen.generation_id,
        "producer": {"suite": "materialization-conformance", "case": producer},
    }


# ---------------------------------------------------------------------------
# X-G01/X-G02: deterministic identity, build-crash atomicity
# ---------------------------------------------------------------------------


async def test_generation_identity_deterministic_across_reopen(mat_env) -> None:
    env = mat_env
    await _seed_two_cuts(env["service"])
    gen_service = GenerationService(env["factory"])
    snapshot = await resolve_snapshot(env["factory"], "ws-a")
    first = await gen_service.build_and_activate(snapshot)

    engine2, factory2 = _new_engine(env)
    try:
        rebuilt = await GenerationService(factory2).build(
            await resolve_snapshot(factory2, "ws-a")
        )
    finally:
        await engine2.dispose()
    assert rebuilt.generation_id == first.generation_id
    assert rebuilt.content_digest == first.content_digest
    # Raw audit: exactly one generation row for the workspace cut.
    async with env["factory"]() as session:
        rows = (
            (
                await session.execute(
                    select(KernelGeneration).where(
                        KernelGeneration.workspace_id == "ws-a"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.parametrize(
    "fault_phase",
    [
        PHASE_GEN_BUILD_BEGIN,
        PHASE_GEN_SOURCE_READ,
        PHASE_GEN_RECORDS_MATERIALIZED,
        PHASE_GEN_STAGED,
    ],
)
async def test_build_crash_exposes_no_partial_generation(mat_env, fault_phase) -> None:
    env = mat_env
    await _seed_two_cuts(env["service"])
    gen_service = GenerationService(env["factory"])
    snapshot = await resolve_snapshot(env["factory"], "ws-a")
    with pytest.raises(Exception, match="fault"):
        await gen_service.build(snapshot, _inject_fault_at=fault_phase)
    async with env["factory"]() as session:
        generations = (
            (await session.execute(select(KernelGeneration))).scalars().all()
        )
        heads = len(
            (await session.execute(select(KernelGenerationHead))).scalars().all()
        )
    # A fault before the staged-manifest commit leaves nothing at all;
    # the staged-phase fault leaves a durable staged generation (by
    # design — activation is the separate linearization step) that can
    # never be mistaken for current: no head row exists in any case.
    assert heads == 0
    if fault_phase is PHASE_GEN_STAGED:
        assert [g.state for g in generations] == ["staged"]
    else:
        assert generations == []


# ---------------------------------------------------------------------------
# X-G03/X-G04: validation reads stored rows; failed validation never moves head
# ---------------------------------------------------------------------------


async def test_validation_detects_tampered_materialized_rows(mat_env) -> None:
    env = mat_env
    await _seed_two_cuts(env["service"])
    gen_service = GenerationService(env["factory"])
    snapshot = await resolve_snapshot(env["factory"], "ws-a")
    gen = await gen_service.build_and_activate(snapshot)
    assert (await verify_generation(env["factory"], gen.generation_id)).ok

    # Tamper one materialized payload row behind the digest's back.
    async with env["factory"]() as session:
        await session.execute(
            update(KernelGenerationRecord)
            .where(KernelGenerationRecord.generation_id == gen.generation_id)
            .values(payload_json='{"tampered": true}')
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    report = await verify_generation(env["factory"], gen.generation_id)
    assert not report.ok
    # Verification is derived from STORED rows, not construction-time
    # memory, on both backends; the current head still resolves.
    current = await resolve_current_generation(env["factory"], "ws-a")
    assert current is not None


# ---------------------------------------------------------------------------
# X-G05/X-G06: concurrent activation, immutable reader view
# ---------------------------------------------------------------------------


async def test_concurrent_activation_yields_one_current_head(mat_env) -> None:
    """Two DIFFERENT valid generations race to become current: exactly
    one wins the pointer; the loser lands superseded."""
    env = mat_env
    service = env["service"]
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(_assertion("a1"), _observation("o1", payload=b"evidence" * 8)),
        )
    )
    snapshot_1 = await resolve_snapshot(env["factory"], "ws-a")
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a2"),))
    )
    snapshot_2 = await resolve_snapshot(env["factory"], "ws-a")
    gen_service = GenerationService(env["factory"])

    gen_1 = await gen_service.build(snapshot_1)
    gen_2 = await gen_service.build(snapshot_2)
    assert gen_1.generation_id != gen_2.generation_id

    await asyncio.gather(
        gen_service.activate(gen_1.generation_id),
        gen_service.activate(gen_2.generation_id),
        return_exceptions=True,
    )

    async with env["factory"]() as session:
        head = (
            await session.execute(
                select(KernelGenerationHead).where(
                    KernelGenerationHead.workspace_id == "ws-a"
                )
            )
        ).scalar_one()
        states = {
            row.generation_id: row.state
            for row in (
                await session.execute(
                    select(KernelGeneration).where(
                        KernelGeneration.workspace_id == "ws-a"
                    )
                )
            )
            .scalars()
            .all()
        }
    current_states = [s for s in states.values() if s == GENERATION_STATE_ACTIVE]
    assert len(current_states) == 1
    assert states[head.current_generation_id] == GENERATION_STATE_ACTIVE
    assert GENERATION_STATE_SUPERSEDED in states.values()
    current = await resolve_current_generation(env["factory"], "ws-a")
    assert current is not None
    assert current.generation_id == head.current_generation_id


async def test_reader_sees_one_immutable_generation_across_activation(mat_env) -> None:
    env = mat_env
    service = env["service"]
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a1"),))
    )
    snapshot_1 = await resolve_snapshot(env["factory"], "ws-a")
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a2"),))
    )
    snapshot_2 = await resolve_snapshot(env["factory"], "ws-a")
    gen_service = GenerationService(env["factory"])

    gen_1 = await gen_service.build_and_activate(snapshot_1)
    reader = GenerationReader(env["factory"], gen_1.generation_id)
    records_before = await reader.count_records()

    gen_2 = await gen_service.build_and_activate(snapshot_2)
    assert gen_2.generation_id != gen_1.generation_id

    records_after = await reader.count_records()
    assert records_after == records_before  # immutable reader view
    assert await verify_generation(env["factory"], gen_1.generation_id)


# ---------------------------------------------------------------------------
# X-G07/X-G08/X-G09: pins and holds
# ---------------------------------------------------------------------------


async def test_reader_pin_protects_and_expires_correctly(mat_env) -> None:
    env = mat_env
    await _seed_two_cuts(env["service"])
    gen_service = GenerationService(env["factory"])
    snapshot = await resolve_snapshot(env["factory"], "ws-a")
    gen = await gen_service.build_and_activate(snapshot)

    pin = await retention.acquire_reader_pin(
        env["factory"], gen.generation_id, lease_seconds=60.0
    )
    assert pin.generation_id == gen.generation_id
    renewed = await retention.renew_reader_pin(
        env["factory"], pin.pin_id, lease_seconds=60.0
    )
    assert renewed.pin_id == pin.pin_id
    pins = await retention.active_reader_pins(
        env["factory"], generation_id=gen.generation_id
    )
    assert [p.pin_id for p in pins] == [pin.pin_id]

    # Expired pins are purged without touching live ones.
    await retention.acquire_reader_pin(env["factory"], gen.generation_id, lease_seconds=0.01)
    await asyncio.sleep(0.05)
    purged = await retention.purge_expired_pins(env["factory"])
    assert purged >= 1
    remaining = await retention.active_reader_pins(
        env["factory"], generation_id=gen.generation_id
    )
    assert [p.pin_id for p in remaining] == [pin.pin_id]
    async with env["factory"]() as session:
        pin_rows = (
            (
                await session.execute(
                    select(KernelReaderPin).where(
                        KernelReaderPin.generation_id == gen.generation_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {p.pin_id for p in pin_rows} == {pin.pin_id}


async def test_retention_hold_survives_restart(mat_env) -> None:
    env = mat_env
    await _seed_two_cuts(env["service"])
    gen_service = GenerationService(env["factory"])
    snapshot = await resolve_snapshot(env["factory"], "ws-a")
    gen = await gen_service.build_and_activate(snapshot)

    hold = await retention.declare_hold(
        env["factory"], **_hold_kwargs(gen, "restart"), expires_at=None
    )
    engine2, factory2 = _new_engine(env)
    try:
        reopened = await retention.get_hold(factory2, hold.root_id)
        assert reopened is not None
        assert reopened.state == retention.ROOT_STATE_ACTIVE
        # Re-declaration is idempotent across the reopen.
        again = await retention.declare_hold(
            factory2, **_hold_kwargs(gen, "restart"), expires_at=None
        )
        assert again.root_id == hold.root_id
    finally:
        await engine2.dispose()
    async with env["factory"]() as session:
        roots = len(
            (
                await session.execute(
                    select(KernelRetentionRoot).where(
                        KernelRetentionRoot.workspace_id == "ws-a"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert roots == 1


async def test_hold_expiry_and_release_transitions(mat_env) -> None:
    env = mat_env
    await _seed_two_cuts(env["service"])
    gen_service = GenerationService(env["factory"])
    snapshot = await resolve_snapshot(env["factory"], "ws-a")
    gen = await gen_service.build_and_activate(snapshot)

    expiring = await retention.declare_hold(
        env["factory"],
        **_hold_kwargs(gen, "expiry"),
        expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=10),
    )
    await asyncio.sleep(0.05)
    view = await retention.get_hold(env["factory"], expiring.root_id)
    assert view is not None
    assert not view.active  # expired holds stop protecting

    held = await retention.declare_hold(
        env["factory"], **_hold_kwargs(gen, "release"), expires_at=None
    )
    assert await retention.release_hold(env["factory"], held.root_id)
    released = await retention.get_hold(env["factory"], held.root_id)
    assert released is not None and not released.active
