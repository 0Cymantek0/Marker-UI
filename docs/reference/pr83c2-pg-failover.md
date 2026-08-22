# PR83C2 — Real PostgreSQL Primary/Standby Promotion: Acknowledged Durability, Fencing Coherence, Measured RPO/RTO

**Session date:** 2026-08-22
**Implementation start SHA:** `13091518df461b377e6ba80eb576d2607f19b541` (PR83C1 evidence head)
**Evidence head:** see `docs/reference/measurements/pr83c2-pg-failover.json`
**Plan:** `planning/v2/marker-ui-v2-PR83C2-postgresql-failover-plan-20260822.md`

## What this slice proves

PR83C1 closed the recovery boundary with application-process failover
and explicitly refused standby promotion. This slice closes the
database-authority gap the master plan names as a release test
(11B.12/11B.13: *fail over the database after a lease renewal and
terminal commit*): a real physical standby is promoted after a hard
primary kill, and every Marker UI transition the tested durability
policy already **acknowledged** survives — proven from pre-fault
observations only, never from post-fault catch-up.

| Required truth (plan §9 O1–O12) | Proof |
|---|---|
| Two real PostgreSQL instances in primary/physical-standby roles, observed directly (O1) | `tests/pg_failover_topology.py` provisions two `postgres:16-alpine` containers on a dedicated bridge network; the standby's data volume is seeded by a one-shot `pg_basebackup -Xs -R` loader (application name embedded in `primary_conninfo`); roles are observed via `pg_is_in_recovery()`, replication via `pg_stat_replication` — never inferred from container names |
| The durability policy is executable, not prose (O2) | `DURABILITY_POLICY` in `tests/failover_drills.py` declares `synchronous_commit=remote_apply` + `synchronous_standby_names='FIRST 1 (marker_standby)'`; the topology applies it **after** streaming exists and **before** any workload, and `assert_policy_active` refuses to continue unless the live settings + `sync_state='sync'` prove it. Covered master-plan-critical transitions: lease/fence acquisition+renewal, accepted stable publication + terminal work state, kernel document commits. Source-cursor and irreversible-effect lanes are explicitly declared not exercised |
| Acknowledged durable truth survives primary loss (O3/O9) | every acknowledgement is the commit return of its own transaction; under `remote_apply` the primary returns success only after standby replay. The drill records the primary LSN **after** the last acknowledgement, proves `pg_last_wal_replay_lsn()` already covered it **before** the fault, then `docker kill`s the primary (SIGKILL, no graceful handoff). After promotion the recovered cut equals the last acknowledged pre-failure cut exactly — measured RPO for the acknowledged class is **0 commits** |
| The standby is actually promoted (O4) | `pg_promote(wait=true)`, then observed: `pg_is_in_recovery()` flips false, the checkpoint **timeline advances 1→2**, the **postmaster start time is unchanged** (a role change, not a restart), and the promoted server accepts writes |
| Reconnection without invented truth (O5/O11) | the parent disposes its primary engine before the kill; a recovery probe still pinned to the dead primary's URL **fails honestly** (non-zero, no recovery verdict); a fresh process bound to the promoted authority recovers through the standard kernel seams — no production routing abstraction was added because none was needed |
| Ownership/publication coherence across the role change (O6) | duplicate redelivery of the pre-failure accepted result converges (`already_accepted`, still exactly one publication); a real supersession **through the promoted authority** (late owner claims fresh work, is killed, replacement takes the lapsed lease, token advances) rejects the dead owner's old token with `StaleFenceError`; exactly one accepted publication per work item, outboxes `done` |
| The promoted database reconstructs the expected KernelSnapshot (O7) | the PR83C1 recovery oracle (`verify_recovery`) runs against the **promoted live topology** with the pre-failure manifest: database head, cut resolution, payload closure, source closure, publication (incl. deterministic query replay), ownership — all six green simultaneously. Object truth is the same industrial store, unaffected by database failover |
| A fresh post-promotion write succeeds (O8) | the replacement commits new kernel truth through the promoted authority; the recovered snapshot cut, the promoted head, and the new commit are asserted in strict order |
| RPO computed from committed Marker UI truth (O9) | sync lane: 0 acknowledged commits lost (both recorded runs). Async comparison lane (declared lossy **in advance**): with the standby stopped, 3 acknowledged tail commits vanished with the primary; the promoted state held exactly the prefix (nothing beyond the promoted head, head = newest record, promoted cut ≤ acknowledged cut) and the loss is a measurement, not a failure |
| RTO ends at verified write readiness (O10) | kill epoch → `write_ready` milestone (new kernel commit through the promoted authority), with component clocks: promote start/complete, boot, semantic, source, query, work, write |
| No false success when safe promotion is impossible (O11) | required standby unavailable → durability-class commits **never acknowledge** (client times out, 0 acknowledgements; they resume after the standby returns); dead primary → honest failure; object store down during post-promotion verification → oracle readiness is **false** until it returns; restarted old primary → its stale cut is **detected at the Marker UI truth boundary** (head behind the promoted authority) — no quiet split authority in the accepted proof |
| Local and single-PostgreSQL behavior remains intact (O12) | SQLite is untouched; the industrial matrix still runs one PostgreSQL + one object store (413/0/0); the failover suite lives in its own strict runner + CI job and cannot green through skips |

## Design decisions

* **Topology as test/control-plane glue.** The two-node topology is a
  test helper (`pg_failover_topology.py`), not production architecture.
  No Patroni/etcd, no cluster manager, no routing abstraction in the
  kernel — the drill needed none of them to prove the semantics.
* **Policy applied after streaming, verified live.** `synchronous_
  standby_names` is set only once the standby streams, so no workload
  can be acknowledged before a sync standby exists; `remote_apply` was
  chosen over `on`/`remote_write` because Marker UI's acknowledgement
  is "this transition survives primary loss" — replay, not just flush.
  The settings live in the primary's `postgresql.auto.conf` only (set
  **after** the base backup), so the promoted node is not born with a
  sync-standby dependency.
* **Durability condition established pre-fault.** The drill records the
  primary WAL LSN after the last acknowledgement and proves the standby
  had already replayed past it **before** the kill. Nothing waits for
  replication after the fault — the exact trap the plan calls
  post-fault catch-up disguised as pre-fault protection.
* **One promotion per cluster, loud on reorder.** `promote()` asserts
  the standby is still in recovery; a second run of the core drill
  against the same cluster fails loudly rather than silently passing.
* **Async lane declared lossy before it ran.** The comparison lane's
  contract is written into `DURABILITY_POLICY.declared_lossy_lane`
  before any measurement; its measured loss is evidence about the
  *async* acknowledgement policy, never conflated with the sync claim.
* **RTO is an honest upper bound.** The kill→write clock includes
  deliberate drill choreography (the dead-primary negative probe, the
  promotion `CHECKPOINT`), recorded as a non-claim rather than trimmed.

## Adversarial review round

An independent reviewer pass attacked the new code as if trying to make
an unsafe implementation pass, and the full regression run itself
caught one more defect the reviewer could not see statically:

* a **module-scoped async fixture** whose setup skips (the plain
  regression, no service env) left the session event loop closed —
  every async fixture setup in later files errored (`Event loop is
  closed`, 1068 setup errors). Reproduced minimally, fixed with a
  function-scoped fixture (which also removes cross-test ordering),
  re-verified clean end to end.

Verified defects fixed:

* the async lane's "prefix property" was **vacuous** — the head CAS
  already guarantees no record beyond the head on any replay, so the
  check could never fail; it now proves the real invariants (lost tail
  fully absent, head pointer = newest record present, promoted cut ≤
  acknowledged cut) and documents why sequence gaps inside the
  surviving prefix are legitimate;
* the no-skip gates anchored `N skipped` to line start and were blind to
  pytest's mixed summaries (`4 passed, 1 skipped in ...`) — fixed in the
  failover runner at birth and retroactively in the kernel/industrial
  runners and the industrial CI grep;
* the primary container's anonymous data volume leaked per provision
  (`docker rm -f` without `-v`) — fixed in all teardowns.

Refuted on the evidence (recorded because the refutations matter to the
safety argument): promotion returning before the standby is writable
(`pg_promote(wait=true)` waits for cluster-wide recovery exit, and the
drill proves an application commit afterwards); stale pooled
connections reaching the old primary (the engine is disposed before the
kill; probes are separate OS processes); post-fault catch-up (replay
proof strictly precedes the kill in both lanes); port-open RTO (the
clock stops at a verified kernel write); spurious `blocked=false` while
the sync standby is away (the container is fully stopped, sync rep does
not demote, and the probe asserts zero acknowledgements alongside the
blocked observation); loader quoting hazards on Windows (single
`sh -c` layer via subprocess list args).

## Measured results

Single-host drill measurements (full JSON:
`docs/reference/measurements/pr83c2-pg-failover.json`):

| Metric | Value (recorded run) | Clock definition |
|---|---|---|
| Failover RTO (sync lane, verified post-promotion write) | 10.57 s | kill epoch → replacement's `write_ready` milestone |
| RTO components | promote start 6.70 / promote complete 8.26 → boot 10.24 → semantic 10.37 → source 10.40 → query 10.44 → work 10.53 → write 10.57 (seconds from kill) | milestone/promotion epochs minus kill epoch |
| RPO, acknowledged durable class (sync) | **0 commits** (both recorded runs) | pre-failure head cut − recovered cut on the promoted authority |
| RPO, async comparison lane | 3 commits (3 acknowledged tail commits lost) | acknowledged cut − promoted cut after standby-cut + kill + promote |
| Promotion wall time | 1.57 s | `pg_promote` start → completion (role change on the live process) |
| Durability tax (commit p50/p95) | sync 31/125 ms vs async 31/125 ms (localhost) | per-commit latency, 10 sync / 6 async payload-bearing kernel commits |
| Standby unavailable | 0 acknowledgements in an 8 s client window; blocking window 10.1 s until resync green | client-side timeout; stop→start→sync_state='sync' wall clock |
| Run-to-run spread (fresh topology) | 10.57 s vs 10.39 s (spread 0.19 s), zero loss both runs | two full drills from fresh containers/basebackup/database |

RTO never stops at "port open": the timer stops only when a fresh
process has simultaneously proven semantic truth, source and payload
closure, deterministic query service, ownership takeover, and one new
post-promotion commit. The absolute value is dominated by the drill's
own choreography (dead-primary probe, promotion CHECKPOINT, lease-lapse
takeover waits) — an honest upper bound, not a tuned number.

## Evidence runs

All numbers are from the final evidence runs at the evidence head,
embedded verbatim in the measurement JSON.

* **Strict failover matrix** (real two-node PostgreSQL + real object
  store, zero skips): **4 passed / 0 failed / 0 skipped** in ~166–190 s
  (`scripts/run_failover_conformance.py`, run green three times —
  repetition evidence for the timing-sensitive drills; each test owns a
  fresh cluster since the per-test-topology fix). The object-outage
  phase stopped and restarted the real object-store container mid-run.
* **Strict industrial matrix** (single PostgreSQL 16.14 + MinIO, 18
  targets): **413 passed / 0 failed / 0 skipped in 813.36 s** — every
  previously included industrial target still executes (PR83C1's
  recorded 411 predated the two same-commit oracle-mutation tests it
  shipped with; 413 is that list, unchanged, plus those two).
* **PR83C1 recovery suites** re-ran green on the same head: 18/18.
* **Full backend regression:** **3075 passed / 0 failed / 205 skipped
  in 1908.70 s** plus the conformance suite **75 passed / 0 failed** —
  together 3150 passed, matching the PR83C1 baseline's pass count
  exactly. Skip delta vs the recorded 199: +4 = the failover suite
  outside its service env (by design; those four execute for real in
  the strict failover matrix), with the residual ±2 being the same
  machine-conditional gated conventions (service/opt-in gates), zero
  failures throughout. An earlier full-regression attempt on
  pre-fix code self-destructed with 1068 setup errors — a
  module-scoped async fixture that skipped poisoned the session event
  loop for every later file; that defect was found by this very
  regression run, fixed (function-scoped fixture), and re-run clean.

## What is still a non-claim

* Single-host CI-grade drill measurements — **not** a production RTO/RPO
  SLO, not multi-region, not multi-standby quorum.
* No Patroni/etcd/Consul or automated cluster-manager orchestration;
  promotion is a deliberate test/control-plane action. Automated HA
  remains unclaimed.
* Streaming replication only — **no** WAL archiving/PITR claim
  (PostgreSQL treats those as a different backup discipline; PR83C1's
  logical dumps remain the per-point recovery mechanism).
* No old-primary automated rejoin or fleet healing: the split-authority
  case is **detected** (stale cut at the truth boundary) and fenced out
  of the accepted proof, not healed.
* No object-store HA claim (a single object store spans the drill).
* Source-cursor advancement and irreversible-effect authorization lanes
  are not exercised by this drill (declared in
  `DURABILITY_POLICY.not_exercised_this_session`).
* Post-promotion writes run under the promoted node's local
  `synchronous_commit` default — the sync pair is not re-established
  after promotion.

## Reproduction

```bash
# strict two-node failover suite (provisions its own topology + object store)
python backend/scripts/run_failover_conformance.py

# measured evidence bundle (requires the env the failover runner sets)
python backend/scripts/run_failover_conformance.py --keep-services
MARKER_TEST_S3_ENDPOINT=http://127.0.0.1:55463 \
MARKER_TEST_S3_ACCESS_KEY=marker MARKER_TEST_S3_SECRET_KEY=marker-marker \
python backend/scripts/bench_pr83c2_failover.py --write \
  --failover-log <runner log> --strict-matrix-log <industrial log> \
  --regression-log <regression log>
```

## What should come next

The master plan's PR83 release-test row ("fail over the database after
a lease renewal and terminal commit") now has executable evidence
against real services. The remaining named PR83 candidates are
continuous archive/PITR (a separate discipline, per PostgreSQL's own
docs) if the readiness audit deems it blocking. PR84's first job should
be mapping each readiness invariant to executable evidence, not
rewriting the system.
