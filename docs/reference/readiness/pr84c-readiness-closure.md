# PR84C — 23C.7 Readiness Closure: Capability Accountability, Claim Discipline, Final Displacement Test

**Branch:** `markerui-v2` · **Slice:** V3.2 amendment 23C.7, invariants 59 / 60 / 62
**Plan:** `planning/v2/marker-ui-v2-readiness-claims-displacement-implementation-plan.md`
**Readiness before:** 49/62 proven, 0 failed, 13 without full evidence
**Readiness after:** 52/62 proven, 0 failed, 10 without full evidence (see §6)

This is a closure record, not a proof: the proofs are the executable bindings in
`readiness-ledger.json` and the pinned artifacts under `docs/reference/measurements/`.

---

## 1. What was built

**Invariant 59 — capability/subsystem accountability**

- `backend/app/eval/accountability/capability_matrix.py` — fail-closed record
  contract: every capability must carry disposition, complexity-adjusted utility
  basis with evidence identity, support owner domain, a rollback path with an
  AST-verified pytest verification node, an expiry/retest boundary, and a
  machine-evaluable kill condition.
- `backend/app/eval/accountability/inventory.py` — the authoritative 22-subject
  in-scope population with an explicit excluded-category policy (why a category
  is *not* in the invariant-59 population is itself a validated record).
- `backend/app/eval/accountability/population.py` — the populated matrix:
  11 promoted, 6 experimental/shadow, 4 non-promoted, 1 disabled. Completeness
  validation enforces the inventory↔record bijection, evidence SHA-256 digests,
  and rollback-node AST existence.

**Invariant 60 — scoped leadership claims**

- `backend/app/eval/accountability/leadership_claim.py` — the claim contract:
  all nine governing dimensions mandatory, exact-set comparator equality,
  stale/superseded evidence cannot support affirmative claims, anti-
  universalization disclaimer, and pure-Python one-sided 95% upper-bound
  derivations (rule of three, exact binomial/Clopper-Pearson, Poisson, Wilson)
  so a declared bound below the honest mathematical bound is rejected.
- `backend/app/eval/accountability/claims.py` — the authoritative registry plus
  executable audit: SHA-256 + metric-pointer deep verification of evidence
  bindings against the committed artifacts, strict comparator containment in
  the artifact's real system population, review-burden discipline, and a
  release-documentation leadership-verb scanner with an explicit finite
  allowlist.

**Invariant 62 — final rational-user displacement test**

- `backend/app/eval/accountability/displacement/` — preregistration contracts,
  a fail-closed PR80B adapter (missing metrics are never defaulted to zero),
  and a deterministic decision engine with a fairness gate, dangerous-failure
  budget, explicit reason-to-leave ledger, and cryptographic re-derivation
  binding (`validate_persisted_decision`).

**Evidence and binding**

- `backend/scripts/bench_pr84c_accountability.py` — executes all three audits
  in-process and writes:
  - `docs/reference/measurements/pr84c-capability-accountability-evidence.json`
  - `docs/reference/measurements/pr84c-leadership-claims-evidence.json`
  - `docs/reference/measurements/pr84c-displacement-decision-evidence.json`
- Ledger entries 59/60/62 gained full-coverage test bindings (121 accountability
  test nodes) and full-coverage measurement bindings pinning the artifact
  counts, scenarios, and verdicts; the pre-existing partial measurement
  bindings (PR81B/PR82/PR80B) remain as supporting evidence.

## 2. What was intentionally not built

- No runtime auto-shutdown machinery for kill conditions: kill conditions are
  objective, evaluable thresholds over committed evidence; the accountability
  state machine must not represent a killed/expired capability as currently
  supported, but no daemon was added.
- No new benchmark corpora, no live provider calls, no ARM64/CUDA/topology
  work (out of slice; see remaining gaps).
- No second truth store: the registry binds to the existing PR80B/PR81A/PR82
  measurement artifacts by digest instead of copying values.

## 3. Actual outcomes and concessions

- **All three registered leadership claims are `withheld`.** The PR80B invoice
  result (17/24 doc-exact vs 0/24 for both specialists, zero hallucinations)
  would justify a scoped `beats` claim, but the evaluation was retrospective
  on a 24-document synthetic slice without prospective preregistration or
  population statistical bounds, so fail-closed discipline records `withheld`.
- **Universal document superiority is explicitly not claimed**, and that
  withholding carries no evidence binding at all: no finite artifact can bound
  an unbounded population, so binding one would launder provenance into
  support.
- **Final displacement decision: `marker_retained`** on the declared invoice
  workflow (frozen retrospective PR80B replay). Fairness gate passed; zero
  blockers. The LLM specialist's raw-generative-coverage reason to leave is
  recorded as **measured** (its advantage is unusable as accepted authority on
  this corpus: 0% evidence coverage, 1 fabrication, 2 conflicts, 3 silent
  contradictions); no reason is currently `conceded` or `integrated` on this
  workflow.
- Claim comparator identifiers are the exact system keys of the bound
  measurement artifacts (e.g. `visual-dense:openai/clip-vit-base-patch32`),
  never friendlier renames.

## 4. Reproduction commands

```bash
# focused accountability suites (121 tests)
python -X utf8 -m pytest backend/tests/test_eval_capability_matrix.py \
  backend/tests/test_eval_leadership_claim.py \
  backend/tests/test_eval_accountability_inventory.py \
  backend/tests/test_eval_accountability_displacement_contracts.py \
  backend/tests/test_eval_accountability_displacement_decision.py \
  backend/tests/test_eval_accountability_displacement_pr80b.py \
  backend/tests/test_eval_accountability_displacement_compat.py \
  backend/tests/test_eval_accountability_claims.py -q

# regenerate the three PR84C measurement artifacts
python -X utf8 backend/scripts/bench_pr84c_accountability.py

# readiness: execute all ledger bindings and regenerate evidence-run + reports
python backend/scripts/readiness_audit.py --mode run-evidence
python backend/scripts/readiness_audit.py --mode integrity

# broad backend regression
cd backend && python -m pytest tests conformance -q
```

## 5. Verification record

- Focused accountability suites: **121 passed, 0 failed** (includes 2 new
  adversarial tests added during review: comparator-absent-from-population and
  population-free-artifact rejection).
- PR84C bench: all three artifacts `verdict=…_proven` / `…_executed`, every
  named scenario `passed`.
- Readiness integrity: clean (no structural, stale-scope, digest, expectation,
  or claim-mismatch findings) after `--mode run-evidence` regeneration.
- Broad backend/conformance and readiness-suite results: recorded in §5.1.

### 5.1 Final gate results

- Broad backend + conformance suite (`python -m pytest tests conformance -q`):
  3825 passed, 208 skipped, and exactly 1 failure —
  `tests/test_kernel_liveness.py::test_external_request_liveness_is_bounded_by_request_activity`,
  a timing-sensitive test that passed standalone immediately after, passed on
  three consecutive full-file reruns, and passed inside the freshly executed
  readiness evidence-run on the same tree. It shares no code path with this
  slice (which touches only `app/eval/accountability/*`, readiness runner
  argument passing, and committed evidence). Recorded as an under-load flake,
  not a slice regression.
- Readiness tests (`test_readiness_auditor.py`,
  `test_readiness_integration.py`, `test_readiness_inventory.py`): 62 passed,
  0 failed — including the new runner regression test for pytest `@argsfile`
  node passing (the full bound-node population exceeds the Windows
  CreateProcess command-line cap, which previously aborted `--mode
  run-evidence`).

## 6. Remaining 23C gaps outside this slice

Ten invariants remain without full evidence, all pre-existing and untouched by
this slice: 1, 3, 4, 6 (truth/persistence conformance), 13 (cross-page
fragment property), 25 (held-out routing promotion bar), 30 (real-CUDA OOM
stress), 37/38 (external destination semantics; full pressure/failure matrix),
43 (numeric revocation SLO). None was weakened, reworded, or silently
invalidated by this work.
