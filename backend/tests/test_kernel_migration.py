"""Kernel spine migration tests (V3.2 PR63A, plan workstream A).

Extends the PR62 acceptance matrix to the new Alembic head
``20260815_0004``:

- fresh DB upgrades to head with all four kernel tables;
- a database at the previous head ``20260709_0003`` upgrades to the new
  head with existing rows preserved;
- the kernel commit service refuses to run against an unmigrated
  database (no runtime self-heal of kernel schema);
- downgraded spine schema is classified as PENDING_UPGRADE (fail-closed
  classification, not repair).
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
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.records import ClaimAssertionRecord

KERNEL_TABLES = {
    "kernel_commit_heads",
    "kernel_commit_manifests",
    "kernel_records",
    "kernel_record_edges",
}

PREVIOUS_HEAD = "20260709_0003"


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
