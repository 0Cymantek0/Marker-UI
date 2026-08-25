"""Regression tests for isolated migrated test databases."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.db_migration import DatabaseState, inspect_database


@pytest.mark.asyncio
async def test_kernel_migration_template_is_current(_kernel_migration_template: Path):
    url = f"sqlite+aiosqlite:///{_kernel_migration_template.as_posix()}"
    assert inspect_database(url).state is DatabaseState.CURRENT


@pytest.mark.asyncio
async def test_kernel_env_starts_from_pristine_schema(kernel_env, _kernel_migration_template):
    async with kernel_env() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM kernel_records"))
        assert result.scalar_one() == 0

    assert _kernel_migration_template.exists()
    assert not Path(f"{_kernel_migration_template}-wal").exists()
    assert not Path(f"{_kernel_migration_template}-shm").exists()
