# PR67B — Kernel Runtime Authority Integration

**Date:** 2026-08-16
**Branch:** `markerui-v2`
**Scope:** live local conversion path governed by the PR66/PR67 kernel runtime contracts (fair dispatch, challenge-backed liveness, fenced terminal publication, durable status projection), with the PR68A ArtifactHandle data plane preserved.

This document is the reproducible evidence bundle required by the PR67B plan (Section 11). A reviewer should be able to verify every claim from code, tests, and the commands recorded here without trusting the narrative.

---

## 1. What changed

| Area | File | Change |
|---|---|---|
| Runtime coordinator (new) | `backend/app/services/kernel_runtime.py` | `KernelRuntimeCoordinator`: authorization bridge, dispatch loop, evidence-backed renewal, acceptance/failure/cancellation, watchdog, restart recovery, accepted-result descriptor. |
| TaskManager integration | `backend/app/services/task_manager.py` | `submit_conversion` (authorize entry), claim-bound executor submission (`submit_job(..., claim=)`), activity taps (`report_stage_progress`, `add_job_log`, worker status events) feeding liveness, kernel-ordered `_finalize_job`/`_fail_job`, kernel-aware `cancel_job`, coordinator lifecycle in `shutdown`. |
| REST/agent submission | `backend/app/routes/convert.py`, `backend/app/agent_api.py` | Kernel-mode marker on rows (`queue_backend='kernel'`), `durable_filepath` persisted into config (upload/URL sources survive kernel dispatch), `submit_conversion` instead of direct executor submission, cancel route re-checks terminal truth. |
| Startup | `backend/app/main.py` | Kernel branch: backend selection first, coordinator bind, `recover()`, `start()`; dispatch-loop start failure unbinds so submissions fall back to the legacy runtime. Legacy recovery path preserved verbatim for kernel-off mode. |
| Config | `backend/app/core/config.py` | `MARKER_KERNEL_RUNTIME` (default on) + lease/renew/dispatch/watchdog/fan-out knobs. |
| Tests (new) | `backend/tests/test_kernel_runtime.py` | 27 integration tests over the plan's evidence matrix (10.1–10.10) plus adversarial regressions. |

No new database tables: the bridge is built from the existing kernel commit/outbox/scheduler/fencing/liveness/events schema plus the `conversion_jobs` compatibility row. `ConversionJob` remains a read model; `queue_backend='kernel'` is metadata, not authority.

## 2. Authority flow (what must be true, and where it is enforced)

```text
POST /api/convert/upload (kernel mode)
  -> ConversionJob(status=pending, queue_backend='kernel')     [compatibility row]
  -> coordinator.authorize(job_id, config)
       KernelCommitBatch(
           NativeObjectRecord(record_id='conversion-request.<job_id>', ...),   # the honest request object
           OutboxIntent(work_kind='conversion.execute', payload={'job_id'}))   # same tx: authorized == executable
       + scheduler.register_work(resource_class='conversion', group=config.scheduling_group|'default')
  -> dispatch loop: scheduler.claim_fair(...)                                  [ONE ownership authority]
       lease + fence token + challenge nonce + work.claimed event in one tx
       row -> 'processing' (projection of the claim)
       TaskManager.submit_job(..., claim=ActiveClaim)                           [generation-bound execution]
  -> real control-loop activity (tqdm progress / worker logs / worker status)
       increments claim.activity; renewal task renews ONLY on fresh evidence
       (rotating nonce, strictly-advancing counter) + coalesced kernel_progress
  -> executor result
       [process backend: ArtifactHandle envelope resolved strictly first]
       write_conversion_output -> durable files on disk
       build_result_descriptor (bounded hashes; result_path for recovery)
       scheduler.accept_work -> fencing.accept                                 [PR66 linearization point]
       fencing.complete_work (outbox ack behind accepted truth + current fence)
       row -> 'completed'                                                      [projection LAST]
```

Enforcement points:

- **One scheduler.** In kernel mode the legacy `SQLiteDurableQueueBackend` resubmission path is disabled (`TaskManager.recover_and_sweep_durable_jobs` returns early when the coordinator is bound); startup adoption converts legacy durable rows into kernel work instead of resubmitting them. `MARKER_KERNEL_RUNTIME=0` restores the entire legacy runtime unchanged.
- **Row never leads acceptance.** `TaskManager._finalize_job` under a bound runtime requires a live claim, crosses `accept_result` first, and refuses the terminal `completed` UPDATE for any other disposition (`superseded`/`conflict`/`cancelled`). A completion arriving with no live claim (zombie generation) is refused outright.
- **Stale generations.** Executors carry their launching claim (`submit_job(..., claim=)`); `fail_execution` re-checks the fence before touching state; `accept_result` relies on the PR66 stale-fence rejection; the watchdog marks lapsed claims superseded and kills zombie worker PIDs.
- **Cancellation.** `prepare_cancel` durably observes cancellation (`liveness.report_cancellation`), releases the fence, appends `work.cancelled`, acks the outbox — then (and only then) the guarded row write (`only_if_active`) and the route's terminal re-check. Cancel of already-accepted work is refused at three levels.
- **ArtifactHandles are transport.** Acceptance descriptors are built after `_resolve_artifact_payload`; the publication stores hashes/lengths of resolved logical output. No handle pathname is accepted truth (proved by `test_resolved_handle_result_crosses_acceptance`).

## 3. Crash/restart convergence matrix

`coordinator.recover()` (run at startup, before the dispatch loop starts):

| Crash boundary | Durable state left | Recovery action | Converged state |
|---|---|---|---|
| Row committed, authorize never ran | row `pending`, `queue_backend='kernel'`, no work | adoption: authorize | work pending → dispatched |
| Authorized, not dispatched | outbox `pending` | nothing needed | dispatched |
| Claimed, owner died (lease lapsed) | outbox `in_flight`, expired lease | watchdog/recovery: release fence → requeue (one lapse retry always allowed; then retry budget) or terminal fail | takeover (token+1) or `failed` |
| Result written, acceptance never committed | files on disk, leased work | lease lapse path → requeue → re-execution (at-least-once) | accepted exactly once |
| Accepted, outbox ack lost | publication + lease `accepted`, outbox `in_flight` | `complete_work` repair | acked, done |
| Accepted, row projection lost | publication, outbox done, row non-terminal | `_project_publication` (re-reads primary output file via descriptor `result_path`) | row `completed` |
| Cancel/fail event committed, projection lost | `work.cancelled`/`work.failed` event, row non-terminal | `_project_terminal_event` + ack | row terminal |
| Pre-kernel non-durable rows | `queue_backend IS NULL`, pending/processing | swept to `failed` ("Interrupted by server restart") — unchanged legacy semantics | failed |

## 4. Residual limitations (deliberate)

- **SSE stays the compatibility surface.** No PR79 signed cursors/auth epochs; `job_events` remains an in-memory poll merged with the durable row. Slow consumers are proven not to couple to execution (`test_slow_sse_consumer_does_not_block_execution`), but reconnect state comes from the row, not an event cursor.
- **Thread-backend zombies cannot be killed**, only fenced out: a wedged thread that wakes after takeover produces files and a late result that acceptance rejects (`StaleFenceError`) and the row guard refuses to project. Orphaned output files may exist on disk until the successor overwrites the same deterministic per-job paths; they are never user-visible (download requires the accepted row).
- **Process-backend generation identity is parent-side.** A zombie process worker that un-wedges after takeover is killed by the watchdog when its PID is known; a result event racing that kill is still funneled through the current claim and the accept-time cancellation check. Worker-side generation tagging is left to a later slice if evidence demands it.
- **Legacy queue metadata is compatibility-only** in kernel mode: `enqueue/mark_started/mark_terminal` are not on the dispatch path; their columns are cleared/maintained only by the legacy runtime (`MARKER_KERNEL_RUNTIME=0`).
- **Lapse-retry policy:** a lease lapse (crash/hang) always earns one retry even at `max_retries=0` (legacy recovery parity: a crash is not an executed failure); the explicit retry budget then governs. Executor failures use the strict budget (`attempts < max_retries`).
- PR69 (dynamic admission/model leases), PR70 (source identity), PR79 (event protocol) are untouched by design.

## 5. Review-question answers (plan Section 15)

1. **What durable fact makes a submission executable?** The kernel outbox row (`conversion.execute` intent) committed in the same transaction as the `conversion-request.<job_id>` `NativeObjectRecord`. The `ConversionJob` row alone executes nothing.
2. **Who can create a new ownership generation?** Only `scheduler.claim_fair` (dispatch loop) and the cancel path's `fencing.acquire`/`release` behind it. Legacy recovery is disabled in kernel mode.
3. **Can any path call an executor without that authority?** No: `submit_job` is only invoked by `_launch` after a successful claim (or by legacy mode, which is the documented kill-switch runtime).
4. **What must the owner prove to renew?** Current owner + fence token + the rotating challenge nonce issued at claim/last renewal + a strictly advancing activity counter fed only by real progress/log/status events + the bound active request id.
5. **If the worker hangs but a heartbeat helper survives?** There is no helper: renewal without fresh counter evidence is skipped by construction, so the lease lapses and the watchdog takes over.
6. **What makes REST report completed?** The row projection that runs strictly after `fencing.accept` committed the unique publication (ordering asserted in tests via `accepted_at <= completed_at`).
7. **Can a stale worker write output files?** Yes (threads cannot be killed), but download/assets require the accepted row, and the successor overwrites the same per-job output paths; stale bytes cannot become accepted truth (fence) or user-visible truth (row guard).
8. **Duplicate successful result?** Same result hash converges (`already_accepted`, no second event); duplicate submission authorizes exactly one work item.
9. **Divergent duplicate result?** `PublicationConflictError` surfaced; accepted state and row unchanged.
10. **What survives restart?** Kernel commits/outbox/leases/publications/events/progress, plus the compatibility rows; in-memory smoothing does not (and cannot matter).
11. **Can a slow SSE client increase write-lock duration?** No — SSE polls in-memory dicts; all kernel writes are short busy-retried transactions on the serving loop(s), never held open by a reader.
12. **Which legacy queue fields still matter?** `queue_backend` (migration/audit marker), `max_retries` (retry budget), `retry_count` (legacy-mode only). None are authoritative in kernel mode.
13. **Does the process backend still use ArtifactHandles?** Yes — PR68A producer staging and strict parent resolution are untouched; only what happens after resolution changed.
14. **Is the accepted result independent of handle lifetime?** Yes — the publication describes resolved logical output; blobs are consumed (unlinked) before acceptance is even attempted.
15. **Still missing for PR69/PR79?** Dynamic token/memory admission and model leases/residency (PR69); signed cursors, auth-epoch invalidation, agent-facing event protocol (PR79).

## 6. Test and validation evidence

Environment: Windows (win32), Python 3.11, local dev box. Exact commands (run from `backend/`):

```text
python -m pytest tests/test_kernel_runtime.py -q                      # 27 passed (final code)
python -m pytest tests/test_kernel_runtime.py tests/test_task_manager.py \
  tests/test_durable_queue.py tests/test_convert.py tests/test_convert_retry.py \
  tests/test_gpu_worker.py tests/test_artifact_handles.py -q          # 250 passed (final code)
python -m pytest -q                                                   # full backend suite (below)
```

- Full backend suite **before** review fixes: `1815 passed, 3 skipped` (583 s).
- Full backend suite **after** review fixes: recorded below in Section 6.1 (this doc is written before that run finishes; the commit message and this section carry the final numbers — no claim of "tests pass" without them).
- Migration/drift gates: `tests/test_database_migration.py` (M1–M14 authority suite) and `tests/test_kernel_migration.py` are part of the full run. No schema changes were made in PR67B, so `EXPECTED_HEAD`/`KERNEL_TABLES` constants are untouched.
- Known pre-existing local limitation: `python -m app.cli provenance --verify` fails on this dev box (stale venv vs lockfiles, ~81 pins) and passes in CI from a fresh lock install — unchanged since PR63A.

### 6.1 Final full-suite result

- Full backend suite (all logic in place): `python -m pytest -q` →
  **1819 passed, 3 skipped, exit 0** in 557.68 s (2026-08-16, Windows dev box).
- The three final cosmetic edits (a captured `failure_message` local in
  `_run_conversion`'s failure path, removal of an unused test import and
  unused test locals — no behavior change) postdate that run; the touched
  files were rerun on the exact committed code:
  `tests/test_kernel_runtime.py` → 27 passed, and the full adjacent
  selection above → 250 passed.
- New PR67B suite stability: `tests/test_kernel_runtime.py` run 3×
  consecutively early in development → 26/26/26; the final 27-test file
  passed on every subsequent run.

## 7. Representative authority trace (happy job, from test run)

From `test_completed_only_after_fenced_acceptance` (`tests/test_kernel_runtime.py`):

```text
job_id:            job-happy-1
kernel work_id:    1 (outbox row, work_kind=conversion.execute, state=done)
claim:             work.claimed event, fencing_token=1, challenge nonce seeded
liveness:          renewal ticks on activity; kernel_progress coalesced row
acceptance:        kernel_publications row (result_hash over the bounded
                   descriptor: result_text sha256, result_file bytes/sha256,
                   formats list, result_path)
events:            work.claimed -> work.accepted (per-work sequence, one each)
compatibility row: completed, result_text/result_path/progress=100,
                   accepted_at <= completed_at (ordering asserted)
```

Stale-worker trace (`test_late_success_cannot_complete_or_renew`): token 1 (worker A) lapses → watchdog requeue → dispatch reclaims as token ≥2 (worker B) → B accepts → A's late `accept_work(token=1)` raises `StaleFenceError`; A's late renewal raises; exactly one publication; row completed by B only.

ArtifactHandle trace (`test_resolved_handle_result_crosses_acceptance`): >1 MiB text field staged (envelope, 1 handle) → strict parent resolution rebuilds logical payload → descriptor `result_text.sha256` equals sha256 of the resolved big text, `artifact_handles` absent from the accepted publication → row completed → blobs directory empty (consumed).
