"""Kernel spine migration tests (V3.2 PR63A, plan workstream A;
PR64 payload/outbox, PR65A generation, PR65B retention, and PR66
fencing/publication revision coverage).

Extends the PR62 acceptance matrix to the kernel Alembic heads:

- fresh DB upgrades to head with all kernel tables;
- a database at the previous head ``20260709_0003`` upgrades to the new
  head with existing rows preserved;
- PR64: a database at ``20260815_0004`` with committed PR63A data
  upgrades to ``20260815_0005`` preserving every record, and the
  payload/outbox tables arrive empty (no fabricated durability truth);
- the kernel commit service refuses to run against an unmigrated
  database (no runtime self-heal of kernel schema);
- downgraded spine schema is classified as PENDING_UPGRADE (fail-closed
  classification, not repair);
- PR66: a database at ``20260815_0007`` with committed kernel history,
  an active generation, and declared retention upgrades to
  ``20260816_0008`` with all prior truth preserved and the fencing
  tables arriving empty; fencing calls fail closed on the missing
  revision; downgrade drops fencing/publication truth honestly.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command

from app import db_migration
from app.db_migration import (
    DatabaseState,
    IncompatibleDatabaseError,
    inspect_database,
    upgrade_database,
    verify_database_ready,
)
from app.kernel import fencing, outbox
from app.kernel import events as kernel_events
from app.kernel import liveness as kernel_liveness
from app.kernel import scheduler as kernel_scheduler
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import GenerationService
from app.kernel.gc import plan_collection
from app.kernel.outbox import OutboxIntent
from app.kernel.payloads import LocalPayloadStore
from app.kernel.records import ClaimAssertionRecord, ObservationRecord
from app.kernel.reconcile import verify_payload_availability
from app.kernel.retention import ROOT_KIND_SNAPSHOT_HOLD, declare_hold
from app.kernel.snapshots import resolve_snapshot

KERNEL_TABLES = {
    "kernel_commit_heads",
    "kernel_commit_manifests",
    "kernel_records",
    "kernel_record_edges",
}

PR64_TABLES = {
    "kernel_payload_objects",
    "kernel_outbox",
}

PREVIOUS_HEAD = "20260709_0003"
PR63A_HEAD = "20260815_0004"
PR64_HEAD = "20260815_0005"
PR65A_HEAD = "20260815_0006"
PR65B_HEAD = "20260815_0007"
PR66_HEAD = "20260816_0008"
CURRENT_HEAD = "20260816_0009"

PR65A_TABLES = {
    "kernel_generations",
    "kernel_generation_records",
    "kernel_generation_edges",
    "kernel_generation_heads",
}

PR65B_TABLES = {
    "kernel_retention_roots",
    "kernel_reader_pins",
    "kernel_payload_retirements",
}

PR66_TABLES = {
    "kernel_work_leases",
    "kernel_publications",
}

PR67A_TABLES = {
    "kernel_scheduling_entries",
    "kernel_scheduling_groups",
    "kernel_liveness",
    "kernel_events",
    "kernel_progress",
}


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest_asyncio.fixture
async def kernel_db(tmp_path: Path):
    """File-backed database migrated to head, with a session factory."""
    url = _db_url(tmp_path / "kernel.db")
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory, url, tmp_path / "kernel.db"
    finally:
        await engine.dispose()


def _assertion(claim_key: str = "k1") -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=claim_key,
        subject="doc:report.pdf",
        predicate="contains_table",
        value=True,
    )


# ---------------------------------------------------------------------------
# fresh installs and upgrade installs both land the spine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_upgrade_creates_kernel_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    await upgrade_database(url=_db_url(db_path))
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'kernel_%'"
            )
        }
    assert KERNEL_TABLES <= tables
    # Head table starts empty: 0 is the implicit initial state until the
    # first commit creates the row.
    with sqlite3.connect(db_path) as conn:
        heads = conn.execute("SELECT COUNT(*) FROM kernel_commit_heads").fetchone()[0]
    assert heads == 0


@pytest.mark.asyncio
async def test_upgrade_from_previous_head_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "existing.db"
    url = _db_url(db_path)
    await asyncio.to_thread(db_migration._run_upgrade, url, PREVIOUS_HEAD)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversion_jobs "
            "(id, filename, original_name, status, input_format, output_format, "
            " progress, config_json, result_text) "
            "VALUES ('job-1', 'a.pdf', 'a.pdf', 'completed', 'pdf', 'markdown', "
            "100, '{}', '# out')"
        )
        conn.commit()

    await upgrade_database(url=url)

    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT
    with sqlite3.connect(db_path) as conn:
        assert KERNEL_TABLES <= {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'kernel_%'"
            )
        }
        job = conn.execute(
            "SELECT status FROM conversion_jobs WHERE id = 'job-1'"
        ).fetchone()
    assert job == ("completed",)


@pytest.mark.asyncio
async def test_reupgrade_at_head_is_idempotent(tmp_path: Path) -> None:
    url = _db_url(tmp_path / "again.db")
    first = await upgrade_database(url=url)
    second = await upgrade_database(url=url)
    assert first.action == "initialized"
    assert second.action == "already-current"


# ---------------------------------------------------------------------------
# fail-closed behavior for kernel schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kernel_service_fails_closed_on_unmigrated_database(
    tmp_path: Path,
) -> None:
    """Runtime must not create kernel tables; only Alembic may."""
    db_path = tmp_path / "unmigrated.db"
    url = _db_url(db_path)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory, readiness_check=lambda: verify_database_ready(url=url))
    try:
        with pytest.raises(IncompatibleDatabaseError):
            await service.commit(
                KernelCommitBatch(workspace_id="ws", records=(_assertion(),))
            )
    finally:
        await engine.dispose()
    # Nothing was created by the failed attempt.
    assert not db_path.exists() or _kernel_tables_in(db_path) == set()


@pytest.mark.asyncio
async def test_downgraded_spine_is_pending_upgrade_not_current(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "downgraded.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    def _downgrade() -> None:
        command.downgrade(db_migration._alembic_config(url), PREVIOUS_HEAD)

    await asyncio.to_thread(_downgrade)
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE
    with pytest.raises(IncompatibleDatabaseError):
        await verify_database_ready(url=url)
    await upgrade_database(url=url)
    assert inspect_database(url=url).state is DatabaseState.CURRENT


def _kernel_tables_in(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'kernel_%'"
            )
        }


# ---------------------------------------------------------------------------
# PR64: 20260815_0004 -> 20260815_0005 upgrade preserves committed truth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_from_pr63a_head_preserves_committed_data(tmp_path: Path) -> None:
    db_path = tmp_path / "pr63a.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    # Roll the schema back to the PR63A head only, then commit real data
    # through the service exactly as PR63A did.
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR63A_HEAD
    )

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        for i in range(3):
            await service.commit(
                KernelCommitBatch(
                    workspace_id="ws-pr63a",
                    records=(
                        ClaimAssertionRecord(
                            claim_key=f"k{i}",
                            subject="doc:x.pdf",
                            predicate="p",
                            value=i,
                        ),
                    ),
                )
            )
    finally:
        await engine.dispose()

    await upgrade_database(url=url)
    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == CURRENT_HEAD
        records = conn.execute(
            "SELECT COUNT(*) FROM kernel_records WHERE workspace_id = 'ws-pr63a'"
        ).fetchone()[0]
        head = conn.execute(
            "SELECT head_kernel_commit_id FROM kernel_commit_heads "
            "WHERE workspace_id = 'ws-pr63a'"
        ).fetchone()[0]
        payload_rows = conn.execute(
            "SELECT COUNT(*) FROM kernel_payload_objects"
        ).fetchone()[0]
        outbox_rows = conn.execute("SELECT COUNT(*) FROM kernel_outbox").fetchone()[0]
    assert records == 3
    assert head == 3
    # New tables arrive empty: PR63A history was hash-metadata only, and
    # the migration must not fabricate availability truth.
    assert payload_rows == 0
    assert outbox_rows == 0


@pytest.mark.asyncio
async def test_runtime_fails_closed_at_pr63a_head(tmp_path: Path) -> None:
    """A database missing only the PR64 revision is PENDING_UPGRADE."""
    db_path = tmp_path / "behind.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR63A_HEAD
    )
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE
    with pytest.raises(IncompatibleDatabaseError):
        await verify_database_ready(url=url)


@pytest.mark.asyncio
async def test_pr64_downgrade_drops_durability_truth_then_reupgrade_converges(
    tmp_path: Path,
) -> None:
    """Downgrade discards registry/outbox truth (documented limitation).

    The spine (records/manifests) survives the PR64 downgrade; only the
    PR64 tables drop. Re-upgrading recreates them empty — durability
    truth lost by downgrade is not silently rebuilt.
    """
    db_path = tmp_path / "downgrade.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    assert PR64_TABLES <= _kernel_tables_in(db_path)

    def _downgrade_to_pr63a() -> None:
        command.downgrade(db_migration._alembic_config(url), PR63A_HEAD)

    await asyncio.to_thread(_downgrade_to_pr63a)
    tables = _kernel_tables_in(db_path)
    assert not (PR64_TABLES & tables)
    assert KERNEL_TABLES <= tables  # spine intact

    await upgrade_database(url=url)
    assert (KERNEL_TABLES | PR64_TABLES) <= _kernel_tables_in(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kernel_payload_objects").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# PR65A: 20260815_0005 -> 20260815_0006 generation read model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_from_pr64_head_preserves_committed_data(
    tmp_path: Path,
) -> None:
    """A PR64 database with committed payload-bearing history upgrades
    to the PR65A head with every record preserved and the generation
    tables arriving empty (no fabricated read model)."""
    db_path = tmp_path / "pr64.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR64_HEAD
    )

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        for i in range(2):
            await service.commit(
                KernelCommitBatch(
                    workspace_id="ws-pr64",
                    records=(
                        ClaimAssertionRecord(
                            claim_key=f"k{i}",
                            subject="doc:x.pdf",
                            predicate="p",
                            value=i,
                        ),
                    ),
                )
            )
    finally:
        await engine.dispose()

    await upgrade_database(url=url)
    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == CURRENT_HEAD
        records = conn.execute(
            "SELECT COUNT(*) FROM kernel_records WHERE workspace_id = 'ws-pr64'"
        ).fetchone()[0]
        generations = conn.execute(
            "SELECT COUNT(*) FROM kernel_generations"
        ).fetchone()[0]
        heads = conn.execute(
            "SELECT COUNT(*) FROM kernel_generation_heads"
        ).fetchone()[0]
    assert records == 2
    assert generations == 0 and heads == 0  # derived state is never fabricated


@pytest.mark.asyncio
async def test_runtime_fails_closed_at_pr64_head(tmp_path: Path) -> None:
    """A database missing only the PR65A revision is PENDING_UPGRADE;
    generation builds must fail closed, not self-heal the schema."""
    db_path = tmp_path / "behind-pr65a.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR64_HEAD
    )
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    gen_service = GenerationService(
        factory, readiness_check=lambda: verify_database_ready(url=url)
    )
    try:
        with pytest.raises(IncompatibleDatabaseError):
            await _build_on_unmigrated(gen_service, factory)
    finally:
        await engine.dispose()
    assert PR65A_TABLES.isdisjoint(_kernel_tables_in(db_path))


async def _build_on_unmigrated(gen_service, factory) -> None:
    snapshot = await resolve_snapshot(factory, "ws")
    await gen_service.build_and_activate(snapshot)


@pytest.mark.asyncio
async def test_pr65a_downgrade_drops_generations_then_reupgrade_converges(
    tmp_path: Path,
) -> None:
    """Downgrade discards generation truth (documented destructive
    limitation): kernel truth and payloads survive, derived read state
    and activation history are dropped, re-upgrade recreates the tables
    empty and a rebuild reproduces the generation."""
    db_path = tmp_path / "pr65a-downgrade.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(
                ClaimAssertionRecord(
                    claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                ),
            ),
        )
    )
    gen_service = GenerationService(factory)
    ref = await gen_service.build_and_activate(await resolve_snapshot(factory, "ws"))
    await engine.dispose()

    assert PR65A_TABLES <= _kernel_tables_in(db_path)

    def _downgrade_to_pr64() -> None:
        command.downgrade(db_migration._alembic_config(url), PR64_HEAD)

    await asyncio.to_thread(_downgrade_to_pr64)
    tables = _kernel_tables_in(db_path)
    assert not (PR65A_TABLES & tables)
    assert (KERNEL_TABLES | PR64_TABLES) <= tables  # kernel truth intact

    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE
    await upgrade_database(url=url)
    assert inspect_database(url=url).state is DatabaseState.CURRENT
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kernel_generations").fetchone()[0] == 0

    # the dropped generation rebuilds deterministically from the kernel
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    gen_service = GenerationService(factory)
    rebuilt = await gen_service.build(await resolve_snapshot(factory, "ws"))
    await engine.dispose()
    assert rebuilt.generation_id == ref.generation_id
    assert rebuilt.content_digest == ref.content_digest


# ---------------------------------------------------------------------------
# PR65B: 20260815_0006 -> 20260815_0007 retention contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_from_pr65a_head_preserves_committed_data(
    tmp_path: Path,
) -> None:
    """A PR65A database with committed history, an active generation, and
    a declared hold upgrades to the PR65B head with everything preserved
    and the retention/GC tables arriving empty (no fabricated roots or
    tombstones — nothing is retired by a migration)."""
    db_path = tmp_path / "pr65a-full.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR65A_HEAD
    )

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    gen_service = GenerationService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-pr65b",
                records=(
                    ClaimAssertionRecord(
                        claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                    ),
                ),
            )
        )
        await gen_service.build_and_activate(
            await resolve_snapshot(factory, "ws-pr65b")
        )
    finally:
        await engine.dispose()

    await upgrade_database(url=url)
    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == CURRENT_HEAD
        records = conn.execute(
            "SELECT COUNT(*) FROM kernel_records WHERE workspace_id = 'ws-pr65b'"
        ).fetchone()[0]
        generations = conn.execute(
            "SELECT COUNT(*) FROM kernel_generations WHERE state = 'active'"
        ).fetchone()[0]
        roots = conn.execute(
            "SELECT COUNT(*) FROM kernel_retention_roots"
        ).fetchone()[0]
        pins = conn.execute("SELECT COUNT(*) FROM kernel_reader_pins").fetchone()[0]
        tombstones = conn.execute(
            "SELECT COUNT(*) FROM kernel_payload_retirements"
        ).fetchone()[0]
    assert records == 1
    assert generations == 1  # the active generation survived the upgrade
    assert roots == 0 and pins == 0 and tombstones == 0  # nothing fabricated


@pytest.mark.asyncio
async def test_retention_calls_fail_closed_at_pr65a_head(tmp_path: Path) -> None:
    """A database missing only the PR65B revision is PENDING_UPGRADE;
    retention/GC calls fail closed on the missing tables — they never
    self-heal the schema."""
    db_path = tmp_path / "behind-pr65b.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR65A_HEAD
    )
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        with pytest.raises(Exception) as hold_exc:
            await declare_hold(
                factory,
                workspace_id="ws",
                root_kind=ROOT_KIND_SNAPSHOT_HOLD,
                kernel_commit_id=0,
            )
        assert "no such table" in str(hold_exc.value).lower()
        with pytest.raises(Exception) as gc_exc:
            await plan_collection(
                factory, LocalPayloadStore(tmp_path / "payloads")
            )
        assert "no such table" in str(gc_exc.value).lower()
    finally:
        await engine.dispose()
    assert PR65B_TABLES.isdisjoint(_kernel_tables_in(db_path))


@pytest.mark.asyncio
async def test_pr65b_downgrade_drops_retention_then_reupgrade_converges(
    tmp_path: Path,
) -> None:
    """Downgrade forgets the retention contract (documented destructive
    limitation): holds/pins/tombstone history are dropped while kernel
    truth survives; re-upgrade recreates the tables empty. Downgrading
    does NOT restore retired bytes — the only heal is re-staging the
    exact bytes, which this test proves still works afterwards."""
    db_path = tmp_path / "pr65b-downgrade.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    store = LocalPayloadStore(tmp_path / "payloads")
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory, payload_store=store)
    gen_service = GenerationService(factory)
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws",
            records=(
                ObservationRecord(
                    observer="marker",
                    derivation={"stage": "layout"},
                    payload_bytes=b"retention downgrade probe",
                ),
            ),
        )
    )
    await gen_service.build_and_activate(await resolve_snapshot(factory, "ws"))
    hold = await declare_hold(
        factory,
        workspace_id="ws",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
    )
    assert hold.active
    await engine.dispose()

    assert PR65B_TABLES <= _kernel_tables_in(db_path)

    def _downgrade_to_pr65a() -> None:
        command.downgrade(db_migration._alembic_config(url), PR65A_HEAD)

    await asyncio.to_thread(_downgrade_to_pr65a)
    tables = _kernel_tables_in(db_path)
    assert not (PR65B_TABLES & tables)
    assert (KERNEL_TABLES | PR64_TABLES | PR65A_TABLES) <= tables  # truth intact

    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE
    await upgrade_database(url=url)
    assert inspect_database(url=url).state is DatabaseState.CURRENT
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kernel_retention_roots").fetchone()[0] == 0

    # the hold is gone (no silent resurrection); declaring it again works,
    # and payload bytes survived the round-trip untouched
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redeclared = await declare_hold(
        factory,
        workspace_id="ws",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=1,
    )
    assert redeclared.root_id == hold.root_id  # deterministic identity
    availability = await verify_payload_availability(factory, store, workspace_id="ws")
    assert availability.payload_backed_complete
    await engine.dispose()


# ---------------------------------------------------------------------------
# PR66: 20260815_0007 -> 20260816_0008 fenced work authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_from_pr65b_head_preserves_committed_data(
    tmp_path: Path,
) -> None:
    """A PR65B database with committed history, payload state, an outbox
    row, an active generation, and declared retention upgrades to the
    PR66 head with everything preserved and the fencing tables arriving
    empty (no fabricated ownership or accepted results)."""
    db_path = tmp_path / "pr65b-full.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR65B_HEAD
    )

    store = LocalPayloadStore(tmp_path / "payloads")
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory, payload_store=store)
    gen_service = GenerationService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-pr66",
                records=(
                    ObservationRecord(
                        observer="marker",
                        derivation={"stage": "layout"},
                        payload_bytes=b"pr66 upgrade probe",
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={"t": 1}),),
            )
        )
        await gen_service.build_and_activate(await resolve_snapshot(factory, "ws-pr66"))
        await declare_hold(
            factory,
            workspace_id="ws-pr66",
            root_kind=ROOT_KIND_SNAPSHOT_HOLD,
            kernel_commit_id=1,
        )
    finally:
        await engine.dispose()

    await upgrade_database(url=url)
    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == CURRENT_HEAD
        records = conn.execute(
            "SELECT COUNT(*) FROM kernel_records WHERE workspace_id = 'ws-pr66'"
        ).fetchone()[0]
        payloads = conn.execute(
            "SELECT COUNT(*) FROM kernel_payload_objects"
        ).fetchone()[0]
        outbox_rows = conn.execute("SELECT COUNT(*) FROM kernel_outbox").fetchone()[0]
        generations = conn.execute(
            "SELECT COUNT(*) FROM kernel_generations WHERE state = 'active'"
        ).fetchone()[0]
        roots = conn.execute(
            "SELECT COUNT(*) FROM kernel_retention_roots"
        ).fetchone()[0]
        leases = conn.execute("SELECT COUNT(*) FROM kernel_work_leases").fetchone()[0]
        publications = conn.execute(
            "SELECT COUNT(*) FROM kernel_publications"
        ).fetchone()[0]
    assert records == 1 and payloads == 1 and outbox_rows == 1
    assert generations == 1 and roots == 1  # PR65B truth survived
    assert leases == 0 and publications == 0  # nothing fabricated


@pytest.mark.asyncio
async def test_fencing_calls_fail_closed_at_pr65b_head(tmp_path: Path) -> None:
    """A database missing only the PR66 revision is PENDING_UPGRADE;
    fencing calls fail closed on the missing tables — they never
    self-heal the schema."""
    db_path = tmp_path / "behind-pr66.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR65B_HEAD
    )
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    ClaimAssertionRecord(
                        claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={}),),
            )
        )
        (outbox_row,) = await outbox.list_outbox(factory)
        with pytest.raises(Exception) as acquire_exc:
            await fencing.acquire(
                factory, work_id=outbox_row.id, owner_id="worker-a"
            )
        assert "no such table" in str(acquire_exc.value).lower()
        with pytest.raises(Exception) as accept_exc:
            await fencing.accept(
                factory, work_id=outbox_row.id, fencing_token=1, result={}
            )
        assert "no such table" in str(accept_exc.value).lower()
    finally:
        await engine.dispose()
    assert PR66_TABLES.isdisjoint(_kernel_tables_in(db_path))


@pytest.mark.asyncio
async def test_pr66_downgrade_drops_fencing_then_reupgrade_converges(
    tmp_path: Path,
) -> None:
    """Downgrade forgets fencing and accepted-publication truth
    (documented destructive limitation): kernel truth, payloads, and
    outbox intent survive; a database below the PR66 head is honestly
    PENDING_UPGRADE and never reports ready; re-upgrade converges to
    empty fencing tables from which fresh authority can be built."""
    db_path = tmp_path / "pr66-downgrade.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    ClaimAssertionRecord(
                        claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={}),),
            )
        )
        (outbox_row,) = await outbox.list_outbox(factory)
        await outbox.claim(factory, outbox_row.id)
        lease = await fencing.acquire(factory, work_id=outbox_row.id, owner_id="w1")
        assert lease is not None and lease.fencing_token == 1
        outcome = await fencing.accept(
            factory, work_id=outbox_row.id, fencing_token=1, result={"ok": True}
        )
        assert not outcome.already_accepted
    finally:
        await engine.dispose()

    assert PR66_TABLES <= _kernel_tables_in(db_path)

    def _downgrade_to_pr65b() -> None:
        command.downgrade(db_migration._alembic_config(url), PR65B_HEAD)

    await asyncio.to_thread(_downgrade_to_pr65b)
    tables = _kernel_tables_in(db_path)
    assert not (PR66_TABLES & tables)
    assert (KERNEL_TABLES | PR64_TABLES | PR65A_TABLES | PR65B_TABLES) <= tables

    # An interrupted/failed upgrade state is never reported ready (T15).
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE
    with pytest.raises(IncompatibleDatabaseError):
        await verify_database_ready(url=url)

    await upgrade_database(url=url)
    assert inspect_database(url=url).state is DatabaseState.CURRENT
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kernel_work_leases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kernel_publications").fetchone()[0] == 0
        outbox_state = conn.execute(
            "SELECT state FROM kernel_outbox"
        ).fetchone()[0]

    # outbox truth survived the downgrade; fresh fencing works after the
    # re-upgrade (the dropped authority is rebuilt, not resurrected).
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        assert outbox_state == "in_flight"
        lease = await fencing.acquire(factory, work_id=outbox_row.id, owner_id="w2")
        assert lease is not None and lease.fencing_token == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# PR67A: fair scheduling, challenge liveness, durable semantic events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_from_pr66_head_preserves_committed_data(
    tmp_path: Path,
) -> None:
    """A PR66 database with committed history, an outbox item under a
    live fence, and an accepted publication upgrades to the PR67A head
    with every prior authority fact preserved and the scheduler,
    liveness, and event tables arriving empty — no fabricated
    scheduling state, liveness evidence, or semantic history."""
    db_path = tmp_path / "pr66-full.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR66_HEAD
    )

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-pr67a",
                records=(
                    ClaimAssertionRecord(
                        claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={"t": 1}),),
            )
        )
        (outbox_row,) = await outbox.list_outbox(factory)
        await outbox.claim(factory, outbox_row.id)
        lease = await fencing.acquire(factory, work_id=outbox_row.id, owner_id="w1")
        assert lease is not None
        await fencing.accept(
            factory, work_id=outbox_row.id, fencing_token=1, result={"ok": True}
        )
    finally:
        await engine.dispose()

    await upgrade_database(url=url)
    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == CURRENT_HEAD
        leases = conn.execute("SELECT COUNT(*) FROM kernel_work_leases").fetchone()[0]
        publications = conn.execute(
            "SELECT COUNT(*) FROM kernel_publications"
        ).fetchone()[0]
        for table in sorted(PR67A_TABLES):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} must arrive empty, found {count}"
    assert leases == 1 and publications == 1  # PR66 authority survived intact


@pytest.mark.asyncio
async def test_scheduler_calls_fail_closed_at_pr66_head(tmp_path: Path) -> None:
    """A database missing only the PR67A revision is PENDING_UPGRADE;
    scheduler, event, and liveness calls fail closed on the missing
    tables — they never self-heal the schema."""
    db_path = tmp_path / "behind-pr67a.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR66_HEAD
    )
    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    ClaimAssertionRecord(
                        claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={}),),
            )
        )
        with pytest.raises(Exception) as claim_exc:
            await kernel_scheduler.claim_fair(factory, owner_id="worker-a")
        assert "no such table" in str(claim_exc.value).lower()
        with pytest.raises(Exception) as append_exc:
            await kernel_events.append(
                factory, workspace_id="ws", event_type="probe.event", payload={}
            )
        assert "no such table" in str(append_exc.value).lower()
        # The fence itself exists at this head; liveness evidence does not:
        # renewal must fail closed on the missing evidence table after a
        # legitimate acquire.
        (outbox_row,) = await outbox.list_outbox(factory)
        await outbox.claim(factory, outbox_row.id)
        lease = await fencing.acquire(factory, work_id=outbox_row.id, owner_id="w1")
        assert lease is not None
        with pytest.raises(Exception) as renew_exc:
            await kernel_liveness.renew_lease(
                factory,
                work_id=outbox_row.id,
                owner_id="w1",
                fencing_token=lease.fencing_token,
                challenge_nonce="stale",
                progress=1,
                active_request_id="req-1",
            )
        assert "no such table" in str(renew_exc.value).lower()
    finally:
        await engine.dispose()
    assert PR67A_TABLES.isdisjoint(_kernel_tables_in(db_path))


@pytest.mark.asyncio
async def test_pr67a_downgrade_drops_scheduler_then_reupgrade_converges(
    tmp_path: Path,
) -> None:
    """Downgrade forgets scheduler, liveness, and event truth (documented
    destructive limitation): PR66 ownership/publication authority and
    outbox intent survive; a database below the PR67A head is honestly
    PENDING_UPGRADE and never reports ready; re-upgrade converges with
    empty scheduler/event tables, a fresh semantic sequence, and the
    missing semantic history re-derivable from the surviving
    authorities rather than resurrected."""
    db_path = tmp_path / "pr67a-downgrade.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    try:
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws",
                records=(
                    ClaimAssertionRecord(
                        claim_key="k", subject="doc:x.pdf", predicate="p", value=1
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={}),),
            )
        )
        claimed = await kernel_scheduler.claim_fair(factory, owner_id="worker-a")
        assert claimed is not None and claimed.lease.fencing_token == 1
        await kernel_events.append_progress(
            factory, workspace_id="ws", work_id=claimed.work_id, counter=7
        )
        await kernel_liveness.renew_lease(
            factory,
            work_id=claimed.work_id,
            owner_id="worker-a",
            fencing_token=1,
            challenge_nonce=claimed.challenge_nonce,
            progress=1,
            active_request_id="req-1",
        )
        assert await kernel_events.get_latest_sequence(factory, workspace_id="ws") >= 1
    finally:
        await engine.dispose()

    assert PR67A_TABLES <= _kernel_tables_in(db_path)

    def _downgrade_to_pr66() -> None:
        command.downgrade(db_migration._alembic_config(url), PR66_HEAD)

    await asyncio.to_thread(_downgrade_to_pr66)
    tables = _kernel_tables_in(db_path)
    assert not (PR67A_TABLES & tables)
    assert (KERNEL_TABLES | PR64_TABLES | PR65A_TABLES | PR65B_TABLES | PR66_TABLES) <= (
        tables
    )

    status = inspect_database(url=url)
    assert status.state is DatabaseState.PENDING_UPGRADE
    with pytest.raises(IncompatibleDatabaseError):
        await verify_database_ready(url=url)

    await upgrade_database(url=url)
    assert inspect_database(url=url).state is DatabaseState.CURRENT
    with sqlite3.connect(db_path) as conn:
        for table in sorted(PR67A_TABLES):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        outbox_state = conn.execute("SELECT state FROM kernel_outbox").fetchone()[0]
        lease_token = conn.execute(
            "SELECT fencing_token FROM kernel_work_leases"
        ).fetchone()[0]
    assert outbox_state == "in_flight"  # PR66 delivery truth survived
    assert lease_token == 1  # ownership authority survived

    # The semantic log restarts empty and never duplicates: the sequence
    # begins again at 1, and repair derives only what the surviving
    # authorities still prove.
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        assert await kernel_events.get_latest_sequence(factory, workspace_id="ws") == 0
        repaired = await kernel_scheduler.reconcile_dispatch(factory)
        assert len(repaired["events_repaired"]) == 1
        first = repaired["events_repaired"][0]
        assert first.event_type == "work.claimed" and first.semantic_sequence == 1
        assert first.payload["repair"] is True
        assert await kernel_scheduler.reconcile_dispatch(factory) == {
            "orphaned_deliveries_released": 0,
            "events_repaired": [],
        }
    finally:
        await engine.dispose()
