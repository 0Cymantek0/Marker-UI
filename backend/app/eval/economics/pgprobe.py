"""PostgreSQL probes for the industrial economics envelope (PG16+).

Every probe reads real cumulative statistics or catalog functions —
``pg_stat_wal`` windows around a controlled workload, ``pg_stat_io``,
exact ``COUNT(*)`` per table, and the object-size functions — and every
snapshot keeps the raw counters so derived amplification ratios always
retain their numerator and denominator. A probe that cannot run raises;
it never substitutes zero.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

PG_STAT_WAL_COLUMNS = (
    "wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full",
    "wal_write", "wal_sync", "stats_reset",
)


async def pg_stat_wal_snapshot(conn: AsyncConnection) -> dict[str, Any]:
    """Cluster WAL counters; deltas between snapshots isolate the window."""
    row = (await conn.execute(text(
        "SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full, "
        "wal_write, wal_sync, stats_reset::text FROM pg_stat_wal"
    ))).mappings().one()
    return dict(row)


def wal_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    """Non-negative deltas; a decreasing counter means the window is
    invalid (stats_reset happened mid-run) and must fail, not clamp."""
    delta: dict[str, int] = {}
    for key in ("wal_records", "wal_fpi", "wal_bytes", "wal_buffers_full", "wal_write", "wal_sync"):
        difference = int(after[key]) - int(before[key])
        if difference < 0:
            raise ValueError(
                f"pg_stat_wal counter {key} decreased inside the measurement window "
                f"(stats_reset between snapshots?) — the window is invalid"
            )
        delta[f"{key}_delta"] = difference
    return delta


async def pg_stat_io_snapshot(conn: AsyncConnection) -> dict[str, Any]:
    """Supplemental I/O telemetry (PG16), grouped by backend/context/object."""
    rows = (await conn.execute(text(
        "SELECT backend_type, object, context, reads, writes, "
        "write_bytes, extends, extend_bytes, op_bytes "
        "FROM pg_stat_io ORDER BY backend_type, object, context"
    ))).mappings().all()
    return {"rows": [dict(row) for row in rows]}


async def pg_stat_database_snapshot(
    conn: AsyncConnection, database_name: str
) -> dict[str, Any]:
    row = (await conn.execute(text(
        "SELECT xact_commit, xact_rollback, blks_read, blks_written, "
        "tuples_inserted, tuples_updated, tuples_deleted "
        "FROM pg_stat_database WHERE datname = :name"
    ), {"name": database_name})).mappings().one()
    return dict(row)


async def pg_database_bytes(conn: AsyncConnection, database_name: str) -> int:
    value = (await conn.execute(text(
        "SELECT pg_database_size(:name)"
    ), {"name": database_name})).scalar_one()
    return int(value)


async def exact_row_counts(
    conn: AsyncConnection, tables: list[str]
) -> dict[str, int]:
    """Exact per-table counts — never ``pg_class.reltuples`` estimates."""
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int((await conn.execute(text(
            f'SELECT COUNT(*) FROM "{table}"'
        ))).scalar_one())
    return counts


async def relation_sizes(
    conn: AsyncConnection, tables: list[str]
) -> dict[str, dict[str, int]]:
    """Physical relation sizes per table (heap / indexes / total incl. TOAST)."""
    rows = (await conn.execute(text(
        "SELECT c.relname, "
        "pg_relation_size(c.oid) AS heap_bytes, "
        "pg_indexes_size(c.oid) AS index_bytes, "
        "pg_total_relation_size(c.oid) AS total_bytes "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = ANY(:tables) "
        "ORDER BY c.relname"
    ), {"tables": tables})).mappings().all()
    return {row["relname"]: dict(row) for row in rows}


async def server_banner(conn: AsyncConnection) -> str:
    return str((await conn.execute(text("SELECT version()"))).scalar_one())
