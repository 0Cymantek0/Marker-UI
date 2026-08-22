#!/usr/bin/env python3
"""PR83C2 failover measurement harness.

Runs the promotion drills end to end against REAL services (two-node
Docker PostgreSQL primary/standby + S3-compatible store; strict:
refuses to run without Docker and the object store) and records the
measured evidence bundle:

* topology + replication provenance observed before the fault (roles,
  sync state, banners, WAL positions, the live durability policy);
* synchronous-lane failover: RTO from the hard kill to the first
  verified post-promotion Marker UI write, with promotion and recovery
  component clocks; RPO for the acknowledged durable class (must be
  zero — established from pre-fault observations only);
* fencing/publication/oracle verdicts across the role change;
* the durability tax: commit latency with the synchronous standby
  attached vs the asynchronous lane, and the blocking observation while
  the required standby is unavailable;
* the declared-lossy asynchronous lane's measured tail loss;
* a second full drill from fresh topology state for run-to-run spread.

``--write`` emits docs/reference/measurements/pr83c2-pg-failover.json;
the run exits non-zero if any structural acceptance boolean is false.
These are single-host CI-grade measurements under a controlled fault
drill — NOT a production SLO claim (see the non-claims in the bundle).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped)\b")


def _parse_test_log(path: Path) -> dict:
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

from app.db_migration import upgrade_database  # noqa: E402
from tests.failover_drills import (  # noqa: E402
    DURABILITY_POLICY,
    ProbeHarness,
    async_loss_drill,
    promotion_drill,
)
from tests.pg_failover_topology import FailoverCluster  # noqa: E402
from tests.pg_provisioning import (  # noqa: E402
    create_postgres_database,
    drop_postgres_database,
)

MEASUREMENTS_PATH = (
    BACKEND_DIR.parent / "docs" / "reference" / "measurements" / "pr83c2-pg-failover.json"
)

STRUCTURAL_KEYS = (
    "promoted_writable",
    "promotion_not_restart",
    "timeline_advanced",
    "pre_fault_replay_proven",
    "zero_acknowledged_loss",
    "fresh_process_recovered",
    "failover_rto_ready_computed",
    "stale_owner_rejected",
    "duplicate_converged",
    "exactly_one_publication_per_work",
    "promoted_oracle_ready",
    "dead_primary_probe_failed",
    "object_outage_not_ready_then_recovers",
    "stale_authority_detected",
    "sync_standby_unavailable_blocked",
    "async_lane_loss_recorded",
    "async_prefix_property",
    "durability_cost_measured",
    "repeated_drill_zero_loss",
)


def _fail_fast(message: str) -> None:
    raise SystemExit(f"ERROR: {message} (bench requires real services)")


def _s3_banner(endpoint: str) -> str:
    health = endpoint.rstrip("/") + "/minio/health/live"
    with urllib.request.urlopen(health, timeout=5.0) as response:
        return response.headers.get("Server", "")


def _stats_ms(latencies_ms: list[float]) -> dict[str, float]:
    if not latencies_ms:
        return {}
    ordered = sorted(latencies_ms)

    def pct(fraction: float) -> float:
        index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
        return ordered[index]

    return {
        "n": len(ordered),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "max_ms": ordered[-1],
    }


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(BACKEND_DIR.parent),
        capture_output=True,
        text=True,
    ).stdout.strip()


async def _commit_probe(
    cluster: FailoverCluster, harness: ProbeHarness, db_url: str, *,
    count: int, timeout_s: float, label: str, node_dir: Path | None = None,
) -> dict:
    env = harness.base_env(node_dir or harness.node_a, db_url)
    env["PROBE_COMMITS"] = str(count)
    env["PROBE_COMMIT_TIMEOUT"] = str(timeout_s)
    env["PROBE_COMMIT_LABEL"] = label
    code, events, err = await harness.run_probe("commit_probe", env)
    assert code == 0, err
    return events["commits"]


async def measure_sync_standby_blocking(cluster: FailoverCluster, tmp: Path) -> dict:
    """Durability tax + honest blocking while the required standby is away."""
    evidence: dict = {}
    db_url = await create_postgres_database(cluster.primary_admin_url)
    try:
        await upgrade_database(url=db_url)
        harness = ProbeHarness(tmp, workspace="failover-cost")

        attached = await _commit_probe(
            cluster, harness, db_url,
            count=10, timeout_s=30.0, label="sync-attached",
        )
        assert attached["blocked"] is False, attached
        evidence["sync_commit_latencies_ms"] = attached["latencies_ms"]
        evidence["sync_commit_stats"] = _stats_ms(attached["latencies_ms"])

        stopped_epoch = await cluster.stop_standby()
        blocked = await _commit_probe(
            cluster, harness, db_url,
            count=1, timeout_s=8.0, label="sync-blocked",
        )
        evidence["standby_unavailable"] = {
            "blocked": blocked["blocked"],
            "acknowledged_before_block": blocked["completed"],
            "client_timeout_seconds": 8.0,
        }
        assert blocked["blocked"] is True and blocked["completed"] == 0, blocked
        started_epoch = await cluster.start_standby()
        resumed = await _commit_probe(
            cluster, harness, db_url,
            count=4, timeout_s=60.0, label="sync-resumed",
        )
        assert resumed["blocked"] is False, resumed
        evidence["standby_unavailable"]["blocking_window_seconds"] = (
            started_epoch - stopped_epoch
        )
        evidence["standby_unavailable"]["acknowledgements_resumed"] = (
            resumed["completed"] == 4
        )
    finally:
        await drop_postgres_database(cluster.primary_admin_url, db_url)
    return evidence


async def measure_core_drill(cluster: FailoverCluster, tmp: Path) -> dict:
    """The full synchronous-lane promotion drill with component clocks."""
    evidence: dict = {}
    async with promotion_drill(cluster, tmp) as drill:
        ev = drill.facts
        milestones = ev["milestones"]
        kill = ev["kill_epoch"]
        promotion = ev["promotion"]

        evidence["topology"] = {
            "primary_container": cluster.primary_container,
            "standby_container": cluster.standby_container,
            "primary_port": cluster.primary_port,
            "standby_port": cluster.standby_port,
            "postgres_image": cluster.image,
            "pre_fault_primary_banner": ev["pre_fault"]["primary_facts"]["banner"],
            "pre_fault_standby_banner": ev["pre_fault"]["standby_facts"]["banner"],
            "policy": ev["pre_fault"]["policy"],
            "replication": ev["pre_fault"]["replication"],
        }
        evidence["pre_fault_replay"] = {
            "primary_lsn_after_acks": ev["pre_fault"]["primary_lsn_after_acks"],
            "standby_replay_lsn_before_fault": (
                ev["pre_fault"]["standby_replay_lsn_observed_before_fault"]
            ),
        }
        evidence["promotion"] = {
            key: promotion[key]
            for key in (
                "promote_wall_seconds",
                "timeline_before",
                "timeline_after",
                "promoted_writable",
                "promotion_not_restart",
                "postmaster_start_time",
                "promoted_banner",
            )
        }
        evidence["rto_components_seconds"] = {
            name: round(epoch - kill, 6) for name, epoch in sorted(milestones.items())
        }
        evidence["rto_components_seconds"]["promote_started"] = round(
            promotion["promote_started_epoch"] - kill, 6
        )
        evidence["rto_components_seconds"]["promote_completed"] = round(
            promotion["promote_completed_epoch"] - kill, 6
        )
        evidence["failover_rto_ready"] = milestones["write_ready"] - kill
        evidence["rpo_acknowledged_commits_lost"] = ev["rpo_acknowledged_commits_lost"]
        evidence["cuts"] = {
            "captured_cut": ev["captured_cut"],
            "pre_fail_head_cut": ev["pre_fail_head_cut"],
            "recovered_cut": ev["recovered"]["recovered_cut"],
            "promoted_head_cut": ev["promoted_head_cut"],
            "post_promotion_commit": ev["recovered"]["new_commit"],
        }
        evidence["fencing"] = ev["fencing"]
        evidence["publications_per_work"] = ev["publications_per_work"]
        evidence["outbox_states"] = ev["outbox_states"]
        evidence["promoted_oracle_ready"] = ev["promoted_oracle_ready"]
        evidence["promoted_oracle"] = ev["promoted_oracle"]
        evidence["dead_primary_probe_failed"] = ev["dead_primary_probe_failed"]
        evidence["object_outage_not_ready"] = ev["object_outage_oracle_not_ready"]
        evidence["object_outage_recovers"] = ev["object_outage_recovers"]
        evidence["stale_authority"] = ev["stale_authority"]
        evidence["workspace"] = ev["workspace"]
        evidence["recovery_point_id"] = ev["recovery_point_id"]
    return evidence


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_pr83c2_failover.py")
    parser.add_argument("--write", action="store_true", help="write the JSON bundle")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    parser.add_argument(
        "--strict-matrix-log", type=Path, default=None,
        help="embed pass counts from a run_industrial_conformance.py log",
    )
    parser.add_argument(
        "--failover-log", type=Path, default=None,
        help="embed pass counts from a run_failover_conformance.py log",
    )
    parser.add_argument(
        "--regression-log", type=Path, default=None,
        help="embed pass counts from a full-regression pytest log",
    )
    args = parser.parse_args(argv)

    endpoint = os.environ.get("MARKER_TEST_S3_ENDPOINT")
    if not endpoint:
        _fail_fast(
            "MARKER_TEST_S3_ENDPOINT is required (run scripts/run_failover_conformance.py "
            "or scripts/run_industrial_conformance.py to provision)"
        )
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
    except Exception:
        _fail_fast("a reachable Docker daemon is required for the two-node topology")

    base: dict = {
        "schema": "marker.pr83c2_failover_measurement.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_head(),
        "object_store_banner": _s3_banner(endpoint),
        "object_store_endpoint": endpoint,
        "durability_policy": DURABILITY_POLICY,
        "non_claims": [
            "single-host CI-grade drill measurements, not a production SLO",
            "no multi-region or multi-standby quorum topology",
            "no Patroni/etcd or automated cluster-manager orchestration; "
            "promotion is deliberate test/control-plane action",
            "no PITR/WAL archiving claim (streaming replication only)",
            "no old-primary automated rejoin; the split-authority case is "
            "detected and fenced out, not healed",
            "no object-store HA claim (single object store across the drill)",
            "source cursor advancement and irreversible-effect lanes are not "
            "exercised by this drill (see durability_policy.not_exercised)",
            "post-promotion writes run under the promoted node's local "
            "synchronous_commit default, not a re-established sync pair",
            "failover RTO includes deliberate drill choreography between "
            "the kill and the recovery spawn (dead-primary negative probe, "
            "promotion CHECKPOINT): an honest upper bound, not a pure "
            "promotion latency",
        ],
    }

    # -- lane 1: the synchronous durable profile --------------------------
    print("[pr83c2] provisioning sync primary/standby pair...")
    sync_cluster = FailoverCluster()
    await sync_cluster.provision()
    try:
        print("[pr83c2] measuring sync commit cost + standby-unavailable blocking...")
        cost = await measure_sync_standby_blocking(
            sync_cluster, Path(tempfile.mkdtemp(prefix="pr83c2-cost-"))
        )
        print(
            f"[pr83c2] sync p50={cost['sync_commit_stats']['p50_ms']:.1f}ms "
            f"p95={cost['sync_commit_stats']['p95_ms']:.1f}ms; "
            f"blocked window={cost['standby_unavailable']['blocking_window_seconds']:.1f}s"
        )
        print("[pr83c2] running the core promotion drill...")
        core = await measure_core_drill(
            sync_cluster, Path(tempfile.mkdtemp(prefix="pr83c2-core-"))
        )
        print(
            f"[pr83c2] failover RTO={core['failover_rto_ready']:.3f}s "
            f"(kill->write_ready); RPO(acked)={core['rpo_acknowledged_commits_lost']}"
        )
    finally:
        sync_cluster.teardown()

    # -- lane 2: the declared-lossy asynchronous comparison ---------------
    print("[pr83c2] provisioning async comparison pair...")
    async_cluster = FailoverCluster(
        synchronous=False,
        tag="bench-async",
        primary_port=55471,
        standby_port=55472,
    )
    await async_cluster.provision()
    try:
        print("[pr83c2] running the async loss lane...")
        async with async_loss_drill(
            async_cluster, Path(tempfile.mkdtemp(prefix="pr83c2-async-"))
        ) as drill:
            lane = dict(drill.facts)
    finally:
        async_cluster.teardown()
    async_stats = _stats_ms(lane["async_commit_latencies_ms"])
    print(
        f"[pr83c2] async p50={async_stats['p50_ms']:.1f}ms "
        f"p95={async_stats['p95_ms']:.1f}ms; measured tail loss="
        f"{lane['rpo_acknowledged_commits_lost']} commits"
    )

    # -- repetition: a second full drill from fresh topology ---------------
    print("[pr83c2] provisioning fresh repeat pair...")
    repeat_cluster = FailoverCluster(
        tag="bench-repeat", primary_port=55481, standby_port=55482
    )
    await repeat_cluster.provision()
    try:
        print("[pr83c2] running the repeated promotion drill...")
        repeat = await measure_core_drill(
            repeat_cluster, Path(tempfile.mkdtemp(prefix="pr83c2-repeat-"))
        )
    finally:
        repeat_cluster.teardown()
    spread = abs(repeat["failover_rto_ready"] - core["failover_rto_ready"])
    print(
        f"[pr83c2] repeat RTO={repeat['failover_rto_ready']:.3f}s "
        f"(spread {spread:.3f}s); RPO(acked)={repeat['rpo_acknowledged_commits_lost']}"
    )

    bundle = {
        **base,
        "sync_lane": {**core, "commit_cost": cost},
        "async_lane": {
            "commit_stats": async_stats,
            "terminal_ack_cut": lane["terminal_ack_cut"],
            "tail_acknowledged_commits": lane["tail_acknowledged_commits"],
            "tail_ack_cut_range": lane["tail_ack_cut_range"],
            "promoted_head_cut": lane["promoted_head_cut"],
            "rpo_acknowledged_commits_lost": lane["rpo_acknowledged_commits_lost"],
            "terminal_truth_survived": lane["terminal_truth_survived"],
            "prefix_property_holds": lane["prefix_property_holds"],
            "post_promotion_commit": lane["post_promotion_commit"],
            "promotion": lane["promotion"],
        },
        "repetition": {
            "failover_rto_ready": repeat["failover_rto_ready"],
            "rto_spread_seconds": spread,
            "rpo_acknowledged_commits_lost": repeat["rpo_acknowledged_commits_lost"],
            "promotion": repeat["promotion"],
            "cuts": repeat["cuts"],
        },
    }
    if args.strict_matrix_log is not None:
        bundle["strict_matrix"] = _parse_test_log(args.strict_matrix_log)
    if args.failover_log is not None:
        bundle["failover_matrix"] = _parse_test_log(args.failover_log)
    if args.regression_log is not None:
        bundle["regression"] = _parse_test_log(args.regression_log)
    # matrix gates: the STRICT matrices must be perfectly green; the
    # plain regression legitimately carries env-gated skips (services
    # absent outside the strict env), so only failures are fatal there
    for section in ("strict_matrix", "failover_matrix"):
        counts = bundle.get(section)
        if counts and (counts.get("failed") or counts.get("skipped")):
            print(
                f"ERROR: embedded {section} log reports "
                f"{counts.get('failed', 0)} failed / "
                f"{counts.get('skipped', 0)} skipped",
                file=sys.stderr,
            )
            return 1
    regression = bundle.get("regression")
    if regression and regression.get("failed"):
        print(
            f"ERROR: embedded regression log reports "
            f"{regression['failed']} failed",
            file=sys.stderr,
        )
        return 1

    def _lsn_int(lsn: str | int) -> int:
        if isinstance(lsn, int):
            return lsn
        high, _, low = lsn.partition("/")
        return (int(high, 16) << 32) | int(low, 16)

    sync = bundle["sync_lane"]
    fencing = sync["fencing"]
    replay = sync["pre_fault_replay"]
    structural = {
        "promoted_writable": bool(sync["promotion"]["promoted_writable"]),
        "promotion_not_restart": bool(sync["promotion"]["promotion_not_restart"]),
        "timeline_advanced": (
            sync["promotion"]["timeline_after"]
            == sync["promotion"]["timeline_before"] + 1
        ),
        "pre_fault_replay_proven": _lsn_int(
            replay["standby_replay_lsn_before_fault"]
        ) >= _lsn_int(replay["primary_lsn_after_acks"]),
        "zero_acknowledged_loss": sync["rpo_acknowledged_commits_lost"] == 0,
        "fresh_process_recovered": (
            sync["cuts"]["recovered_cut"] == sync["cuts"]["pre_fail_head_cut"]
            and sync["cuts"]["post_promotion_commit"] > sync["cuts"]["recovered_cut"]
        ),
        "failover_rto_ready_computed": sync["failover_rto_ready"] > 0,
        "stale_owner_rejected": bool(fencing.get("stale_owner_rejected")),
        "duplicate_converged": bool(fencing.get("duplicate_a_converged")),
        "exactly_one_publication_per_work": (
            sync["publications_per_work"] == {"a": 1, "b": 1}
        ),
        "promoted_oracle_ready": bool(sync.get("promoted_oracle_ready")),
        "dead_primary_probe_failed": bool(sync.get("dead_primary_probe_failed")),
        "object_outage_not_ready_then_recovers": bool(
            sync.get("object_outage_not_ready") and sync.get("object_outage_recovers")
        ),
        "stale_authority_detected": bool(
            sync["stale_authority"].get("staleness_detected")
        ),
        "sync_standby_unavailable_blocked": bool(
            sync["commit_cost"]["standby_unavailable"]["blocked"]
        ),
        "async_lane_loss_recorded": (
            0
            <= bundle["async_lane"]["rpo_acknowledged_commits_lost"]
            <= bundle["async_lane"]["tail_acknowledged_commits"]
        ),
        "async_prefix_property": bool(bundle["async_lane"]["prefix_property_holds"]),
        "durability_cost_measured": bool(
            sync["commit_cost"]["sync_commit_stats"]
            and bundle["async_lane"]["commit_stats"]
        ),
        "repeated_drill_zero_loss": (
            bundle["repetition"]["rpo_acknowledged_commits_lost"] == 0
        ),
    }
    bundle["structural_acceptance"] = structural
    # drift guard: the computed verdict set must match the declared
    # acceptance vocabulary exactly, or the bundle is invalid
    if set(structural) != set(STRUCTURAL_KEYS):
        print(
            f"ERROR: structural acceptance keys drifted: computed "
            f"{sorted(set(structural) ^ set(STRUCTURAL_KEYS))}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"structural_acceptance": structural}, indent=2))
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            # default=str: node facts carry datetimes (postmaster start
            # times) as observed by asyncpg; they serialize as ISO strings
            json.dumps(bundle, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"[pr83c2] wrote {args.output}")
    if not all(structural.values()):
        print("ERROR: structural acceptance failed", file=sys.stderr)
        return 1
    print("[pr83c2] PASS: all structural acceptance booleans true")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
