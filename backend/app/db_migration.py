"""Alembic is the sole persistent schema authority for Marker UI (V3.2 PR62).

Contract:

- Alembic is the ONLY component allowed to create or mutate persistent
  schema. Application runtime validates compatibility but never repairs.
- ``upgrade_database`` is the single mutation entrypoint. It is exposed as
  ``python -m app.db_migration upgrade`` (run from ``backend/``) and invoked
  by every supported launcher before Uvicorn starts.
- ``verify_database_ready`` is the runtime gate. It must find the database
  at the current Alembic head with the schema shape the ORM expects, or it
  raises ``IncompatibleDatabaseError`` with an actionable diagnostic. It
  never mutates schema.
- Databases created before Alembic became authoritative (no
  ``alembic_version`` table) are adopted explicitly: their shape is
  validated as a subset of the head schema (no unknown tables, no unknown
  columns, compatible type affinities), then the full guarded revision
  chain is replayed from the base revision. Adoption relies on revisions
  ``20260626_0001``..``20260709_0003`` being inspect-and-skip guarded,
  which makes replay convergent on any subset shape; newer revisions only
  ever run on databases that already carry a trustworthy version table.
- A cross-process lock file next to the database serializes migration
  writers so concurrent launchers cannot produce mixed schema authority.
- Unknown or contradictory states fail closed with diagnostics; a failed
  migration can never leave the database reported as current (the runner
  re-verifies after every action).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import DATABASE_URL
from app.database import Base  # noqa: F401 - shared declarative base
from app.kernel.models import (  # noqa: F401 - register kernel spine tables
    KernelCommitHead,
    KernelCommitManifest,
    KernelRecord,
    KernelRecordEdge,
)
from app.models.audit import AuditEvent  # noqa: F401 - register tables on Base.metadata
from app.models.job import ConversionJob  # noqa: F401
from app.models.job_event import JobEvent  # noqa: F401
from app.models.settings import Setting  # noqa: F401

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPT_LOCATION = BACKEND_DIR / "alembic"

DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0
STALE_LOCK_SECONDS = 600.0


class MigrationError(Exception):
    """Base error for migration-authority failures (actionable message)."""


class MigrationLockTimeoutError(MigrationError):
    """Another migration writer held the lock for too long."""


class IncompatibleDatabaseError(MigrationError):
    """Database state is not safely consumable; operator action required."""


class DatabaseState(str, Enum):
    EMPTY = "empty"  # no file / no user tables
    CURRENT = "current"  # at head, shape matches migrations and ORM
    PENDING_UPGRADE = "pending-upgrade"  # known older revision, upgradeable
    ADOPTABLE_LEGACY = "adoptable-legacy"  # verified pre-Alembic shape
    INCONSISTENT_VERSION = "inconsistent-version"  # fail closed
    UNKNOWN_REVISION = "unknown-revision"  # fail closed
    HEAD_DIVERGENT = "head-divergent"  # claims head, shape broken (fail closed)
    MODEL_DRIFT = "model-drift"  # ORM metadata outgrew migrations (fail closed)
    DIVERGENT_AT_REVISION = "divergent-at-revision"  # unknown objects at known rev
    UNRECOGNIZED_LEGACY = "unrecognized-legacy"  # foreign/divergent schema


@dataclass
class DatabaseStatus:
    """Observable migration state of one database (invariant 6.6)."""

    state: DatabaseState
    revision: str | None
    head: str
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.state is DatabaseState.CURRENT

    def describe(self) -> str:
        lines = [
            f"database state: {self.state.value}",
            f"database revision: {self.revision if self.revision is not None else '<none>'}",
            f"migration head: {self.head}",
        ]
        lines.extend(f"problem: {problem}" for problem in self.problems)
        return "\n".join(lines)


@dataclass
class MigrationResult:
    """Outcome of one ``upgrade_database`` run."""

    action: str  # initialized | upgraded | adopted-legacy | already-current
    from_revision: str | None
    to_revision: str


# ---------------------------------------------------------------------------
# Alembic plumbing
# ---------------------------------------------------------------------------


def migration_head() -> str:
    """Return the single expected head revision known to the code."""
    script = ScriptDirectory(str(SCRIPT_LOCATION))
    heads = script.get_heads()
    if len(heads) != 1:
        raise MigrationError(
            f"alembic revision graph must have exactly one head, found {sorted(heads)}"
        )
    return heads[0]


def _revision_chain() -> list[str]:
    """All revision ids, base -> head order."""
    script = ScriptDirectory(str(SCRIPT_LOCATION))
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def _alembic_config(url: str) -> Config:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    cfg = Config()  # programmatic only: no ini, so env.py skips fileConfig
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.attributes["database_url"] = url
    return cfg


def _run_upgrade(url: str, revision: str) -> None:
    command.upgrade(_alembic_config(url), revision)


def _run_stamp(url: str, revision: str) -> None:
    command.stamp(_alembic_config(url), revision)


# ---------------------------------------------------------------------------
# Schema shapes (table -> column -> SQLite type affinity)
# ---------------------------------------------------------------------------

Shape = dict[str, dict[str, str]]

_AFFINITY_RULES = (
    ("INT", "INTEGER"),
    ("CHAR", "TEXT"),
    ("CLOB", "TEXT"),
    ("TEXT", "TEXT"),
    ("BLOB", "BLOB"),
    ("REAL", "REAL"),
    ("FLOA", "REAL"),
    ("DOUB", "REAL"),
)


def _affinity(type_name: str) -> str:
    upper = (type_name or "").upper()
    if not upper:
        return "BLOB"
    for needle, affinity in _AFFINITY_RULES:
        if needle in upper:
            return affinity
    return "NUMERIC"


def _read_shape(conn: sqlite3.Connection) -> Shape:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
    ]
    shape: Shape = {}
    for table in tables:
        columns = {
            row[1]: _affinity(row[2])
            for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        shape[table] = columns
    return shape


def _orm_shape() -> Shape:
    return {
        table.name: {
            column.name: _affinity(str(column.type)) for column in table.columns
        }
        for table in Base.metadata.sorted_tables
    }


_HEAD_REFERENCE_SHAPE: Shape | None = None


def _head_reference_shape() -> Shape:
    """Schema shape Alembic produces at head (fresh temp DB, cached per process)."""
    global _HEAD_REFERENCE_SHAPE
    if _HEAD_REFERENCE_SHAPE is None:
        # Silence Alembic's INFO lines for the throwaway reference database so
        # operator logs only mention the real target database.
        migration_logger = logging.getLogger("alembic.runtime.migration")
        previous_level = migration_logger.level
        migration_logger.setLevel(logging.WARNING)
        try:
            with tempfile.TemporaryDirectory(prefix="marker-ref-schema-") as tmp:
                db_path = Path(tmp) / "reference-head.db"
                url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
                _run_upgrade(url, migration_head())
                with closing(sqlite3.connect(db_path)) as conn:
                    _HEAD_REFERENCE_SHAPE = _read_shape(conn)
        finally:
            migration_logger.setLevel(previous_level)
    return _HEAD_REFERENCE_SHAPE


def _diff_problems(observed: Shape, expected: Shape, label: str) -> list[str]:
    problems: list[str] = []
    for table in sorted(set(expected) - set(observed)):
        problems.append(f"{label}: missing table '{table}'")
    for table in sorted(set(observed) - set(expected)):
        problems.append(f"{label}: unexpected table '{table}'")
    for table in sorted(set(observed) & set(expected)):
        missing = sorted(set(expected[table]) - set(observed[table]))
        extra = sorted(set(observed[table]) - set(expected[table]))
        for column in missing:
            problems.append(f"{label}: missing column '{table}.{column}'")
        for column in extra:
            problems.append(f"{label}: unexpected column '{table}.{column}'")
        for column in sorted(set(observed[table]) & set(expected[table])):
            if observed[table][column] != expected[table][column]:
                problems.append(
                    f"{label}: column '{table}.{column}' has affinity "
                    f"{observed[table][column]}, expected {expected[table][column]}"
                )
    return problems


def _subset_problems(observed: Shape, reference: Shape) -> list[str]:
    """Unknown-object check: observed must introduce nothing foreign to reference."""
    problems: list[str] = []
    for table in sorted(set(observed) - set(reference)):
        problems.append(f"unknown table '{table}' (not in migration head schema)")
    for table in sorted(set(observed) & set(reference)):
        for column in sorted(set(observed[table]) - set(reference[table])):
            problems.append(f"unknown column '{table}.{column}'")
        for column in sorted(set(observed[table]) & set(reference[table])):
            if observed[table][column] != reference[table][column]:
                problems.append(
                    f"column '{table}.{column}' has affinity "
                    f"{observed[table][column]}, expected {reference[table][column]}"
                )
    return problems


# ---------------------------------------------------------------------------
# URL / lock helpers
# ---------------------------------------------------------------------------


def _sqlite_path(url: str) -> Path:
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            tail = url[len(prefix) :]
            break
    else:
        raise MigrationError(
            f"unsupported database URL for migrations: {url!r} "
            "(only file-backed SQLite URLs are supported)"
        )
    if not tail or tail == ":memory:" or "mode=memory" in tail:
        raise MigrationError(
            "in-memory databases cannot be migration-managed; "
            "use a file-backed SQLite URL"
        )
    return Path(tail)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


class _MigrationLock:
    """Exclusive cross-process lock file serializing migration writers."""

    def __init__(self, db_path: Path, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self._lock_path = db_path.parent / (db_path.name + ".migration.lock")
        self._timeout = max(0.0, timeout)
        self._acquired = False

    def acquire(self) -> None:
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._try_steal_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise MigrationLockTimeoutError(
                        f"another migration writer holds {self._lock_path}; "
                        f"waited {self._timeout:.0f}s. Retry once the other "
                        "launcher finishes, or delete the lock file if its "
                        "owner process is gone."
                    )
                time.sleep(0.25)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "created": time.time()}, handle)
            self._acquired = True
            return

    def _try_steal_stale(self) -> bool:
        try:
            raw = self._lock_path.read_text(encoding="utf-8")
            info = json.loads(raw)
            pid = int(info.get("pid", -1))
            created = float(info.get("created", 0.0))
        except (OSError, ValueError, TypeError):
            pid, created = -1, 0.0
        pid_gone = pid > 0 and not _pid_alive(pid)
        expired = (time.time() - created) > STALE_LOCK_SECONDS
        if pid_gone or expired or pid <= 0:
            try:
                self._lock_path.unlink()
            except OSError:
                return False
            logger.warning("Removed stale migration lock %s", self._lock_path)
            return True
        return False

    def release(self) -> None:
        if self._acquired:
            self._acquired = False
            try:
                self._lock_path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "_MigrationLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def inspect_database(url: str | None = None) -> DatabaseStatus:
    """Classify the migration state of the database at ``url`` (read-only)."""
    url = url or DATABASE_URL
    head = migration_head()
    path = _sqlite_path(url)
    if not path.exists():
        return DatabaseStatus(DatabaseState.EMPTY, None, head)
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        except sqlite3.DatabaseError as exc:
            return DatabaseStatus(
                DatabaseState.UNRECOGNIZED_LEGACY,
                None,
                head,
                [f"sqlite database unreadable: {exc}"],
            )
        if not any(not name.startswith("sqlite_") for name in tables):
            return DatabaseStatus(DatabaseState.EMPTY, None, head)
        shape = _read_shape(conn)
        if "alembic_version" not in tables:
            return _classify_legacy(shape, head)
        try:
            rows = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]
        except sqlite3.DatabaseError as exc:
            return DatabaseStatus(
                DatabaseState.INCONSISTENT_VERSION,
                None,
                head,
                [f"alembic_version table unreadable: {exc}"],
            )
    if len(rows) != 1:
        return DatabaseStatus(
            DatabaseState.INCONSISTENT_VERSION,
            None,
            head,
            [f"alembic_version must hold exactly one row, found {len(rows)}: {rows}"],
        )
    revision = rows[0]
    if revision not in _revision_chain():
        return DatabaseStatus(
            DatabaseState.UNKNOWN_REVISION,
            revision,
            head,
            [
                f"alembic_version references unknown revision '{revision}'; "
                f"known revisions: {_revision_chain()}"
            ],
        )
    if revision == head:
        return _classify_at_head(shape, head)
    problems = _subset_problems(shape, _head_reference_shape())
    if problems:
        return DatabaseStatus(DatabaseState.DIVERGENT_AT_REVISION, revision, head, problems)
    return DatabaseStatus(DatabaseState.PENDING_UPGRADE, revision, head)


def _classify_at_head(shape: Shape, head: str) -> DatabaseStatus:
    head_reference = _head_reference_shape()
    physical = _diff_problems(shape, head_reference, "schema claims head but")
    orm = _orm_shape()
    drift = _diff_problems(orm, head_reference, "ORM metadata drifted from migrations:")
    if physical:
        return DatabaseStatus(DatabaseState.HEAD_DIVERGENT, head, head, physical)
    if drift:
        return DatabaseStatus(DatabaseState.MODEL_DRIFT, head, head, drift)
    return DatabaseStatus(DatabaseState.CURRENT, head, head)


def _classify_legacy(shape: Shape, head: str) -> DatabaseStatus:
    problems = _subset_problems(shape, _head_reference_shape())
    if problems:
        return DatabaseStatus(DatabaseState.UNRECOGNIZED_LEGACY, None, head, problems)
    return DatabaseStatus(DatabaseState.ADOPTABLE_LEGACY, None, head)


# ---------------------------------------------------------------------------
# Public entrypoints (async; heavy work runs in a worker thread because
# Alembic's async env calls asyncio.run internally)
# ---------------------------------------------------------------------------


async def verify_database_ready(url: str | None = None) -> DatabaseStatus:
    """Runtime gate: the database must be usable, or this raises. Never mutates."""
    return await asyncio.to_thread(_verify_database_ready_sync, url)


def _verify_database_ready_sync(url: str | None) -> DatabaseStatus:
    status = inspect_database(url)
    if not status.usable:
        raise IncompatibleDatabaseError(
            "database is not compatible with this version of Marker UI and was "
            "NOT auto-repaired. Alembic is the sole schema authority.\n"
            + status.describe()
            + "\noperator action: run `python -m app.db_migration upgrade` "
            "(from the backend directory) or relaunch via start.sh / start.ps1 "
            "/ the container entrypoint, which run migrations automatically."
        )
    return status


async def upgrade_database(
    url: str | None = None,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> MigrationResult:
    """The only persistent-schema mutation entrypoint (invariant 6.1)."""
    return await asyncio.to_thread(_upgrade_database_sync, url, lock_timeout)


def _upgrade_database_sync(
    url: str | None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> MigrationResult:
    url = url or DATABASE_URL
    head = migration_head()
    path = _sqlite_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _MigrationLock(path, lock_timeout):
        status = inspect_database(url)
        if status.state is DatabaseState.EMPTY:
            action, from_revision = "initialized", None
            _run_upgrade(url, head)
        elif status.state is DatabaseState.CURRENT:
            return MigrationResult("already-current", head, head)
        elif status.state is DatabaseState.PENDING_UPGRADE:
            action, from_revision = "upgraded", status.revision
            _run_upgrade(url, head)
        elif status.state is DatabaseState.ADOPTABLE_LEGACY:
            action, from_revision = "adopted-legacy", None
            _run_stamp(url, "base")
            _run_upgrade(url, head)
        else:
            raise IncompatibleDatabaseError(
                "refusing to migrate an unrecognized database state; "
                "no schema was changed.\n" + status.describe()
            )
        # A completed migration must actually be current (invariant 6.8).
        final = inspect_database(url)
        if final.state is not DatabaseState.CURRENT:
            raise MigrationError(
                f"migration action '{action}' finished but the database did not "
                f"verify as current; no false success is reported.\n{final.describe()}"
            )
        logger.info("Database migration: %s -> %s (%s)", from_revision, head, action)
        return MigrationResult(action, from_revision, final.revision or head)


# ---------------------------------------------------------------------------
# CLI: python -m app.db_migration {upgrade,status,check}
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=None,
        help="Database URL override (defaults to MARKER_DATABASE_URL / app config).",
    )
    parser = argparse.ArgumentParser(
        prog="python -m app.db_migration",
        description="Alembic migration authority for Marker UI persistent schema.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "upgrade",
        parents=[common],
        help="Bring the database to the migration head (the only schema-mutating path).",
    )
    sub.add_parser("status", parents=[common], help="Print the migration state of the database.")
    sub.add_parser(
        "check",
        parents=[common],
        help="Exit 0 if the database is ready for app runtime, 1 otherwise.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "upgrade":
            result = asyncio.run(upgrade_database(args.url))
            print(
                f"migration OK: action={result.action} "
                f"from={result.from_revision or '<none>'} to={result.to_revision}"
            )
            return 0
        if args.command == "status":
            print(inspect_database(args.url).describe())
            return 0
        # check
        asyncio.run(verify_database_ready(args.url))
        print("database ready: at migration head and structurally compatible")
        return 0
    except MigrationError as exc:
        print(f"migration failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
