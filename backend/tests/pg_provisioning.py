"""Shared PostgreSQL provisioning for dual-backend conformance fixtures.

Every conformance suite that needs a real PostgreSQL server uses the
same contract: ``MARKER_TEST_POSTGRES_ADMIN_URL`` points at the
server's maintenance database, each test creates and drops its own
throwaway database through it, and ``MARKER_TEST_POSTGRES_STRICT``
turns a missing URL into a failure so an invoked industrial target can
never pass silently through skips.

The helpers here are importable from plain (non-async) test modules;
the create/drop coroutines are awaited by the fixtures that use them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass

import pytest

ADMIN_URL_ENV = "MARKER_TEST_POSTGRES_ADMIN_URL"
STRICT_ENV = "MARKER_TEST_POSTGRES_STRICT"

BACKENDS = ("sqlite", "postgresql")


def postgres_admin_url() -> str | None:
    return os.getenv(ADMIN_URL_ENV) or None


def strict_mode() -> bool:
    return os.getenv(STRICT_ENV, "").lower() in ("1", "true", "yes")


def require_postgres_admin_url() -> str:
    """The admin URL, or skip/fail with an actionable reason.

    Strict mode (set by the conformance runners) refuses to skip: an
    invoked industrial target must fail loudly when the server is
    missing rather than report green through skips.
    """
    url = postgres_admin_url()
    if url is None:
        message = (
            "PostgreSQL kernel conformance needs "
            f"{ADMIN_URL_ENV} (server maintenance-database URL); run "
            "backend/scripts/run_kernel_pg_conformance.py to provision a "
            "real server automatically"
        )
        if strict_mode():
            pytest.fail(f"strict mode refuses to skip: {message}")
        pytest.skip(message)
    return url


async def create_postgres_database(admin_url: str) -> str:
    """Create one throwaway database; returns its full asyncpg URL."""
    import asyncpg
    from sqlalchemy.engine import make_url

    admin = make_url(admin_url)
    db_name = f"marker_conf_{uuid.uuid4().hex[:10]}"
    conn = await asyncpg.connect(
        admin.set(database="postgres").set(drivername="postgresql").render_as_string(
            hide_password=False
        )
    )
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()
    return admin.set(database=db_name).render_as_string(hide_password=False)


async def drop_postgres_database(admin_url: str, url: str) -> None:
    import asyncpg
    from sqlalchemy.engine import make_url

    admin = make_url(admin_url)
    db_name = make_url(url).database
    conn = await asyncpg.connect(
        admin.set(database="postgres").set(drivername="postgresql").render_as_string(
            hide_password=False
        )
    )
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            db_name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await conn.close()


@dataclass
class ProvisionedBackend:
    """One freshly migrated database URL plus its cleanup handle."""

    backend: str
    url: str
    admin_url: str | None


@contextlib.asynccontextmanager
async def provisioned_database(backend: str, sqlite_path):
    """Yield a fresh database URL for the requested backend.

    PostgreSQL databases are created/dropped through the admin URL;
    SQLite uses the caller-provided path. Callers run the Alembic
    chain themselves — provisioning here is only the database object.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    if backend == "postgresql":
        admin_url = require_postgres_admin_url()
        url = await create_postgres_database(admin_url)
        try:
            yield ProvisionedBackend(backend, url, admin_url)
        finally:
            # Give in-flight connection returns a moment to land before
            # teardown; then force any stragglers off so the throwaway
            # database always drops (tests must not leak databases even
            # when a cancellation races a pooled connection return).
            await asyncio.sleep(0.2)
            await drop_postgres_database(admin_url, url)
    else:
        url = f"sqlite+aiosqlite:///{sqlite_path}"
        yield ProvisionedBackend(backend, url, None)


def engine_kwargs_for(backend: str) -> dict:
    if backend == "sqlite":
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}
