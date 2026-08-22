"""PR83C2 promotion-drill orchestration over the real two-node topology.

Single authority for the database-failover choreography: build real
Marker UI truth on the PRIMARY (fixture, lease renewal, terminal commit,
accepted publication), establish the durability condition BEFORE the
fault (the standby is observed to possess the acknowledged truth), hard-
kill the primary, promote the physical standby, prove a fresh process
recovers through the PROMOTED authority, and verify ownership/publication
coherence with the PR83C1 recovery oracle vocabulary.

The declared durability policy is executable (``DURABILITY_POLICY``):
under ``remote_apply`` + one synchronous standby, every kernel
acknowledgement — the commit return of its own transaction — implies the
transition was replayed on the standby before the caller saw success.
The drills hold the system accountable to exactly that claim, never more.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.fencing import acquire as acquire_lease
from app.kernel.models import KernelOutbox, KernelPublication
from app.kernel.recovery import (
    PgSidecarTools,
    capture_recovery_point,
    current_head_commit,
)
from app.kernel.scheduler import accept_work
from tests.pg_failover_topology import (
    STANDBY_APPLICATION_NAME,
    SUPERUSER,
    SUPERUSER_PASSWORD,
    FailoverCluster,
)
from tests.pg_provisioning import (
    create_postgres_database,
    drop_postgres_database,
    engine_kwargs_for,
)
from tests.recovery_drills import _payload_store, _source_store
from tests.s3_provisioning import require_s3_env, unique_bucket

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

#: Work area A: the tested durability/failure contract, executable.
#: The topology's ``assert_policy_active`` refuses to run the drill
#: unless the live cluster reports exactly these settings and a
#: ``sync`` standby, so the declaration cannot drift from reality.
DURABILITY_POLICY: dict[str, Any] = {
    "synchronous_commit": "remote_apply",
    "synchronous_standby_names": f"FIRST 1 ({STANDBY_APPLICATION_NAME})",
    "acknowledgement_meaning": (
        "a kernel acknowledgement is the commit return of its own "
        "transaction; under this policy the primary returns success only "
        "after the standby replayed the transition"
    ),
    "covered_transitions": [
        "lease/fence acquisition and renewal (master plan 11B.12)",
        "accepted stable publication + terminal work state (11B.12/11B.13)",
        "kernel document commits (KernelSnapshot cut truth)",
    ],
    "declared_lossy_lane": (
        "the asynchronous comparison lane acknowledges on local WAL flush "
        "only; its acknowledged tail is NOT claimed durable across primary "
        "loss and the drill measures the actual loss"
    ),
    "not_exercised_this_session": [
        "source cursor advancement (connector lanes)",
        "irreversible-effect authorization/acknowledgement lanes",
    ],
}


def container_by_port(host_port: int) -> str:
    """Find the container publishing host_port (works for locally
    provisioned containers and CI service containers alike)."""
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
        f"no container publishes host port {host_port}; the failover "
        "drills need docker control over the real services"
    )


def s3_endpoint_port() -> int:
    from urllib.parse import urlparse

    endpoint, _access, _secret = require_s3_env()
    return urlparse(endpoint).port or 9000


async def wait_s3_ready(endpoint: str, timeout: float = 60.0) -> None:
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


async def register_pending_work(
    session_factory: async_sessionmaker,
    payload_store: Any,
    source_store: Any,
    *,
    workspace_id: str,
    block: dict[str, Any],
    cache_root: Path,
    record_id: str,
    job_id: str,
) -> int:
    """Register one pending conversion work item against committed source
    truth (the same registration shape the fixture uses). Shared by the
    pre-fault terminal probe and the post-promotion takeover phase so the
    work-item shape has a single authority."""
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.outbox import OutboxIntent
    from app.kernel.records import KernelEdge, NativeObjectRecord
    from app.services.source_acquisition import SourceAcquisitionService

    acquisition = SourceAcquisitionService(
        session_factory,
        KernelCommitService(session_factory),
        source_store,
        workspace_id=workspace_id,
        cache_root=cache_root,
    )
    revision = await acquisition.resolve(block)
    assert revision is not None, "pending-work registration source must resolve"
    record = NativeObjectRecord(
        record_id=record_id,
        source_uri=revision.source_id,
        locator=revision.blob_key,
        media_type=revision.media_type,
        extractor_name="failover-drill",
        extractor_version="1",
    )
    receipt = await KernelCommitService(
        session_factory, payload_store=payload_store
    ).commit(
        KernelCommitBatch(
            workspace_id=workspace_id,
            records=(record,),
            edges=(
                KernelEdge(
                    edge_kind="depends_on",
                    source_ref=record.record_id,
                    target_ref=revision.content_revision_id,
                ),
            ),
            outbox=(OutboxIntent(work_kind="conversion.execute", payload={"job_id": job_id}),),
        )
    )
    from app.kernel.scheduler import register_work

    work_id = receipt.outbox_ids[0]
    await register_work(session_factory, work_id=work_id, resource_class="conversion")
    return work_id


class ProbeHarness:
    """Parent-side orchestration of the failover probe processes.

    Unlike the PR83C1 harness (one database URL for life), the database
    URL is a per-call parameter: pre-fault probes point at the primary,
    post-promotion probes at the promoted standby.
    """

    def __init__(self, tmp_path: Path, *, workspace: str):
        self.tmp_path = tmp_path
        self.workspace = workspace
        self.node_a = tmp_path / "node-a"
        self.node_b = tmp_path / "node-b"
        self.node_a.mkdir(parents=True, exist_ok=True)
        self.node_b.mkdir(parents=True, exist_ok=True)
        endpoint, access_key, secret_key = require_s3_env()
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.payload_bucket = unique_bucket()
        self.source_bucket = unique_bucket()

    def base_env(self, node_dir: Path, db_url: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PROBE_DB_URL": db_url,
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
        events: dict[str, Any] = {}
        milestones: dict[str, float] = {}
        for line in text.splitlines():
            if line.startswith("SETUP:"):
                events["setup"] = json.loads(line[len("SETUP:") :])
            elif line.startswith("CLAIM:"):
                events["claim"] = json.loads(line[len("CLAIM:") :])
            elif line.startswith("RENEWED:"):
                events["renewed"] = json.loads(line[len("RENEWED:") :])
            elif line.startswith("TERMINAL:"):
                events["terminal"] = json.loads(line[len("TERMINAL:") :])
            elif line.startswith("COMMITS:"):
                events["commits"] = json.loads(line[len("COMMITS:") :])
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


@dataclass
class DrillEvidence:
    """Everything the drill observed, for tests and the bench bundle."""

    cluster: FailoverCluster
    harness: ProbeHarness
    workspace: str
    database_name: str
    primary_url: str
    standby_url: str
    facts: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.facts[key]


@contextlib.asynccontextmanager
async def promotion_drill(
    cluster: FailoverCluster,
    tmp_path: Path,
    *,
    workspace: str = "failover-core",
    include_object_outage_check: bool = True,
    include_stale_authority_check: bool = True,
    include_takeover_check: bool = True,
):
    """The core database-failover drill against a provisioned cluster.

    Choreography (each acknowledgement is a real commit on the named
    authority): fixture + claim + lease renewal + terminal commit +
    accepted publication + pending work B on the PRIMARY; recovery-point
    capture binding the cut; pre-fault standby-replay proof; hard primary
    kill; dead-primary negative probe; real promotion; fresh-process
    recovery through the promoted authority; stale-fence rejection;
    duplicate redelivery convergence; exactly-one publications; the
    PR83C1 recovery oracle over the promoted live topology; optional
    object-store outage and stale-authority checks.
    """
    if cluster.synchronous:
        await cluster.assert_policy_active()

    primary_url = await create_postgres_database(cluster.primary_admin_url)
    database_name = primary_url.rsplit("/", 1)[-1]
    await upgrade_database(url=primary_url)
    standby_url = cluster.url_for("standby", database_name)

    harness = ProbeHarness(tmp_path, workspace=workspace)
    evidence: dict[str, Any] = {"workspace": workspace, "database_name": database_name}
    primary_engine = create_async_engine(
        primary_url, **engine_kwargs_for("postgresql")
    )
    primary_factory = async_sessionmaker(
        primary_engine, class_=AsyncSession, expire_on_commit=False
    )
    standby_engine: Any = None
    backup_payload = _payload_store(
        harness.endpoint, harness.access_key, harness.secret_key, unique_bucket()
    )
    backup_source = _source_store(
        harness.endpoint, harness.access_key, harness.secret_key, unique_bucket()
    )

    try:
        # -- phase 1: real truth on the PRIMARY --------------------------
        setup_env = harness.base_env(harness.node_a, primary_url)
        setup_env["PROBE_SOURCES_DIR"] = str(tmp_path / "sources")
        (tmp_path / "sources").mkdir(exist_ok=True)
        code, events, err = await harness.run_probe("setup", setup_env)
        assert code == 0, f"setup probe failed: {err}"
        setup = events["setup"]
        work_id_a = int(setup["work_id"])
        evidence["setup"] = setup

        hold_env = harness.base_env(harness.node_a, primary_url)
        hold_env["PROBE_OWNER"] = "worker-a"
        hold_env["PROBE_LEASE_SECONDS"] = "1.5"
        hold_proc = await harness.spawn("hold", hold_env)
        claim_line = await asyncio.wait_for(hold_proc.stdout.readline(), 30)
        claim = json.loads(claim_line.decode().strip()[len("CLAIM:") :])
        evidence["claim"] = claim

        terminal_env = harness.base_env(harness.node_a, primary_url)
        terminal_env["PROBE_OWNER"] = "worker-a"
        terminal_env["PROBE_WORK_ID"] = str(work_id_a)
        terminal_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
        terminal_env["PROBE_RESULT"] = json.dumps(
            {"job_id": "drill-job-a", "status": "completed", "marker": "result-a"}
        )
        code, events, err = await harness.run_probe("terminal", terminal_env)
        assert code == 0, f"terminal probe failed: {err}"
        terminal = events["terminal"]
        evidence["terminal"] = terminal
        assert events.get("renewed") is not None, (
            "the lease renewal acknowledgement was never observed"
        )
        evidence["renewal"] = events["renewed"]
        assert terminal["completed"] is True
        work_id_b = int(terminal["work_id_b"])
        hold_proc.kill()
        await hold_proc.wait()
        shutil.rmtree(harness.node_a, ignore_errors=True)
        harness.node_a.mkdir(parents=True, exist_ok=True)

        # -- phase 2: bind the cut with a recovery point -----------------
        live_payload = _payload_store(
            harness.endpoint,
            harness.access_key,
            harness.secret_key,
            harness.payload_bucket,
        )
        live_source = _source_store(
            harness.endpoint,
            harness.access_key,
            harness.secret_key,
            harness.source_bucket,
        )
        backup_root = tmp_path / "backups"
        manifest = await capture_recovery_point(
            primary_factory,
            workspace_id=workspace,
            payload_store=live_payload,
            source_store=live_source,
            backup_payload_store=backup_payload,
            backup_source_store=backup_source,
            pg_tools=PgSidecarTools(
                host="host.docker.internal",
                port=cluster.primary_port,
                user=SUPERUSER,
                password=SUPERUSER_PASSWORD,
            ),
            database_name=database_name,
            backup_root=backup_root,
        )
        evidence["recovery_point_id"] = manifest.recovery_point_id
        evidence["captured_cut"] = manifest.kernel_cut

        # -- phase 3: durability condition BEFORE the fault --------------
        pre_fail_head = await current_head_commit(primary_factory, workspace)
        evidence["pre_fail_head_cut"] = pre_fail_head
        assert pre_fail_head == manifest.kernel_cut, (
            "head moved between capture and the fault boundary"
        )
        primary_lsn = await cluster.head_lsn("primary")
        standby_replayed = await cluster.wait_standby_replayed(primary_lsn)
        evidence["pre_fault"] = {
            "primary_lsn_after_acks": primary_lsn,
            "standby_replay_lsn_observed_before_fault": standby_replayed,
            "primary_facts": await cluster.node_facts("primary"),
            "standby_facts": await cluster.node_facts("standby"),
            "replication": await cluster.replication_facts(),
            "policy": await cluster.effective_policy(),
        }
        await primary_engine.dispose()

        # -- phase 4: kill + dead-authority negative probe ----------------
        kill_epoch = await cluster.kill_primary()
        evidence["kill_epoch"] = kill_epoch

        dead_env = harness.base_env(harness.node_b, primary_url)
        dead_env["PROBE_WORK_ID"] = str(work_id_b)
        dead_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
        dead_env["PROBE_QUERY"] = json.dumps(setup["query_expectation"])
        dead_env["PROBE_OWNER"] = "worker-b"
        dead_env["PROBE_KILL_EPOCH"] = str(kill_epoch)
        code, events, err = await harness.run_probe("recover", dead_env)
        evidence["dead_primary_probe_failed"] = code != 0 and "recovered" not in events
        assert evidence["dead_primary_probe_failed"], (
            "a recovery probe pinned to the dead primary must fail honestly, "
            f"got exit {code}"
        )

        # -- phase 5: real promotion --------------------------------------
        promotion = await cluster.promote()
        evidence["promotion"] = promotion

        # -- phase 6: fresh process recovers through the new authority ---
        recover_env = harness.base_env(harness.node_b, standby_url)
        recover_env["PROBE_WORK_ID"] = str(work_id_b)
        recover_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
        recover_env["PROBE_QUERY"] = json.dumps(setup["query_expectation"])
        recover_env["PROBE_OWNER"] = "worker-b"
        recover_env["PROBE_RESULT"] = json.dumps(
            {"job_id": "drill-job-b", "status": "completed", "marker": "result-b"}
        )
        recover_env["PROBE_KILL_EPOCH"] = str(kill_epoch)
        code, events, err = await harness.run_probe("recover", recover_env)
        assert code == 0, f"recover probe failed on the promoted authority: {err}"
        recovered = events["recovered"]
        evidence["recovered"] = recovered
        evidence["milestones"] = events["milestones"]
        # zero RPO for the declared durable class: the recovered cut IS
        # the last acknowledged pre-failure cut, not a prefix of it
        evidence["rpo_acknowledged_commits_lost"] = (
            pre_fail_head - recovered["recovered_cut"]
        )
        assert recovered["recovered_cut"] == pre_fail_head, (
            f"acknowledged truth lost: recovered cut {recovered['recovered_cut']} "
            f"!= pre-failure cut {pre_fail_head}"
        )
        assert recovered["new_commit"] > recovered["recovered_cut"]

        standby_engine = create_async_engine(
            standby_url, **engine_kwargs_for("postgresql")
        )
        standby_factory = async_sessionmaker(
            standby_engine, class_=AsyncSession, expire_on_commit=False
        )
        evidence["promoted_head_cut"] = await current_head_commit(
            standby_factory, workspace
        )
        assert evidence["promoted_head_cut"] == recovered["new_commit"]

        # -- phase 7: fencing coherence across the role change -----------
        fencing: dict[str, Any] = {}

        # duplicate redelivery of A's accepted result converges
        outcome, _appended = await accept_work(
            standby_factory,
            work_id=work_id_a,
            fencing_token=terminal["fencing_token"],
            result={"job_id": "drill-job-a", "status": "completed", "marker": "result-a"},
        )
        fencing["duplicate_a_converged"] = bool(outcome.already_accepted)

        if include_takeover_check:
            # a real supersession through the PROMOTED authority: a late
            # owner claims fresh work, is killed, the replacement takes the
            # lapsed lease over (token advances), and the dead owner's
            # replay under the old token is rejected
            work_id_d = await register_pending_work(
                standby_factory,
                live_payload,
                live_source,
                workspace_id=workspace,
                # the beta revision: NativeObjectRecord identity is the
                # (source, locator, extractor lineage) tuple, so a second
                # work item over the SAME revision would collide with B
                block=setup["source_blocks"][1],
                cache_root=harness.node_b / "source-cache",
                record_id="conversion-request.drill-job-d",
                job_id="drill-job-d",
            )
            evil_env = harness.base_env(harness.node_b, standby_url)
            evil_env["PROBE_OWNER"] = "worker-evil"
            evil_env["PROBE_LEASE_SECONDS"] = "1.5"
            evil_proc = await harness.spawn("hold", evil_env)
            evil_line = await asyncio.wait_for(evil_proc.stdout.readline(), 30)
            evil_claim = json.loads(evil_line.decode().strip()[len("CLAIM:") :])
            assert evil_claim["work_id"] == work_id_d
            evil_proc.kill()
            await evil_proc.wait()

            takeover = None
            deadline = time.monotonic() + 45.0
            while takeover is None and time.monotonic() < deadline:
                takeover = await acquire_lease(
                    standby_factory,
                    work_id=work_id_d,
                    owner_id="worker-b",
                    lease_seconds=30.0,
                )
                if takeover is None:
                    await asyncio.sleep(0.25)
            assert takeover is not None, "replacement never took the lapsed lease"
            outcome_d, _ = await accept_work(
                standby_factory,
                work_id=work_id_d,
                fencing_token=takeover.fencing_token,
                result={"job_id": "drill-job-d", "status": "completed", "marker": "result-d"},
            )
            fencing["takeover_work_id"] = work_id_d
            fencing["takeover_advanced_fence"] = (
                takeover.fencing_token > evil_claim["fencing_token"]
            )

            stale_env = harness.base_env(harness.node_b, standby_url)
            stale_env["PROBE_WORK_ID"] = str(work_id_d)
            stale_env["PROBE_TOKEN"] = str(evil_claim["fencing_token"])
            stale_env["PROBE_RESULT"] = json.dumps(
                {"job_id": "drill-job-d", "status": "completed", "marker": "evil"}
            )
            code, events, err = await harness.run_probe("stale", stale_env)
            fencing["stale_owner_rejected"] = (
                code == 0 and events.get("stale_rejected") is True
            )
            assert fencing["stale_owner_rejected"], (
                f"stale fence accepted on the promoted authority: {err}"
            )
        evidence["fencing"] = fencing

        # -- phase 8: publication/outbox coherence ------------------------
        async with standby_factory() as session:
            counts = {}
            for label, work_id in (("a", work_id_a), ("b", work_id_b)):
                rows = (
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
                counts[label] = len(rows)
            states = {
                label: (await session.get(KernelOutbox, work_id)).state
                for label, work_id in (("a", work_id_a), ("b", work_id_b))
            }
        evidence["publications_per_work"] = counts
        evidence["outbox_states"] = states
        assert counts["a"] == 1 and counts["b"] == 1, counts
        assert states["a"] == "done" and states["b"] == "done", states

        # -- phase 9: the PR83C1 oracle over the promoted live topology --
        oracle_env = harness.base_env(harness.node_b, standby_url)
        oracle_env["PROBE_MANIFEST"] = json.dumps(manifest.as_dict())
        oracle_env["PROBE_QUERY"] = json.dumps(setup["query_expectation"])
        code, events, err = await harness.run_probe("oracle", oracle_env)
        assert code == 0, f"recovery oracle not ready on promoted authority: {err}"
        evidence["promoted_oracle"] = events["oracle"]
        evidence["promoted_oracle_ready"] = True

        # -- phase 10: object-store outage during post-promotion verify ---
        if include_object_outage_check:
            import subprocess as sync_subprocess

            s3_container = container_by_port(s3_endpoint_port())
            sync_subprocess.run(["docker", "stop", s3_container], check=True)
            try:
                code, events, err = await harness.run_probe("oracle", oracle_env)
                evidence["object_outage_oracle_not_ready"] = (
                    code != 0 and "oracle" in events
                    and events["oracle"].get("ready") is False
                )
                assert evidence["object_outage_oracle_not_ready"], (
                    "oracle must not report ready while the object store is down"
                )
            finally:
                sync_subprocess.run(["docker", "start", s3_container], check=True)
            await wait_s3_ready(harness.endpoint)
            code, events, err = await harness.run_probe("oracle", oracle_env)
            assert code == 0, err
            evidence["object_outage_recovers"] = True

        # -- phase 11: stale-authority (split-brain) detection ------------
        if include_stale_authority_check:
            old_primary = await cluster.restart_old_primary()
            old_engine = create_async_engine(
                cluster.url_for("primary", database_name),
                **engine_kwargs_for("postgresql"),
            )
            try:
                old_factory = async_sessionmaker(
                    old_engine, class_=AsyncSession, expire_on_commit=False
                )
                old_head = await current_head_commit(old_factory, workspace)
            finally:
                await old_engine.dispose()
            await cluster.stop_old_primary()
            evidence["stale_authority"] = {
                "old_primary_facts": old_primary,
                "old_primary_head_cut": old_head,
                "promoted_head_cut": evidence["promoted_head_cut"],
                "staleness_detected": old_head < evidence["promoted_head_cut"],
            }
            assert evidence["stale_authority"]["staleness_detected"], (
                "restarted old primary must be detectably behind the promoted "
                "authority at the Marker UI truth boundary"
            )

        yield DrillEvidence(
            cluster=cluster,
            harness=harness,
            workspace=workspace,
            database_name=database_name,
            primary_url=primary_url,
            standby_url=standby_url,
            facts=evidence,
        )
    finally:
        if standby_engine is not None:
            await standby_engine.dispose()
        await primary_engine.dispose()
        for store in (backup_payload, backup_source):
            close = getattr(store, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        # drop the drill database through whichever authority is alive
        for admin in (cluster.standby_admin_url, cluster.primary_admin_url):
            try:
                await drop_postgres_database(admin, standby_url)
                break
            except Exception:
                continue


@contextlib.asynccontextmanager
async def async_loss_drill(
    cluster: FailoverCluster,
    tmp_path: Path,
    *,
    workspace: str = "failover-async",
    tail_commits: int = 3,
):
    """The declared-lossy asynchronous comparison lane.

    Terminal truth is placed while replication streams (and the standby
    is observed to possess it), then the standby is stopped, N more
    commits are acknowledged by the primary, the primary is hard-killed,
    the standby is restarted and promoted. The measured loss — the
    acknowledged-but-unpossessed tail — is the honest RPO of the async
    acknowledgement policy; nothing here claims it durable.
    """
    assert not cluster.synchronous, "the loss lane must run on an async cluster"

    primary_url = await create_postgres_database(cluster.primary_admin_url)
    database_name = primary_url.rsplit("/", 1)[-1]
    await upgrade_database(url=primary_url)

    harness = ProbeHarness(tmp_path, workspace=workspace)
    evidence: dict[str, Any] = {"workspace": workspace, "database_name": database_name}

    # baseline async commit latencies (comparison lane for the sync tax)
    baseline_env = harness.base_env(harness.node_a, primary_url)
    baseline_env["PROBE_COMMITS"] = "6"
    baseline_env["PROBE_COMMIT_TIMEOUT"] = "20"
    baseline_env["PROBE_COMMIT_LABEL"] = "async-lane"
    code, events, err = await harness.run_probe("commit_probe", baseline_env)
    assert code == 0, err
    assert events["commits"]["blocked"] is False, events["commits"]
    evidence["async_commit_latencies_ms"] = events["commits"]["latencies_ms"]

    setup_env = harness.base_env(harness.node_a, primary_url)
    setup_env["PROBE_SOURCES_DIR"] = str(tmp_path / "sources")
    (tmp_path / "sources").mkdir(exist_ok=True)
    code, events, err = await harness.run_probe("setup", setup_env)
    assert code == 0, f"setup probe failed: {err}"
    setup = events["setup"]
    work_id_a = int(setup["work_id"])

    hold_env = harness.base_env(harness.node_a, primary_url)
    hold_env["PROBE_OWNER"] = "worker-a"
    hold_env["PROBE_LEASE_SECONDS"] = "1.5"
    hold_proc = await harness.spawn("hold", hold_env)
    claim_line = await asyncio.wait_for(hold_proc.stdout.readline(), 30)
    claim = json.loads(claim_line.decode().strip()[len("CLAIM:") :])

    terminal_env = harness.base_env(harness.node_a, primary_url)
    terminal_env["PROBE_OWNER"] = "worker-a"
    terminal_env["PROBE_WORK_ID"] = str(work_id_a)
    terminal_env["PROBE_BLOCK"] = json.dumps(setup["source_blocks"][0])
    terminal_env["PROBE_RESULT"] = json.dumps(
        {"job_id": "drill-job-a", "status": "completed", "marker": "result-a"}
    )
    code, events, err = await harness.run_probe("terminal", terminal_env)
    assert code == 0, f"terminal probe failed: {err}"
    hold_proc.kill()
    await hold_proc.wait()

    engine = create_async_engine(primary_url, **engine_kwargs_for("postgresql"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    promoted_engine: Any = None
    payload_store = _payload_store(
        harness.endpoint, harness.access_key, harness.secret_key, harness.payload_bucket
    )
    try:
        from app.kernel.commit import KernelCommitBatch, KernelCommitService
        from app.kernel.records import ObservationRecord

        commit_service = KernelCommitService(factory, payload_store=payload_store)
        acked_head = await current_head_commit(factory, workspace)
        evidence["terminal_ack_cut"] = acked_head
        terminal_lsn = await cluster.head_lsn("primary")
        # the lane's guarantee floor: terminal truth the standby already
        # possessed before the replication cut
        await cluster.wait_standby_replayed(terminal_lsn)

        # -- replication cut: standby away, tail acknowledged on primary -
        stopped_epoch = await cluster.stop_standby()
        tail_ids = []
        for index in range(tail_commits):
            receipt = await commit_service.commit(
                KernelCommitBatch(
                    workspace_id=workspace,
                    records=(
                        ObservationRecord(
                            observer="drill.async-tail",
                            derivation={"step": "async-tail", "index": index},
                            payload_bytes=b"ASYNC-TAIL-"
                            + bytes([48 + index]) * 24,
                        ),
                    ),
                )
            )
            tail_ids.append(receipt.kernel_commit_id)
        pre_kill_head = await current_head_commit(factory, workspace)
        evidence["tail_ack_cut_range"] = [acked_head + 1, pre_kill_head]
        evidence["tail_acknowledged_commits"] = len(tail_ids)
        await engine.dispose()

        # -- primary loss, standby restart, promotion ---------------------
        kill_epoch = await cluster.kill_primary()
        await cluster.start_standby(expect_streaming=False)
        promotion = await cluster.promote()
        evidence["kill_epoch"] = kill_epoch
        evidence["promotion"] = promotion

        promoted_engine = create_async_engine(
            cluster.url_for("standby", database_name),
            **engine_kwargs_for("postgresql"),
        )
        promoted_factory = async_sessionmaker(
            promoted_engine, class_=AsyncSession, expire_on_commit=False
        )
        promoted_head = await current_head_commit(promoted_factory, workspace)
        evidence["promoted_head_cut"] = promoted_head
        evidence["rpo_acknowledged_commits_lost"] = pre_kill_head - promoted_head
        evidence["terminal_truth_survived"] = promoted_head >= acked_head
        assert evidence["terminal_truth_survived"], (
            "terminal truth the standby possessed before the cut was lost"
        )

        # prefix property: WAL replay yields a strict prefix of the
        # acknowledged commit sequence — no holes, no half-commits
        from sqlalchemy import text as sa_text

        async with promoted_factory() as session:
            beyond = await session.scalar(
                sa_text(
                    "SELECT count(*) FROM kernel_records "
                    "WHERE kernel_commit_id > :cut"
                ),
                {"cut": promoted_head},
            )
        evidence["prefix_property_holds"] = beyond == 0
        assert evidence["prefix_property_holds"], (
            f"{beyond} records beyond the promoted head: replay is not a prefix"
        )

        # the promoted authority still serves new writes
        receipt = await KernelCommitService(
            promoted_factory, payload_store=payload_store
        ).commit(
            KernelCommitBatch(
                workspace_id=workspace,
                records=(
                    ObservationRecord(
                        observer="drill.async-post",
                        derivation={"step": "async-post-promotion"},
                        payload_bytes=b"ASYNC-POST-" + b"p" * 24,
                    ),
                ),
            )
        )
        evidence["post_promotion_commit"] = receipt.kernel_commit_id
        assert receipt.kernel_commit_id > promoted_head

        yield DrillEvidence(
            cluster=cluster,
            harness=harness,
            workspace=workspace,
            database_name=database_name,
            primary_url=primary_url,
            standby_url=cluster.url_for("standby", database_name),
            facts=evidence,
        )
    finally:
        if promoted_engine is not None:
            await promoted_engine.dispose()
        await engine.dispose()
        for admin in (cluster.standby_admin_url, cluster.primary_admin_url):
            try:
                await drop_postgres_database(admin, cluster.url_for("standby", database_name))
                break
            except Exception:
                continue
