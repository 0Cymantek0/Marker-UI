#!/usr/bin/env python3
"""Strict PR83C2 failover conformance runner (real two-node PostgreSQL).

Provisions (or reuses) a real S3-compatible object store, then runs the
promotion-drill suite in strict mode. The two-node PostgreSQL topology
itself is provisioned by the tests (each drill owns its containers) —
this runner guarantees the object store, refuses to run without a
Docker daemon, and enforces the same no-skip contract as the industrial
runner: any failure, any skip, or any missing prerequisite is a red
exit, never a green skip.

Usage (from backend/ or repo root)::

    python backend/scripts/run_failover_conformance.py
    python scripts/run_failover_conformance.py --keep-services

Requires: a reachable Docker daemon and asyncpg in the interpreter env.

Run this suite in isolation (its own CI job / its own object store):
one drill phase deliberately stops and restarts the object-store
container named by MARKER_TEST_S3_ENDPOINT, which is only safe when
nothing else consumes that endpoint concurrently.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: The failover promotion suite this strict runner owns. The tests
#: provision the two-node PostgreSQL topology themselves; nothing here
#: may execute against a fake backend.
TEST_TARGETS = (
    "tests/test_kernel_pg_failover_promotion.py",
)

DEFAULT_MINIO_CONTAINER = "marker-minio-failover"
DEFAULT_MINIO_IMAGE = "minio/minio:latest"
DEFAULT_MINIO_PORT = 55463
MINIO_USER = "marker"
MINIO_PASSWORD = "marker-marker"
READY_TIMEOUT_SECONDS = 120.0

S3_ENDPOINT_ENV = "MARKER_TEST_S3_ENDPOINT"
S3_ACCESS_ENV = "MARKER_TEST_S3_ACCESS_KEY"
S3_SECRET_ENV = "MARKER_TEST_S3_SECRET_KEY"
S3_STRICT_ENV = "MARKER_TEST_S3_STRICT"
FAILOVER_STRICT_ENV = "MARKER_TEST_FAILOVER_STRICT"

#: Throwaway local credentials for an ephemeral test container — not a
#: secret of any kind; override via the standard env vars to reuse an
#: external object store instead.


def _docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
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


def start_minio_container(name: str, image: str, port: int) -> None:
    _docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:9000",
            "-e",
            f"MINIO_ROOT_USER={MINIO_USER}",
            "-e",
            f"MINIO_ROOT_PASSWORD={MINIO_PASSWORD}",
            image,
            "server",
            "/data",
            "--console-address",
            ":9001",
        ]
    )


def wait_s3_ready(endpoint: str, timeout: float = READY_TIMEOUT_SECONDS) -> str:
    health = endpoint.rstrip("/") + "/minio/health/live"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2.0) as response:
                if response.status == 200:
                    return response.headers.get("Server", "")
        except Exception:
            time.sleep(1.0)
    raise SystemExit(
        f"object store did not become ready at {endpoint} within "
        f"{timeout:.0f}s; refusing to run failover conformance"
    )


def _port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_failover_conformance.py",
        description=(
            "Run the strict PR83C2 two-node PostgreSQL failover suite "
            "against real services (Docker-provisioned or env-provided)."
        ),
    )
    parser.add_argument(
        "--minio-container-name", default=DEFAULT_MINIO_CONTAINER,
        help="Object-store container to reuse/start (default "
        f"{DEFAULT_MINIO_CONTAINER})."
    )
    parser.add_argument("--minio-port", type=int, default=DEFAULT_MINIO_PORT)
    parser.add_argument(
        "--keep-services", action="store_true",
        help="Leave a container this run started in place for reuse."
    )
    parser.add_argument(
        "pytest_args", nargs="*", help="Extra arguments passed through to pytest."
    )
    args = parser.parse_args(argv)

    if not docker_available():
        print(
            "ERROR: Docker is not available; the failover drills provision "
            "real two-node PostgreSQL and need docker. Start Docker Desktop "
            "and retry.",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    started_minio = False
    endpoint = os.getenv(S3_ENDPOINT_ENV)
    if endpoint is None:
        state = container_state(args.minio_container_name)
        if state is None:
            if _port_in_use(args.minio_port):
                print(
                    f"ERROR: port {args.minio_port} is occupied by a "
                    "non-matching service and no object-store endpoint is "
                    f"provided; set {S3_ENDPOINT_ENV} or free the port.",
                    file=sys.stderr,
                )
                return 2
            print(
                f"[failover] starting object store {args.minio_container_name}"
            )
            start_minio_container(
                args.minio_container_name, DEFAULT_MINIO_IMAGE, args.minio_port
            )
            started_minio = True
        elif not state.startswith("Up"):
            print(f"[failover] starting existing container {args.minio_container_name}")
            _docker(["start", args.minio_container_name])
        else:
            print(f"[failover] reusing running container {args.minio_container_name}")
        endpoint = f"http://127.0.0.1:{args.minio_port}"
        env[S3_ENDPOINT_ENV] = endpoint
        env.setdefault(S3_ACCESS_ENV, MINIO_USER)
        env.setdefault(S3_SECRET_ENV, MINIO_PASSWORD)

    banner = wait_s3_ready(endpoint)
    print(f"[failover] object store ready at {endpoint} (server: {banner!r})")
    env.setdefault(S3_ACCESS_ENV, MINIO_USER)
    env.setdefault(S3_SECRET_ENV, MINIO_PASSWORD)
    env[S3_STRICT_ENV] = "1"
    env[FAILOVER_STRICT_ENV] = "1"

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *TEST_TARGETS,
        "-q",
        "-rs",
        "--no-header",
        *args.pytest_args,
    ]
    print(f"[failover] {' '.join(cmd)}")
    completed = subprocess.run(
        cmd, cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    print(output, end="")

    if started_minio and not args.keep_services:
        print(f"[failover] removing container {args.minio_container_name}")
        _docker(["rm", "-f", args.minio_container_name], check=False)

    if completed.returncode != 0:
        return completed.returncode
    # Mixed summaries put "N skipped" mid-line ("4 passed, 1 skipped in
    # ..."), so the scan must not anchor to line start.
    if re.search(r"\b\d+ skipped\b", output):
        print("ERROR: failover run reported skipped tests", file=sys.stderr)
        return 3
    print("[failover] PASS: two-node PostgreSQL failover suite green, no skips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
