# PR88 — Verification-policy operational closure (invariants 22, 23, 26)

PR88 closes the three V3.2 verification-policy readiness gaps in one
bounded slice: claim/region-relative usability (22), calibration
artifact discipline (23), and operational review policy (26). It adds
no new truth authority, no global document status, and no review queue
infrastructure.

## What is now claimed

### Invariant 22 — verification-status-relative

- `backend/app/kernel/assessment_view.py` resolves the effective
  assessment per assertion under one declared context (exact policy
  id/revision + workflow class + commit cut). Visibility uses the
  commit that carries each record, so later commits never rewrite what
  an earlier cut knew. Assertions with no matching assessment resolve
  to `unresolved_unavailable` — never a neighbor's outcome.
- `summarize_regions` preserves every usability class as explicit
  counts plus one document-state label. There is deliberately no
  document-wide verified/unverified boolean.
- Executable proof: `backend/tests/test_verification_region_status.py`
  (kernel seam: two regions in one commit, bounded reads, policy and
  snapshot relativity, append-only history) and
  `backend/tests/test_extraction_region_usability.py` (extraction seam:
  accepted region committed and usable, conflicted region cannot reach
  the claim layer without review, adjudication leaves the accepted
  region's committed identity untouched).

### Invariant 23 — calibration-artifact-discipline

- `backend/app/eval/verification_risk/applicability.py` composes a v1
  `CalibrationResult` into a versioned
  `marker.calibration.applicability.v2` artifact with first-class named
  population, closed-vocabulary assumptions (label definition and
  sampling frame mandatory), machine-evaluable expiry window plus
  retest triggers, explicit support/uncertainty/shift state, and an
  exact one-sided 95% Clopper-Pearson catastrophic-failure upper
  bound. Zero observed failures serialize `zero_failures_implies_
  zero_risk: false` plus a strictly positive bound — never risk = 0.
- The v1 calibration contract and the kernel evidence-record schema
  are unchanged; old artifacts keep their original meaning.
- The high-risk gate now fails closed when risk evidence has no
  `expires_at` or does not name its population
  (`metadata.calibration_population`). Committed records keep their
  historical meaning; only NEW high-risk authorization is stricter.
  This closes the `expires_at` follow-up recorded in PR75's residual
  limits. A latent gate bug (committed-row risk-evidence payloads were
  never JSON-decoded, so cross-commit evidence reuse always failed) is
  also fixed and covered.
- Executable proof: `backend/tests/test_calibration_applicability.py`,
  `backend/tests/test_kernel_verification_risk_integration.py` (new
  expiry/population rejections).

### Invariant 26 — review-policy-operational

- `backend/app/extraction/review_ops.py` adds the narrowest truthful
  missing piece: durable transition records (non-authoritative
  native-object views) written at the authoritative moments — a run
  persisting review-required fields, a decision committing (atomically
  with its decision record), a stale refusal, and a bypass refusal.
- `derive_review_metrics` computes coverage, dwell (decision minus
  first-required, deterministic via injectable clock), outcome
  distribution, backlog, stale rejections, and bypass rate from
  committed transitions alone; reload reproduces identical accounting.
  Zero denominators stay explicit undefined states.
- `validate_review_ops_report` fails closed on invented zeroes,
  inconsistent backlog, disordered dwell, and unknown shapes.
- Executable proof: `backend/tests/test_review_ops.py` (lifecycle,
  replay idempotency, stale/bypass refusals, reload, zero-denominator
  honesty) and the integrated measurement artifact
  `docs/reference/measurements/pr88-review-policy-ops.json` produced by
  `backend/scripts/bench_review_policy_ops.py`, re-validated by
  `backend/tests/test_review_policy_ops_artifact.py`.

## Reproduce

From repository root:

```text
python backend/scripts/bench_review_policy_ops.py --write
python -m pytest backend/tests/test_calibration_applicability.py backend/tests/test_verification_region_status.py backend/tests/test_extraction_region_usability.py backend/tests/test_review_ops.py backend/tests/test_review_policy_ops_artifact.py backend/tests/test_kernel_verification_risk_integration.py -q
```

The bench drives the real kernel commit/publication/query/extraction/
review seams on SQLite with injected deterministic clocks (no
wall-clock sleeps) and fails closed if any check or the validator
fails.

## Regression record

Session environment: Windows, CPython 3.11.9, SQLite (aiosqlite),
`python -X utf8` from `backend`.

- Focused PR88 suites (calibration applicability, region status kernel
  + extraction, review ops, artifact honesty, kernel risk + gate
  integration, verification-risk conformance): all green before each
  commit in this session.
- Readiness evidence run at the evidence head (`5fa39de`): every bound
  binding executed — 84 passed, 4 `skipped_env_gated` (docker/postgres
  industrial partial bindings, never backing a proven claim), 0 failed;
  auditor derives 47/62 proven, integrity passes.
- Full backend regression (`tests` + `conformance`):
  **3625 passed, 208 skipped, 0 failed** in 36m47s. The 208 skips are
  the branch's known environment-gated lanes (GPU/CUDA, docker
  postgres/S3, source-ingress timeouts); no lane was intentionally left
  unexecuted beyond those gates.

## Bypass semantics

A bypass is an attempt to obtain acceptance without the evidence the
policy requires: re-adjudicating an already-accepted field, or
accepting a field with no grounded candidate. Both are refused at the
review seam and leave a durable `review_bypass_refused` transition.
Leaving a review-required field unreviewed is NOT a bypass — it is an
explicit unresolved backlog state. The strongest structural proof is
that the only authoritative acceptance path (`_persist_result`) commits
claims for accepted fields alone, so a review-required value cannot
silently become kernel truth.

## Non-claims

- Fixture-scale operational accounting; no production staffing
  capacity, queue infrastructure, or human-time claim.
- Dwell is the declared deterministic measure (decision timestamp minus
  first-required timestamp), not human review time.
- Calibration support is the committed conformance corpus, not
  production traffic.
- No claim about routing promotion (invariant 25): the held-out,
  shifted, catastrophic-utility promotion experiment remains the next
  research/evaluation slice, exactly as the verification-policy
  closure plan records.

## Handoff

Follow-ups, in priority order:

1. **Invariant 25 (routing promotion)** — genuinely held-out shift +
   catastrophic-utility evaluation; do not smuggle into an
   implementation session.
2. Workflow-specific review capacity/service-level targets can now be
   SET as product decisions on top of the measurable burden PR88
   exposes; no universal threshold was invented here.
3. The dwell measure generalizes to wall-clock review latency once a
   production review UI writes decisions; the seam (`review_clock`) is
   already injectable.
