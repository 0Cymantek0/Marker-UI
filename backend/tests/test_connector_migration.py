"""Connector migration / compatibility slice (PR71B readiness T27/T28).

These tests exercise the canonical Alembic migration entrypoint against
file-backed SQLite (the sqlite lane) and assert the exact contract the
``20260823_0014_add_connector_convergence`` revision must produce:

* a fresh upgrade creates ``kernel_connector_streams`` /
  ``kernel_connector_inbox`` with the columns, types, nullability,
  primary keys, the ``(stream_id, provider_event_id)`` unique authority,
  the FK, and the non-unique indexes;
* an upgrade from the pre-connector revision ``20260819_0013`` (T27)
  brings an existing database to head and leaves the kernel commit spine
  intact (a commit still produces ``kernel_commit_id == 1``);
* a downgrade/upgrade round trip removes and restores the tables;
* the SQLAlchemy ORM models match the migrated schema (no drift).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import (
    _alembic_config,
    _run_upgrade,
    upgrade_database,
)
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.models import KernelConnectorInbox, KernelConnectorStream
from app.kernel.records import SourceObservationRecord

pytestmark = pytest.mark.asyncio

STREAMS_TABLE = "kernel_connector_streams"
INBOX_TABLE = "kernel_connector_inbox"

# Expected column -> (inspected type string, nullable)
STREAMS_COLUMNS = {
    "stream_id": ("VARCHAR(128)", False),
    "workspace_id": ("VARCHAR(128)", False),
    "cursor_token": ("VARCHAR(512)", False),
    "cursor_seq": ("BIGINT", True),
    "state": ("VARCHAR(32)", False),
    "reconciliation_reason": ("TEXT", True),
    "applied_kernel_commit_id": ("INTEGER", False),
    "created_at": ("DATETIME", True),
    "updated_at": ("DATETIME", True),
}

INBOX_COLUMNS = {
    "id": ("INTEGER", False),
    "workspace_id": ("VARCHAR(128)", False),
    "stream_id": ("VARCHAR(128)", False),
    "provider_event_id": ("VARCHAR(256)", False),
    "event_kind": ("VARCHAR(32)", False),
    "provider_item_id": ("VARCHAR(256)", True),
    "provider_revision": ("VARCHAR(256)", True),
    "provider_seq": ("BIGINT", True),
    "applied_state": ("VARCHAR(32)", False),
    "applied_kernel_commit_id": ("INTEGER", False),
    "result_json": ("TEXT", False),
    "received_at": ("DATETIME", True),
}

STREAM_INDEXES = {
    "ix_kernel_connector_streams_workspace_id",
    "ix_kernel_connector_streams_state",
}
INBOX_INDEXES = {
    "ix_kernel_connector_inbox_workspace_id",
    "ix_kernel_connector_inbox_stream_id",
    "ix_kernel_connector_inbox_stream_state",
}


def _db_url(tmp_path: Path, name: str) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"


def _sync_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _assert_streams_contract(insp) -> None:
    assert STREAMS_TABLE in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns(STREAMS_TABLE)}
    assert set(cols) == set(STREAMS_COLUMNS), set(cols) ^ set(STREAMS_COLUMNS)
    for col_name, (exp_type, exp_nullable) in STREAMS_COLUMNS.items():
        assert str(cols[col_name]["type"]) == exp_type, (
            f"{STREAMS_TABLE}.{col_name} type {cols[col_name]['type']!r} != {exp_type!r}"
        )
        assert cols[col_name]["nullable"] == exp_nullable
    pk = insp.get_pk_constraint(STREAMS_TABLE)
    assert pk["constrained_columns"] == ["stream_id"], pk

    # Non-unique indexes present.
    idx_names = {i["name"] for i in insp.get_indexes(STREAMS_TABLE)}
    assert STREAM_INDEXES <= idx_names, STREAM_INDEXES - idx_names


def _assert_inbox_contract(insp) -> None:
    assert INBOX_TABLE in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns(INBOX_TABLE)}
    assert set(cols) == set(INBOX_COLUMNS), set(cols) ^ set(INBOX_COLUMNS)
    for col_name, (exp_type, exp_nullable) in INBOX_COLUMNS.items():
        assert str(cols[col_name]["type"]) == exp_type, (
            f"{INBOX_TABLE}.{col_name} type {cols[col_name]['type']!r} != {exp_type!r}"
        )
        assert cols[col_name]["nullable"] == exp_nullable
    pk = insp.get_pk_constraint(INBOX_TABLE)
    assert pk["constrained_columns"] == ["id"], pk

    # FK -> kernel_connector_streams.stream_id ondelete RESTRICT.
    fks = insp.get_foreign_keys(INBOX_TABLE)
    assert len(fks) == 1, fks
    fk = fks[0]
    assert fk["constrained_columns"] == ["stream_id"], fk
    assert fk["referred_table"] == STREAMS_TABLE, fk
    assert fk["referred_columns"] == ["stream_id"], fk
    assert (fk.get("options") or {}).get("ondelete") == "RESTRICT", fk

    # Unique authority constraint (stream_id, provider_event_id).
    uniques = insp.get_unique_constraints(INBOX_TABLE)
    found = any(
        uc["column_names"] == ["stream_id", "provider_event_id"] for uc in uniques
    )
    if not found:
        # aiosqlite/sqlite may reflect the unique scope as an index instead.
        idx_names = {
            i["name"]
            for i in insp.get_indexes(INBOX_TABLE)
            if i["unique"]
            and i["column_names"] == ["stream_id", "provider_event_id"]
        }
        assert found or idx_names, (uniques, insp.get_indexes(INBOX_TABLE))

    idx_names = {i["name"] for i in insp.get_indexes(INBOX_TABLE)}
    assert INBOX_INDEXES <= idx_names, INBOX_INDEXES - idx_names


class TestFreshUpgradeContract:
    async def test_fresh_upgrade_creates_connector_tables_with_contract(self, tmp_path: Path):
        url = _db_url(tmp_path, "fresh.db")
        await upgrade_database(url=url)

        # Inspect via a sync engine on the same file.
        engine = create_engine(_sync_url(tmp_path, "fresh.db"))
        try:
            insp = inspect(engine)
            _assert_streams_contract(insp)
            _assert_inbox_contract(insp)
        finally:
            engine.dispose()

    async def test_duplicate_provider_event_rejected_by_database(self, tmp_path: Path):
        url = _db_url(tmp_path, "dup.db")
        await upgrade_database(url=url)

        engine = create_async_engine(
            url, connect_args={"check_same_thread": False}
        )
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                session.add(
                    KernelConnectorStream(
                        stream_id="s-dup",
                        workspace_id="ws-dup",
                        cursor_token="tok",
                        state="consuming",
                        applied_kernel_commit_id=0,
                    )
                )
                await session.commit()

                session.add(
                    KernelConnectorInbox(
                        workspace_id="ws-dup",
                        stream_id="s-dup",
                        provider_event_id="evt-1",
                        event_kind="sync",
                        applied_state="applied",
                        applied_kernel_commit_id=0,
                        result_json="{}",
                    )
                )
                session.add(
                    KernelConnectorInbox(
                        workspace_id="ws-dup",
                        stream_id="s-dup",
                        provider_event_id="evt-1",
                        event_kind="sync",
                        applied_state="applied",
                        applied_kernel_commit_id=0,
                        result_json="{}",
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.commit()
                await session.rollback()
        finally:
            await engine.dispose()


class TestUpgradeFromPreConnectorSchema:
    async def test_upgrade_from_20260819_0013_keeps_spine(self, tmp_path: Path):
        url = _db_url(tmp_path, "mig.db")
        # Bring the db ONLY to the pre-connector revision. The alembic env
        # uses asyncio.run internally, so run it off the pytest event loop.
        await asyncio.to_thread(_run_upgrade, url, "20260819_0013")

        engine = create_engine(_sync_url(tmp_path, "mig.db"))
        try:
            insp = inspect(engine)
            assert STREAMS_TABLE not in insp.get_table_names()
            assert INBOX_TABLE not in insp.get_table_names()
            # Spine must already be present at the pre-connector revision.
            assert "kernel_records" in insp.get_table_names()
        finally:
            engine.dispose()

        # Canonical head upgrade.
        result = await upgrade_database(url=url)
        assert result.to_revision == "20260823_0014"

        engine = create_engine(_sync_url(tmp_path, "mig.db"))
        try:
            insp = inspect(engine)
            _assert_streams_contract(insp)
            _assert_inbox_contract(insp)
        finally:
            engine.dispose()

        # Pre-existing kernel state remains readable: a single commit yields
        # kernel_commit_id == 1, proving the spine survived the migration.
        engine = create_async_engine(
            url, connect_args={"check_same_thread": False}
        )
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        service = KernelCommitService(factory)
        try:
            receipt = await service.commit(
                KernelCommitBatch(
                    workspace_id="ws-mig",
                    records=(
                        SourceObservationRecord(
                            observer="t",
                            source_ref="src.mig.1",
                            outcome="policy_updated",
                        ),
                    ),
                )
            )
            assert receipt.kernel_commit_id == 1
        finally:
            await engine.dispose()


class TestDowngradeReUpgradeRoundTrip:
    async def test_downgrade_removes_tables_then_upgrade_restores(self, tmp_path: Path):
        url = _db_url(tmp_path, "roundtrip.db")
        await upgrade_database(url=url)

        engine = create_engine(_sync_url(tmp_path, "roundtrip.db"))
        try:
            insp = inspect(engine)
            assert STREAMS_TABLE in insp.get_table_names()
            assert INBOX_TABLE in insp.get_table_names()
            assert "kernel_records" in insp.get_table_names()
        finally:
            engine.dispose()

        # Downgrade back to the pre-connector revision (sync entrypoint).
        await asyncio.to_thread(
            command.downgrade, _alembic_config(url), "20260819_0013"
        )

        engine = create_engine(_sync_url(tmp_path, "roundtrip.db"))
        try:
            insp = inspect(engine)
            assert STREAMS_TABLE not in insp.get_table_names()
            assert INBOX_TABLE not in insp.get_table_names()
            # kernel_records must survive the downgrade.
            assert "kernel_records" in insp.get_table_names()
        finally:
            engine.dispose()

        # Upgrade again -> tables return.
        await asyncio.to_thread(
            command.upgrade, _alembic_config(url), "20260823_0014"
        )

        engine = create_engine(_sync_url(tmp_path, "roundtrip.db"))
        try:
            insp = inspect(engine)
            _assert_streams_contract(insp)
            _assert_inbox_contract(insp)
        finally:
            engine.dispose()


class TestModelsMatchMigration:
    async def test_orm_columns_match_migrated_schema(self, tmp_path: Path):
        url = _db_url(tmp_path, "drift.db")
        await upgrade_database(url=url)

        engine = create_engine(_sync_url(tmp_path, "drift.db"))
        try:
            insp = inspect(engine)

            migrated_streams = {c["name"] for c in insp.get_columns(STREAMS_TABLE)}
            model_streams = set(KernelConnectorStream.__table__.columns.keys())
            assert migrated_streams == model_streams, (
                migrated_streams ^ model_streams
            )

            migrated_inbox = {c["name"] for c in insp.get_columns(INBOX_TABLE)}
            model_inbox = set(KernelConnectorInbox.__table__.columns.keys())
            assert migrated_inbox == model_inbox, migrated_inbox ^ model_inbox
        finally:
            engine.dispose()
