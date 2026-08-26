# Invariant 25 — Held-Out Routing Promotion Evidence Closure

**Branch:** `markerui-v2` · **Slice:** V3.2 amendment 23C.3, invariant 25
**Plan:** `planning/v2/2026-08-26-marker-ui-v2-heldout-routing-promotion-plan.md`
**Readiness before:** 52/62 proven, 0 failed, 10 without full evidence
**Readiness after:** 53/62 proven, 0 failed, 9 without full evidence

This is a closure record, not a proof: the proofs are the executable bindings
in `readiness-ledger.json` and the pinned artifact under
`docs/reference/measurements/`.

---

## 1. What was built

**Workstream A — semantic actors and the frozen question**

- `backend/app/eval/routing_promotion/contract.py` —
  `PromotionContract` (`marker.routing_promotion.contract.v1`,
  `inv25-routing-promotion-v1`), content-hashed and frozen
  (`2026-08-26T00:00:00+00:00`) before any final holdout outcome was
  consumed. It binds the three actors, the scalar utility weights
  (+1 correct accept / −2 ordinary false verification / −10 catastrophic
  false verification / 0 abstain), the masterplan 7A.3/14C.5 materiality
  rule (fixed rules capturing ≥ 98% of candidate utility keep authority),
  the 0.10 catastrophic-error ceiling, and the rule-of-three exposure floor
  (29 zero-failure trials at 95% confidence; `ln(0.05)/ln(0.90)` = 28.43).
- `backend/app/eval/routing_promotion/actors.py` — `ActorRegistry` mapping
  each masterplan role to an executable policy with per-actor rationale and
  governing citations: candidate = `dependency_aware_policy` (7B.2 level 3
  EVC-based continuation; 7C.1 empirical-risk-band EVC estimator whose
  promotion must be gated), fixed rules = `deterministic_source_native_only`
  (7B.4 deterministic native-vs-OCR rule), best single engine =
  `best_single_witness` (7B.4 fixed best-single-engine route). The registry
  fails closed if a declared policy is not executable in the current tree.
- Offline containment proven by test: the dependency-aware candidate policy
  has **no import outside `app/eval`** (the production kernel maintains its
  own conservative verification gate), so routing authority stays
  shadow/offline regardless of the gate's decision.

**Workstream B — genuinely independent evaluation evidence**

- `backend/app/eval/routing_promotion/population.py` — the final holdout
  population `inv25-final-holdout-v1`: procedurally generated from declared
  document-family semantics (no RNG, arithmetic label rules), fresh witness
  families and dependency structures unseen in PR75/PR82A (correlated
  rend-p7 renderer pair preserved), 39 matched / 25 shifted / 3 thin
  samples with 22 catastrophic-opportunity samples and a rotated-degradation
  shift slice per masterplan 14B.8.
- Contamination controls, all machine-checkable and fail-closed: a declared
  exclusion manifest pinning the PR75 fixture and the consumed PR82A
  adversarial corpus by semantic identity (stale manifest = invalid
  evidence), sample-id collision detection, renamed-sample content
  collision detection, witness dependency-key overlap detection, and proof
  that neither PR75 nor PR82A can serve as its own pristine holdout.

**Workstreams C/D — paired comparison and the fail-closed gate**

- `backend/app/eval/verification_risk/baselines.py` —
  `dependency_aware_evaluation` public per-slice/per-sample view of the
  candidate policy; `evaluate_baselines` now aggregates exactly this
  computation (pinned PR75 conformance identities unchanged).
- `backend/app/eval/routing_promotion/decision.py` — `evaluate_promotion`:
  runs candidate, fixed rules, and best single engine on identical samples,
  derives utility under frozen weights, bounds catastrophic-accept risk with
  the exact Clopper-Pearson upper bound (reusing the PR88 authority), and
  classifies along the frozen ordering `invalid_evidence` →
  `insufficient_evidence` → `shadow` → `promote`. Every evidence-validity
  failure (temporal ordering, contamination, stale manifest, missing slice,
  inapplicable comparator) produces a decision with closed-vocabulary
  reasons, never an exception operators can ignore. All eleven criteria are
  recorded with numeric detail even when a higher tier short-circuits.

**Workstream E — tests and adversarial controls**

- 62 bound test nodes (63 focused tests) across four unit suites and one
  conformance suite,
  covering: reachable promote path (the gate is not hardcoded refusal),
  best-single-wins, sub-material 98% rule, matched-win/shifted-loss,
  catastrophic miss hidden by 85% aggregate accuracy, thin-support
  zero-failure honesty, consumed-corpus rejection (PR75 and PR82A),
  stale-manifest/missing-slice/missing-comparator failures, temporal
  ordering, deterministic rerun with runtime excluded from identity, and
  known-bad-policy discrimination (naive majority accepts the model-only
  consensus traps the candidate refuses).

**Workstream F — evidence closure**

- `backend/scripts/bench_inv25_routing_promotion.py` — runs the gate twice
  in-process and refuses to write unless semantic identities agree;
  runtime-annotated re-evaluation must not change the decision identity.
- `docs/reference/measurements/inv25-routing-promotion-gate.json` — the
  frozen decision artifact (`marker.routing_promotion.evidence.v1`) binding
  contract, actor-registry, population, and decision identities, the full
  paired comparison, the catastrophic assessment, leakage report,
  limitations, and reproduce commands.
- `backend/conformance/test_routing_promotion_conformance.py` — hand-checked
  constants and pinned identities; silent drift in contract, registry,
  population, policy, or artifact fails the suite.

## 2. The decision, and why invariant 25 is proven

**Final decision: `insufficient_evidence` — the candidate correctly stays
shadow, and no promotion claim is made.**

- Support: 22 catastrophic-opportunity samples vs the frozen floor of 29;
  12 zero-failure candidate exposure trials vs the required 29.
- Catastrophic bound: exact one-sided 95% upper bound `0.2209221919` at 12
  zero-failure trials vs the 0.10 ceiling — zero observed failures on thin
  exposure certifies nothing (14C.4), so the bound is uncertifiable and the
  study cannot support a production claim.
- The paired comparison itself is recorded and, under this population,
  favors the candidate (matched utility 33/39 = 0.846 vs fixed rules 27/39
  = 0.692, capture 0.818 < 0.98 materiality bound; best single −64/39 with
  12 catastrophic errors vs 0 for the candidate; shifted slice conservative
  abstention after the empirical gate fails) — but the invariant asks
  whether promotion is *earned*, and the frozen answer is no: the gate
  refuses, quantifies exactly what more is required (≥ 29 reviewed
  catastrophic-opportunity samples with ≥ 29 zero-failure exposure trials),
  and the candidate remains offline by construction.

The invariant's literal statement — EVC-style routing **remains
shadow/offline until** it beats fixed rules and best-single-engine
baselines under held-out shift and catastrophic utility — is demonstrated
end to end: the actors are executable and semantically mapped, the
until-condition is a frozen, fail-closed, reproducible gate evaluated on
genuinely held-out data including shift and catastrophic utility, the
current decision is not-promotion with reasons, and containment is proven.
A future promotion can occur only by clearing the same frozen bars on a
larger independent holdout, never by reinterpreting this evidence.

## 3. What was intentionally not built

- No learned router was trained or designed to make the candidate win
  (explicitly out of scope in the plan and the masterplan's 7B.7 support
  floors).
- No production/serving switch: nothing in this slice changes runtime
  behavior; the accountability registry's `routing.specialist_hybrid_bridge`
  `experimental_shadow` disposition remains correct.
- No re-solving of the other nine readiness gaps; PR75/PR82A regression
  evidence and the PR75 bench `_promotion` helper are untouched.

## 4. Reproduction commands

```bash
# focused suites (63 tests; 62 bound nodes)
python -X utf8 -m pytest backend/tests/test_eval_routing_promotion_contract.py \
  backend/tests/test_eval_routing_promotion_actors.py \
  backend/tests/test_eval_routing_promotion_population.py \
  backend/tests/test_eval_routing_promotion_decision.py \
  backend/conformance/test_routing_promotion_conformance.py -q

# regenerate the frozen artifact (self-verifies reproducibility)
python -X utf8 backend/scripts/bench_inv25_routing_promotion.py --write

# readiness: execute all ledger bindings and regenerate evidence-run + reports
python backend/scripts/readiness_audit.py --mode run-evidence
python backend/scripts/readiness_audit.py --mode integrity

# broad backend regression
cd backend && python -m pytest tests conformance -q
```

## 5. Verification record

- Focused suites: **63 passed, 0 failed** (15 contract + 12 actors +
  13 population + 17 decision + 6 conformance; 62 of them are bound ledger
  nodes — the unbound actor rationale-tamper check is extra).
- Subsystem regression: verification-risk eval, PR82A dependence, PR75
  conformance vectors, and calibration applicability suites all green after
  the `baselines.py` accessor refactor (pinned semantic identities
  unchanged).
- Cross-process determinism: population and decision identities identical
  under different `PYTHONHASHSEED` values and separate invocations; only
  `generated_at_utc`, `git_sha`, and wall-clock floats vary (non-semantic
  by convention, matching the PR75 artifact).
- Readiness: `--mode run-evidence` regenerated — **53/62 proven, 0 failed**,
  both invariant-25 bindings (62-node test binding and the measurement
  binding with 15 pinned artifact scalars) passed; `--mode integrity` clean
  (no structural, stale-scope, digest, expectation, or claim-mismatch
  findings; the NOT_READY verdict reflects the nine remaining environment-
  gated gaps, unchanged by this slice).
- Broad backend/conformance run: recorded in §5.1.

### 5.1 Final gate results

- Broad backend + conformance suite (`cd backend && python -m pytest tests
  conformance -q`): **3889 passed, 208 skipped, 0 failed** in 7:04 (the
  PR84C broad run was 3825/208/1; the delta is this slice's 63 focused
  tests plus the PR84C-recorded liveness flake passing here); the 208 skips
  are the standing environment-gated PostgreSQL/Docker/CUDA cohorts).
- Readiness evidence-run flake investigation (recorded exactly as it
  happened): the FIRST `--mode run-evidence` regeneration on this slice's
  tree failed two sqlite kernel-runtime timing nodes on invariant 38's
  binding —
  `test_kernel_runtime.py::TestCancellation::test_cancel_during_execution_beats_late_result[sqlite]`
  (assert 'pending' == 'done') and
  `test_kernel_runtime.py::TestStaleWorker::test_late_success_cannot_complete_or_renew[sqlite]`
  (sqlite3.OperationalError: database is locked). This is **not** the
  PR84C-known liveness flake. Triage: the full
  `test_kernel_runtime.py` file passed standalone immediately after
  (29 passed, 29 env-gated skips); a control `run-evidence` on the
  pre-slice tree (stash + regenerate) produced 52/62 proven with 0 failed
  on the same runner; and the final `run-evidence` on this slice's tree at
  identical default settings passed all 97 bindings including both nodes.
  Conclusion: transient under-load timing flake in async sqlite
  cancellation tests whose xdist scheduling shifted with the 63 new bound
  nodes; no code path is shared with this slice (which touches only
  `app/eval/routing_promotion/*`, one identity-preserving accessor in
  `app/eval/verification_risk/baselines.py`, committed evidence, and test
  files). If it recurs, treat as new until proven otherwise.

## 6. Remaining 23C gaps outside this slice

Nine invariants remain without full evidence, all pre-existing and
untouched by this slice: 1, 3, 4, 6 (truth/persistence conformance), 13
(cross-page fragment property), 30 (real-CUDA OOM stress), 37/38 (external
destination semantics; full pressure/failure matrix), 43 (numeric
revocation SLO). None was weakened, reworded, or silently invalidated by
this work.
