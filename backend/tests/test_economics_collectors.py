"""Granular tests for the economics collectors against a real seeded
SQLite workspace: exact row counts reconcile with category sums, FTS
shadow tables are attributed to the lexical category, generation states
track the revision lifecycle, and DBSTAT absence degrades to a truthful
flag instead of a silent zero.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.eval.economics import collectors
from app.eval.pr81a.corpus import load_corpus
from app.eval.pr81a.kernel_seed import revise_document, seed_workspace
from app.kernel.commit import KernelCommitService
from app.kernel.payloads import LocalPayloadStore

pytestmark = pytest.mark.asyncio

CORPUS = load_corpus(Path(__file__).resolve().parent.parent / "eval_data" / "pr81a")


def test_table_categorization_rules():
    assert collectors.categorize_table("kernel_records") == "logical_authority"
    assert collectors.categorize_table("kernel_commit_heads") == "logical_authority"
    assert collectors.categorize_table("kernel_lexical_rows") == "lexical"
    assert collectors.categorize_table("kernel_fts_abc123") == "lexical"
    assert collectors.categorize_table("kernel_generations") == "derived_generation"
    assert collectors.categorize_table("kernel_publication_sets") == "publication"
    assert collectors.categorize_table("kernel_retention_roots") == "retention_gc"
    assert collectors.categorize_table("kernel_unknown_new") == "other_kernel"
    assert collectors.categorize_table("alembic_version") == "non_kernel"


def test_row_counts_by_category_sums_exactly():
    counts = {"kernel_records": 10, "kernel_fts_x": 4, "alembic_version": 1}
    categories = collectors.row_counts_by_category(counts)
    assert categories == {"logical_authority": 10, "lexical": 4, "non_kernel": 1}
    assert sum(categories.values()) == sum(counts.values())


async def test_seeded_workspace_row_envelope_reconciles(tmp_path: Path):
    db_path = tmp_path / "kernel.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payloads = LocalPayloadStore(tmp_path / "payloads")
    service = KernelCommitService(factory, payload_store=payloads)
    try:
        ws = await seed_workspace(
            factory=factory,
            service=service,
            corpus=CORPUS,
            workspace_id="ws-collectors",
            source_root=tmp_path / "sources",
        )

        conn = collectors.sqlite_connect_readonly(db_path)
        try:
            counts = collectors.table_row_counts(conn)
            categories = collectors.row_counts_by_category(counts)
            shadows = collectors.fts_shadow_tables(conn)
            states = collectors.generation_state_counts(conn)
            lexical = collectors.lexical_generation_stats(conn)
        finally:
            conn.close()

        # exact counts reconcile: category sums equal the table sum
        assert sum(categories.values()) == sum(counts.values())
        # the seeded corpus commits one source identity + content revision +
        # view document per document through the real authority
        assert counts["kernel_records"] >= len(CORPUS.docs) * 3
        # a published lexical generation exists with its physical FTS5 tables
        assert lexical and lexical[0]["row_count"] > 0
        assert shadows, "published FTS5 tables must have discoverable shadow tables"
        assert all(name.startswith(lexical[0]["fts_table"]) for name in shadows[:5])
        # active materialized generation is live pre-revision
        assert states.get("active", 0) >= 1

        payload_counts = collectors.payload_store_stats(payloads)
        assert payload_counts["stage_calls"] == 0  # corpus PDFs stage as sources

        source_profile = collectors.object_store_profile(tmp_path / "sources")
        assert source_profile["object_count"] == len(CORPUS.docs)
        assert source_profile["object_bytes"] > 0

        await revise_document(ws, "doc-rev-01", "v4")
        conn = collectors.sqlite_connect_readonly(db_path)
        try:
            states_after = collectors.generation_state_counts(conn)
        finally:
            conn.close()
        assert states_after.get("superseded", 0) >= 1
        assert states_after.get("active", 0) >= 1
    finally:
        await engine.dispose()


async def test_storage_profile_reports_truthful_dbstat_state(tmp_path: Path):
    db_path = tmp_path / "probe.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    profile = collectors.storage_profile(db_path)
    assert profile["page_count"] > 0
    assert profile["page_size"] > 0
    assert profile["db_bytes"] == profile["page_count"] * profile["page_size"]
    assert profile["journal_mode"] == "delete"
    assert profile["wal_file_present"] is False
    assert profile["wal_bytes"] == 0
    # whatever the build says about DBSTAT, content and flag must agree
    if profile["dbstat_available"]:
        assert profile["dbstat_bytes_by_table"], "available dbstat must return rows"
    else:
        assert profile["dbstat_bytes_by_table"] is None


async def test_storage_profile_wal_file_counted_when_present(tmp_path: Path):
    db_path = tmp_path / "wal.db"
    keeper = sqlite3.connect(db_path)
    keeper.execute("PRAGMA journal_mode=wal")
    keeper.execute("CREATE TABLE t(x)")
    keeper.execute("INSERT INTO t VALUES (1)")
    keeper.commit()
    # a second open connection keeps the WAL file alive (the last close
    # checkpoints and removes it), which is exactly what the probe must see
    holder = sqlite3.connect(db_path)
    profile = collectors.storage_profile(db_path)
    assert profile["journal_mode"] == "wal"
    assert profile["wal_file_present"] is True
    assert profile["wal_bytes"] > 0
    holder.close()
    keeper.close()
