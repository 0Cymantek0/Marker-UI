# Invariants 37 & 38 — External-Effect Semantics and Runtime Failure-Plane Closure

**Branch:** `markerui-v2` · **Slice:** V3.2 amendment 23C.4, invariants 37 + 38
**Plan:** `planning/v2/marker-ui-v2-focused-plan-runtime-failure-plane.md`
**Readiness before:** 53/62 proven, 0 failed, 9 without full evidence
**Readiness after:** 55/62 proven, 0 failed, 7 without full evidence
**Session commits:** `391e12b` (inv 37 declaration module), `be052b7` (runtime
defect fixes), `6734d11` (failure-plane suite)

This is a closure record, not a proof: the proofs are the executable bindings
in `readiness-ledger.json` and the tests they name.

---

## 1. What was built

### Invariant 37 — per-destination external-effect semantics declaration

`backend/app/kernel/effect_semantics.py` is the single authoritative
declaration table. Every external-effect destination carries:

- a `DestinationCapabilities` vector of boolean **facts** about the real
  primitive's behavior;
- a semantics label **derived** from those facts by `derive_semantics`
  (never hand-assigned, so label and facts cannot drift);
- the dotted import path of the owning primitive, which the suite resolves
  against real code (a renamed or removed primitive fails the suite);
- an honest reconciliation note where effects can outlive their authority.

Declarations:

| Destination | Semantics | Basis |
|---|---|---|
| `kernel.accepted_publication` | `exactly_once` | `fencing.accept` linearizes fence check + publication insert + lease flip in one transaction; same-result redelivery converges; divergent results raise `PublicationConflictError`; stale tokens are rejected before comparison. |
| `filesystem.conversion_output` | `manual_reconciliation_required` | Per-file atomic rename only, no cross-file transaction; the default collision-avoiding mode writes a new `-N` set on redelivery, so interrupted/superseded/re-executed writes orphan predecessor files. Accepted truth stays singular (the publication descriptor bounds which paths are truth), but nothing sweeps orphans automatically. |
| `compatibility.conversion_job_row` | `exactly_once` (derived) | Guarded `status not_in terminal` projection of accepted truth; replay converges; terminal rows are never overwritten; the row may only read `completed` after fenced acceptance. |

`backend/tests/test_kernel_effect_semantics.py` proves the derivation rules
are total (all four labels reachable), that registry semantics always match
derived facts, and exercises each declared fact against the real primitives
(fencing/publication facts run on both first-class database backends;
output-writer facts run against the real writer including an interrupted-set
injection; projection facts run against the real coordinator).

### Invariant 38 — runtime failure plane

`backend/tests/test_kernel_runtime_failure_plane.py` drives the production
bridge (TaskManager → authorization → `claim_fair` dispatch → fenced
acceptance → projection) with faults injected at the real boundaries:

- **Destination matrix:** a hung destination cannot block unrelated work;
  deterministic destination failure is terminal truth with no publication;
  transient failure retries with visible at-least-once redelivery and
  exactly-once acceptance; a hung destination rides out a **real** lease
  timeout (no forced SQL expiry) to watchdog takeover and converges while
  the superseded generation's late finalize stays fenced out.
- **Acceptance boundary:** database outage in the result-to-acceptance gap
  retries and converges with exactly one publication; persistent outage is
  terminal, never fake success.
- **Cancellation durability:** cancelled work survives full manager/
  coordinator recreation without resurrection or re-execution.
- **Model-service crash:** a **real child process** is killed mid-work
  (OOM-kill analog); truthful terminal failure, isolated peer, and
  crash-then-retry convergence with exactly-once acceptance.
- **Shared-memory pressure:** `MemoryError` from the converter is a truthful
  terminal failure with an isolated peer.
- **Pressure:** the fan-out cap never oversubscribes under twice its load; a
  mixed fast/slow/transient/hard/cancelled workload conflences with exact
  retry accounting and one publication per completed job; evidence-backed
  renewals hold four concurrent leases with zero lapse-retries.

## 2. Defects found and fixed (behavior changed, not just proven)

All three were reproduced deterministically before fixing
(`be052b7`):

1. **Destination write parked the shared runtime event loop.**
   `_finalize_job` ran `write_conversion_output` (blocking per-file
   filesystem IO) on the loop that owns dispatch, lease renewal, and the
   watchdog. Instrumented repro: a hung destination froze an unrelated
   healthy job's acceptance and row write for the entire hang (completion at
   32.5s instead of ~2.5s). The write (and the accepted-output read in
   `_project_publication`) now run via `asyncio.to_thread`.
2. **Watchdog acted on stale snapshots.** The owner paths release the fence
   *before* the outbox row; a watchdog pass snapshotted inside that window
   requeued or terminal-failed work whose legitimate retry release was
   mid-flight (this consumed retry budgets and produced false terminal
   failures under load). Both decision points now re-read the current
   durable outbox state.
3. **Claimed-but-pool-queued work is liveness-blind.** With executor workers
   below `max_in_flight`, claimed work queues with no possible activity
   evidence and can ride out its whole lease window. `start_kernel_runtime`
   now warns on the misalignment; renewal itself stays strictly
   evidence-only (a coordinator-heartbeat patch was evaluated and reverted —
   see §5).

## 3. Environments exercised

- Executed: **sqlite-dev** (both new suites; every binding that claims full
  coverage in this closure ran green here).
- Env-gated locally, executable in CI when provisioned
  (`MARKER_TEST_POSTGRES_ADMIN_URL` / industrial lanes): the `[postgresql]`
  parameterizations of both new suites, plus the pre-existing PG failover /
  primary-kill / WAL bindings already attached to invariants 37/38. Those
  lanes remain honestly recorded in the ledger and re-execute in their CI
  environments.

## 4. Exact commands and counts

```bash
# focused suites (stability-verified; combined loop 3/3 green)
python -m pytest backend/tests/test_kernel_effect_semantics.py -q          # 19 passed, 7 skipped
python -m pytest backend/tests/test_kernel_runtime_failure_plane.py -q     # 13 passed, 8 skipped
python -m pytest backend/tests/test_kernel_runtime_failure_plane.py \
  backend/tests/test_kernel_runtime.py -q                                  # 42 passed, 37 skipped (x3)

# evidence + integrity (regenerated for the new bindings)
python backend/scripts/readiness_audit.py --mode run-evidence
python backend/scripts/readiness_audit.py --mode integrity

# broad regression
cd backend && python -m pytest tests conformance -q
# 3917 passed / 3 failed / 224 skipped in 24m06s
# The 3 failures are PR84C displacement-eval tests outside this slice's
# diff surface; each passed standalone in three consecutive isolated
# runs (18/18 per run) — the same under-load transient class the two
# previous closure records documented (§5.1). Skip delta +16 vs the
# prior closure = this slice's [postgresql] parameterizations.
```

Skips are the PostgreSQL parameterizations without a provisioned admin URL —
recorded, not hidden.

## 5. Flakes encountered and how they were distinguished from defects

- Initial rotating failures across the retry-budget tests under combined
  load were **not** dismissed as flakes: instrumentation traced them to
  defect 2 (the watchdog stale-snapshot race) and to hyper-tuned test
  intervals (50 ms renewals across 8–12 concurrent claims) pushing SQLite
  past its write envelope. The race was fixed at the root; the pressure
  tests use calmer intervals (still an order of magnitude faster than
  production's 5 s / 15 s) and align the executor pool with the fan-out cap.
- A prestart coordinator-heartbeat renewal patch (covering the claim-to-start
  gap) was implemented, found to destabilize the existing suite (HEAD ran
  3/3 clean, the patch flaked), reverted, and replaced with the structural
  alignment + loud warning of defect 3. Renewal remains evidence-only.
- The mixed-workload test initially deadlocked itself by holding slow gates
  across the cancellation step (the fan-out cap correctly refuses to claim
  behind held workers); the scenario was reordered so the cancellation race
  is still exercised mid-flight.

## 6. Residual boundaries (honest, executable)

- PostgreSQL-specific locking/failover/WAL behavior of the new suites'
  `[postgresql]` parameterizations and the pre-existing PG failover bindings
  execute when their CI environments are provisioned; the local closure run
  records them as skipped, not passed.
- S3 destination semantics (source-revision object store) are bound to the
  existing industrial S3 lanes and were not re-declared here.
