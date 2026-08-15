"""Generation lifecycle fault-injection tests (V3.2 PR65A, matrix 10.5).

For every meaningful fault point in build → validate → activate, the
outcome is binary: the prior accepted generation remains authoritative,
or the new generation became active only after all declared material
was present and validated. No third state exists. Also proves: staged
residue is identifiable but never current, restart never selects a
half-built generation, and no kernel payload object is ever deleted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InjectedFaultError
from app.kernel.generations import (
    GENERATION_FAULT_PHASES,
    GENERATION_STATE_ACTIVE,
    GENERATION_STATE_SUPERSEDED,
    GENERATION_STATE_VALIDATED,
    GenerationReader,
    GenerationService,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.records import ClaimAssertionRecord
from app.kernel.snapshots import resolve_snapshot

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


async def _commit(service: KernelCommitService, key: str) -> None:
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion(key),))
    )


async def _active_baseline(
    factory: async_sessionmaker, service: KernelCommitService
) -> str:
    gen_service = GenerationService(factory)
    ref = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    return ref.generation_id


def _generation_rows(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT generation_id, state FROM kernel_generations"
        ).fetchall()


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


async def _attempt_next_generation(
    factory: async_sessionmaker,
    service: KernelCommitService,
    fault: str,
) -> None:
    """Commit something new, then try to build+activate at the new head
    with the fault injected."""
    await _commit(service, "extra")
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, "ws-a")
    await gen_service.build_and_activate(snapshot, _inject_fault_at=fault)


async def test_every_fault_phase_is_recognized(payload_env: tuple) -> None:
    assert GENERATION_FAULT_PHASES == frozenset(
        {
            "gen-build-begin",
            "gen-source-read",
            "gen-records-materialized",
            "gen-staged",
            "gen-validate-begin",
            "gen-validated",
            "gen-pre-activate",
            "gen-post-activate",
        }
    )


@pytest.mark.parametrize(
    "fault",
    [
        "gen-build-begin",
        "gen-source-read",
        "gen-records-materialized",
        "gen-staged",
        "gen-validate-begin",
        "gen-validated",
        "gen-pre-activate",
    ],
)
async def test_prior_generation_survives_every_pre_switch_fault(
    payload_env: tuple, fault: str
) -> None:
    factory, store, service = payload_env
    await _commit(service, "base")
    baseline = await _active_baseline(factory, service)
    objects_before = set(await store.list_objects())

    with pytest.raises(InjectedFaultError):
        await _attempt_next_generation(factory, service, fault)

    # outcome 1: prior accepted generation remains authoritative
    current = await resolve_current_generation(factory, "ws-a")
    assert current is not None and current.generation_id == baseline
    assert current.state == GENERATION_STATE_ACTIVE
    assert (await verify_generation(factory, baseline)).ok

    # no generation may be active other than the baseline
    states = dict(_generation_rows(_db_path(factory)))
    active = [gid for gid, state in states.items() if state == "active"]
    assert active == [baseline]

    # no kernel payload object was deleted by any fault path
    assert set(await store.list_objects()) == objects_before


@pytest.mark.parametrize("fault", ["gen-build-begin", "gen-source-read", "gen-records-materialized"])
async def test_early_faults_leave_no_generation_rows(payload_env: tuple, fault: str) -> None:
    factory, store, service = payload_env
    await _commit(service, "base")
    await _active_baseline(factory, service)
    baseline_rows = len(_generation_rows(_db_path(factory)))

    with pytest.raises(InjectedFaultError):
        await _attempt_next_generation(factory, service, fault)

    rows = _generation_rows(_db_path(factory))
    assert len(rows) == baseline_rows  # nothing staged: transaction rolled back


@pytest.mark.parametrize("fault", ["gen-staged", "gen-validate-begin"])
async def test_mid_faults_leave_identifiable_staged_residue(
    payload_env: tuple, fault: str
) -> None:
    factory, store, service = payload_env
    await _commit(service, "base")
    baseline = await _active_baseline(factory, service)

    with pytest.raises(InjectedFaultError):
        await _attempt_next_generation(factory, service, fault)

    gen_service = GenerationService(factory)
    staged = await gen_service.list_generations(state="staged")
    assert len(staged) == 1
    assert staged[0].kernel_commit_id > 1  # the newer cut, not the baseline
    # residue is identifiable for later cleanup but never current
    assert (await resolve_current_generation(factory, "ws-a")).generation_id == baseline

    # restart view: fresh engine must not select the half-built generation
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    url = f"sqlite+aiosqlite:///{_db_path(factory).as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    fresh = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    restarted = await resolve_current_generation(fresh, "ws-a")
    assert restarted.generation_id == baseline
    await engine.dispose()

    # the staged generation is completable: validate + activate converge
    validated = await gen_service.validate(staged[0].generation_id)
    activated = await gen_service.activate(staged[0].generation_id)
    assert validated.state == GENERATION_STATE_VALIDATED
    assert activated.state == GENERATION_STATE_ACTIVE
    assert (
        await resolve_current_generation(factory, "ws-a")
    ).generation_id == staged[0].generation_id
    assert (
        await gen_service.get_generation(baseline)
    ).state == GENERATION_STATE_SUPERSEDED


async def test_pre_activate_rollback_then_retry_converges(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "base")
    baseline = await _active_baseline(factory, service)

    with pytest.raises(InjectedFaultError):
        await _attempt_next_generation(factory, service, "gen-pre-activate")

    gen_service = GenerationService(factory)
    validated = [
        g for g in await gen_service.list_generations(state="validated")
        if g.generation_id != baseline
    ]
    assert len(validated) == 1  # fully built and validated, switch rolled back

    # retry the activation: converges on the fully validated generation
    activated = await gen_service.activate(validated[0].generation_id)
    assert activated.state == GENERATION_STATE_ACTIVE
    current = await resolve_current_generation(factory, "ws-a")
    assert current.generation_id == validated[0].generation_id
    assert (await verify_generation(factory, activated.generation_id)).ok


async def test_post_activate_fault_leaves_fully_valid_current(
    payload_env: tuple,
) -> None:
    """Fault after the switch commit: outcome 2 — the new generation is
    fully valid and current, even though the caller saw an error."""
    factory, store, service = payload_env
    await _commit(service, "base")
    await _active_baseline(factory, service)

    with pytest.raises(InjectedFaultError):
        await _attempt_next_generation(factory, service, "gen-post-activate")

    current = await resolve_current_generation(factory, "ws-a")
    assert current.state == GENERATION_STATE_ACTIVE
    assert current.kernel_commit_id == 2
    assert (await verify_generation(factory, current.generation_id)).ok
    reader = GenerationReader(factory, current.generation_id)
    assert await reader.count_records() == 2


async def test_unknown_fault_phase_rejected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _commit(service, "base")
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, "ws-a")
    from app.kernel.errors import KernelError

    with pytest.raises(KernelError):
        await gen_service.build(snapshot, _inject_fault_at="not-a-phase")
    with pytest.raises(KernelError):
        await gen_service.build_and_activate(
            snapshot, _inject_fault_at="not-a-phase"
        )


async def test_failed_generation_never_current_and_rebuildable(
    payload_env: tuple,
) -> None:
    """End-to-end residue honesty: tamper a staged build, validation
    marks it failed, the failed row is identifiable, a clean rebuild of
    the same declared inputs succeeds, and payload evidence survives."""
    import sqlite3 as sq

    factory, store, service = payload_env
    await _commit(service, "base")
    baseline = await _active_baseline(factory, service)
    await _commit(service, "extra")
    objects_before = set(await store.list_objects())

    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, "ws-a")
    with pytest.raises(InjectedFaultError):
        await gen_service.build(snapshot, _inject_fault_at="gen-staged")

    staged = (await gen_service.list_generations(state="staged"))[0]
    with sq.connect(_db_path(factory)) as conn:
        conn.execute(
            "UPDATE kernel_generation_records SET payload_json = '{\"x\":1}' "
            "WHERE generation_id = ?",
            (staged.generation_id,),
        )
        conn.commit()

    with pytest.raises(Exception):
        await gen_service.validate(staged.generation_id)
    assert (
        await resolve_current_generation(factory, "ws-a")
    ).generation_id == baseline
    failed = await gen_service.list_generations(state="failed")
    assert [g.generation_id for g in failed] == [staged.generation_id]

    # clean rebuild of the same declared inputs succeeds
    rebuilt = await gen_service.build(snapshot)
    assert rebuilt.state == GENERATION_STATE_VALIDATED
    activated = await gen_service.activate(rebuilt.generation_id)
    assert activated.state == GENERATION_STATE_ACTIVE
    # no authoritative kernel payload was touched
    assert set(await store.list_objects()) == objects_before
