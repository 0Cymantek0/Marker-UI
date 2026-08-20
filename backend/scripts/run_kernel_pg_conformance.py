#!/usr/bin/env python3
"""Dual-backend kernel conformance runner (PR83A).

Provisions a real PostgreSQL server (reusing a running container,
starting one via Docker, or accepting an external server URL), then
runs ``tests/test_kernel_dual_backend_conformance.py`` in strict mode:
the PostgreSQL parameters FAIL when the server is missing instead of
skipping, so an invoked industrial conformance target can never pass
silently. Exits non-zero on any test failure, any skip, or any missing
prerequisite (Docker unavailable, server never becoming ready).

Usage (from backend/ or repo root)::

    python backend/scripts/run_kernel_pg_conformance.py
    python scripts/run_kernel_pg_conformance.py --postgres-version 17
    python scripts/run_kernel_pg_conformance.py --external-url \
        postgresql+asyncpg://user:pass@host:5432/postgres
    python scripts/run_kernel_pg_conformance.py --keep-container

Requires: a reachable Docker daemon (unless ``--external-url``), and
asyncpg installed in the running interpreter's environment.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_TARGET = "tests/test_kernel_dual_backend_conformance.py"
ADMIN_URL_ENV = "MARKER_TEST_POSTGRES_ADMIN_URL"
STRICT_ENV = "MARKER_TEST_POSTGRES_STRICT"

DEFAULT_CONTAINER = "marker-pg-pr83a"
DEFAULT_IMAGE = "postgres:16-alpine"
DEFAULT_PORT = 55432
DEFAULT_USER = "marker"
DEFAULT_PASSWORD = "marker"
READY_TIMEOUT_SECONDS = 120.0

#: Throwaway local credentials for an ephemeral test container — not a
#: secret of any kind; override via --user/--password when needed.


def _docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def docker_available() -> bool:
    try:
        return _docker(["info"], check=False).returncode == 0
    except FileNotFoundError:
        return False


def container_state(name: str) -> str | None:
    result = _docker(
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
        check=False,
    )
    status = result.stdout.strip()
    return status or None


def start_container(name: str, image: str, port: int, user: str, password: str) -> None:
    _docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_DB=postgres",
            "-p",
            f"127.0.0.1:{port}:5432",
            image,
        ]
    )


def wait_tcp_ready(
    host: str, port: int, user: str, password: str, timeout: float, *, database: str = "postgres"
) -> None:
    """Wait until the server accepts TCP connections and answers a query."""
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                pass
        except OSError:
            time.sleep(1.0)
            continue
        try:
            asyncio.run(_pg_ping(host, port, user, password, database))
            return
        except Exception:
            time.sleep(1.0)
    raise SystemExit(
        f"PostgreSQL did not become ready on {host}:{port} within "
        f"{timeout:.0f}s; refusing to run conformance against an "
        "unresponsive server"
    )


async def _pg_ping(host: str, port: int, user: str, password: str, database: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(
        host=host, port=port, user=user, password=password, database=database
    )
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


def _parse_admin_url(url: str) -> tuple[str, int, str, str]:
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    return (
        parsed.host or "127.0.0.1",
        parsed.port or 5432,
        parsed.username or "postgres",
        parsed.password or "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_kernel_pg_conformance.py",
        description="Run the dual-backend kernel conformance suite against real PostgreSQL.",
    )
    parser.add_argument(
        "--postgres-version",
        default="16",
        help="Major PostgreSQL version for the provisioned container (default 16).",
    )
    parser.add_argument(
        "--container-name", default=DEFAULT_CONTAINER, help="Container name to reuse/start."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Host port (default 55432).")
    parser.add_argument("--user", default=DEFAULT_USER, help="Container superuser name.")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Container superuser password.")
    parser.add_argument(
        "--external-url",
        default=None,
        help="Use an existing PostgreSQL server (asyncpg URL to its "
        "'postgres' maintenance database) instead of Docker.",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Leave a container this run started in place for reuse.",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="Extra arguments passed through to pytest.",
    )
    args = parser.parse_args(argv)

    started_container = False
    admin_url = args.external_url
    if admin_url is None:
        if not docker_available():
            print(
                "ERROR: Docker is not available and no --external-url was "
                "provided; cannot provision real PostgreSQL. Start Docker "
                "Desktop or point --external-url at a running server.",
                file=sys.stderr,
            )
            return 2
        state = container_state(args.container_name)
        if state is None:
            image = f"postgres:{args.postgres_version}-alpine"
            print(f"[conformance] starting container {args.container_name} ({image})")
            start_container(
                args.container_name, image, args.port, args.user, args.password
            )
            started_container = True
        elif not state.startswith("Up"):
            print(f"[conformance] starting existing container {args.container_name}")
            _docker(["start", args.container_name])
        else:
            print(f"[conformance] reusing running container {args.container_name}")
        admin_url = (
            f"postgresql+asyncpg://{args.user}:{args.password}"
            f"@127.0.0.1:{args.port}/postgres"
        )

    host, port, user, password = _parse_admin_url(admin_url)
    wait_tcp_ready(host, port, user, password, READY_TIMEOUT_SECONDS)

    env = dict(os.environ)
    env[ADMIN_URL_ENV] = admin_url
    env[STRICT_ENV] = "1"

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        TEST_TARGET,
        "-q",
        "-rs",
        "--no-header",
        *args.pytest_args,
    ]
    print(f"[conformance] {ADMIN_URL_ENV} set; strict mode on")
    print(f"[conformance] {' '.join(cmd)}")
    completed = subprocess.run(
        cmd, cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    print(output, end="")

    if started_container and not args.keep_container:
        print(f"[conformance] stopping container {args.container_name}")
        _docker(["rm", "-f", args.container_name], check=False)

    if completed.returncode != 0:
        return completed.returncode

    # Belt-and-braces: strict mode already turns skips into failures,
    # but never report success for a run that skipped anything.
    if re.search(r"^\s*\d+ skipped\b", output, re.M):
        print("ERROR: conformance run reported skipped tests", file=sys.stderr)
        return 3
    print("[conformance] PASS: dual-backend kernel conformance green with real PostgreSQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
