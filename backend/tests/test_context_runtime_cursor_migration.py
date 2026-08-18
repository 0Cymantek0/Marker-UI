"""PR79A cursor schema migration coverage."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from app import db_migration
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
