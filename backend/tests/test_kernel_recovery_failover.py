"""PR83C1 Workstream D: replacement-process failover over real services.

Real OS processes, killed without graceful cleanup, with their node-
local directories destroyed; the replacement starts with fresh empty
directories against the SAME real PostgreSQL + S3 services. The drills
prove: durable truth survives, published queries stay deterministic,
source bytes rematerialize from shared storage, lapsed work is taken
over through the fence, the dead owner's late completion is rejected,
exactly one accepted publication remains, and new work commits under
the new authority — with the failover RTO measured from kill to a
verified post-recovery write (never to "port open").

Service-outage variants stop/start the real containers: recovery must
fail honestly while the authority is down and complete after it returns
— never report a false success.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db_migration import upgrade_database
from app.kernel.models import KernelOutbox, KernelPublication
from tests.pg_provisioning import (
    engine_kwargs_for,
    provisioned_database,
)
from tests.recovery_drills import require_recovery_services
from tests.s3_provisioning import unique_bucket

pytestmark = pytest.mark.asyncio

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROBE_LEASE_SECONDS = 1.5


def _port_from_url(url: str, default: int) -> int:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.port or default


def _pg_port() -> int:
    return _port_from_url(os.environ["MARKER_TEST_POSTGRES_ADMIN_URL"], 5432)


def _s3_port() -> int:
    return _port_from_url(os.environ["MARKER_TEST_S3_ENDPOINT"], 9000)


def _container_by_port(host_port: int) -> str:
    """Find the container publishing host_port — works for locally
    provisioned containers and CI service containers alike (their names
    are generated, their published ports are not)."""
    import subprocess as sync_subprocess

    output = sync_subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Names}} {{.Ports}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and f"{host_port}->" in parts[2]:
            return parts[1]
    raise AssertionError(
        f"no container publishes host port {host_port}; the outage drills "
        "need docker control over the real services"
    )


class DrillHarness:
    """Parent-side orchestration of the probe processes."""

    def __init__(self, tmp_path: Path, *, workspace: str = "recovery-failover"):
        self.tmp_path = tmp_path
        self.workspace = workspace
        self.node_a = tmp_path / "node-a"
        self.node_b = tmp_path / "node-b"
        self.node_a.mkdir(parents=True)
        self.node_b.mkdir(parents=True)
        self.setup_state: dict | None = None
        self._engine = None
        self._factory = None

    def base_env(self, node_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PROBE_DB_URL": self.db_url,
                "PROBE_WORKSPACE": self.workspace,
                "PROBE_PAYLOAD_BUCKET": self.payload_bucket,
                "PROBE_SOURCE_BUCKET": self.source_bucket,
                "MARKER_SOURCE_STORE_PROFILE": "s3",
                "MARKER_SOURCE_S3_ENDPOINT": self.endpoint,
                "MARKER_SOURCE_S3_ACCESS_KEY": self.access_key,
                "MARKER_SOURCE_S3_SECRET_KEY": self.secret_key,
                "MARKER_SOURCE_CACHE_ROOT": str(node_dir / "source-cache"),
                "MARKER_SOURCE_STORE_ROOT": str(node_dir / "source-store"),
                "MARKER_WORKSPACE_ROOTS": str(self.tmp_path),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    async def spawn(self, mode: str, env: dict[str, str]) -> asyncio.subprocess.Process:
        import sys as _sys

        merged = dict(env)
        merged["PROBE_MODE"] = mode
        return await asyncio.create_subprocess_exec(
            os.environ.get("PROBE_PYTHON", _sys.executable),
            "-X",
            "utf8",
            "-m",
            "tests.recovery_probe",
            env=merged,
            cwd=str(_BACKEND_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def run_probe(self, mode: str, env: dict[str, str]) -> tuple[int, dict, str]:
        """Run one probe to completion; parse its JSON lines + exit code."""
        proc = await self.spawn(mode, env)
        stdout, stderr = await proc.communicate()
        text = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        events: dict[str, object] = {}
        milestones: dict[str, float] = {}
        for line in text.splitlines():
            if line.startswith("SETUP:"):
                events["setup"] = json.loads(line[len("SETUP:") :])
            elif line.startswith("CLAIM:"):
                events["claim"] = json.loads(line[len("CLAIM:") :])
            elif line.startswith("RECOVERED:"):
                events["recovered"] = json.loads(line[len("RECOVERED:") :])
            elif line.startswith("ORACLE:"):
                events["oracle"] = json.loads(line[len("ORACLE:") :])
            elif line.startswith("MILESTONE:"):
                name, _, epoch = line[len("MILESTONE:") :].partition(" ")
                milestones[name] = float(epoch)
            elif line.strip() == "STALE_REJECTED":
                events["stale_rejected"] = True
            elif line.startswith("STALE_ACCEPTED:"):
                events["stale_accepted"] = json.loads(line[len("STALE_ACCEPTED:") :])
        events["milestones"] = milestones
        return proc.returncode or 0, events, err


@contextlib.asynccontextmanager
async def full_drill(tmp_path: Path, *, kill_before_claim: bool = False):
    """The core A-dies / B-recovers drill, bound to the provisioned
    database's lifetime: everything the caller asserts — including
    stale-owner replays — runs while the shared services still hold the
    drill's database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    admin_url, endpoint, access, secret = require_recovery_services()
    async with provisioned_database(
        "postgresql", (tmp_path / "kernel.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        harness = DrillHarness(tmp_path)
        harness.db_url = prov.url
        harness.admin_url = admin_url
        harness.endpoint = endpoint
        harness.access_key = access
        harness.secret_key = secret
        harness.payload_bucket = unique_bucket()
        harness.source_bucket = unique_bucket()

        # -- process A: build the world ------------------------------------
        setup_env = harness.base_env(harness.node_a)
        setup_env["PROBE_SOURCES_DIR"] = str(tmp_path / "sources")
        (tmp_path / "sources").mkdir(exist_ok=True)
        code, events, err = await harness.run_probe("setup", setup_env)
        assert code == 0, f"setup probe failed: {err}"
        setup = events["setup"]
        work_id = int(setup["work_id"])

        claim: dict | None = None
        if not kill_before_claim:
            hold_env = harness.base_env(harness.node_a)
            hold_env["PROBE_OWNER"] = "worker-a"
            hold_env["PROBE_LEASE_SECONDS"] = str(PROBE_LEASE_SECONDS)
            proc = await harness.spawn("hold", hold_env)
            claim_line = await asyncio.wait_for(proc.stdout.readline(), 30)
            claim = json.loads(claim_line.decode().strip()[len("CLAIM:") :])
            # A is now parked mid-execution holding the fence
            kill_epoch = time.time()
            proc.kill()
            await proc.wait()
        else:
            kill_epoch = time.time()

        # -- destroy A's node-local world ----------------------------------
        shutil.rmtree(harness.node_a, ignore_errors=True)
        harness.node_a.mkdir(parents=True)

        # -- process B: recover from shared truth only ----------------------
        recover_env = harness.base_env(harness.node_b)
        recover_env["PROBE_WORK_ID"] = str(work_id)
        recover_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
        recover_env["PROBE_QUERY"] = json.dumps(setup["query_expectation"])
        recover_env["PROBE_OWNER"] = "worker-b"
        recover_env["PROBE_RESULT"] = json.dumps(
            {"job_id": "drill-job-a", "status": "completed", "marker": "result-b"}
        )
        recover_env["PROBE_KILL_EPOCH"] = str(kill_epoch)
        code, events, err = await harness.run_probe("recover", recover_env)
        assert code == 0, f"recover probe failed: {err}"
        recovered = events["recovered"]
        milestones: dict[str, float] = events["milestones"]

        engine = create_async_engine(prov.url, **engine_kwargs_for("postgresql"))
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        try:
            yield {
                "setup": setup,
                "claim": claim,
                "kill_epoch": kill_epoch,
                "milestones": milestones,
                "recovered": recovered,
                "factory": factory,
                "work_id": work_id,
                "engine": engine,
                "harness": harness,
            }
        finally:
            await engine.dispose()


async def test_replacement_process_takeover_with_stale_owner_rejection(
    tmp_path: Path,
) -> None:
    """R1-R6 + R18: kill A mid-claim, B recovers everything, A's late
    completion is fenced out, exactly one publication remains, and the
    RTO from kill to verified post-recovery write is measured."""
    async with full_drill(tmp_path) as drill:
        claim, milestones = drill["claim"], drill["milestones"]
        recovered = drill["recovered"]
        work_id = drill["work_id"]
        factory = drill["factory"]

        # B took the fence over (token advanced past A's)
        assert claim is not None
        assert recovered["fencing_token"] > claim["fencing_token"]
        # recovery milestones, in order, all after the kill
        order = ["boot", "semantic_ready", "source_ready", "query_ready", "work_ready", "write_ready"]
        epochs = [milestones[name] for name in order]
        assert epochs == sorted(epochs), milestones
        assert all(e >= drill["kill_epoch"] for e in epochs)
        # B committed NEW truth beyond the recovered cut
        assert recovered["new_commit"] > recovered["recovered_cut"]

        # -- A's ghost: late completion under the dead fence ---------------
        stale_env = drill["harness"].base_env(drill["harness"].node_b)
        stale_env["PROBE_WORK_ID"] = str(work_id)
        stale_env["PROBE_TOKEN"] = str(claim["fencing_token"])
        stale_env["PROBE_RESULT"] = json.dumps(
            {"job_id": "drill-job-a", "status": "completed", "marker": "result-a-stale"}
        )
        code, events, err = await drill["harness"].run_probe("stale", stale_env)
        assert code == 0, err
        assert events.get("stale_rejected") is True
        assert "stale_accepted" not in events

        # -- exactly one accepted publication, acked behind B's fence ------
        async with factory() as session:
            publications = (
                (
                    await session.execute(
                        select(KernelPublication).where(
                            KernelPublication.work_id == work_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(publications) == 1
            outbox = await session.get(KernelOutbox, work_id)
            assert outbox is not None and outbox.state == "done"

        # -- duplicate delivery converges: B's re-accept is idempotent -----
        from app.kernel.scheduler import accept_work

        outcome, _appended = await accept_work(
            factory,
            work_id=work_id,
            fencing_token=recovered["fencing_token"],
            result={"job_id": "drill-job-a", "status": "completed", "marker": "result-b"},
        )
        assert outcome.already_accepted
        async with factory() as session:
            count = len(
                (
                    await session.execute(
                        select(KernelPublication).where(
                            KernelPublication.work_id == work_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert count == 1


async def test_crash_before_claim_leaves_work_pending_for_replacement(
    tmp_path: Path,
) -> None:
    """Variant: A dies before claiming. The work was never owned; B
    picks it up as a fresh claim (token 1) and completes it."""
    async with full_drill(tmp_path, kill_before_claim=True) as drill:
        recovered = drill["recovered"]
        assert recovered["fencing_token"] == 1, (
            "unclaimed work must be claimable without a takeover advance"
        )
        assert drill["milestones"]["work_ready"] > 0
        assert recovered["new_commit"] > recovered["recovered_cut"]


async def test_database_outage_during_takeover_fails_honestly(
    tmp_path: Path,
) -> None:
    """R16: with PostgreSQL gone, the replacement must fail — never
    report recovery — and must complete once the authority returns."""
    import subprocess as sync_subprocess

    admin_url, endpoint, access, secret = require_recovery_services()
    async with provisioned_database(
        "postgresql", (tmp_path / "kernel.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        harness = DrillHarness(tmp_path)
        harness.db_url = prov.url
        harness.admin_url = admin_url
        harness.endpoint = endpoint
        harness.access_key = access
        harness.secret_key = secret
        harness.payload_bucket = unique_bucket()
        harness.source_bucket = unique_bucket()

        setup_env = harness.base_env(harness.node_a)
        setup_env["PROBE_SOURCES_DIR"] = str(tmp_path / "sources")
        (tmp_path / "sources").mkdir(exist_ok=True)
        code, events, err = await harness.run_probe("setup", setup_env)
        assert code == 0, err
        setup = events["setup"]
        work_id = int(setup["work_id"])

        hold_env = harness.base_env(harness.node_a)
        hold_env["PROBE_OWNER"] = "worker-a"
        hold_env["PROBE_LEASE_SECONDS"] = str(PROBE_LEASE_SECONDS)
        proc = await harness.spawn("hold", hold_env)
        await asyncio.wait_for(proc.stdout.readline(), 30)
        proc.kill()
        await proc.wait()
        shutil.rmtree(harness.node_a, ignore_errors=True)

        recover_env = harness.base_env(harness.node_b)
        recover_env["PROBE_WORK_ID"] = str(work_id)
        recover_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
        recover_env["PROBE_QUERY"] = json.dumps(setup["query_expectation"])
        recover_env["PROBE_OWNER"] = "worker-b"
        recover_env["PROBE_KILL_EPOCH"] = str(time.time())

        # -- PostgreSQL goes down mid-recovery -----------------------------
        pg_container = _container_by_port(_pg_port())
        sync_subprocess.run(["docker", "stop", pg_container], check=True)
        try:
            code, events, err = await harness.run_probe("recover", recover_env)
            assert code != 0, "recovery must not succeed without the database"
            assert "RECOVERED" not in err and "recovered" not in events
        finally:
            sync_subprocess.run(["docker", "start", pg_container], check=True)
        await _wait_postgres_ready(admin_url)

        # -- authority returns: recovery completes -------------------------
        code, events, err = await harness.run_probe("recover", recover_env)
        assert code == 0, err
        assert events["recovered"]["new_commit"] > events["recovered"]["recovered_cut"]


async def _wait_postgres_ready(admin_url: str, timeout: float = 60.0) -> None:
    from sqlalchemy.engine import make_url

    import asyncpg

    parsed = make_url(admin_url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(
                host=parsed.host,
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                database=parsed.database or "postgres",
            )
            await conn.close()
            return
        except Exception:
            await asyncio.sleep(0.5)
    raise AssertionError("PostgreSQL did not come back after restart")


async def test_s3_outage_during_source_recovery_fails_honestly(
    tmp_path: Path,
) -> None:
    """R17: with the object store gone, source rematerialization fails
    closed; after it returns, recovery completes from shared storage."""
    import subprocess as sync_subprocess

    admin_url, endpoint, access, secret = require_recovery_services()
    async with provisioned_database(
        "postgresql", (tmp_path / "kernel.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        harness = DrillHarness(tmp_path)
        harness.db_url = prov.url
        harness.admin_url = admin_url
        harness.endpoint = endpoint
        harness.access_key = access
        harness.secret_key = secret
        harness.payload_bucket = unique_bucket()
        harness.source_bucket = unique_bucket()

        setup_env = harness.base_env(harness.node_a)
        setup_env["PROBE_SOURCES_DIR"] = str(tmp_path / "sources")
        (tmp_path / "sources").mkdir(exist_ok=True)
        code, events, err = await harness.run_probe("setup", setup_env)
        assert code == 0, err
        setup = events["setup"]
        work_id = int(setup["work_id"])

        hold_env = harness.base_env(harness.node_a)
        hold_env["PROBE_OWNER"] = "worker-a"
        hold_env["PROBE_LEASE_SECONDS"] = str(PROBE_LEASE_SECONDS)
        proc = await harness.spawn("hold", hold_env)
        await asyncio.wait_for(proc.stdout.readline(), 30)
        proc.kill()
        await proc.wait()
        shutil.rmtree(harness.node_a, ignore_errors=True)

        recover_env = harness.base_env(harness.node_b)
        recover_env["PROBE_WORK_ID"] = str(work_id)
        recover_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
        recover_env["PROBE_QUERY"] = json.dumps(setup["query_expectation"])
        recover_env["PROBE_OWNER"] = "worker-b"
        recover_env["PROBE_KILL_EPOCH"] = str(time.time())

        s3_container = _container_by_port(_s3_port())
        sync_subprocess.run(["docker", "stop", s3_container], check=True)
        try:
            code, events, err = await harness.run_probe("recover", recover_env)
            assert code != 0, "recovery must not succeed without the object store"
        finally:
            sync_subprocess.run(["docker", "start", s3_container], check=True)
        await _wait_s3_ready(endpoint)

        code, events, err = await harness.run_probe("recover", recover_env)
        assert code == 0, err
        assert events["recovered"]["fencing_token"] >= 2


async def _wait_s3_ready(endpoint: str, timeout: float = 60.0) -> None:
    import urllib.request

    health = endpoint.rstrip("/") + "/minio/health/live"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2.0) as response:
                if response.status == 200:
                    return
        except Exception:
            await asyncio.sleep(0.5)
    raise AssertionError("object store did not come back after restart")
