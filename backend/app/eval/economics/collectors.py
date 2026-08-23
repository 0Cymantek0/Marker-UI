"""Local-profile collectors for the economics envelope.

Exact, attributable probes over the artifacts the kernel actually
creates: synchronous SQLite introspection (exact row counts — never
estimates — plus page/storage accounting with an optional ``dbstat``
breakdown that degrades to a truthful "unavailable" when the build does
not expose the virtual table), object-store byte walks, and payload
store observability counters.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

#: table-name prefixes -> envelope row categories. Everything the
#: migrations create is covered; unknown kernel tables land in
#: "other_kernel" rather than being silently dropped.
TABLE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("kernel_lexical_", "lexical"),
    ("kernel_fts_", "lexical"),
    ("kernel_publication", "publication"),
    ("kernel_generation", "derived_generation"),
    ("kernel_view_heads", "derived_generation"),
    ("kernel_retention_", "retention_gc"),
    ("kernel_reader_pins", "retention_gc"),
    ("kernel_payload_retirements", "retention_gc"),
    ("kernel_work_leases", "runtime"),
    ("kernel_scheduling_", "runtime"),
    ("kernel_liveness", "runtime"),
    ("kernel_events", "runtime"),
    ("kernel_progress", "runtime"),
    ("kernel_query_cursors", "query_runtime"),
    ("kernel_answer_", "answer_evidence"),
    ("kernel_context_disclosures", "answer_evidence"),
    ("kernel_connector_", "connector"),
    ("kernel_commit_", "logical_authority"),
    ("kernel_records", "logical_authority"),
    ("kernel_record_edges", "logical_authority"),
    ("kernel_payload_objects", "logical_authority"),
    ("kernel_outbox", "logical_authority"),
)

FTS5_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def categorize_table(name: str) -> str:
    for prefix, category in TABLE_CATEGORIES:
        if name.startswith(prefix):
            return category
    if name.startswith("kernel_"):
        return "other_kernel"
    return "non_kernel"


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Exact ``COUNT(*)`` per table — never an estimate."""
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in list_tables(conn)}


def row_counts_by_category(counts: dict[str, int]) -> dict[str, int]:
    categories: dict[str, int] = {}
    for name, count in counts.items():
        categories[categorize_table(name)] = categories.get(categorize_table(name), 0) + count
    return dict(sorted(categories.items()))


def fts_shadow_tables(conn: sqlite3.Connection) -> list[str]:
    """Physical FTS5 shadow tables for the published lexical generations."""
    try:
        rows = conn.execute(
            "SELECT fts_table FROM kernel_lexical_generations"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    names = {row[0] for row in rows if row[0]}
    shadows = set()
    for name in names:
        for suffix in FTS5_SHADOW_SUFFIXES:
            candidate = f"{name}{suffix}"
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (candidate,),
            ).fetchone():
                shadows.add(candidate)
    return sorted(shadows)


def lexical_generation_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT lexical_generation_id, fts_table, row_count, text_char_count "
            "FROM kernel_lexical_generations ORDER BY lexical_generation_id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "lexical_generation_id": row[0],
            "fts_table": row[1],
            "row_count": row[2],
            "text_char_count": row[3],
        }
        for row in rows
    ]


def generation_state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = conn.execute(
            "SELECT state, COUNT(*) FROM kernel_generations GROUP BY state"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: row[1] for row in rows}


def storage_profile(db_path: Path) -> dict[str, Any]:
    """Page-level storage accounting for one SQLite database file.

    ``dbstat`` per-table bytes are included when the build exposes the
    optional DBSTAT virtual table and reported as unavailable (with the
    capability flag) otherwise — never silently dropped or zero-filled.
    """
    conn = sqlite3.connect(db_path)
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        dbstat: dict[str, int] | None
        try:
            rows = conn.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY name"
            ).fetchall()
            dbstat = {row[0]: int(row[1]) for row in rows}
            dbstat_available = True
        except sqlite3.OperationalError:
            dbstat = None
            dbstat_available = False
    finally:
        conn.close()
    wal_path = db_path.parent / (db_path.name + "-wal")
    return {
        "page_count": int(page_count),
        "page_size": int(page_size),
        "freelist_pages": int(freelist),
        "journal_mode": str(journal_mode),
        "db_bytes": int(page_count) * int(page_size),
        "wal_bytes": int(wal_path.stat().st_size) if wal_path.exists() else 0,
        "wal_file_present": wal_path.exists(),
        "dbstat_available": dbstat_available,
        "dbstat_bytes_by_table": dbstat,
    }


def object_store_profile(root: Path) -> dict[str, int]:
    """Count + size every object file under a content-addressed store root."""
    objects = root / "objects"
    files = count = 0
    if objects.is_dir():
        for path in objects.rglob("*"):
            if path.is_file():
                count += 1
                files += path.stat().st_size
    return {"object_count": count, "object_bytes": files}


def payload_store_stats(store: Any) -> dict[str, int]:
    """Observability counters shared by the payload store implementations."""
    keys = ("stage_calls", "dedup_hits", "bytes_logical", "bytes_written", "bytes_read_back")
    return {key: int(getattr(store, key, 0)) for key in keys}


def sqlite_connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Read-only introspection connection (callers must close it)."""
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
