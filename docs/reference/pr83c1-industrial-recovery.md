# PR83C1 — Industrial Recovery Boundary: Coherent Backup/Restore, Replacement-Process Failover, Measured RPO/RTO

**Session date:** 2026-08-22
**Implementation start SHA:** `7043268d4850f09caa0cc0d4c7320b48d71e1110` (PR83B3 evidence head)
**Evidence head:** see `docs/reference/measurements/pr83c1-industrial-recovery.json`
**Plan:** `planning/v2/marker-ui-v2-PR83C1-industrial-recovery-plan-20260822.md`

## What this slice proves

PR83B3's evidence ended with recovery/HA/RPO/RTO as explicit
non-claims. This slice closes the operational-recovery gap: a declared
recovery point can be captured coherently, restored to **fresh**
backing services, and proven by an executable oracle; a replacement
application process can take over from a killed owner with the dead
node's local state destroyed; stale owners cannot publish; and RPO/RTO
are **measured** from controlled fault drills, not inferred from
configuration.

| Required truth (plan §7/§9–§15) | Proof |
|---|---|
| A recovery point names an **exact semantic cut**, not a timestamp | `RecoveryPointManifest` binds `kernel_commit_id <= K` + the replayable `KernelSnapshot` identity + the payload/source object sets + publication lineage; deterministic `recovery_point_id` hashes the semantic dimensions only (`app/kernel/recovery.py`) |
| Backup closure cannot lose a required payload to GC | capture holds the **session-level advisory lock on the existing `PAYLOAD_DECISION_LOCK_SCOPE`** — the same lock tag every GC deletion decision, retention root write, generation activation, and payload-carrying commit already linearizes on; R7 test proves a fully-eligible collector blocks inside the window and may only take the originals after it closes |
| Source artifacts are part of the recovery closure | every `content_revision` record ≤ cut is enumerated and its bytes copied + verified into the backup source namespace; malformed committed payloads refuse the capture |
| Interrupted capture is never discoverable as complete | manifest written **last** into a staging directory atomically renamed on completion; crash-window fault vocabulary `rec-*` pins every boundary; hard-crash staging residue is discarded by the next attempt; tampered manifest (identity mismatch) and damaged dump (digest mismatch) refuse on load |
| Retry is convergent, not contradictory | same semantic point → same deterministic identity → the retry loads and re-verifies the existing point (captured_at identical) |
| Destructive restore removes hidden dependence | original database **dropped** and original object namespaces **deleted** before the oracle runs; restored topology = fresh PostgreSQL target (pg_restore) + fresh object namespaces seeded from verified backup copies only |
| A restore is more than "PostgreSQL starts" | `verify_recovery` oracle: migration-head database, declared cut resolves to the manifest snapshot as complete/replayable, payload closure, source closure, publication deep-verification + deterministic query replay, ownership closure — all six must be green simultaneously; every destructive/fault test asserts both what must happen and what must not |
| Tail semantics are exact | restoring an earlier point (R11): restored head == that cut, zero records beyond it, tail source objects absent from closure; the RPO commit delta and lost source objects are explicit measurements |
| Missing/corrupt dependencies degrade truthfully | missing payload → `payload_closure` fails **and** the cut honestly degrades (replayable completeness cannot hold), never ready; corrupted restored source object → fail closed; dropped lexical physical table → readiness held until the derived serving state is rebuilt at the recorded lineage (B4), then green |
| Replacement process recovers from shared truth only | real OS subprocess A is killed mid-claim without graceful cleanup, its node-local directories destroyed; subprocess B (fresh empty roots, same real PostgreSQL + S3) recovers semantic truth, rematerializes source bytes into its own cache, replays the deterministic published query, takes the lapsed lease through the fence, accepts + acks, and commits **new** truth beyond the recovered cut |
| A dead owner's late completion can never become truth | A's ghost (fresh process, A's fencing token) is rejected with `StaleFenceError` before result comparison; exactly one accepted publication remains; duplicate redelivery converges idempotently |
| Service outages fail honestly | PostgreSQL gone mid-recovery → non-zero, no false success; object store gone during source recovery → fail closed; both complete after the authority returns (containers really stopped/started) |
| RPO/RTO are measured, with declared clocks | failover RTO = kill → verified post-recovery **write** (milestone epochs from the probe, anchored at the kill); disaster-restore RPO = recovered cut vs last committed pre-disaster cut from the capture experiment; `scripts/bench_pr83c1_recovery.py` refuses green on any false structural boolean |
| Strict real-service gate, no skips | 3 new suites in `run_industrial_conformance.py` `TEST_TARGETS` + CI `industrial-persistence` job; outage drills locate the real service containers by published port (works for locally provisioned containers and CI service containers alike) |

## Design decisions (Workstream A record)

* **Authoritative after a disaster:** the PostgreSQL kernel tables plus
  the payload and source object bytes a declared cut requires. Derived
  serving state (lexical physical tables, materialized generations)
  is rebuildable; node-local caches are never authorities.
* **Backup strategy:** one `pg_dump -Fc` logical backup per recovery
  point, executed through a versioned sidecar container
  (`postgres:16-alpine`, `--add-host host.docker.internal:host-gateway`)
  so the same mechanism works against a Docker-provisioned server, a
  CI service container, or an external server. A per-point logical
  backup gives exact tail semantics without WAL-archive infrastructure;
  PITR/standby promotion remains an explicit non-claim.
* **Quiescence:** the capture window takes the **session-level**
  advisory lock on the same `PAYLOAD_DECISION_LOCK_SCOPE` key space the
  transaction-scoped writers already use (`pg_advisory_lock` and
  `pg_advisory_xact_lock` share the lock-tag namespace), so payload GC
  and payload-carrying commits block for the window without any new
  lock vocabulary. Publication activation does not join that scope, so
  capture additionally compares publication heads before and after the
  dump and refuses the attempt if they moved. The window is measured
  and recorded in the manifest.
* **Lexical serving state:** the physical per-generation tables live
  inside PostgreSQL, so the dump restores them coherently (plan B4
  choice 1). When physical state is lost anyway, readiness is held
  until it is rebuilt — and the rebuild path was hardened (below).
* **Failover model:** application-level replacement over surviving
  shared services, through the existing fence/lease/scheduler
  authorities — no new scheduler, no second ownership truth.
* **No new tables.** The manifest is a filesystem artifact; retention
  semantics ride the existing authorities.

## Publications hardening found by the drill

`build_lexical`'s idempotent-reuse path returned existing validated
manifest rows without checking that their **physical** artifact still
exists. A restore that lost a physical lexical table (or physical
corruption) could therefore not rebuild through the normal path. The
reuse path now verifies the physical layer
(`physical_integrity_problems`) and, when it is gone, rematerializes
the physical table + locator rows in one transaction from the
digest-verified corpus — same identity, same digest, no new truth.
Publication suites re-run green (113 tests, dual backend).

## Adversarial review round

An independent reviewer pass over the new code was verified
finding-by-finding before acceptance. Real defects fixed: an inverted
fail-closed default in the backup-side source-copy verification guard
(a corrupted copy could pass); the measurement log parser losing
failed-counts for outcome-ordered pytest summaries; the ownership
oracle not flagging in-flight work under a vacated lease nor accepted
publications whose recorded fencing token diverged from the accepted
lease; the payload-closure loop aborting the oracle on a store refusal
instead of recording a failed check; and a failover-probe takeover
loop that abandoned polling on a wrong-work claim. Two tests were
added to pin the ownership-mutation and store-refusal paths (18
recovery tests total). One reviewer claim was rejected on the merits:
the "vacuous `all()`" on an empty backup directory is the *correct*
predicate for "nothing discoverable"; the staging-invisibility proof
was nonetheless strengthened to hold by directory **name**, not by
malformed-id rejection.

## Measured results

Single-host drill measurements (full JSON:
`docs/reference/measurements/pr83c1-industrial-recovery.json`):

| Metric | Value (recorded run) | Clock definition |
|---|---|---|
| Failover RTO (full application-recovery-ready) | 1.60 s | kill epoch → B's `write_ready` milestone (new commit under B's authority) |
| RTO components | boot 1.35 → semantic 1.45 → source 1.47 → query 1.51 → work 1.56 → write 1.60 (seconds from kill) | milestone epochs minus kill epoch |
| Disaster-restore RPO | 2 commits + 1 source revision (deliberate post-point tail) | last committed pre-disaster cut − recovered cut, from the capture experiment |
| Capture operational tax | 1.66 s total (quiesce window = whole capture), dump 75,059 B, 2 payload + 2 source objects copied | capture-side wall clocks |
| Restore + oracle | restore 2.17 s, oracle 0.41 s | restore start → target ready; oracle invocation → verdict |

RTO never stops at "port open": the timer stops only when a fresh
process simultaneously proves database/kernel integrity, source and
payload closure, deterministic query service, ownership takeover, and
one new post-recovery write.

## Evidence runs

All numbers below are from the final evidence runs at the evidence
head (after the adversarial-review hardening), embedded verbatim in
`docs/reference/measurements/pr83c1-industrial-recovery.json`.

* **Strict industrial matrix** (real PostgreSQL 16.14 + real MinIO,
  zero skips): **411 passed / 0 failed / 0 skipped in 777.67 s** —
  baseline PR83B3 395 + exactly the 16 new recovery tests
  (`run_industrial_conformance.py`, 18 targets). The outage drills
  stopped and restarted the real service containers mid-run.
* **Full backend regression:** **3150 passed / 0 failed / 199 skipped
  in 1738.30 s** (baseline 3150/0/183; +16 skips = the recovery suites
  outside the strict env, the established S3/PG-gated convention). An
  earlier full-suite run on the same code had one environmental flake
  — `test_artifact_handles.py::TestResolveWorkerPayload::
  test_deleted_backing_fails_resolution` (PR68A code, untouched since
  `a25f04f`), an age-vs-mtime race (`sweep(older_than_seconds=0)` on a
  same-tick file under full-suite load); it passed in isolation then
  and did not reproduce in the final recorded run.
* **Repetition:** the failover suite was run twice consecutively green
  (process-boundary drills are timing-sensitive; both runs passed),
  and the whole recovery block re-ran green after the review hardening
  (16/16, then 18/18 with the two new oracle-mutation tests).

## What is still a prototype / non-claims

* Single-host CI-grade drill measurements — **not** a production RTO
  SLO, not multi-region, not universal production HA.
* Logical per-point `pg_dump` backup; **no** PITR, WAL archiving,
  standby promotion, or automated database HA orchestration.
* Write quiescence covers payload decisions (GC + payload-carrying
  commits) and publication-head movement is detected-and-retried;
  non-payload commits during the dump window are not blocked (in the
  controlled drills none occur; documented contract).
* No vector-generation industrialization (vector slot stays explicitly
  absent), no source-artifact GC/retention design, no PR84 readiness.
* RTO/RPO are measured for the tested topology only.

## Reproduction

```bash
# strict matrix incl. all recovery suites (real PostgreSQL + MinIO)
python backend/scripts/run_industrial_conformance.py

# measured evidence bundle (requires the same env the runner sets)
MARKER_TEST_POSTGRES_ADMIN_URL=postgresql+asyncpg://marker:marker@127.0.0.1:55445/postgres \
MARKER_TEST_S3_ENDPOINT=http://127.0.0.1:55446 \
MARKER_TEST_S3_ACCESS_KEY=marker MARKER_TEST_S3_SECRET_KEY=marker-marker \
python backend/scripts/bench_pr83c1_recovery.py --write
```

## What should come next

1. **PR83C2 operational hardening:** continuous/WAL-based capture and
   standby promotion now have the oracle + drill harness to prove
   themselves against.
2. **Industrial vector-generation parity** at its masterplan seam.
3. **PR84 readiness/claim audit** once every PR83 non-claim the
   masterplan treats as blocking has executable evidence.
