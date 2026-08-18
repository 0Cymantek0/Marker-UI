"""PR79A cursor schema migration coverage."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import db_migration
from app.context_runtime import QUERY_SCHEMA_VERSION, parse_query_request
from app.context_runtime.contract import normalized_query
from app.context_runtime.continuation import parse_cursor_state_json
from app.context_runtime.continuation_state import (
    canonical,
    initial_budget,
    initial_keyset,
    validate_budget,
    validate_keyset,
)
from app.context_runtime.continuation_store import CursorStore
from app.db_migration import DatabaseState, inspect_database, upgrade_database

PR76_HEAD = "20260817_0011"
PR79A_HEAD = "20260818_0012"
CURSOR_TABLE = "kernel_query_cursors"
CURSOR_COLUMNS = {
    "handle",
    "workspace_id",
    "query_json",
    "snapshot_json",
    "publication_json",
    "authorization_json",
    "keyset_json",
    "cumulative_budget_json",
    "page_count",
    "expires_at",
    "pin_id",
    "status",
    "nonce",
    "replay_state",
    "created_at",
    "updated_at",
}


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_pr79a_upgrade_adds_empty_cursor_state_table(tmp_path: Path) -> None:
    db_path = tmp_path / "cursor.db"
    url = _url(db_path)
    await upgrade_database(url=url)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1]
            for row in conn.execute(f'PRAGMA table_info("{CURSOR_TABLE}")')
        }
        row_count = conn.execute(f"SELECT COUNT(*) FROM {CURSOR_TABLE}").fetchone()[0]

    assert version == PR79A_HEAD
    assert CURSOR_TABLE in tables
    assert columns == CURSOR_COLUMNS
    assert row_count == 0
    assert inspect_database(url=url).state is DatabaseState.CURRENT


@pytest.mark.asyncio
async def test_pr79a_downgrade_discards_cursor_state_and_reupgrade_converges(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cursor-downgrade.db"
    url = _url(db_path)
    await upgrade_database(url=url)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO kernel_query_cursors ("
            "handle, workspace_id, query_json, snapshot_json, publication_json, "
            "authorization_json, keyset_json, cumulative_budget_json, page_count, "
            "expires_at, pin_id, status, nonce, replay_state, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "handle-a",
                "workspace-a",
                '{"operations":[]}',
                '{"snapshot_id":"snapshot-a"}',
                '{"publication_set_id":"publication-a"}',
                '{"policy_digest":"sha256:a"}',
                '{"last":"record-a"}',
                '{"candidates":1}',
                1,
                "2026-08-18 12:00:00",
                "pin-a",
                "active",
                "nonce-a",
                "fresh",
                "2026-08-18 11:00:00",
                "2026-08-18 11:00:00",
            ),
        )
        conn.commit()

    await asyncio.to_thread(
        command.downgrade, db_migration._alembic_config(url), PR76_HEAD
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'kernel_query_cursors'"
        ).fetchone()[0] == 0

    await upgrade_database(url=url)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kernel_query_cursors"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PR79A_HEAD


@pytest.mark.asyncio
async def test_pr79a_cursor_store_round_trips_through_migrated_schema(
    tmp_path: Path,
) -> None:
    """Real store payloads must survive the migration-built schema.

    This closes the gap between "columns exist" and "the ORM model agrees
    with the migration": every canonical JSON column written by
    ``CursorStore`` is parsed back through the strict state validators, and
    the nonce claim/rotate/terminalize transitions commit against the
    migrated table.
    """

    url = _url(tmp_path / "cursor-roundtrip.db")
    await upgrade_database(url=url)
    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        fixed = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        store = CursorStore(factory, lambda: fixed)
        request = parse_query_request(
            {
                "schema_version": QUERY_SCHEMA_VERSION,
                "workspace_id": "ws-rt",
                "operations": [
                    {"op": "lexical_search", "text": "needle", "limit": 5},
                    {"op": "record_get", "record_id": "view-1", "node_id": "n1"},
                ],
            }
        )
        keyset = initial_keyset(request)
        budget = initial_budget()
        budget["pages"] = 1
        budget["evidence_units"] = 2
        publication = {
            "publication_set_id": "pub-rt",
            "workspace_id": "ws-rt",
            "profile": "local_v1",
            "kernel_commit_id": 3,
            "snapshot_id": "snap-rt",
            "materialized_generation_id": "gen-rt",
            "lexical_generation_id": "lex-rt",
            "tokenizer": "unicode61",
            "vector_generation_id": None,
            "lexical_row_count": 2,
        }
        expires_at = fixed + timedelta(seconds=60)
        handle, nonce = await store.insert(
            request=request,
            publication=publication,
            authorization={"profile": "local_v1", "policy_digest": "sha256:rt"},
            keyset=keyset,
            cumulative_budget=budget,
            expires_at=expires_at,
            pin_id="pin-rt",
        )

        row = await store.load(handle)
        assert row is not None
        assert row.workspace_id == "ws-rt"
        assert row.status == "active"
        assert row.replay_state == "fresh"
        assert row.page_count == 1
        assert row.query_json == canonical(normalized_query(request))
        assert parse_cursor_state_json(row.publication_json) == publication
        assert parse_cursor_state_json(row.snapshot_json) == {
            "snapshot_id": "snap-rt",
            "materialized_generation_id": "gen-rt",
        }
        assert validate_keyset(
            parse_cursor_state_json(row.keyset_json), request
        ) == parse_cursor_state_json(row.keyset_json)
        assert validate_budget(
            parse_cursor_state_json(row.cumulative_budget_json)
        ) == parse_cursor_state_json(row.cumulative_budget_json)

        assert await store.claim(handle, nonce)
        assert not await store.claim(handle, nonce)
        rotated = await store.rotate(
            handle=handle,
            old_nonce=nonce,
            keyset=keyset,
            cumulative_budget=budget,
            pin_id="pin-rt",
            expires_at=expires_at,
        )
        assert rotated is not None and rotated != nonce
        # Terminalization commits while a claim is held, so the rotated
        # nonce must be claimed first — matching the service's page flow.
        assert await store.claim(handle, rotated)
        assert await store.terminalize_claimed(handle, "exhausted")
        row = await store.load(handle)
        assert row is not None
        assert row.status == "exhausted"
        assert row.replay_state == "consumed"
        assert row.pin_id is None
    finally:
        await engine.dispose()
