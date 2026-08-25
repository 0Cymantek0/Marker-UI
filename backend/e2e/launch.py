"""Playwright-consumable E2E backend launcher.

Boots the REAL FastAPI application (real routes, real database, real as-of
enforcement, real auth middleware) on a throwaway SQLite database seeded
with one deterministic completed job, over real HTTP via Uvicorn. A later
Playwright suite drives the API exactly as a browser would.

The ONLY swapped seam is the conversion render path:
``_app_state.conversion_service.convert_file_formats`` (plus its
``supports_multiple_formats`` gate) is replaced with a deterministic
in-process stub. This is the same seam the backend test suite patches
(``tests/conftest.py`` swaps FakeMarkerService into ``_app_state``;
``tests/test_as_of_contract.py::_stub_render`` patches the very same two
methods on the very same instance). The swap is legitimate because the
behavior under E2E test — the as-of contract: token derivation, verified
vs historical downloads, stale-state 409s, regenerate preconditions —
lives entirely in the routes and ``app.operational.as_of``, not in the
converter. The stub's render output embeds a monotonically increasing
counter (seeded past every number already cached in the database) so every
regenerate produces new content, rotates ``result_digest``, and therefore
rotates the derived ``state_token``: each Playwright regenerate is
guaranteed to move the observable state, even on a reused scratch dir.

Everything runs from a scratch directory (default ``tempfile.mkdtemp``)
so the launcher never touches the repository's ``data/`` directory:
``MARKER_DATABASE_URL`` and the ``app.core.config`` data-dir attributes
are pointed at the scratch directory BEFORE any app module is imported
(config binds these at import time, so ordering is load-bearing).

Usage (either cwd works — module paths resolve relative to this file):

    python backend/e2e/launch.py
    cd backend && python e2e/launch.py

Environment:
    MARKER_E2E_PORT   TCP port to serve on (default 8917).
    MARKER_E2E_DB_DIR Scratch directory for DB + uploads. Optional; a
                      fresh ``tempfile.mkdtemp(prefix="marker-e2e-")``
                      is used when unset. Re-running against an existing
                      directory is idempotent (seed row is not duplicated).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_PORT = 8917
HOST = "127.0.0.1"
SEEDED_JOB_ID = "job-e2e-seeded"
SEED_SOURCE_BYTES = b"%PDF-1.4 e2e seeded source"
READY_LINE_PREFIX = "MARKER_E2E_READY"
_RENDER_MARKER = re.compile(r"render (\d+)")

_render_counter: itertools.count[int] = itertools.count(start=1)


def _resolve_scratch_dir() -> Path:
    """Scratch directory: MARKER_E2E_DB_DIR or a fresh temp directory."""
    configured = os.environ.get("MARKER_E2E_DB_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(tempfile.mkdtemp(prefix="marker-e2e-"))


def _prepare_environment(db_url: str) -> None:
    """Set env vars BEFORE any app import (config reads them at import time)."""
    os.environ["MARKER_DATABASE_URL"] = db_url
    # Deterministic key so seeded secrets stay decryptable across restarts of
    # a reused scratch dir; mirrors tests/conftest.py's pre-import setup.
    os.environ.setdefault("ENCRYPTION_KEY", "ZTJlLWVuY3J5cHRpb24ta2V5LWZvci1ydW5uaW5nLW9ubHk=")


def _redirect_data_dirs(scratch: Path) -> None:
    """Point every data-dir attribute of app.core.config into the scratch dir.

    UPLOAD_DIR and friends are module-level constants with no env override, and
    consumer modules bind them by value at import time (``from app.core.config
    import UPLOAD_DIR``). Patching the attributes BEFORE importing any consumer
    therefore redirects every one of them, including function-level re-imports.
    """
    import app.core.config as config

    config.UPLOAD_DIR = scratch / "uploads"
    config.OUTPUT_DIR = scratch / "output"
    config.KERNEL_PAYLOAD_ROOT = scratch / "kernel_payloads"
    config.SOURCE_STORE_ROOT = scratch / "source_store"
    config.SOURCE_CACHE_ROOT = scratch / "source_cache"
    config.ARTIFACT_HANDLE_ROOT = scratch / "artifact_handles"
    for directory in (
        config.UPLOAD_DIR,
        config.OUTPUT_DIR,
        config.KERNEL_PAYLOAD_ROOT,
        config.SOURCE_STORE_ROOT,
        config.SOURCE_CACHE_ROOT,
        config.ARTIFACT_HANDLE_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _seed_job() -> Any:
    """The deterministic completed job, mirroring the canonical test builder.

    Row shape copies ``tests/test_as_of_contract.py::_completed_job`` for the
    TOCTOU case: no config/metadata JSON (dimensions honestly absent), a flat
    ``formats_json`` cache holding a markdown entry, progress 100, completed.
    """
    from app.models.job import ConversionJob

    return ConversionJob(
        id=SEEDED_JOB_ID,
        filename=f"{SEEDED_JOB_ID}.pdf",
        original_name=f"{SEEDED_JOB_ID.replace('job-', 'doc-')}.pdf",
        status="completed",
        input_format="pdf",
        output_format="markdown",
        result_text="# Converted output",
        config_json=None,
        result_metadata_json=None,
        formats_json=json.dumps({"markdown": "# E2E seeded output v1"}),
        progress=100,
        completed_at=datetime.now(timezone.utc),
    )


def _write_seed_source() -> None:
    """Store the source file regenerate will re-render (upload-copy path)."""
    from app.core.config import UPLOAD_DIR

    (UPLOAD_DIR / f"{SEEDED_JOB_ID}.pdf").write_bytes(SEED_SOURCE_BYTES)


def _latest_render_number(formats_json: str | None) -> int:
    """Highest render number already cached in the job's formats.

    The stub's render counter starts past this so the FIRST regenerate of a
    fresh launcher session on a reused scratch dir still produces new content
    (and therefore rotates the derived state token) instead of replaying the
    text the previous session ended on.
    """
    if not formats_json:
        return 0
    try:
        formats = json.loads(formats_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(formats, dict):
        return 0
    numbers = [
        int(match)
        for text in formats.values() if isinstance(text, str)
        for match in _RENDER_MARKER.findall(text)
    ]
    return max(numbers, default=0)


async def _migrate_and_seed(db_url: str) -> int:
    """Bring the scratch DB to the migration head and seed the job row.

    Uses its own async engine/session (conftest's kernel_env pattern) and
    disposes it before Uvicorn starts, so no session state crosses loops.
    Idempotent: a reused scratch dir keeps its existing seed row. Returns
    the latest render number cached for the seeded job.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.db_migration import upgrade_database
    from app.models.job import ConversionJob

    await upgrade_database(url=db_url)

    engine = create_async_engine(db_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            existing = await session.execute(
                select(ConversionJob).where(ConversionJob.id == SEEDED_JOB_ID)
            )
            row = existing.scalar_one_or_none()
            if row is None:
                session.add(_seed_job())
                await session.commit()
                return 0
            return _latest_render_number(row.formats_json)
    finally:
        await engine.dispose()


def _stub_conversion_render(latest_render: int) -> None:
    """Swap the render seam on the loaded app's conversion service.

    Mirrors ``tests/test_as_of_contract.py::_stub_render``: the same two
    attributes on the same ``_app_state.conversion_service`` instance the
    backend suite patches. Rendered text embeds a per-call counter that
    starts past every number already cached in the DB, so each regenerate
    changes ``formats_json`` -> rotates ``result_digest`` -> rotates the
    derived ``state_token`` — across launcher sessions too, not just within
    one process.
    """
    global _render_counter

    from app.main import _app_state

    service = _app_state.conversion_service
    _render_counter = itertools.count(start=latest_render + 1)

    def render(
        filepath: str,
        config: dict[str, Any],
        formats: list[str],
        device: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        render_number = next(_render_counter)
        return {
            fmt: {
                "text": (
                    f"# E2E stub render {render_number}\n\n"
                    f"Deterministic content for {fmt} of {Path(filepath).name}."
                ),
                "extension": fmt,
                "images": {},
                "metadata": {},
            }
            for fmt in formats
        }

    service.convert_file_formats = render  # type: ignore[method-assign]
    service.supports_multiple_formats = lambda filepath, config: True  # type: ignore[method-assign]


def main() -> None:
    port = int(os.environ.get("MARKER_E2E_PORT", str(DEFAULT_PORT)))
    scratch = _resolve_scratch_dir()
    scratch.mkdir(parents=True, exist_ok=True)
    db_path = scratch / "e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    _prepare_environment(db_url)
    _redirect_data_dirs(scratch)
    latest_render = asyncio.run(_migrate_and_seed(db_url))
    _write_seed_source()

    import app.main as app_main

    _stub_conversion_render(latest_render)

    import uvicorn

    print(
        f"{READY_LINE_PREFIX} host={HOST} port={port} job_id={SEEDED_JOB_ID} "
        f"db_dir={scratch}",
        flush=True,
    )
    uvicorn.run(app_main.app, host=HOST, port=port)


if __name__ == "__main__":
    main()
