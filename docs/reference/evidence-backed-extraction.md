# Evidence-Backed Structured Extraction (PR80A)

**Status:** PR80A vertical slice merged on `markerui-v2`.
**Scope statement:** one versioned extraction program over published
kernel evidence, with field-level lineage, deterministic validation,
versioned reconciliation, honest missing/unresolved/review outcomes,
and a review seam with stale-context protection. This is the semantic
spine of the master plan's PR80; the direct-specialist displacement
experiment is deliberately deferred to PR80B.

## Contracts

| Contract | Identity | Owner |
|---|---|---|
| Extraction schema family | `marker.extraction.schema.v1` | `app/extraction/schema.py` |
| Extraction result | `marker.extraction.result.v1` | `app/extraction/results.py` |
| Reconciliation policy | `marker.extraction.reconcile` `v1` | `app/extraction/reconciliation.py` |
| Review authority rule | `marker.extraction.review.v1` | `app/extraction/review.py` |
| Proof schema (consumed) | `marker.kernel.proof_support.v1` et al. | `app/kernel/proofs.py` |
| Query/evidence (consumed) | `marker.query.v1`, `marker.evidence_packet.v1` | `app/context_runtime/` |

The proof schema is **demo.invoice@1.0.0** (`INVOICE_SCHEMA`): five
scalar fields (string/date/enum/optional-string/decimal), one repeated
line-item structure (`items`) identified by `sku` with quantity/unit
price/amount columns, and one `sum_equality` invariant
(`total_due` vs the sum of accepted `items[].amount`, tolerance 0.01).

## Authority integration — what extraction does NOT own

- **Evidence:** every candidate is grounded in units served by
  `execute_query` against one pinned `PublicationSet`. There is no
  second retrieval route, index, or authorization path.
- **Truth:** accepted values commit as kernel records
  (`ClaimAssertion` + `ClaimAssessment` outcome `source_exact` +
  one `ProofSupportRecord` witness edge per distinct evidence record).
  The kernel's proof DAG, cycle, grounding, and snapshot-honesty rules
  apply unchanged; extraction cannot bypass them (proven by test).
- **Result storage:** the full result (candidates, conflicts, missing
  fields, invariant findings) is one non-authoritative
  `NativeObjectRecord` view record (`extraction.result.<identity16>`)
  whose payload is the canonical result JSON. It can never act as
  witness evidence for the claim it derives from.
- **Review:** decisions commit as kernel `DecisionRecord`s bound to
  the reviewed result identity; corrections commit as
  `accepted_with_warning` assessments (human-sourced, never
  authority-bearing). Accepting a field with no grounded candidate is
  rejected — a reviewer cannot mint evidence.

## Outcome honesty

Run statuses: `accepted`, `partial`, `review_required`,
`invalid_request` (typed request errors), `stale_context`,
`policy_fail_closed`, `execution_failure`.

Field/row statuses: `accepted`, `corrected`, `rejected`, `unresolved`,
`review_required`, `missing`, `invalid`. Missing/null/empty/zero stay
distinguishable: a zero decimal is a real value; a missing field has no
candidates and no value; an invalid field preserves its failing
candidates with parse errors.

## Reconciliation rules (versioned, attributable)

| Rule | Semantics |
|---|---|
| `missing.no_evidence.v1` | No grounded candidate → missing (required fields escalate to review). |
| `invalid.all_candidates_invalid.v1` | Every candidate failed typed parsing → invalid. |
| `dedup.witness_repetition.v1` | Same (record, revision) repeated collapses before votes are counted. |
| `agree.distinct_witnesses.v1` | Distinct witnesses agree → accepted with corroboration count. |
| `conflict.witness_count.v1` | Strictly more distinct witnesses → that value wins; rule recorded. |
| `conflict.preserved_unresolved.v1` | Tied witnesses → unresolved (escalates on required fields); candidates preserved. |
| `row.collapse_duplicate_identity.v1` | Identical row identity (per schema `identity_keys`) collapses; rows differing on an identity key never merge. |

Agreement between correlated candidates (same witness) is never
counted as independent verification. Document-level totals cannot
prove incomplete rows: the sum invariant is `not_evaluable` (never
`satisfied`) unless every row and the target are accepted.

## Determinism and identity

- The deterministic anchor route (`anchor.v1`) parses literal labeled
  values and pipe-delimited item rows from served evidence units — no
  model, no clock, no randomness.
- Result identity = canonical hash over the semantic payload (schema
  identity, publication/generation, policy, field/item outcomes,
  invariant findings, error). It deliberately excludes the kernel
  commit head and packet identity ids so a deterministic rerun over
  frozen truth yields the same identity even after the first run's own
  persistence advanced the head.
- Idempotent reruns rely on the kernel's semantic-identity uniqueness:
  re-committing identical extraction records converges onto the
  existing ones (`DuplicateRecordIdentityError` is treated as replay).

## Proven scenarios (tests)

All scenarios publish real view documents, build/activate a lexical
generation, publish a `PublicationSet`, and run through the
authoritative query path (`backend/tests/test_extraction_*.py`).

- **Happy path:** scalars + 3 line items accepted, per-field lineage
  back to the serving record/revision/publication, sum invariant
  satisfied, deterministic rerun identity-equal (matrix B).
- **Missing evidence:** optional field missing + run `partial`;
  required field missing escalates to `review_required`; header-only
  document leaves the invariant honestly `not_evaluable` (C).
- **Conflicts:** 2-vs-1 distinct-witness conflict resolved by
  `conflict.witness_count.v1` with all witnesses visible; tied 1-vs-1
  conflict stays unresolved; same-witness repetition never counts as
  two votes (D).
- **Line items:** duplicate rows collapse under the `sku` identity
  rule; same-values-new-sku rows never merge; a broken row poisons
  neither sibling rows nor the invariant (it goes to review) (E).
- **Snapshot/revision:** pinned-context runs refuse a moved
  publication (`stale_context`); `revalidate` reports recorded-vs-
  current publication; a fresh run after republication preserves the
  old/new total conflict instead of silently picking the newest (G).
- **Authorization:** the extraction surface is exactly the query
  surface — foreign/empty workspaces produce missing fields, never
  borrowed evidence (H).
- **Review:** accept/correct/reject on an unresolved conflict,
  original candidates and result preserved, decision audited as a
  kernel record; stale review after republication rejected (I).
- **Proof integrity:** authority-bearing without support rejected;
  evidence set must equal support graph; the result record cannot
  witness the claim it derives from (laundering rejected, nothing
  committed); self-support rejected; rerun idempotent (F).

## Reproduction

```text
cd backend
python -m pytest tests/test_extraction_contract.py tests/test_extraction_service.py \
  tests/test_extraction_review.py tests/test_extraction_proof_integrity.py -q
```

## PR80A Track 0 — suite-order ingress stability (closed)

Root cause: a main-thread `asyncio.run` (Alembic's sync migration
entry, driven in-process by `tests/test_cli_errors.py` on first DB
creation) leaves the thread's current event loop unset.
pytest-asyncio 0.24 runs async **tests** on the *current* loop but
async **fixtures** on the session-scoped `event_loop` fixture; the old
conftest repair minted a fresh loop, splitting the two. Fixture-built
kernel coordinators then sat parked on the session loop while test
bodies ran elsewhere — every source-ingress job stayed `pending` until
its 30s timeout.

Fix: `tests/conftest.py::ensure_current_event_loop` now rebinds the
session loop as current for every test. Regression:
`tests/test_event_loop_hygiene.py` (2 failures against the old
conftest, 4 passes against the fix).

Measured on the implementing machine (Python 3.11.9, pytest 8.3.4,
pytest-asyncio 0.24.0):

```text
# Pre-fix (bisect): files 16-29 + ingress -> 7 failed, 288 passed
#                   test_cli_errors.py + ingress alone -> 7 failed
#                   ingress alone -> 7 passed (control)
# Post-fix:
python -m pytest tests/test_cli_errors.py tests/test_kernel_source_ingress.py -q
  # run 1: 16 passed ... run 2: 16 passed ... run 3: 16 passed
python -m pytest <files 16-29> tests/test_kernel_source_ingress.py -q
  # 295 passed
```

## Non-claims

- No claim that this beats trained specialists; the displacement
  benchmark is PR80B and was not run.
- One schema family (`demo.invoice`), not a general extraction DSL;
  the full Document Program language does not exist.
- Review-required rates on real corpora are unmeasured.
- Retrieval evidence is not entailment: accepted means
  evidence-grounded under the declared policy, not universally true.
- No new authorization profile: extraction inherits the existing
  workspace/authorization model, no enterprise per-user ACLs added.
- No model dependency was added; the deterministic route is the only
  extraction route in this slice.

## Deferred (PR80B and later)

- Direct-specialist displacement measurement and workflow-cost
  comparison.
- Additional extraction routes (constrained model generation) behind
  the same contract; schema registry persistence and versioned
  migration; agent-facing (MCP) extraction surface; review queue/UI;
  broader corpora and multi-document merge policies beyond the
  versioned rules shipped here.
