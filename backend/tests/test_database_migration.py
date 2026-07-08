"""Regression tests for the additive-column self-heal in ``create_tables``.

A DB created before a model gained a column must self-heal on startup, else
every query against that table fails (the production bug:
``no such column: conversion_jobs.result_metadata_json``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db
import app.models.job  # noqa: F401 — registers ConversionJob on Base.metadata

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        assert "formats_json" in cols
        assert "retry_count" in cols
        assert "max_retries" in cols
        rows = (await conn.execute(text("SELECT result_metadata_json FROM conversion_jobs"))).fetchall()
        assert rows == [(None,)]  # existing row preserved, new column NULL
        queue_rows = (await conn.execute(text("SELECT retry_count, max_retries FROM conversion_jobs"))).fetchall()
        assert queue_rows == [(0, 0)]

    await engine.dispose()


def test_alembic_versions_cover_conversion_job_columns() -> None:
    """Alembic history must not drift behind additive runtime self-heal columns."""

    version_dir = REPO_ROOT / "backend" / "alembic" / "versions"
    migration_text = "\n".join(path.read_text(encoding="utf-8") for path in version_dir.glob("*.py"))
    model_columns = set(db.Base.metadata.tables["conversion_jobs"].columns.keys())

    initial_columns = _migration_columns_from_stale_ddl(_STALE_DDL)
    additive_columns = model_columns - initial_columns

    assert additive_columns
    for column in additive_columns:
        assert column in migration_text

    assert _migration_heads(version_dir) == {"20260709_0003"}


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


def _migration_columns_from_stale_ddl(ddl: str) -> set[str]:
    columns: set[str] = set()
    for raw_line in ddl.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.startswith("CREATE TABLE") or line == ")":
            continue
        columns.add(line.split(maxsplit=1)[0])
    return columns


def _migration_heads(version_dir: Path) -> set[str]:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in version_dir.glob("*.py"):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions.add(module.revision)
        down_revision = module.down_revision
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif down_revision:
            parents.update(down_revision)
    return revisions - parents
