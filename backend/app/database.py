"""Async SQLAlchemy database configuration for Marker UI.

The engine serves both first-class kernel profiles (PR83A): the local
SQLite profile and the industrial PostgreSQL profile. Backend-specific
connection behavior is selected from the configured URL — SQLite needs
``check_same_thread=False`` for the async driver, while PostgreSQL gets
liveness pre-ping so a restarted or failed-over server is detected at
checkout instead of surfacing as a stale-connection error mid-commit.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import DATA_DIR, DATABASE_URL

DATA_DIR.mkdir(exist_ok=True)


def engine_connect_kwargs(url: str) -> dict:
    """Backend-appropriate ``create_async_engine`` keyword arguments.

    SQLite-only connection options must not leak into other backends:
    ``check_same_thread`` is an aiosqlite argument and is rejected by
    other drivers. PostgreSQL connections are validated at pool checkout
    (``pool_pre_ping``) so long-lived processes survive server restarts
    and failovers without poisoning the pool.
    """
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    **engine_connect_kwargs(DATABASE_URL),
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
