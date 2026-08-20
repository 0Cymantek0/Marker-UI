"""PostgreSQL profile of the Alembic migration authority (PR83A).

The migration contract described in ``app.db_migration`` applies
unchanged; this module implements its PostgreSQL profile:

* Introspection reads the schema contract from ``pg_catalog`` /
  ``information_schema`` through asyncpg and maps PostgreSQL types
  into the same affinity buckets the SQLite profile compares with,
  so one contract vocabulary serves both backends.
* Migration writers serialize on a session-level ``pg_advisory_lock``
  keyed by the target database — the PostgreSQL equivalent of the
  SQLite profile's OS file lock next to the database file. Ownership
  ends exactly when the holding connection closes.
* At-head verification compares the observed contract against the ORM
  metadata contract (the drift gate). The SQLite profile additionally
  diffs migrations-versus-ORM through a throwaway reference database;
  that gate runs on the local profile, and the comparison here
  inherits its conclusion because both profiles consume the same
  revision chain and the same metadata.
* Pre-Alembic legacy adoption is a local-profile convenience and is
  deliberately unavailable here: a PostgreSQL database must be born
  from Alembic (or arrive at a known stamped revision) to be touched.

Entrypoints mirror ``app.db_migration``: ``inspect_database`` (sync
wrapper with a running-loop guard), ``inspect_database_async``, and
``upgrade_database_sync`` (used by the shared async entrypoint's
worker thread). All require the asyncpg driver.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

POSTGRESQL_BACKEND = "postgresql"

DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.25

#: PostgreSQL ``information_schema.columns.data_type`` → the shared
#: affinity vocabulary. Types absent from this map (numeric, boolean,
#: json, date/time families, ...) intentionally fall through to NUMERIC,
#: which is the same bucket the ORM-side affinity rules assign to their
#: SQLAlchemy source types (Boolean/JSON/DateTime/... → NUMERIC).
_PG_AFFINITY = {
    "character varying": "TEXT",
    "character": "TEXT",
    "text": "TEXT",
    "name": "TEXT",
    "uuid": "TEXT",
    "inet": "TEXT",
    "cidr": "TEXT",
    "smallint": "INTEGER",
    "integer": "INTEGER",
    "bigint": "INTEGER",
    "bytea": "BLOB",
    "real": "REAL",
    "double precision": "REAL",
}


def _pg_affinity(data_type: str | None) -> str:
    return _PG_AFFINITY.get((data_type or "").lower(), "NUMERIC")


def _temporal(data_type: str | None) -> bool:
    upper = (data_type or "").upper()
    return "DATE" in upper or "TIME" in upper


def _advisory_lock_key(url: str) -> int:
    """Deterministic per-database advisory-lock key."""
    parsed = make_url(url)
    material = (
        f"marker-migration:{parsed.host}:{parsed.port}:{parsed.database}"
    ).encode()
    return int.from_bytes(
        hashlib.blake2b(material, digest_size=8).digest(), "big"
    ) % (1 << 63)


async def _connect(url: str):
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - environment-dependent
        from app.db_migration import MigrationError

        raise MigrationError(
            "the PostgreSQL migration profile requires the asyncpg driver "
            "(pip install asyncpg)"
        ) from exc
    dsn = make_url(url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    try:
        return await asyncpg.connect(dsn)
    except Exception as exc:
        from app.db_migration import MigrationError

        raise MigrationError(
            "cannot reach the PostgreSQL server for migrations; check that "
            "the database exists and the server accepts connections "
            f"({type(exc).__name__}: {exc})"
        ) from exc


async def _user_tables(conn) -> set[str]:
    from app.db_migration import RUNTIME_FTS_TABLE_PREFIX

    rows = await conn.fetch(
        """
        SELECT c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname = 'public'
          AND c.relname <> 'alembic_version'
        """
    )
    return {
        row["table_name"]
        for row in rows
        if not row["table_name"].startswith(RUNTIME_FTS_TABLE_PREFIX)
    }


async def _read_contract(conn, tables) -> "Contract":
    """Structured correctness-relevant schema facts (shared vocabulary)."""
    from app.db_migration import Contract

    contract: Contract = {}
    for table in sorted(tables):
        columns = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            """,
            table,
        )
        pk_rows = await conn.fetch(
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            CROSS JOIN LATERAL unnest(i.indkey::int[])
                WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = k.attnum
            WHERE i.indrelid = to_regclass($1) AND i.indisprimary
            ORDER BY k.ord
            """,
            f"public.{table}",
        )
        pk = tuple(row["column_name"] for row in pk_rows)
        pk_members = set(pk)
        notnull = sorted(
            row["column_name"]
            for row in columns
            if row["is_nullable"] == "NO"
            and row["column_name"] not in pk_members
            and not _temporal(row["data_type"])
        )
        unique_scopes: set[tuple[tuple[str, ...], bool]] = set()
        index_rows = await conn.fetch(
            """
            SELECT idx.indexrelid AS index_oid,
                   (idx.indpred IS NOT NULL) AS partial,
                   k.ord,
                   CASE WHEN k.attnum = 0
                        THEN '<expr#' || k.ord || '>'
                        ELSE a.attname END AS column_name
            FROM pg_index idx
            CROSS JOIN LATERAL unnest(idx.indkey::int[])
                WITH ORDINALITY AS k(attnum, ord)
            LEFT JOIN pg_attribute a
              ON a.attrelid = idx.indrelid AND a.attnum = k.attnum
            WHERE idx.indrelid = to_regclass($1)
              AND idx.indisunique AND NOT idx.indisprimary
            ORDER BY idx.indexrelid, k.ord
            """,
            f"public.{table}",
        )
        scope_columns: dict[int, list[str]] = {}
        scope_partial: dict[int, bool] = {}
        for row in index_rows:
            oid = row["index_oid"]
            scope_columns.setdefault(oid, []).append(row["column_name"])
            scope_partial[oid] = bool(row["partial"])
        for oid, cols in scope_columns.items():
            unique_scopes.add((tuple(cols), scope_partial[oid]))
        foreign_keys = sorted(
            (row["column_name"], row["ref_table"], row["ref_column"])
            for row in await conn.fetch(
                """
                SELECT src.attname AS column_name,
                       refcls.relname AS ref_table,
                       dst.attname AS ref_column
                FROM pg_constraint c
                CROSS JOIN LATERAL unnest(c.conkey)
                    WITH ORDINALITY AS s(attnum, ord)
                JOIN pg_attribute src
                  ON src.attrelid = c.conrelid AND src.attnum = s.attnum
                JOIN LATERAL unnest(c.confkey)
                    WITH ORDINALITY AS d(attnum, ord)
                  ON d.ord = s.ord
                JOIN pg_attribute dst
                  ON dst.attrelid = c.confrelid AND dst.attnum = d.attnum
                JOIN pg_class refcls ON refcls.oid = c.confrelid
                JOIN pg_namespace refns ON refns.oid = refcls.relnamespace
                WHERE c.conrelid = to_regclass($1)
                  AND c.contype = 'f'
                  AND refns.nspname = 'public'
                ORDER BY s.ord
                """,
                f"public.{table}",
            )
        )
        contract[table] = {
            "columns": {
                row["column_name"]: _pg_affinity(row["data_type"])
                for row in columns
            },
            "pk": pk,
            "notnull": tuple(notnull),
            "unique": tuple(sorted(unique_scopes)),
            "foreign_keys": tuple(foreign_keys),
        }
    return contract


async def _classify(conn, url: str) -> "DatabaseStatus":
    from app.db_migration import (
        DatabaseState,
        DatabaseStatus,
        _contract_problems,
        _orm_contract,
        _revision_chain,
        migration_head,
    )

    head = migration_head()
    tables = await _user_tables(conn)
    if not tables:
        return DatabaseStatus(DatabaseState.EMPTY, None, head)
    contract = await _read_contract(conn, tables)
    has_version_table = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname = 'public'
              AND c.relname = 'alembic_version'
        )
        """
    )
    if not has_version_table:
        return DatabaseStatus(
            DatabaseState.UNRECOGNIZED_LEGACY,
            None,
            head,
            [
                "PostgreSQL database has user tables but no alembic_version; "
                "legacy adoption is a local-profile concept — a PostgreSQL "
                "database must be initialized by `python -m app.db_migration "
                "upgrade` from empty"
            ],
        )
    version_rows = await conn.fetch("SELECT version_num FROM alembic_version")
    rows = [row["version_num"] for row in version_rows]
    if len(rows) != 1:
        return DatabaseStatus(
            DatabaseState.INCONSISTENT_VERSION,
            None,
            head,
            [
                f"alembic_version must hold exactly one row, found "
                f"{len(rows)}: {rows}"
            ],
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
        problems = _contract_problems(contract, _orm_contract(), "schema claims head but")
        if problems:
            return DatabaseStatus(DatabaseState.HEAD_DIVERGENT, head, head, problems)
        return DatabaseStatus(DatabaseState.CURRENT, head, head)
    from app.db_migration import _subset_problems

    problems = _subset_problems(contract, _orm_contract())
    if problems:
        return DatabaseStatus(DatabaseState.DIVERGENT_AT_REVISION, revision, head, problems)
    return DatabaseStatus(DatabaseState.PENDING_UPGRADE, revision, head)


async def inspect_database_async(url: str) -> "DatabaseStatus":
    conn = await _connect(url)
    try:
        return await _classify(conn, url)
    finally:
        await conn.close()


def inspect_database(url: str) -> "DatabaseStatus":
    """Sync PostgreSQL introspection; requires a thread without a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(inspect_database_async(url))
    from app.db_migration import MigrationError

    raise MigrationError(
        "inspect_database(url) cannot introspect PostgreSQL from a running "
        "event loop; use inspect_database_async (app.db_migration_postgres) "
        "or call from a worker thread"
    )


def upgrade_database_sync(
    url: str, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> "MigrationResult":
    """The only persistent-schema mutation entrypoint (PostgreSQL).

    One dedicated connection holds the advisory lock for the whole
    operation; Alembic itself runs on its own connections (as on
    SQLite), and the lock serializes competing migration writers
    process-to-process through the server.
    """
    from app.db_migration import (
        DatabaseState,
        IncompatibleDatabaseError,
        MigrationError,
        MigrationLockTimeoutError,
        MigrationResult,
        _run_upgrade,
        migration_head,
    )

    head = migration_head()
    loop = asyncio.new_event_loop()
    try:
        conn = loop.run_until_complete(_connect(url))
        lock_key = _advisory_lock_key(url)
        try:
            deadline = time.monotonic() + max(0.0, lock_timeout)
            while True:
                acquired = loop.run_until_complete(
                    conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
                )
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise MigrationLockTimeoutError(
                        "another migration writer holds the PostgreSQL "
                        f"advisory lock on {make_url(url).database!r}; "
                        f"waited {lock_timeout:.0f}s"
                    )
                time.sleep(_LOCK_POLL_SECONDS)
            status = loop.run_until_complete(_classify(conn, url))
            if status.state is DatabaseState.EMPTY:
                action, from_revision = "initialized", None
            elif status.state is DatabaseState.CURRENT:
                return MigrationResult("already-current", head, head)
            elif status.state is DatabaseState.PENDING_UPGRADE:
                action, from_revision = "upgraded", status.revision
            else:
                raise IncompatibleDatabaseError(
                    "refusing to migrate an unrecognized PostgreSQL database "
                    "state; no schema was changed.\n" + status.describe()
                )
            # Alembic runs synchronously on its own connections while this
            # thread's loop (and the advisory lock it holds) stays parked.
            _run_upgrade(url, head)
            final = loop.run_until_complete(_classify(conn, url))
            if final.state is not DatabaseState.CURRENT:
                raise MigrationError(
                    f"migration action '{action}' finished but the database "
                    f"did not verify as current; no false success is "
                    f"reported.\n{final.describe()}"
                )
            logger.info(
                "Database migration (postgresql): %s -> %s (%s)",
                from_revision,
                head,
                action,
            )
            return MigrationResult(action, from_revision, final.revision or head)
        finally:
            loop.run_until_complete(
                conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
            )
            loop.run_until_complete(conn.close())
    finally:
        loop.close()
