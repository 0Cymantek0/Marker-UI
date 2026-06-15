"""Async SQLAlchemy database configuration for Marker UI."""

import logging

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.schema import CreateColumn

from app.core.config import DATA_DIR, DATABASE_URL

logger = logging.getLogger(__name__)

DATA_DIR.mkdir(exist_ok=True)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables() -> None:
    """Create all tables at application startup (dev convenience).

    ``create_all`` only creates *missing tables* — it never adds columns to a
    table that already exists. This app has no Alembic, so a DB created before
    a model gained a new column would keep failing every query against it
    (e.g. ``no such column: conversion_jobs.result_metadata_json``). After
    creating tables we reconcile any *additive* column gaps so a stale local
    DB self-heals on startup.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
    logger.info("Database tables ensured")


def _add_missing_columns(conn) -> None:
    """Add any model columns missing from existing tables (SQLite-safe).

    Only handles additive changes (new nullable / defaulted columns), which is
    all the schema has ever needed. Dropped or retyped columns are out of scope
    and would require a real migration tool.
    """
    inspector = sa_inspect(conn)
    dialect = conn.dialect
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # freshly created by create_all — already complete
        on_disk = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in on_disk:
                continue
            ddl = CreateColumn(column).compile(dialect=dialect)
            conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
            logger.info(
                "Schema migration: added column %s.%s", table.name, column.name
            )
