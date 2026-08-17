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
- A cross-process OS-backed file lock next to the database serializes
  migration writers so concurrent launchers cannot produce mixed schema
  authority. Mutual exclusion is enforced by the kernel (byte-range lock
  on Windows, ``flock`` elsewhere), so ownership is released exactly when
  the holder process exits: a live-but-slow migration can never be
  displaced by wall-clock age, and a dead holder is recovered without a
  timeout heuristic. The lock file body is diagnostic metadata only.
  Supported filesystem assumption: local filesystems with standard lock
  semantics (the same profile SQLite's local single-writer mode needs);
  network filesystems may behave differently and are out of scope.
- ``verify_database_ready`` compares the schema *contract*, not just
  column names/affinities: primary-key shape, required unique scopes,
  explicit nullability of authority columns, and foreign keys. Losing a
  correctness-critical constraint (damaged database) fails closed even
  when every table and column still exists. Non-unique indexes are
  performance details, not startup invariants.
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
from sqlalchemy import Column, UniqueConstraint

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
# Schema contracts (structured, semantics-relevant introspection)
# ---------------------------------------------------------------------------
# A contract captures, per table, exactly the schema properties the kernel
# relies on for correctness: column affinities, primary-key shape (column
# set AND order), required unique scopes, explicit nullability of
# non-primary-key authority columns, and foreign keys. Non-unique indexes
# are deliberately excluded: they are performance details, not invariants,
# so an accidentally dropped performance index must not brick startup.
# Names (auto-index names, constraint names) are normalized away — two
# schemas with equal semantics but different rendering compare equal.

Contract = dict[str, dict[str, Any]]

#: Runtime-managed FTS5 virtual tables (one per lexical generation, PR76)
#: and their shadow tables share this prefix. They are rebuildable
#: derived serving state created at index-build time — never Alembic
#: schema authority and never ORM models — so both sides of every
#: contract comparison exclude them symmetrically.
RUNTIME_FTS_TABLE_PREFIX = "kernel_fts_"


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


def _temporal(type_name: str) -> bool:
    """Audit-timestamp columns (DATETIME/DATE/TIME families).

    Kernel doctrine: wall-clock timestamps are descriptive audit fields,
    never ordering or truth authority (ordering is integer commit/
    sequence identity). Their nullability is therefore deliberately NOT
    a startup-fatal invariant — migrations historically render them
    nullable while the ORM derives NOT NULL — so both sides of the
    contract exclude them from the notnull comparison symmetrically.
    Non-temporal authority columns keep full nullability enforcement.
    """
    upper = (type_name or "").upper()
    return "DATE" in upper or "TIME" in upper


def _read_contract(conn: sqlite3.Connection) -> Contract:
    """Structured correctness-relevant schema facts of a live database."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
        # Runtime-managed FTS5 serving state (PR76): rebuildable derived
        # index tables that exist only after a build. Excluded from both
        # sides of every comparison, exactly like sqlite_ internals.
        if not row[0].startswith(RUNTIME_FTS_TABLE_PREFIX)
    ]
    contract: Contract = {}
    for table in tables:
        info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        columns = {row[1]: _affinity(row[2]) for row in info}
        # table_info "pk" is the 1-based position within the primary key;
        # NULLability of PK members is governed by the pk check itself.
        pk = tuple(
            row[1] for row in sorted((r for r in info if r[5] > 0), key=lambda r: r[5])
        )
        notnull = sorted(
            row[1] for row in info if row[3] and row[5] == 0 and not _temporal(row[2])
        )
        unique_scopes: set[tuple[tuple[str, ...], bool]] = set()
        # index_list: (seq, name, unique, origin, partial). origin 'u' is a
        # UNIQUE constraint, 'c' a created index, 'pk' the implicit primary
        # key index (covered by the pk tuple above).
        for index in conn.execute(f'PRAGMA index_list("{table}")'):
            is_unique, origin, partial = bool(index[2]), index[3], bool(index[4])
            if not is_unique or origin == "pk":
                continue
            cols = tuple(
                row[2] if row[2] is not None else f"<expr#{row[1]}>"
                for row in conn.execute(f'PRAGMA index_info("{index[1]}")')
            )
            unique_scopes.add((cols, partial))
        foreign_keys = sorted(
            (row[3], row[2], row[4])
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
        )
        contract[table] = {
            "columns": columns,
            "pk": pk,
            "notnull": tuple(notnull),
            "unique": tuple(sorted(unique_scopes)),
            "foreign_keys": tuple(foreign_keys),
        }
    return contract


def _orm_contract() -> Contract:
    """The contract the ORM metadata declares (drift gate)."""
    contract: Contract = {}
    for table in Base.metadata.sorted_tables:
        pk = tuple(column.name for column in table.primary_key.columns)
        notnull = sorted(
            column.name
            for column in table.columns
            if not column.nullable
            and column.name not in pk
            and not _temporal(str(column.type))
        )
        unique_scopes: set[tuple[tuple[str, ...], bool]] = set()
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                unique_scopes.add(
                    (tuple(column.name for column in constraint.columns), False)
                )
        for index in table.indexes:
            if index.unique:
                cols = tuple(
                    column.name if isinstance(column, Column) else str(column)
                    for column in index.columns
                )
                unique_scopes.add(
                    (cols, bool(index.dialect_kwargs.get("sqlite_where")))
                )
        foreign_keys = sorted(
            (fk.parent.name, fk.column.table.name, fk.column.name)
            for fk in table.foreign_keys
        )
        contract[table.name] = {
            "columns": {
                column.name: _affinity(str(column.type)) for column in table.columns
            },
            "pk": pk,
            "notnull": tuple(notnull),
            "unique": tuple(sorted(unique_scopes)),
            "foreign_keys": tuple(foreign_keys),
        }
    return contract


_HEAD_REFERENCE_CONTRACT: Contract | None = None


def _head_reference_contract() -> Contract:
    """Schema contract Alembic produces at head (fresh temp DB, cached per process)."""
    global _HEAD_REFERENCE_CONTRACT
    if _HEAD_REFERENCE_CONTRACT is None:
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
                    _HEAD_REFERENCE_CONTRACT = _read_contract(conn)
        finally:
            migration_logger.setLevel(previous_level)
    return _HEAD_REFERENCE_CONTRACT


def _contract_problems(observed: Contract, expected: Contract, label: str) -> list[str]:
    problems: list[str] = []
    for table in sorted(set(expected) - set(observed)):
        problems.append(f"{label}: missing table '{table}'")
    for table in sorted(set(observed) - set(expected)):
        problems.append(f"{label}: unexpected table '{table}'")
    for table in sorted(set(observed) & set(expected)):
        obs, exp = observed[table], expected[table]
        missing = sorted(set(exp["columns"]) - set(obs["columns"]))
        extra = sorted(set(obs["columns"]) - set(exp["columns"]))
        for column in missing:
            problems.append(f"{label}: missing column '{table}.{column}'")
        for column in extra:
            problems.append(f"{label}: unexpected column '{table}.{column}'")
        for column in sorted(set(obs["columns"]) & set(exp["columns"])):
            if obs["columns"][column] != exp["columns"][column]:
                problems.append(
                    f"{label}: column '{table}.{column}' has affinity "
                    f"{obs['columns'][column]}, expected {exp['columns'][column]}"
                )
        if obs["pk"] != exp["pk"]:
            problems.append(
                f"{label}: primary key of '{table}' is {list(obs['pk'])}, "
                f"expected {list(exp['pk'])}"
            )
        for column in sorted(set(exp["notnull"]) - set(obs["notnull"])):
            problems.append(
                f"{label}: column '{table}.{column}' lost its NOT NULL constraint"
            )
        for column in sorted(set(obs["notnull"]) - set(exp["notnull"])):
            problems.append(
                f"{label}: column '{table}.{column}' is NOT NULL, expected nullable"
            )
        for scope in sorted(set(exp["unique"]) - set(obs["unique"])):
            problems.append(
                f"{label}: table '{table}' lost unique scope {list(scope[0])}"
            )
        for scope in sorted(set(obs["unique"]) - set(exp["unique"])):
            problems.append(
                f"{label}: table '{table}' has unexpected unique scope {list(scope[0])}"
            )
        for fk in sorted(set(exp["foreign_keys"]) - set(obs["foreign_keys"])):
            problems.append(
                f"{label}: table '{table}' lost foreign key "
                f"{fk[0]} -> {fk[1]}.{fk[2]}"
            )
        for fk in sorted(set(obs["foreign_keys"]) - set(exp["foreign_keys"])):
            problems.append(
                f"{label}: table '{table}' has unexpected foreign key "
                f"{fk[0]} -> {fk[1]}.{fk[2]}"
            )
    return problems


def _subset_problems(observed: Contract, reference: Contract) -> list[str]:
    """Unknown-object check for pre-head databases: observed must introduce
    nothing foreign to the head contract. Constraints are not required to
    pre-exist — replaying the guarded revision chain installs them, and the
    post-upgrade head verification re-checks the full contract."""
    problems: list[str] = []
    for table in sorted(set(observed) - set(reference)):
        problems.append(f"unknown table '{table}' (not in migration head schema)")
    for table in sorted(set(observed) & set(reference)):
        for column in sorted(set(observed[table]["columns"]) - set(reference[table]["columns"])):
            problems.append(f"unknown column '{table}.{column}'")
        for column in sorted(
            set(observed[table]["columns"]) & set(reference[table]["columns"])
        ):
            if observed[table]["columns"][column] != reference[table]["columns"][column]:
                problems.append(
                    f"column '{table}.{column}' has affinity "
                    f"{observed[table]['columns'][column]}, expected "
                    f"{reference[table]['columns'][column]}"
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


def _try_lock_nb(fd: int) -> bool:
    """Acquire the OS lock non-blocking; False when another process holds it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # closing the fd releases the lock regardless
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


class _MigrationLock:
    """OS-backed exclusive lock serializing migration writers.

    Mutual exclusion is enforced by the kernel file lock (byte-range
    ``locking`` on Windows, ``flock`` elsewhere), so ownership ends
    exactly when the holder process exits. A live-but-slow migration can
    therefore never be displaced for exceeding a wall-clock age, and a
    dead holder is recovered immediately without any staleness
    heuristic. The lock file is never deleted after release: waiters
    must keep competing for the same inode, or a delete/recreate race
    could split waiters across inodes and admit two migration writers.
    Its body is diagnostic metadata (holder pid, creation time), never
    authority.
    """

    def __init__(self, db_path: Path, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self._lock_path = db_path.parent / (db_path.name + ".migration.lock")
        self._timeout = max(0.0, timeout)
        self._fd: int | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + self._timeout
        while True:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
            if _try_lock_nb(fd):
                self._fd = fd
                # Diagnostic body only; truncation is safe because we
                # hold the OS lock and no reader treats this as authority.
                os.ftruncate(fd, 0)
                # Write past the locked byte (offset 0) so contending
                # readers on Windows — where the byte-range lock denies
                # reads of the locked region — can still see the holder.
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(
                    fd,
                    json.dumps({"pid": os.getpid(), "created": time.time()}).encode(),
                )
                return
            os.close(fd)
            if time.monotonic() >= deadline:
                raise MigrationLockTimeoutError(self._contention_message())
            time.sleep(0.25)

    def _contention_message(self) -> str:
        holder = "unknown"
        try:
            with open(self._lock_path, "rb") as handle:
                handle.seek(1)  # diagnostics live past the locked byte
                raw = handle.read()
            info = json.loads(raw)
            pid = int(info.get("pid", -1))
            created = float(info.get("created", 0.0))
        except (OSError, ValueError, TypeError):
            pid, created = -1, 0.0
        if pid > 0:
            age = max(0.0, time.time() - created)
            liveness = (
                "still running" if _pid_alive(pid) else
                "no longer running (lock outlived its owner: filesystem or "
                "OS anomaly — inspect mounts/antivirus before acting)"
            )
            holder = f"pid {pid}, holding for {age:.0f}s, {liveness}"
        return (
            f"another migration writer holds the OS lock on "
            f"{self._lock_path} (holder: {holder}); waited "
            f"{self._timeout:.0f}s. The lock is released by the operating "
            "system only when the holder process exits — never delete the "
            "lock file while its owner may still be running."
        )

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

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
        contract = _read_contract(conn)
        if "alembic_version" not in tables:
            return _classify_legacy(contract, head)
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
        return _classify_at_head(contract, head)
    problems = _subset_problems(contract, _head_reference_contract())
    if problems:
        return DatabaseStatus(DatabaseState.DIVERGENT_AT_REVISION, revision, head, problems)
    return DatabaseStatus(DatabaseState.PENDING_UPGRADE, revision, head)


def _classify_at_head(contract: Contract, head: str) -> DatabaseStatus:
    head_reference = _head_reference_contract()
    physical = _contract_problems(contract, head_reference, "schema claims head but")
    orm = _orm_contract()
    drift = _contract_problems(orm, head_reference, "ORM metadata drifted from migrations:")
    if physical:
        return DatabaseStatus(DatabaseState.HEAD_DIVERGENT, head, head, physical)
    if drift:
        return DatabaseStatus(DatabaseState.MODEL_DRIFT, head, head, drift)
    return DatabaseStatus(DatabaseState.CURRENT, head, head)


def _classify_legacy(contract: Contract, head: str) -> DatabaseStatus:
    problems = _subset_problems(contract, _head_reference_contract())
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
