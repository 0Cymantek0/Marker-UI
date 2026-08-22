#!/usr/bin/env python3
"""PR83C1 industrial recovery measurement harness.

Runs the recovery drills end to end against REAL services (PostgreSQL +
S3-compatible store, strict: refuses to run without them) and records
the measured evidence bundle:

* replacement-process failover RTO with component clocks (boot ->
  semantic -> source -> query -> work -> write) taken from the probe's
  milestone epochs, anchored at the kill;
* stale-owner rejection + exactly-one-publication proof;
* recovery-point capture operational tax (quiesce window, dump size,
  object bytes/count, per-phase durations);
* disaster-restore RPO (recovered cut vs last committed pre-failure
  cut, explicit lost tail) and restore/oracle durations;
* damaged-backup refusal spot-check.

``--write`` emits docs/reference/measurements/pr83c1-industrial-recovery.json;
the run exits non-zero if any structural acceptance boolean is false.
These are single-host CI-grade measurements under a controlled fault
drill — NOT a production SLO claim (see the non-claims in the report).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped)\b")


def _parse_test_log(path: Path) -> dict:
    """Extract pass/fail/skip counts from a pytest summary.

    Matches every ``N passed|failed|skipped`` occurrence (pytest orders
    these differently depending on outcome — ``2 failed, 393 passed`` —
    so the LAST occurrence per kind wins, which is the summary line).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    counts: dict[str, int] = {}
    for match in _COUNT_RE.finditer(text):
        counts[match.group(2)] = int(match.group(1))
    if not counts:
        return {"unparsed": True}
    return {
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
    }

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.kernel.recovery import (  # noqa: E402
    RecoveryManifestError,
    capture_recovery_point,
    load_recovery_point,
    verify_backup_objects,
    verify_recovery,
)
from tests.recovery_drills import (  # noqa: E402
    recovery_workspace,
    restore_to_fresh_services,
)

MEASUREMENTS_PATH = (
    BACKEND_DIR.parent / "docs" / "reference" / "measurements"
    / "pr83c1-industrial-recovery.json"
)

STRUCTURAL_KEYS = (
    "failover_rto_ready",
    "stale_owner_rejected",
    "exactly_one_publication",
    "post_failover_new_commit",
    "capture_verified",
    "capture_idempotent",
    "restore_oracle_ready",
    "restore_tail_exact",
    "damaged_backup_refused",
)


def _fail_fast(message: str) -> None:
    raise SystemExit(f"ERROR: {message} (bench requires real services)")


async def _pg_banner(admin_url: str) -> str:
    from tests.recovery_drills import parse_admin_url

    host, port, user, password = parse_admin_url(admin_url)
    from app.kernel.recovery import PgSidecarTools

    tools = PgSidecarTools(host=host, port=port, user=user, password=password)
    return await tools.server_banner("postgres")


def _s3_banner(endpoint: str) -> str:
    health = endpoint.rstrip("/") + "/minio/health/live"
    with urllib.request.urlopen(health, timeout=5.0) as response:
        return response.headers.get("Server", "")


async def measure_failover(base: dict) -> dict:
    """The A-dies/B-recovers drill with measured RTO components."""
    from tests.test_kernel_recovery_failover import DrillHarness, full_drill
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pr83c1-failover-"))
    evidence: dict = {}
    async with full_drill(tmp) as drill:
        milestones: dict[str, float] = drill["milestones"]
        kill = drill["kill_epoch"]
        recovered = drill["recovered"]
        evidence["rto_components_seconds"] = {
            name: round(epoch - kill, 6)
            for name, epoch in sorted(milestones.items())
        }
        evidence["failover_rto_ready"] = (
            milestones["write_ready"] - kill
        )
        evidence["recovered_cut"] = recovered["recovered_cut"]
        evidence["post_failover_new_commit"] = (
            recovered["new_commit"] > recovered["recovered_cut"]
        )
        evidence["recovery_fence_token"] = recovered["fencing_token"]
        evidence["takeover_advanced_fence"] = (
            drill["claim"] is not None
            and recovered["fencing_token"] > drill["claim"]["fencing_token"]
        )

        # stale owner replay
        claim = drill["claim"]
        harness: DrillHarness = drill["harness"]
        stale_env = harness.base_env(harness.node_b)
        stale_env["PROBE_WORK_ID"] = str(drill["work_id"])
        stale_env["PROBE_TOKEN"] = str(claim["fencing_token"])
        stale_env["PROBE_RESULT"] = json.dumps(
            {"job_id": "drill-job-a", "status": "completed", "marker": "stale"}
        )
        code, events, _err = await harness.run_probe("stale", stale_env)
        evidence["stale_owner_rejected"] = (
            code == 0 and events.get("stale_rejected") is True
        )
        from app.kernel.models import KernelPublication

        async with drill["factory"]() as session:
            publications = (
                (
                    await session.execute(
                        select(KernelPublication).where(
                            KernelPublication.work_id == drill["work_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
        evidence["exactly_one_publication"] = len(publications) == 1
        evidence["failover_rto_ready"] = bool(
            evidence["post_failover_new_commit"]
            and evidence["takeover_advanced_fence"]
            and evidence["stale_owner_rejected"]
            and evidence["exactly_one_publication"]
        )
    return evidence


async def measure_capture_and_restore(base: dict) -> dict:
    """Capture tax, disaster-restore RPO/RTO, oracle, tail semantics."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pr83c1-restore-"))
    evidence: dict = {}
    async with recovery_workspace(tmp, workspace_id="recovery-measure") as ws:
        backup_root = tmp / "backups"
        payload_backup, source_backup = ws.ensure_backup_stores()

        capture_started = time.monotonic()
        rp1 = await capture_recovery_point(
            ws.session_factory,
            workspace_id=ws.workspace_id,
            payload_store=ws.payload_store,
            source_store=ws.source_store,
            backup_payload_store=payload_backup,
            backup_source_store=source_backup,
            pg_tools=ws.pg_tools,
            database_name=ws.database_name,
            backup_root=backup_root,
        )
        evidence["capture_wall_seconds"] = time.monotonic() - capture_started
        evidence["capture_durations"] = rp1.durations.as_dict()
        evidence["recovery_point_id"] = rp1.recovery_point_id

        loaded = load_recovery_point(backup_root, rp1.recovery_point_id)
        report = await verify_backup_objects(
            loaded,
            backup_payload_store=payload_backup,
            backup_source_store=source_backup,
        )
        evidence["capture_verified"] = report.ready
        evidence["capture_verification"] = report.as_dict()

        # idempotent retry
        again = await capture_recovery_point(
            ws.session_factory,
            workspace_id=ws.workspace_id,
            payload_store=ws.payload_store,
            source_store=ws.source_store,
            backup_payload_store=payload_backup,
            backup_source_store=source_backup,
            pg_tools=ws.pg_tools,
            database_name=ws.database_name,
            backup_root=backup_root,
        )
        evidence["capture_idempotent"] = (
            again.recovery_point_id == rp1.recovery_point_id
            and again.captured_at == rp1.captured_at
        )

        # post-point writes, then a second capture: the disaster tail
        from app.kernel.commit import KernelCommitBatch
        from app.kernel.records import ObservationRecord

        tail_write_started = time.monotonic()
        await ws.commit(
            KernelCommitBatch(
                workspace_id=ws.workspace_id,
                records=(
                    ObservationRecord(
                        observer="drill.tail",
                        derivation={"step": "tail"},
                        payload_bytes=b"MEASURE-TAIL-" + b"m" * 40,
                    ),
                ),
            )
        )
        tail_source = tmp / "tail.pdf"
        tail_source.write_bytes(b"%PDF-1.4 MEASURE TAIL SOURCE")
        await ws.acquire_source(tail_source, job_id="measure-tail")
        await ws.head()  # tail commit landed (asserted via rp2 below)
        evidence["tail_write_seconds"] = time.monotonic() - tail_write_started

        rp2 = await capture_recovery_point(
            ws.session_factory,
            workspace_id=ws.workspace_id,
            payload_store=ws.payload_store,
            source_store=ws.source_store,
            backup_payload_store=payload_backup,
            backup_source_store=source_backup,
            pg_tools=ws.pg_tools,
            database_name=ws.database_name,
            backup_root=backup_root,
        )
        evidence["rpo_commit_delta_lost"] = rp2.kernel_cut - rp1.kernel_cut
        evidence["rpo_source_objects_lost"] = (
            rp2.durations.source_object_count - rp1.durations.source_object_count
        )
        evidence["last_committed_pre_disaster_cut"] = rp2.kernel_cut

        # destroy + restore the EARLIER point
        restore_started = time.monotonic()
        loaded1 = load_recovery_point(backup_root, rp1.recovery_point_id)
        target = await restore_to_fresh_services(ws, loaded1, tmp)
        evidence["restore_wall_seconds"] = time.monotonic() - restore_started
        try:
            oracle_started = time.monotonic()
            oracle = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=rp1,
                payload_store=target.payload_store,
                source_store=target.source_store,
                expected_query=ws.query_expectation,
            )
            evidence["oracle_seconds"] = time.monotonic() - oracle_started
            evidence["restore_oracle_ready"] = oracle.ready
            evidence["restore_oracle"] = oracle.as_dict()

            from sqlalchemy import text

            async with target.session_factory() as session:
                head = await session.scalar(
                    text("SELECT head_kernel_commit_id FROM kernel_commit_heads")
                )
                beyond = await session.scalar(
                    text(
                        "SELECT count(*) FROM kernel_records "
                        "WHERE kernel_commit_id > :cut"
                    ),
                    {"cut": rp1.kernel_cut},
                )
            evidence["restored_head_cut"] = head
            evidence["restore_tail_exact"] = (
                head == rp1.kernel_cut and beyond == 0
            )
        finally:
            await target.close()

        # damaged backup refusal: corrupt the dump copy of rp1
        final_dir = backup_root / rp1.recovery_point_id.split(":", 1)[1]
        dump = final_dir / "database.pgdump"
        keep = dump.read_bytes()
        dump.write_bytes(keep[:-32])
        refused = False
        try:
            load_recovery_point(backup_root, rp1.recovery_point_id)
        except RecoveryManifestError:
            refused = True
        finally:
            dump.write_bytes(keep)
        evidence["damaged_backup_refused"] = refused
        # and the undamaged point loads again
        load_recovery_point(backup_root, rp1.recovery_point_id)
    return evidence


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_pr83c1_recovery.py")
    parser.add_argument("--write", action="store_true", help="write the JSON bundle")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    parser.add_argument(
        "--strict-matrix-log", type=Path, default=None,
        help="embed pass counts from a run_industrial_conformance.py log",
    )
    parser.add_argument(
        "--regression-log", type=Path, default=None,
        help="embed pass counts from a full-regression pytest log",
    )
    args = parser.parse_args(argv)

    admin_url = os.environ.get("MARKER_TEST_POSTGRES_ADMIN_URL")
    endpoint = os.environ.get("MARKER_TEST_S3_ENDPOINT")
    if not admin_url or not endpoint:
        _fail_fast(
            "MARKER_TEST_POSTGRES_ADMIN_URL and MARKER_TEST_S3_ENDPOINT are "
            "required (run scripts/run_industrial_conformance.py to provision)"
        )

    base = {
        "schema": "marker.pr83c1_recovery_measurement.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "postgres_banner": await _pg_banner(admin_url),
        "object_store_banner": _s3_banner(endpoint),
        "object_store_endpoint": endpoint,
        "dump_sidecar_image": "postgres:16-alpine",
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(BACKEND_DIR.parent),
            capture_output=True, text=True,
        ).stdout.strip(),
        "non_claims": [
            "single-host CI-grade drill measurements, not a production SLO",
            "logical per-point pg_dump backup, no PITR/WAL archiving",
            "no standby promotion or multi-region topology tested",
            "application-level failover only; database HA is out of scope",
        ],
    }

    print("[pr83c1] measuring replacement-process failover...")
    failover = await measure_failover(base)
    print(f"[pr83c1] failover RTO={failover['failover_rto_ready']:.3f}s "
          f"components={failover['rto_components_seconds']}")
    print("[pr83c1] measuring capture + disaster restore...")
    restore = await measure_capture_and_restore(base)
    print(
        f"[pr83c1] capture={restore['capture_wall_seconds']:.3f}s "
        f"quiesce={restore['capture_durations']['quiesce_seconds']:.3f}s "
        f"restore={restore['restore_wall_seconds']:.3f}s "
        f"oracle={restore['oracle_seconds']:.3f}s "
        f"RPO={restore['rpo_commit_delta_lost']} commits"
    )

    bundle = {**base, "failover": failover, "capture_restore": restore}
    if args.strict_matrix_log is not None:
        bundle["strict_matrix"] = _parse_test_log(args.strict_matrix_log)
    if args.regression_log is not None:
        bundle["regression"] = _parse_test_log(args.regression_log)
    structural = {key: bool(bundle_flat(bundle).get(key)) for key in STRUCTURAL_KEYS}
    bundle["structural_acceptance"] = structural

    print(json.dumps({"structural_acceptance": structural}, indent=2))
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"[pr83c1] wrote {args.output}")
    if not all(structural.values()):
        print("ERROR: structural acceptance failed", file=sys.stderr)
        return 1
    print("[pr83c1] PASS: all structural acceptance booleans true")
    return 0


def bundle_flat(bundle: dict) -> dict:
    flat: dict = {}
    for section in ("failover", "capture_restore"):
        flat.update(bundle.get(section, {}))
    return flat


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
