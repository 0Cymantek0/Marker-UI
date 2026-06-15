"""Regression tests for the additive-column self-heal in ``create_tables``.

A DB created before a model gained a column must self-heal on startup, else
every query against that table fails (the production bug:
``no such column: conversion_jobs.result_metadata_json``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db
import app.models.job  # noqa: F401 — registers ConversionJob on Base.metadata


# A conversion_jobs table that predates the result_metadata_json column.
_STALE_DDL = """
CREATE TABLE conversion_jobs (
    id VARCHAR(36) PRIMARY KEY,
    filename VARCHAR(512) NOT NULL,
    original_name VARCHAR(512) NOT NULL,
    status VARCHAR(20) NOT NULL,
    input_format VARCHAR(20) NOT NULL,
    output_format VARCHAR(20) NOT NULL,
    config_json TEXT,
    result_text TEXT,
    result_path VARCHAR(1024),
    error_message TEXT,
    progress INTEGER NOT NULL,
    created_at DATETIME,
    updated_at DATETIME,
    completed_at DATETIME
)
"""


@pytest.mark.asyncio
async def test_create_tables_adds_missing_column_to_stale_table(tmp_path, monkeypatch):
    db_path = tmp_path / "stale.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, future=True, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)

    # Seed a stale table missing the new column, with one row.
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_STALE_DDL)
        await conn.exec_driver_sql(
            "INSERT INTO conversion_jobs "
            "(id, filename, original_name, status, input_format, output_format, progress) "
            "VALUES ('j1', 'f.pdf', 'f.pdf', 'completed', 'pdf', 'markdown', 0)"
        )

    # Self-heal, then the previously-missing column must be queryable.
    await db.create_tables()
    async with engine.begin() as conn:
        cols = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(conversion_jobs)")).fetchall()}
        assert "result_metadata_json" in cols
        rows = (await conn.execute(text("SELECT result_metadata_json FROM conversion_jobs"))).fetchall()
        assert rows == [(None,)]  # existing row preserved, new column NULL

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_tables_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "idem.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, future=True, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)

    # Running twice on a fresh DB must not error (no duplicate ADD COLUMN).
    await db.create_tables()
    await db.create_tables()

    await engine.dispose()
