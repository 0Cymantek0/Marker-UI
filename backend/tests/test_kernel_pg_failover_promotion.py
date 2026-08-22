"""PR83C2: real PostgreSQL primary/standby promotion drills.

Everything here runs against two real PostgreSQL instances in
primary/physical-standby roles (provisioned by ``pg_failover_topology``)
plus the real S3-compatible object store named by the environment. The
drills prove, with pre-fault observations only:

* the roles and replication state are observed directly, never inferred;
* the durability policy is executable and live (``remote_apply`` + one
  synchronous standby) before any workload is acknowledged;
* acknowledged durable truth survives a hard primary kill + real standby
  promotion with zero acknowledged loss (sync lane);
* the promoted authority serves a fresh Marker UI process through the
  PR83C1 recovery oracle vocabulary, fencing stays coherent, exactly one
  accepted publication exists per work item, and new truth commits;
* no false success is possible while the required standby, the promoted
  database, or the object store is unavailable;
* the asynchronous comparison lane measures its acknowledged tail loss
  honestly instead of claiming durability it did not earn.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from app.db_migration import upgrade_database
from tests.failover_drills import (
    DURABILITY_POLICY,
    ProbeHarness,
    async_loss_drill,
    promotion_drill,
)
from tests.pg_failover_topology import (
    STANDBY_APPLICATION_NAME,
    FailoverCluster,
    require_failover_docker,
)
from tests.pg_provisioning import create_postgres_database, drop_postgres_database
from tests.s3_provisioning import require_s3_env

pytestmark = pytest.mark.asyncio

#: Fresh clusters for the async lane and the repetition drill live on
#: their own ports so they never collide with the module's sync cluster.
ASYNC_CLUSTER_PORTS = (55471, 55472)
REPEAT_CLUSTER_PORTS = (55481, 55482)


@pytest_asyncio.fixture
async def sync_cluster():
    """The synchronous-policy primary/standby pair, fresh per test.

    Provisioning is real and loud: no Docker daemon or object store
    means skip (or fail under MARKER_TEST_FAILOVER_STRICT) — never a
    mock. A function-scoped async fixture keeps the suite inside the
    repo's proven pytest-asyncio pattern (a module-scoped async fixture
    poisons the session event loop for every later async fixture when
    it skips); each cluster carries exactly one promotion, so per-test
    clusters also remove any cross-test ordering coupling.
    """
    require_failover_docker()
    require_s3_env()
    cluster = FailoverCluster()
    await cluster.provision()
    try:
        yield cluster
    finally:
        cluster.teardown()


def _lsn_int(lsn: str | int | None) -> int:
    if isinstance(lsn, int):
        return lsn
    assert lsn, lsn
    high, _, low = lsn.partition("/")
    return (int(high, 16) << 32) | int(low, 16)


async def test_topology_roles_replication_and_honest_blocking(
    sync_cluster: FailoverCluster, tmp_path: Path
) -> None:
    """Baseline (failure-matrix case 1) + synchronous-standby
    unavailability (case 9): roles and replication observed directly,
    the durability policy live and executable, commit acknowledgements
    measured with the standby attached, and no durability-class
    acknowledgement while the required standby is gone."""
    # O1: real, observed roles before any fault
    primary = await sync_cluster.node_facts("primary")
    standby = await sync_cluster.node_facts("standby")
    assert primary["in_recovery"] is False
    assert standby["in_recovery"] is True
    assert primary["port"] != standby["port"]
    assert primary["banner"] == standby["banner"]

    replication = await sync_cluster.replication_facts()
    assert replication is not None
    assert replication["application_name"] == STANDBY_APPLICATION_NAME
    assert replication["state"] == "streaming"
    assert replication["sync_state"] == "sync"

    # O2: the tested durability policy is live, not prose
    policy = await sync_cluster.effective_policy()
    assert policy["synchronous_commit"] == DURABILITY_POLICY["synchronous_commit"]
    assert (
        policy["synchronous_standby_names"]
        == DURABILITY_POLICY["synchronous_standby_names"]
    )
    await sync_cluster.assert_policy_active()

    # commit acknowledgement cost with the standby attached (sync tax)
    db_url = await create_postgres_database(sync_cluster.primary_admin_url)
    try:
        await upgrade_database(url=db_url)
        harness = ProbeHarness(tmp_path, workspace="failover-baseline")
        env = harness.base_env(harness.node_a, db_url)
        env["PROBE_COMMITS"] = "8"
        env["PROBE_COMMIT_TIMEOUT"] = "30"
        env["PROBE_COMMIT_LABEL"] = "sync-baseline"
        code, events, err = await harness.run_probe("commit_probe", env)
        assert code == 0, err
        assert events["commits"]["blocked"] is False
        assert events["commits"]["completed"] == 8
        assert len(events["commits"]["latencies_ms"]) == 8

        # case 9: required standby unavailable -> no durability success
        stopped_epoch = await sync_cluster.stop_standby()
        blocked_env = harness.base_env(harness.node_a, db_url)
        blocked_env["PROBE_COMMITS"] = "1"
        blocked_env["PROBE_COMMIT_TIMEOUT"] = "8"
        blocked_env["PROBE_COMMIT_LABEL"] = "sync-blocked"
        code, events, err = await harness.run_probe("commit_probe", blocked_env)
        assert code == 0, err
        assert events["commits"]["blocked"] is True
        assert events["commits"]["completed"] == 0

        # standby back: acknowledgements resume (after resync)
        started_epoch = await sync_cluster.start_standby()
        resumed_env = harness.base_env(harness.node_a, db_url)
        resumed_env["PROBE_COMMITS"] = "4"
        resumed_env["PROBE_COMMIT_TIMEOUT"] = "60"
        resumed_env["PROBE_COMMIT_LABEL"] = "sync-resumed"
        code, events, err = await harness.run_probe("commit_probe", resumed_env)
        assert code == 0, err
        assert events["commits"]["blocked"] is False
        assert events["commits"]["completed"] == 4
        assert started_epoch > stopped_epoch
    finally:
        await drop_postgres_database(sync_cluster.primary_admin_url, db_url)


async def test_acknowledged_truth_survives_primary_loss_and_promotion(
    sync_cluster: FailoverCluster, tmp_path: Path
) -> None:
    """The core drill (failure-matrix cases 2-5, 7, plus O11 negatives):
    acknowledged ownership + terminal/publication transitions on the
    primary survive a hard kill + real promotion with zero acknowledged
    loss; a fresh process recovers through the promoted authority; the
    oracle, fencing, and publication uniqueness stay coherent; dead
    primary / object-store outages fail honestly; a restarted old
    primary is detected as stale at the Marker UI truth boundary."""
    async with promotion_drill(sync_cluster, tmp_path) as drill:
        ev = drill.facts

        # -- pre-fault state was observed, not inferred (O1/O2) ---------
        assert ev["pre_fault"]["primary_facts"]["in_recovery"] is False
        assert ev["pre_fault"]["standby_facts"]["in_recovery"] is True
        assert ev["pre_fault"]["replication"]["sync_state"] == "sync"
        assert (
            ev["pre_fault"]["policy"]["synchronous_commit"]
            == DURABILITY_POLICY["synchronous_commit"]
        )

        # -- durability condition established BEFORE the fault (O3) -----
        assert _lsn_int(ev["pre_fault"]["standby_replay_lsn_observed_before_fault"]) >= (
            _lsn_int(ev["pre_fault"]["primary_lsn_after_acks"])
        )

        # -- real promotion: role change, not a restart (O4) -------------
        promotion = ev["promotion"]
        assert promotion["promoted_writable"] is True
        assert promotion["promotion_not_restart"] is True
        assert promotion["timeline_after"] == promotion["timeline_before"] + 1

        # -- zero acknowledged loss (O3/O9) -------------------------------
        assert ev["renewal"]["fencing_token"] == ev["claim"]["fencing_token"]
        assert ev["terminal"]["completed"] is True
        assert ev["rpo_acknowledged_commits_lost"] == 0
        assert ev["recovered"]["recovered_cut"] == ev["pre_fail_head_cut"]
        assert ev["captured_cut"] == ev["pre_fail_head_cut"]

        # -- fresh process through the promoted authority (O5/O8/O10) ----
        milestones = ev["milestones"]
        order = [
            "boot",
            "semantic_ready",
            "source_ready",
            "query_ready",
            "work_ready",
            "write_ready",
        ]
        epochs = [milestones[name] for name in order]
        assert epochs == sorted(epochs), milestones
        assert all(epoch >= ev["kill_epoch"] for epoch in epochs)
        rto = milestones["write_ready"] - ev["kill_epoch"]
        assert rto > 0
        assert milestones["write_ready"] >= promotion["promote_completed_epoch"]
        assert ev["recovered"]["new_commit"] == ev["promoted_head_cut"]
        assert ev["promoted_head_cut"] > ev["pre_fail_head_cut"]

        # -- O11: pinned to the dead primary, recovery fails honestly ----
        assert ev["dead_primary_probe_failed"] is True

        # -- O6: fencing coherent across the role change ------------------
        fencing = ev["fencing"]
        assert fencing["duplicate_a_converged"] is True
        assert fencing["takeover_advanced_fence"] is True
        assert fencing["stale_owner_rejected"] is True
        assert ev["publications_per_work"] == {"a": 1, "b": 1}
        assert ev["outbox_states"] == {"a": "done", "b": "done"}

        # -- O7: the PR83C1 oracle accepts the promoted live topology ----
        oracle = ev["promoted_oracle"]
        assert oracle["ready"] is True
        checks = {check["name"]: check["ok"] for check in oracle["checks"]}
        assert len(checks) == 6
        assert all(checks.values()), oracle["problems"]
        assert set(checks) == {
            "database",
            "cut",
            "payload_closure",
            "source_closure",
            "publication",
            "ownership",
        }

        # -- case 7: object store down => readiness stays false -----------
        assert ev["object_outage_oracle_not_ready"] is True
        assert ev["object_outage_recovers"] is True

        # -- split authority: the old primary is detectably stale ---------
        stale = ev["stale_authority"]
        assert stale["staleness_detected"] is True
        assert stale["old_primary_head_cut"] < stale["promoted_head_cut"]


async def test_async_lane_measures_declared_loss_honestly(tmp_path: Path) -> None:
    """The declared-lossy comparison lane: asynchronous acknowledgement
    (local WAL flush) does NOT claim cross-failure-domain durability.
    Terminal truth the standby possessed survives; the acknowledged tail
    after the replication cut is measured loss (0..N); the replayed
    history is a prefix (nothing beyond the promoted head, head equals
    the newest record, promoted cut never exceeds the acknowledged cut);
    and the promoted authority still serves new writes."""
    cluster = FailoverCluster(
        synchronous=False,
        tag="async",
        primary_port=ASYNC_CLUSTER_PORTS[0],
        standby_port=ASYNC_CLUSTER_PORTS[1],
    )
    await cluster.provision()
    try:
        replication = await cluster.replication_facts()
        assert replication["sync_state"] == "async"
        policy = await cluster.effective_policy()
        assert policy["synchronous_commit"] == "on"

        async with async_loss_drill(cluster, tmp_path) as drill:
            ev = drill.facts
            assert ev["terminal_truth_survived"] is True
            assert (
                0
                <= ev["rpo_acknowledged_commits_lost"]
                <= ev["tail_acknowledged_commits"]
            )
            assert ev["prefix_property_holds"] is True
            assert ev["post_promotion_commit"] > ev["promoted_head_cut"]
            assert len(ev["async_commit_latencies_ms"]) == 6
    finally:
        cluster.teardown()


async def test_repeated_promotion_drill_from_fresh_topology(tmp_path: Path) -> None:
    """Failure-matrix case 10: the timing-sensitive core drill passes a
    second consecutive time from completely fresh topology state (fresh
    containers, fresh basebackup, fresh database) with the same
    structural verdicts as the first run."""
    cluster = FailoverCluster(
        tag="repeat",
        primary_port=REPEAT_CLUSTER_PORTS[0],
        standby_port=REPEAT_CLUSTER_PORTS[1],
    )
    await cluster.provision()
    try:
        repeat_root = tmp_path / "repeat"
        repeat_root.mkdir(parents=True)
        async with promotion_drill(cluster, repeat_root) as drill:
            ev = drill.facts
            assert ev["rpo_acknowledged_commits_lost"] == 0
            assert ev["recovered"]["recovered_cut"] == ev["pre_fail_head_cut"]
            assert ev["fencing"]["stale_owner_rejected"] is True
            assert ev["publications_per_work"] == {"a": 1, "b": 1}
            assert ev["promoted_oracle_ready"] is True
            assert ev["dead_primary_probe_failed"] is True
            assert (
                ev["milestones"]["write_ready"] - ev["kill_epoch"] > 0
            )
    finally:
        cluster.teardown()
