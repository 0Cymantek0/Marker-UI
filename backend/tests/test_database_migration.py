"""Migration authority suite (V3.2 PR62).

Alembic is the sole persistent schema authority. These tests encode the
acceptance matrix from the PR62 plan:

- M1  fresh empty database reaches head via the supported runner;
- M2  already-current database: idempotent, zero schema churn;
- M3  known older Alembic revision upgrades to head;
- M4  recognized pre-Alembic legacy schema is adopted deliberately;
- M5  stale pre-Alembic legacy (missing later additive fields/tables)
      brought forward through the versioned path, never self-heal;
- M6  head-claiming but physically broken schema fails closed, unrepaired;
- M7  unknown revision / foreign or partially-equivalent schema fails closed;
- M8  forced migration failure never reports false success; retry recovers;
- M9  concurrent migration contenders serialize safely;
- M10 sentinel rows/values survive every supported upgrade path;
- M11 migration history integrity + ORM-vs-migration drift detection;
- M12 covered by the full backend suite staying green;
- M13 supported launch paths run the migration phase;
- M14 everything below is hermetic (tmp_path only, no local state).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app import db_migration
from app.db_migration import (
    DatabaseState,
    IncompatibleDatabaseError,
    MigrationLockTimeoutError,
    _MigrationLock,
    migration_head,
    upgrade_database,
    verify_database_ready,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]

EXPECTED_HEAD = "20260816_0009"
EXPECTED_REVISION_CHAIN = [
    "20260816_0009",
    "20260816_0008",
    "20260815_0007",
    "20260815_0006",
    "20260815_0005",
    "20260815_0004",
    "20260709_0003",
    "20260626_0002",
    "20260626_0001",
]
APP_TABLES = {"conversion_jobs", "settings", "audit_events", "job_events"}
KERNEL_TABLES = {
    "kernel_commit_heads",
    "kernel_commit_manifests",
    "kernel_records",
    "kernel_record_edges",
    "kernel_payload_objects",
    "kernel_outbox",
    "kernel_generations",
    "kernel_generation_records",
    "kernel_generation_edges",
    "kernel_generation_heads",
    "kernel_retention_roots",
    "kernel_reader_pins",
    "kernel_payload_retirements",
    "kernel_work_leases",
    "kernel_publications",
    "kernel_scheduling_entries",
    "kernel_scheduling_groups",
    "kernel_liveness",
    "kernel_events",
    "kernel_progress",
}
SENTINEL_JOB_ID = "sentinel-job"
SENTINEL_CREATED_AT = "2026-08-15 00:00:00.000000"


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _sqlite_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _shape(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    ]
    return {table: _sqlite_cols(conn, table) for table in tables}


def _version_row(conn: sqlite3.Connection) -> str | None:
    try:
        rows = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]
    except sqlite3.OperationalError:
        return None  # no version table (legacy database)
    return rows[0] if len(rows) == 1 else None


def _insert_sentinels(
    conn: sqlite3.Connection,
    *,
    include_job_events: bool = True,
    include_audit: bool = True,
) -> None:
    """Seed one recognizable row per table; upgrades must preserve them (M10)."""
    conn.execute(
        "INSERT INTO conversion_jobs "
        "(id, filename, original_name, status, input_format, output_format, "
        " progress, config_json, result_text) "
        "VALUES (?, 'sentinel-doc.pdf', 'sentinel-doc.pdf', 'completed', 'pdf', "
        "'markdown', 100, '{\"sentinel\": true}', '# Sentinel Output')",
        (SENTINEL_JOB_ID,),
    )
    conn.execute(
        "INSERT INTO settings (key, value, category) "
        "VALUES ('sentinel.key', 'sentinel-value', 'general')"
    )
    if include_audit:
        conn.execute(
            "INSERT INTO audit_events (id, event_type, status, redacted_payload_json, created_at) "
            "VALUES ('sentinel-audit', 'job.completed', 'success', '{}', ?)",
            (SENTINEL_CREATED_AT,),
        )
    if include_job_events:
        conn.execute(
            "INSERT INTO job_events (id, job_id, event_type, payload_json, created_at) "
            "VALUES ('sentinel-event', ?, 'progress', '{\"n\": 1}', ?)",
            (SENTINEL_JOB_ID, SENTINEL_CREATED_AT),
        )
    conn.commit()


def _assert_sentinels(
    conn: sqlite3.Connection,
    *,
    include_job_events: bool = True,
    include_audit: bool = True,
) -> None:
    job = conn.execute(
        "SELECT filename, status, config_json, result_text FROM conversion_jobs WHERE id = ?",
        (SENTINEL_JOB_ID,),
    ).fetchone()
    assert job == (
        "sentinel-doc.pdf",
        "completed",
        '{"sentinel": true}',
        "# Sentinel Output",
    )
    setting = conn.execute(
        "SELECT value, category FROM settings WHERE key = 'sentinel.key'"
    ).fetchone()
    assert setting == ("sentinel-value", "general")
    if include_audit:
        audit = conn.execute(
            "SELECT event_type, created_at FROM audit_events WHERE id = 'sentinel-audit'"
        ).fetchone()
        assert audit == ("job.completed", SENTINEL_CREATED_AT)
    if include_job_events:
        event = conn.execute(
            "SELECT job_id, payload_json FROM job_events WHERE id = 'sentinel-event'"
        ).fetchone()
        assert event == (SENTINEL_JOB_ID, '{"n": 1}')


def _build_at_revision(url: str, revision: str) -> None:
    db_migration._run_upgrade(url, revision)


# ---------------------------------------------------------------------------
# M1 - fresh installs reach head through the migration authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m1_runner_initializes_empty_database_to_head(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    url = _db_url(db_path)

    result = await upgrade_database(url=url)

    assert result.action == "initialized"
    assert result.to_revision == EXPECTED_HEAD
    assert result.from_revision is None
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        shape = _shape(conn)
        assert set(shape) == APP_TABLES | KERNEL_TABLES
        assert {"retry_count", "max_retries", "result_metadata_json", "formats_json"} <= shape[
            "conversion_jobs"
        ]
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {
            "ix_settings_key",
            "ix_audit_events_created_at",
            "ix_audit_events_event_type",
            "ix_conversion_jobs_idempotency_key",
            "ix_job_events_created_at",
            "ix_job_events_event_type",
            "ix_job_events_job_id",
        } <= indexes
    status = await verify_database_ready(url=url)
    assert status.state is DatabaseState.CURRENT


def test_m1_fresh_cli_upgrade_from_repo_scripts_creates_schema(tmp_path: Path) -> None:
    """The `alembic -c backend/alembic.ini upgrade head` path stays viable too."""
    db_path = tmp_path / "fresh-alembic.db"
    env = os.environ.copy()
    env["MARKER_DATABASE_URL"] = _db_url(db_path)

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        assert set(_shape(conn)) == APP_TABLES | KERNEL_TABLES


@pytest.mark.asyncio
async def test_m13_migration_cli_upgrade_status_check(tmp_path: Path) -> None:
    """`python -m app.db_migration` is the operator-facing authority entrypoint."""
    db_path = tmp_path / "cli.db"
    url = _db_url(db_path)

    def run_cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "app.db_migration", *args],
            cwd=BACKEND_DIR,
            text=True,
            capture_output=True,
            check=False,
        )

    fresh = run_cli("upgrade", "--url", url)
    assert fresh.returncode == 0, fresh.stderr
    assert "action=initialized" in fresh.stdout

    # status / check on the migrated database
    status = run_cli("status", "--url", url)
    assert status.returncode == 0, status.stderr
    assert f"database revision: {EXPECTED_HEAD}" in status.stdout
    assert "database state: current" in status.stdout
    check = run_cli("check", "--url", url)
    assert check.returncode == 0, check.stderr
    assert "database ready" in check.stdout

    # check fails closed on a database that was never migrated
    other = run_cli("check", "--url", _db_url(tmp_path / "missing.db"))
    assert other.returncode == 1
    assert "migration failure" in other.stderr


# ---------------------------------------------------------------------------
# M2 - already-current database is untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m2_current_database_is_idempotent_and_churn_free(tmp_path: Path) -> None:
    db_path = tmp_path / "current.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    with closing(sqlite3.connect(db_path)) as conn:
        _insert_sentinels(conn)
    mtime_before = db_path.stat().st_mtime_ns
    with closing(sqlite3.connect(db_path)) as conn:
        shape_before = _shape(conn)

    result = await upgrade_database(url=url)

    assert result.action == "already-current"
    assert result.from_revision == EXPECTED_HEAD
    assert db_path.stat().st_mtime_ns == mtime_before  # no DDL, no writes
    with closing(sqlite3.connect(db_path)) as conn:
        assert _shape(conn) == shape_before
        assert _version_row(conn) == EXPECTED_HEAD
        _assert_sentinels(conn)


# ---------------------------------------------------------------------------
# M3 + M10 - older revision upgrades to head, sentinel data survives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m3_upgrade_from_older_revision_preserves_sentinels(tmp_path: Path) -> None:
    db_path = tmp_path / "older.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_build_at_revision, url, "20260626_0002")
    with closing(sqlite3.connect(db_path)) as conn:
        _insert_sentinels(conn)

    result = await upgrade_database(url=url)

    assert result.action == "upgraded"
    assert result.from_revision == "20260626_0002"
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        assert "result_metadata_json" in _sqlite_cols(conn, "conversion_jobs")
        assert "formats_json" in _sqlite_cols(conn, "conversion_jobs")
        _assert_sentinels(conn)  # M10: row identity and values survive
        defaults = conn.execute(
            "SELECT retry_count, max_retries FROM conversion_jobs WHERE id = ?",
            (SENTINEL_JOB_ID,),
        ).fetchone()
        assert defaults == (0, 0)  # server defaults materialized by revision 0002


# ---------------------------------------------------------------------------
# M4 + M5 + M10 - pre-Alembic legacy adoption
# ---------------------------------------------------------------------------


def _make_legacy(db_path: Path, url: str, revision: str, *, drop_tables: tuple[str, ...] = ()) -> None:
    """Build a legacy database: schema of `revision`, but no alembic_version."""
    _build_at_revision(url, revision)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DROP TABLE alembic_version")
        for table in drop_tables:
            conn.execute(f'DROP TABLE "{table}"')
        conn.commit()


@pytest.mark.asyncio
async def test_m4_legacy_at_0001_shape_is_adopted_with_data(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-0001.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_make_legacy, db_path, url, "20260626_0001")
    with closing(sqlite3.connect(db_path)) as conn:
        _insert_sentinels(conn, include_job_events=False)
        shape_before = _shape(conn)

    result = await upgrade_database(url=url)

    assert result.action == "adopted-legacy"
    assert result.to_revision == EXPECTED_HEAD
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        # M5: later additive fields/tables arrive via the versioned path.
        assert "formats_json" in _sqlite_cols(conn, "conversion_jobs")
        assert "job_events" in _shape(conn)
        assert "queue_backend" in _sqlite_cols(conn, "conversion_jobs")
        _assert_sentinels(conn, include_job_events=False)  # M10: legacy rows survive
        assert set(_shape(conn)["conversion_jobs"]) > shape_before["conversion_jobs"]


@pytest.mark.asyncio
async def test_m5_minimal_legacy_missing_table_and_columns(tmp_path: Path) -> None:
    """A legacy DB predating audit_events/job_events and all later columns."""
    db_path = tmp_path / "legacy-minimal.db"
    url = _db_url(db_path)
    await asyncio.to_thread(
        _make_legacy, db_path, url, "20260626_0001", drop_tables=("audit_events",)
    )
    with closing(sqlite3.connect(db_path)) as conn:
        _insert_sentinels(conn, include_job_events=False, include_audit=False)
        assert set(_shape(conn)) == {"conversion_jobs", "settings"}

    result = await upgrade_database(url=url)

    assert result.action == "adopted-legacy"
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        assert set(_shape(conn)) == APP_TABLES | KERNEL_TABLES
        _assert_sentinels(conn, include_job_events=False, include_audit=False)


@pytest.mark.asyncio
async def test_m4_legacy_already_at_head_shape_zero_structural_churn(tmp_path: Path) -> None:
    """Legacy DB whose shape equals head: verified adoption, no blind stamp DDL."""
    db_path = tmp_path / "legacy-head.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_make_legacy, db_path, url, EXPECTED_HEAD)
    with closing(sqlite3.connect(db_path)) as conn:
        _insert_sentinels(conn)
        shape_before = _shape(conn)

    result = await upgrade_database(url=url)

    assert result.action == "adopted-legacy"
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        assert _shape(conn) == shape_before  # nothing rebuilt, nothing lost
        _assert_sentinels(conn)


# ---------------------------------------------------------------------------
# M6 - head-claiming but broken schema fails closed, never repaired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m6_head_claim_missing_column_fails_closed_unrepaired(tmp_path: Path) -> None:
    db_path = tmp_path / "broken-column.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("ALTER TABLE conversion_jobs DROP COLUMN formats_json")
        conn.commit()
    mtime_before = db_path.stat().st_mtime_ns

    with pytest.raises(IncompatibleDatabaseError) as verify_err:
        await verify_database_ready(url=url)
    assert "conversion_jobs.formats_json" in str(verify_err.value)

    with pytest.raises(IncompatibleDatabaseError) as upgrade_err:
        await upgrade_database(url=url)
    assert "refusing to migrate" in str(upgrade_err.value)

    # No repair happened behind the operator's back.
    assert db_path.stat().st_mtime_ns == mtime_before
    with closing(sqlite3.connect(db_path)) as conn:
        assert "formats_json" not in _sqlite_cols(conn, "conversion_jobs")
        assert _version_row(conn) == EXPECTED_HEAD


@pytest.mark.asyncio
async def test_m6_head_claim_missing_table_fails_closed_unrepaired(tmp_path: Path) -> None:
    db_path = tmp_path / "broken-table.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DROP TABLE job_events")
        conn.commit()

    with pytest.raises(IncompatibleDatabaseError) as err:
        await verify_database_ready(url=url)
    assert "job_events" in str(err.value)

    with pytest.raises(IncompatibleDatabaseError):
        await upgrade_database(url=url)
    with closing(sqlite3.connect(db_path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "job_events" not in tables  # not silently recreated


# ---------------------------------------------------------------------------
# M7 - unknown / foreign / partially-equivalent states fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m7_unknown_revision_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown-rev.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)
    bogus = "9999_nonexistent"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", (bogus,))
        conn.commit()

    with pytest.raises(IncompatibleDatabaseError) as verify_err:
        await verify_database_ready(url=url)
    assert bogus in str(verify_err.value)

    with pytest.raises(IncompatibleDatabaseError):
        await upgrade_database(url=url)
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == bogus  # untouched


@pytest.mark.asyncio
async def test_m7_foreign_table_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "foreign.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_make_legacy, db_path, url, EXPECTED_HEAD)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE mystery_widget (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.commit()

    with pytest.raises(IncompatibleDatabaseError) as err:
        await upgrade_database(url=url)
    assert "mystery_widget" in str(err.value)
    with closing(sqlite3.connect(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "mystery_widget" in tables  # not dropped, not altered
        assert "alembic_version" not in tables  # not stamped either


@pytest.mark.asyncio
async def test_m7_partially_equivalent_legacy_rejected(tmp_path: Path) -> None:
    """Legacy shape matching no known state (extra unknown column) is rejected."""
    db_path = tmp_path / "partial.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_make_legacy, db_path, url, "20260626_0001")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("ALTER TABLE conversion_jobs ADD COLUMN custom_extra TEXT")
        conn.commit()

    with pytest.raises(IncompatibleDatabaseError) as err:
        await upgrade_database(url=url)
    assert "custom_extra" in str(err.value)
    with closing(sqlite3.connect(db_path)) as conn:
        assert "custom_extra" in _sqlite_cols(conn, "conversion_jobs")  # untouched
        assert _version_row(conn) is None


@pytest.mark.asyncio
async def test_m7_legacy_incompatible_column_type_rejected(tmp_path: Path) -> None:
    """A legacy column with an incompatible type affinity is not adopted."""
    db_path = tmp_path / "bad-type.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_make_legacy, db_path, url, "20260626_0001")
    with closing(sqlite3.connect(db_path)) as conn:
        # progress is INTEGER in every known revision; recreate it as TEXT.
        conn.execute("ALTER TABLE conversion_jobs DROP COLUMN progress")
        conn.execute("ALTER TABLE conversion_jobs ADD COLUMN progress TEXT")
        conn.execute("UPDATE conversion_jobs SET progress = '42'")
        conn.commit()

    with pytest.raises(IncompatibleDatabaseError):
        await upgrade_database(url=url)


# ---------------------------------------------------------------------------
# M8 - forced failure never becomes false success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m8_failed_upgrade_keeps_honest_state_and_retry_recovers(tmp_path: Path) -> None:
    db_path = tmp_path / "failing.db"
    url = _db_url(db_path)
    await asyncio.to_thread(_build_at_revision, url, "20260626_0002")
    with closing(sqlite3.connect(db_path)) as conn:
        _insert_sentinels(conn)
        shape_before = _shape(conn)

    # Force a disk-level write failure for the migration writer. Restore the
    # original mode afterwards: stat.S_IWRITE alone is write-only (0o200) on
    # POSIX, which would make the verification reopen below fail with
    # "unable to open database file".
    original_mode = stat.S_IMODE(os.stat(db_path).st_mode)
    os.chmod(db_path, stat.S_IREAD)
    try:
        with pytest.raises(Exception) as upgrade_err:
            await upgrade_database(url=url)
        assert not isinstance(upgrade_err.value, IncompatibleDatabaseError)
        # Failure was not swallowed into a success-shaped result.
        with pytest.raises(IncompatibleDatabaseError):
            await verify_database_ready(url=url)
    finally:
        os.chmod(db_path, original_mode)

    with closing(sqlite3.connect(db_path)) as conn:
        # Transactional DDL rolled back: still at 0002 with intact data.
        assert _version_row(conn) == "20260626_0002"
        assert _shape(conn) == shape_before
        _assert_sentinels(conn)

    # Deterministic recovery: rerun the same path after the fault clears.
    result = await upgrade_database(url=url)
    assert result.action == "upgraded"
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD
        _assert_sentinels(conn)


@pytest.mark.asyncio
async def test_m8_lock_timeout_never_reports_success(tmp_path: Path) -> None:
    """While another writer holds the lock, upgrade fails honestly."""
    db_path = tmp_path / "locked.db"
    url = _db_url(db_path)
    with _MigrationLock(db_path, timeout=0.1):
        with pytest.raises(MigrationLockTimeoutError):
            await upgrade_database(url=url, lock_timeout=0.2)
    # After release the same upgrade succeeds.
    result = await upgrade_database(url=url)
    assert result.action == "initialized"


def test_stale_lock_from_dead_process_is_recovered(tmp_path: Path) -> None:
    db_path = tmp_path / "stale-lock.db"
    lock_path = tmp_path / "stale-lock.db.migration.lock"
    lock_path.write_text('{"pid": 2147483646, "created": 1.0}', encoding="utf-8")

    with _MigrationLock(db_path, timeout=0.1):
        pass  # acquired by stealing the stale lock

    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# M9 - concurrent contenders serialize safely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m9_concurrent_cli_upgrades_serialize(tmp_path: Path) -> None:
    db_path = tmp_path / "contended.db"
    url = _db_url(db_path)
    env = os.environ.copy()

    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "app.db_migration", "upgrade", "--url", url],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=120) for process in processes]

    for process, (stdout, stderr) in zip(processes, outputs):
        assert process.returncode == 0, stderr or stdout
    with closing(sqlite3.connect(db_path)) as conn:
        assert _version_row(conn) == EXPECTED_HEAD  # single version row, head
        assert set(_shape(conn)) == APP_TABLES | KERNEL_TABLES
    assert not (tmp_path / "contended.db.migration.lock").exists()


# ---------------------------------------------------------------------------
# M11 - history integrity and ORM/migration drift detection
# ---------------------------------------------------------------------------


def test_m11_single_expected_head_and_linear_history() -> None:
    assert migration_head() == EXPECTED_HEAD

    script = db_migration.ScriptDirectory(str(db_migration.SCRIPT_LOCATION))
    revisions = list(script.walk_revisions())  # head -> base
    assert [rev.revision for rev in revisions] == EXPECTED_REVISION_CHAIN
    assert revisions[-1].down_revision is None


def test_m11_orm_metadata_matches_migration_head(tmp_path: Path) -> None:
    """Adding a model column without a revision fails here (CI drift gate)."""
    from app.database import Base

    db_path = tmp_path / "drift.db"
    _build_at_revision(_db_url(db_path), EXPECTED_HEAD)
    with closing(sqlite3.connect(db_path)) as conn:
        migrated = _shape(conn)

    orm = {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
    }
    assert set(orm) == set(migrated), "model tables and migration tables diverge"
    for table, columns in orm.items():
        assert columns == set(migrated[table]), (
            f"column drift for {table}: model={sorted(columns)} "
            f"migration={sorted(migrated[table])}"
        )


@pytest.mark.asyncio
async def test_m11_model_drift_fails_closed(tmp_path: Path) -> None:
    """Simulated ORM drift (model column without migration) is detected."""
    db_path = tmp_path / "drift-state.db"
    url = _db_url(db_path)
    await upgrade_database(url=url)

    original = db_migration._orm_shape
    drifted = dict(original())
    drifted["conversion_jobs"] = {**drifted["conversion_jobs"], "unreleased_column": "TEXT"}
    db_migration._orm_shape = lambda: drifted  # type: ignore[assignment]
    try:
        with pytest.raises(IncompatibleDatabaseError) as err:
            await verify_database_ready(url=url)
        assert "unreleased_column" in str(err.value)
    finally:
        db_migration._orm_shape = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Runtime gate - startup validates, never mutates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_lifespan_validates_instead_of_creating_schema(db_session) -> None:
    from unittest.mock import AsyncMock, PropertyMock, patch

    from app.main import lifespan
    from fastapi import FastAPI

    class _SessionContextMock:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    gate = AsyncMock()
    app = FastAPI()
    with patch("app.db_migration.verify_database_ready", gate), \
         patch("app.database.async_session_factory", return_value=_SessionContextMock()), \
         patch("app.main._load_models_background"), \
         patch("app.core.api_manager.load_secrets_from_db"), \
         patch(
             "app.services.gpu_service.GPUService.status_dict",
             new_callable=PropertyMock,
             return_value={"cuda_available": False},
         ):
        async with lifespan(app):
            gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_lifespan_fails_closed_on_incompatible_database() -> None:
    from unittest.mock import AsyncMock, patch

    from app.main import lifespan
    from fastapi import FastAPI

    gate = AsyncMock(side_effect=IncompatibleDatabaseError("database not compatible"))
    app = FastAPI()
    with patch("app.db_migration.verify_database_ready", gate):
        with pytest.raises(IncompatibleDatabaseError):
            async with lifespan(app):
                pass


@pytest.mark.asyncio
async def test_agent_api_ready_check_validates_without_mutating() -> None:
    from unittest.mock import AsyncMock, patch

    from app import agent_api

    gate = AsyncMock()
    previous_ready = agent_api._db_ready
    agent_api._db_ready = False
    try:
        with patch("app.agent_api.verify_database_ready", gate):
            await agent_api._ensure_db_ready()
            gate.assert_awaited_once()
            assert agent_api._db_ready is True
            await agent_api._ensure_db_ready()
            gate.assert_awaited_once()  # cached: no re-validation per call
    finally:
        agent_api._db_ready = previous_ready


# ---------------------------------------------------------------------------
# Authority guard - production self-heal must stay dead
# ---------------------------------------------------------------------------


def test_no_production_self_heal_remains() -> None:
    """Reintroducing create_all/add-missing-column repair in production code fails."""
    database_source = (BACKEND_DIR / "app" / "database.py").read_text(encoding="utf-8")
    assert "create_all" not in database_source
    assert "create_tables" not in database_source
    assert "_add_missing_columns" not in database_source

    main_source = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert "create_tables" not in main_source

    agent_source = (BACKEND_DIR / "app" / "agent_api.py").read_text(encoding="utf-8")
    assert "create_tables" not in agent_source


def test_launchers_run_migration_phase_before_backend() -> None:
    """M13: supported launch paths invoke the migration authority before Uvicorn."""
    start_sh = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
    assert "app.db_migration upgrade" in start_sh
    assert start_sh.index("app.db_migration upgrade") < start_sh.index("uvicorn app.main:app")

    start_ps1 = (REPO_ROOT / "start.ps1").read_text(encoding="utf-8")
    assert "app.db_migration" in start_ps1
    assert start_ps1.index("app.db_migration") < start_ps1.index("uvicorn")

    supervisord = (REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
    assert "app.db_migration upgrade" in supervisord
    assert supervisord.index("app.db_migration upgrade") < supervisord.index("uvicorn app.main:app")
