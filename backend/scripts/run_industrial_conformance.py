#!/usr/bin/env python3
"""Industrial persistence conformance runner (PR83B1, Checkpoint G).

One command proving the expanded PR83B industrial matrix — real
PostgreSQL AND a real S3-compatible service, strict mode everywhere,
zero tolerated skips — through the same pytest suites a reviewer runs
locally:

* dual-backend kernel commit conformance (PR83A);
* control-plane conformance: fencing/scheduler/liveness/events (WS2);
* materialization/retention conformance (WS4);
* the production ``KernelRuntimeCoordinator`` integration suite (WS3);
* the shared dialect/transaction vocabulary (WS1);
* payload-store conformance over both the local and S3 profiles (WS5);
* the S3 falsification suite (conditional create, ambiguity, heal);
* the full lifecycle conformance: snapshots/reconcile/GC across both
  databases × both stores (WS6).

Provisioning: reuses a running PostgreSQL container, starts one via
Docker, or accepts ``--external-url``; likewise reuses/starts a MinIO
container or accepts explicit S3 endpoint credentials. Strict env
(``MARKER_TEST_POSTGRES_STRICT`` / ``MARKER_TEST_S3_STRICT``) makes any
missing provisioning a FAILURE, and the runner additionally refuses to
report success when pytest skipped anything.

Usage (from backend/ or repo root)::

    python backend/scripts/run_industrial_conformance.py
    python scripts/run_industrial_conformance.py --keep-services
    python scripts/run_industrial_conformance.py \\
        --external-url postgresql+asyncpg://user:pass@host:5432/postgres \\
        --s3-endpoint http://127.0.0.1:9000 \\
        --s3-access-key minioadmin --s3-secret-key minioadmin

Requires: a reachable Docker daemon (unless both external endpoints are
given) and asyncpg + httpx in the running interpreter's environment.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_kernel_pg_conformance as pg_runner  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: The expanded industrial matrix. Every target must pass with real
#: PostgreSQL and the real S3-compatible service in strict mode.
#: PR83B2 adds real-backend lexical publication + query serving.
#: PR83B3 adds the industrial source-artifact topology (store,
#: acquisition, runtime, and process-boundary proofs).
#: PR83C1 adds the industrial recovery boundary (recovery-point capture,
#: destructive restore + oracle, and OS-process failover drills).
TEST_TARGETS = (
    "tests/test_kernel_dual_backend_conformance.py",
    "tests/test_kernel_control_plane_conformance.py",
    "tests/test_kernel_materialization_conformance.py",
    "tests/test_kernel_runtime.py",
    "tests/test_kernel_dialects.py",
    "tests/test_payload_store_conformance.py",
    "tests/test_payload_store_s3.py",
    "tests/test_kernel_lifecycle_conformance.py",
    "tests/test_kernel_publication_lexical_conformance.py",
    "tests/test_context_runtime_lexical_conformance.py",
    "tests/test_source_store_conformance.py",
    "tests/test_source_store_s3.py",
    "tests/test_kernel_source_acquisition_s3.py",
    "tests/test_kernel_source_runtime_s3.py",
    "tests/test_kernel_source_industrial_topology.py",
    "tests/test_kernel_recovery_point.py",
    "tests/test_kernel_recovery_restore.py",
    "tests/test_kernel_recovery_failover.py",
)

S3_ENDPOINT_ENV = "MARKER_TEST_S3_ENDPOINT"
S3_ACCESS_ENV = "MARKER_TEST_S3_ACCESS_KEY"
S3_SECRET_ENV = "MARKER_TEST_S3_SECRET_KEY"
S3_STRICT_ENV = "MARKER_TEST_S3_STRICT"

DEFAULT_S3_CONTAINER = "marker-minio-industrial"
DEFAULT_S3_IMAGE = "minio/minio:latest"
DEFAULT_S3_PORT = 55446
DEFAULT_S3_USER = "marker"
DEFAULT_S3_PASSWORD = "marker-marker"  # ephemeral test credential
S3_READY_TIMEOUT_SECONDS = 120.0


def start_minio_container(
    name: str, image: str, port: int, user: str, password: str
) -> None:
    pg_runner._docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"MINIO_ROOT_USER={user}",
            "-e",
            f"MINIO_ROOT_PASSWORD={password}",
            "-p",
            f"127.0.0.1:{port}:9000",
            image,
            "server",
            "/data",
            "--console-address",
            ":9001",
        ]
    )


def wait_s3_ready(endpoint: str, timeout: float) -> str:
    """Wait for MinIO's liveness endpoint; return its Server banner."""
    health = endpoint.rstrip("/") + "/minio/health/live"
    deadline = time.monotonic() + timeout
    server_banner = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2.0) as response:
                server_banner = response.headers.get("Server", "")
                if response.status == 200:
                    return server_banner
        except Exception:
            pass
        time.sleep(1.0)
    raise SystemExit(
        f"S3-compatible service did not become ready at {health} within "
        f"{timeout:.0f}s; refusing to run conformance against an "
        "unresponsive object store"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_industrial_conformance.py",
        description=(
            "Run the expanded industrial persistence matrix (real "
            "PostgreSQL + real S3-compatible service, strict, no skips)."
        ),
    )
    parser.add_argument(
        "--postgres-version", default="16",
        help="Major PostgreSQL version for the provisioned container.",
    )
    parser.add_argument(
        "--pg-container-name", default="marker-pg-industrial",
        help="PostgreSQL container name to reuse/start.",
    )
    parser.add_argument(
        "--pg-port", type=int, default=55445, help="PostgreSQL host port."
    )
    parser.add_argument("--pg-user", default="marker", help="Container superuser name.")
    parser.add_argument(
        "--pg-password", default="marker", help="Container superuser password."
    )
    parser.add_argument(
        "--external-url", default=None,
        help="Use an existing PostgreSQL server instead of Docker.",
    )
    parser.add_argument(
        "--s3-container-name", default=DEFAULT_S3_CONTAINER,
        help="MinIO container name to reuse/start.",
    )
    parser.add_argument(
        "--s3-image", default=DEFAULT_S3_IMAGE, help="MinIO image to start."
    )
    parser.add_argument(
        "--s3-port", type=int, default=DEFAULT_S3_PORT, help="MinIO host port."
    )
    parser.add_argument(
        "--s3-endpoint", default=None,
        help="Use an existing S3-compatible endpoint instead of Docker "
        "(requires --s3-access-key/--s3-secret-key).",
    )
    parser.add_argument("--s3-access-key", default=None)
    parser.add_argument("--s3-secret-key", default=None)
    parser.add_argument(
        "--keep-services", action="store_true",
        help="Leave containers this run started in place for reuse.",
    )
    parser.add_argument(
        "pytest_args", nargs="*", help="Extra arguments passed to pytest."
    )
    args = parser.parse_args(argv)

    # ---- PostgreSQL ---------------------------------------------------
    started_pg = False
    admin_url = args.external_url
    if admin_url is None:
        if not pg_runner.docker_available():
            print(
                "ERROR: Docker is not available and no --external-url was "
                "provided; cannot provision real PostgreSQL.",
                file=sys.stderr,
            )
            return 2
        state = pg_runner.container_state(args.pg_container_name)
        if state is None:
            image = f"postgres:{args.postgres_version}-alpine"
            print(f"[industrial] starting {args.pg_container_name} ({image})")
            pg_runner.start_container(
                args.pg_container_name, image, args.pg_port,
                args.pg_user, args.pg_password,
            )
            started_pg = True
        elif not state.startswith("Up"):
            print(f"[industrial] starting existing {args.pg_container_name}")
            pg_runner._docker(["start", args.pg_container_name])
        else:
            print(f"[industrial] reusing running {args.pg_container_name}")
        admin_url = (
            f"postgresql+asyncpg://{args.pg_user}:{args.pg_password}"
            f"@127.0.0.1:{args.pg_port}/postgres"
        )
    host, port, user, password = pg_runner._parse_admin_url(admin_url)
    pg_runner.wait_tcp_ready(host, port, user, password, pg_runner.READY_TIMEOUT_SECONDS)
    pg_banner = str(pg_runner._pg_banner(host, port, user, password))

    # ---- S3-compatible service ----------------------------------------
    started_s3 = False
    s3_endpoint = args.s3_endpoint
    s3_access = args.s3_access_key
    s3_secret = args.s3_secret_key
    if s3_endpoint is None:
        if not pg_runner.docker_available():
            print(
                "ERROR: Docker is not available and no --s3-endpoint was "
                "provided; cannot provision a real object store.",
                file=sys.stderr,
            )
            return 2
        s3_access = DEFAULT_S3_USER
        s3_secret = DEFAULT_S3_PASSWORD
        state = pg_runner.container_state(args.s3_container_name)
        if state is None:
            print(f"[industrial] starting {args.s3_container_name} ({args.s3_image})")
            start_minio_container(
                args.s3_container_name, args.s3_image, args.s3_port,
                s3_access, s3_secret,
            )
            started_s3 = True
        elif not state.startswith("Up"):
            print(f"[industrial] starting existing {args.s3_container_name}")
            pg_runner._docker(["start", args.s3_container_name])
        else:
            print(f"[industrial] reusing running {args.s3_container_name}")
        s3_endpoint = f"http://127.0.0.1:{args.s3_port}"
    elif not (s3_access and s3_secret):
        print(
            "ERROR: --s3-endpoint requires --s3-access-key and "
            "--s3-secret-key.",
            file=sys.stderr,
        )
        return 2
    s3_banner = wait_s3_ready(s3_endpoint, S3_READY_TIMEOUT_SECONDS)

    print(f"[industrial] PostgreSQL: {pg_banner}")
    print(f"[industrial] object store: {s3_endpoint} ({s3_banner or 'server banner n/a'})")

    # ---- strict pytest over the whole matrix ---------------------------
    env = dict(os.environ)
    env[pg_runner.ADMIN_URL_ENV] = admin_url
    env[pg_runner.STRICT_ENV] = "1"
    env[S3_ENDPOINT_ENV] = s3_endpoint
    env[S3_ACCESS_ENV] = s3_access
    env[S3_SECRET_ENV] = s3_secret
    env[S3_STRICT_ENV] = "1"

    cmd = [
        sys.executable, "-m", "pytest", *TEST_TARGETS,
        "-q", "-rs", "--no-header", *args.pytest_args,
    ]
    print(f"[industrial] strict env set; running {' '.join(cmd)}")
    completed = subprocess.run(
        cmd, cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    print(output, end="")

    if started_pg and not args.keep_services:
        pg_runner._docker(["rm", "-f", args.pg_container_name], check=False)
    if started_s3 and not args.keep_services:
        pg_runner._docker(["rm", "-f", args.s3_container_name], check=False)

    if completed.returncode != 0:
        return completed.returncode
    if re.search(r"^\s*\d+ skipped\b", output, re.M):
        print("ERROR: industrial run reported skipped tests", file=sys.stderr)
        return 3
    print(
        "[industrial] PASS: expanded matrix green against real "
        f"PostgreSQL ({pg_banner.split(',')[0]}) and real object store "
        f"({s3_endpoint}) with zero skips"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
