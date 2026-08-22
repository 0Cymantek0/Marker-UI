# PR69 implementation handoff — runtime admission and model leases

**Branch:** `markerui-v2`
**Session date:** 2026-08-22
**Scope executed:** PR69 per `planning/v2/Marker-UI-v2-PR69-runtime-admission-model-leases-plan.md`
**Readiness outcome:** invariant 31 **proven** (full coverage); invariant 30 implementation complete, readiness claim deliberately **no_evidence / partial coverage** pending a real-CUDA characterization run. Ledger: 37 proven / 0 failed / 25 no-evidence, verdict NOT READY (unchanged overall, one gap closed).

## What landed (commit order)

| Commit | Content |
|---|---|
| `95d8893` | Preflight: hermetic readiness integrity via committed release-governing extract of amendment 23C + regeneration script + anti-tamper tests |
| `085cb98` | `runtime_capacity.py`: resource profiles, pinned-preprocessor demand estimation, capacity ledger, residency leases, coordinator (admission tickets, OOM feedback, protective cooldown, observations) |
| `34a3125` | Wiring: worker admission gate + runtime event channel, MarkerService residency observations + safe `release_models` + OOM feedback hook, TaskManager thread-path admission with explicit ticket ownership |
| `55b0231` | Test layers 1–2 (deterministic admission arithmetic + real-thread races) |
| `01daa04` | Test layers 3–4 (worker-seam integration incl. converter-not-entered proof + OOM failure injection) |
| `38feb44` | Characterization harness + committed estimate-mode artifact + reference doc |
| `c033aa6` | Readiness ledger bindings for 30/31 + regenerated evidence run and reports |

## Resource lifecycle (single authority preserved)

Kernel claim/dispatch stays the only durable work authority. Admission
answers one question at the execution boundary: can this runtime instance
safely start this scarce execution now. `gpu_worker.worker_run_job` and
`TaskManager._run_conversion` gate the converter behind
`coordinator.admit()` (estimate -> atomic reservation + model lease) and
settle the ticket on every terminal path (success / failed / oom /
cancelled / abandoned / shutdown). Process-worker crashes die with their
process-local leases; kernel fencing reconciles the work — no
cross-process lease resurrection exists, deliberately.

## Honest residual gaps (do not paper over these)

1. **Invariant 30 full proof needs a real-CUDA dynamic-resolution stress.**
   The RTX 4050 (6 GB) is present but the active venv is CPU torch
   (`2.7.0+cpu`). Next step: build a cu126 venv from
   `backend/requirements-gpu.txt`, run
   `backend/scripts/runtime_admission_characterize.py --mode cuda`, then a
   real marker-model stress, and tighten the declared coefficients per
   profile. Only then promote invariant 30 beyond partial coverage.
2. **Envelope coefficients are declared-conservative, not measured**
   (`ADMISSION_WEIGHTS_BOUND_BYTES` 3 GiB, 24 KiB/recognition-token,
   250 crops/MP, per-slice/per-chunk costs). They currently refuse dense
   full-batch A4 work on a 6 GB card — truthful (that population OOMs
   today) but over-strict; measurement will relax them per profile.
3. **Cold-load timing of the full marker model dict** is structurally
   observable (events + coordinator observations) but not yet captured in
   a committed measurement artifact.
4. Invariant 30's OOD bound (`ADMISSION_MAX_PAGE_LOWRES_PIXELS`, 30M px)
   is a declared policy constant, not a characterized population bound.

## Known design decisions a reviewer may want to confirm

- **Unreadable inputs skip admission** (missing file / non-marker kind /
  unparseable document): no valid marker execution exists to protect, and
  the converter's own truthful failure owns the outcome. Valid-but-anomaly
  inputs still take the exclusive safe path. This was required to keep
  kernel requeue semantics converging when an input can never parse.
- **Unknown/OOD demand takes an exclusive reservation** (whole activation
  budget, runtime otherwise idle) under `ADMISSION_UNKNOWN_POLICY=safe_profile`;
  `reject` is the alternative policy.
- **Volunteer self-eviction**: `release_models()` may surrender the calling
  job's OWN lease (hybrid-OCR low-VRAM protocol) but never another
  borrower's; on drain timeout it raises rather than evicting.
- **Device probe and version identity never import torch in the parent**
  process (this was also the root of a kernel-suite timing flake).
- **Batch halving after OOM is an explicit profile transition** — the
  resolved batch vector (wrapper + foundation) is part of the profile
  fingerprint, so lowered throughput/memory behavior invalidates old
  envelopes instead of hiding as a global mutation.

## Suggested next slice

Per the plan's section 22: the economics/operations cluster
(invariants 57–62) — this session's cold/warm/queue/admission
observations and the characterization harness are its measurement inputs.
Connector event semantics (invariant 42) remains the other independent
candidate.

## Reproduction

```bash
python backend/scripts/readiness_audit.py --mode run-evidence
python backend/scripts/readiness_audit.py --mode integrity   # exit 0 from a clean checkout
python backend/scripts/runtime_admission_characterize.py --mode estimate \
  --output docs/reference/measurements/pr69-admission-estimate.json
python -m pytest backend/tests/test_runtime_capacity.py \
  backend/tests/test_runtime_capacity_races.py \
  backend/tests/test_runtime_admission_integration.py -q
```
