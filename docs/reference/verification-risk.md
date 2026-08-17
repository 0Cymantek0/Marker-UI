# PR75 — Verification-risk evidence

PR75 adds a small, versioned evidence seam for dependency disclosure,
empirical witness risk, and conservative high-risk promotion. It does not add
a global claim status, model training, routing, or a second persistence store.

## Repository boundary

- Start SHA: `17db7dcc6cbe0aceac37da5b1e0000dc4125fd01`
- Branch: `markerui-v2`
- Final SHA: see the commit carrying this document.
- Implementation commits before the evidence bundle:
  - `65dbc04` — deterministic verification-risk evaluator;
  - `f6a8c82` — immutable disclosure/risk records and pure policy;
  - `c3f919e` — transactional high-risk source-native gate;
  - `7ee6430` — modular evaluator package;
  - `100a2c1` — modular kernel risk package;
  - `f0e025f` — dependency classifier fails closed on empty/incomplete
    lineage and shared architecture/training sources;
  - `fa0ebbe` — native risk witnesses are bound to the assessed assertion's
    subject/predicate/value with a negative atomic-failure test;
  - `b32060f` — unknown identity-affecting fields are rejected at corpus
    root, sample, outcome, and nested mappings;
  - `0efd8d7` — `mixed` evidence kind no longer bypasses
    `require_independent_witnesses` in the exported policy;
  - `b53d02b` — the gated workflow rejects qualified assertions because
    it defines no anchor-to-qualifier binding.

Authority integration is narrow and inspectable. `KernelCommitService` runs
PR74 structural proof validation first, then
`check_batch_verification_risk` before any insert. The gate recognizes only
`marker.high_risk.source_native/1` with workflow
`high_risk.source_native.v1`. Risk evidence must be supported as `role=input`;
an additional source-native `native_fact` must be supported as `role=witness`.
All other workflows keep their existing PR74 behavior.

## Reproduce

From repository root:

```text
python backend/scripts/generate_verification_risk_fixtures.py --write
python -m pytest backend/conformance/test_verification_risk_conformance.py -q
python backend/scripts/bench_pr75_verification.py --write
```

The generator uses the real `DependencyDisclosureRecord`,
`VerificationRiskEvidenceRecord`, canonical identity framing, and
`app.eval.verification_risk` evaluator. The fixture generator writes only
semantic vectors. The benchmark writes runtime measurements separately.

## Contract

`backend/conformance/fixtures/verification_risk_corpus_v1.json` is a small,
hand-checkable corpus with complete, partial, and unknown lineage, aliases,
shared renderer/dependency profiles, matched and shifted slices, model-only
consensus, and insufficient support.

`backend/conformance/fixtures/verification_risk_vectors_v1.json` pins:

- canonical identity for disclosure and risk records;
- order-insensitive witness/dependency sets;
- profile and policy-revision identity changes;
- fail-closed rematerialization/unknown-field behavior;
- pair joint-error and disagreement metrics;
- calibration method/version and support state;
- all five baseline names and per-slice outcomes;
- an explicit promotion threshold and conservative decision.

Record identity excludes event `record_id`. Set-like references are sorted by
canonical bytes and duplicate members are rejected. Identity payloads contain
no binary floats; risk values use canonical decimal values in the kernel and
the evaluator keeps numeric report values separate from identity.

## Evaluation and promotion

The evaluator reports these five policies over the same sample slice:

1. deterministic/source-native-only;
2. best single witness;
3. naive majority vote;
4. correlation-blind ensemble;
5. dependency-aware policy.

Pair reports include marginal error, joint/double-fault error, agreement and
disagreement, conditional errors, disagreement-case behavior, catastrophic
joint failures, sample support, and Wilson uncertainty. Calibration reports
method/version, Brier/ECE metrics, accuracy support, and explicit
`insufficient_support` status.

Promotion threshold in this evidence bundle is deliberately narrow:

```json
{
  "min_support": 5,
  "max_false_verified_count": 0,
  "required_distribution": "matched",
  "required_dependency_status": "ok"
}
```

Measured result on the committed corpus:

| Slice | Dependency-aware status | Decision | Why |
|---|---|---|---|
| calibration-fit | `insufficient_support` | `abstain` | 2 fit samples are reported separately and never reused as held-out evidence |
| matched | `ok`, 2 false verifications / 10 | `shadow` | threshold requires zero false verification |
| shifted | `risk_bound_not_met` | `abstain` | joint-error Wilson upper bound exceeds 0.6 |
| insufficient | `insufficient_support` | `abstain` | 2 pair samples, 5 required |

Thus PR75 does not promote this dependency-aware policy. The useful
measurement/disclosure substrate remains; high-risk promotion stays
conservative. Model agreement alone is not promotion evidence.

## Measurements

`docs/reference/measurements/pr75-verification-risk.json` contains the full
machine-readable result. It records best-of-5 `perf_counter` timings for
report, baseline, pair, and calibration probes per slice. Runtime metadata is
not part of semantic identities; changing a recorded runtime cannot change a
report or baseline semantic identity.

The current corpus semantic identity is:

```text
sha256:a6cde0976cdfb22daf1900ebaf6b5446db0f52c52e9c81181b9e1aecf6df7103
```

Current committed-fixture file hashes:

- corpus SHA-256: `59D0628AAFAE91390EC48AF6E3248E3983FCAF7D17F51C7FC8837EEBA7A9BD71`;
- conformance vectors SHA-256: `30561D0250579F60962DAB1FC048C997C2159C8AA6ED92F5E29B1F9581FD2DCA`.

Regenerating vectors reproduced the exact vector-file hash. Regenerating the
benchmark changed wall-clock timings only; every per-slice semantic report
identity remained unchanged.

## Regression record

Focused PR75 runs (`python -X utf8` from `backend`):

- `tests/test_verification_risk_eval.py conformance/test_verification_risk_conformance.py` — 23 passed;
- kernel risk plus PR74 overlap (`test_kernel_verification_risk*.py`, claims/proofs/proof-inputs/claim-patches/assessment-states, `test_pr74_durability.py`, `test_kernel_proof_randomized.py`, `test_kernel_patches.py`, `conformance/test_claim_proof_conformance.py`) — 145 passed, 1 warning.

Full-suite regression was executed in file-list shards, each returning a
real exit code (no aborted runs counted). All 150 test files were covered
exactly once:

| Shard | Files | Result |
|---|---|---|
| conformance/ | 3 conformance files | 75 passed |
| 1 | `test_agent_contract` … `test_cli_mcp` (20) | 405 passed |
| 2 | `test_cli_v1` … `test_gpu_workers` (21) | 344 passed |
| 3 | `test_health_ready_metrics` … `test_local_ocr` (17) | 251 passed |
| 4 | `test_database_migration` (1) | 33 passed |
| 5 | `test_kernel_claim_assessment_states` … `test_kernel_liveness` (19) | 229 passed |
| 6 | `test_kernel_migration` … `test_kernel_proofs` (11) | 157 passed |
| 7 | `test_kernel_reading_order` … `test_kernel_source_acquisition` (9) | 145 passed |
| 8 | `test_kernel_source_anchors` … `test_kernel_source_store` (5) | 78 passed |
| 9 | `test_kernel_source_ingress` (1) | 7 passed |
| 10 | `test_kernel_verification_risk` … `test_models` (13) | 102 passed, 3 skipped |
| 11 | `test_native_converters` … `test_policy_roots` (10) | 88 passed |
| 12 | `test_pr72_durability` … `test_schemas` (10) | 75 passed |
| 13 | `test_secrets` … `test_windows_launcher` (10) | 233 passed |

Shard totals: **2222 passed, 3 skipped, 0 failed**, matching the 2225
collected tests. The PR74 baseline recorded 2155 passed, 8 failed,
4 skipped; its eight known failures (seven source-ingress timeouts and
one Windows migration-lock race) did not reproduce in this session's
environment — shard 9 and shard 4 passed cleanly. No new regression is
present. These per-shard numbers are not combined into any other
aggregate claim.

A second read-only adversarial audit of the full diff then raised two
P2 findings, fixed by `0efd8d7` and `b53d02b` with five new focused
tests. After those fixes the focused evaluator/kernel/PR74 overlap and
conformance set was rerun: 173 passed, 1 warning, and the fixture
generator plus benchmark were regenerated byte-identical — corpus,
vectors, semantic identities, and every reported number above are
unchanged by the audit fixes.

## Residual limits

- Small fixture support cannot establish production-level risk guarantees.
- Wilson bounds are evidence support, not proof of mathematical independence.
- Unknown or partial lineage remains conservative; provider/model labels do
  not create independent evidence.
- Matched evidence does not silently transfer to shifted distributions.
- PR75 does not replace PR74 structural proof validation or add domain
  validators, publication sets, retrieval, or production routing.
- The promotion decision is fixture-scoped. A later release needs frozen
  held-out data, review capacity, expiry/retest rules, and a new evidence
  identity before changing policy.
- Recorded follow-ups from the final adversarial audit, none reachable
  on the current authoritative commit path: `expires_at` is optional, so
  source-native evidence without expiry stays valid forever; the kernel
  disclosure record lacks a `cropper_profile` dimension that the
  evaluator treats as first-class; the evaluator's
  `dependency_key` omits `model_family`; runtime-key exclusion from
  semantic identity is a name denylist rather than an explicit
  sub-object; witness predictions are not finiteness-validated; and the
  gate loads all committed proof supports per commit rather than
  filtering by holder.
