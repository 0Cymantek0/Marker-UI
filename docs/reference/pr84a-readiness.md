# PR84A — Executable V3.2 Readiness Evidence Ledger

PR84A is the acceptance layer that mechanically binds each of the 62 governing
V3.2 readiness invariants (master plan, amendment 23C) to executable evidence,
derives the overall verdict, and reports residual gaps honestly. It is
release-audit metadata only: it never becomes a second product truth authority.

The current mechanically derived snapshot lives in
[`readiness/pr84a-readiness-report.md`](readiness/pr84a-readiness-report.md)
(machine form: `readiness/pr84a-readiness-report.json`) and records
**36 proven / 0 failed / 26 without acceptable evidence → NOT READY** against
its audited source head. A truthful NOT READY is an accepted repository state;
nothing in CI pressures the ledger toward green.

## Artifacts

| Artifact | Path | Role |
|---|---|---|
| Canonical inventory (62 invariants) | `backend/readiness/readiness_invariants.json` | Single canonical copy of amendment 23C wording; tests re-extract it from the master plan to forbid drift |
| Evidence ledger | `docs/reference/readiness/readiness-ledger.json` | Per-invariant status claims, executable bindings, gap classifications |
| Evidence run | `docs/reference/readiness/pr84a-evidence-run.json` | The only trusted execution record: batched pytest outcomes + measurement digests/expectation checks, git content-identity per scope file |
| Generated reports | `docs/reference/readiness/pr84a-readiness-report.{json,md}` | Derived from the same audit result; byte-deterministic |
| Auditor/runner code | `backend/readiness/` | Inventory/ledger parsing, derivation, execution, reporting |
| CLI | `backend/scripts/readiness_audit.py` | `run-evidence` / `audit` / `integrity` / `release-gate` |
| CI gate | `readiness-integrity` job in `.github/workflows/ci.yml` | Integrity-only gate (see below) |
| Tests | `backend/tests/test_readiness_*.py` | Inventory, adversarial derivation, real-repo integration/reproducibility |

## Status semantics

Each invariant derives exactly one governing status:

- **proven** — at least one executable binding (test / measurement /
  failure-injection / conformance) with full asserted coverage passed in the
  recorded run, its scope files' git content identities still match the
  working tree, and (for measurements) the artifact digest and expected
  proving values still hold.
- **failed** — an executed binding failed in the recorded run.
- **no_evidence** — no acceptable executable evidence: nothing bound, only
  non-executable context, coverage narrower than the invariant, proof
  environment-gated in the recorded run, or stale/untrustworthy proof.

Internal reasons (`none_bound`, `docs_only_no_executable_binding`,
`partial_coverage_only`, `environment_limited`,
`stale_or_invalid_evidence`) refine **no_evidence** without weakening it.

Non-proven invariants carry a gap classification:

- **A** implementation missing — the required behavior does not exist
- **B** behavior appears present, executable proof missing
- **C** proof exists but cannot be trusted (stale/corrupt/unsupported)
- **D** evidence valid but narrower than the invariant wording
- **E** compatibility/public boundary unresolved
- **F** measurement/economics/operations closure missing
- **G** governing applicability needs clarification

## Trust boundary

The machine verifies: inventory completeness/uniqueness and master-plan
wording equality; ledger schema; that every binding executed and how (passed /
failed / environment-gated skip); scope-file trackedness and working-tree
content identity (unstaged edits are still detected); measurement artifact
digests and expected proving values at audit time; claim/derivation
consistency; and byte-identical regeneration of both reports.

Humans assert — visibly, with a mandatory rationale — that a binding's
coverage is `full` or `partial` for the invariant wording. Markdown, comments,
screenshots, and manual statements are context (`context_docs`) and can never
support `proven`.

## Reproduction

From the repository root:

```bash
# validate the committed ledger/evidence/reports (fast; no test execution)
python backend/scripts/readiness_audit.py --mode integrity

# re-execute every bound test binding (batched pytest) and re-validate
# measurements, then rewrite the evidence run and regenerate reports
python backend/scripts/readiness_audit.py --mode run-evidence

# final PR84 closeout gate: integrity AND every invariant proven
python backend/scripts/readiness_audit.py --mode release-gate
```

The evidence run records the source head and per-scope-file git blob
identities it certifies. Editing any bound test or measurement file makes the
affected bindings stale; the ledger then needs a re-run
(`run-evidence`) before it can honestly keep claiming `proven`.

## CI behavior

The `readiness-integrity` job runs `--mode integrity` on every push. An
honest NOT READY ledger with valid, fresh, non-overclaiming evidence passes;
the job fails only on structural violations, stale/dangling/corrupted proof,
prose-only proven claims, claim/derivation mismatches, or hand-edited
generated reports. This keeps incremental development possible while making
false readiness mechanically impossible. The stricter `release-gate` mode is
deliberately not a required check until the final PR84 closeout.

## Current residual gap map (2026-08-22 snapshot)

Full detail: [`readiness/pr84a-readiness-report.md`](readiness/pr84a-readiness-report.md).
Summary by type:

- **A — implementation missing (5):** admission memory envelope (30), model
  leases/anti-eviction (31), connector event semantics (42), disclosed-context
  non-revocation documentation (48), AnswerContextTrace separation (54).
- **B — proof missing (10):** JSONL non-authority clause (1), blob-vs-
  observation dedup collision (4), cross-page fragment preservation (13),
  redaction surface beyond audit text (18), per-region usability (22),
  calibration artifact fields (23), review-policy operability (26),
  no-training routing behavior (27), external-effect destination table (37),
  packet-reuse citation/renderer dimensions (53).
- **D — evidence narrower (6):** cross-language/ARM64 (6), cursor crash lanes
  env-gated (3), fixture-scoped routing shadow (25), failure-class coverage
  (38), UI/export as-of end-to-end (56); plus invariant 1's untested clause
  counted under B.
- **F — economics/operations closure (5):** scale envelope (57), visual
  retrieval economics (58), owner/rollback/kill matrix (59), leadership-claim
  discipline (60), final displacement test (62).

### Next-slice recommendation inputs

By count, 23C.7 (economics, 5 non-proven) and 23C.3 (verification/routing, 5)
lead, followed by 23C.1/23C.4 (4 each). Under the PR84A plan's rubric the
dominant true gap beyond existing coverage is the 57–62 economics/operations/
claim-displacement cluster (all Type F — the system works, the governing
claims lack reproducible cost, operational-burden, cold/warm, and
whole-system displacement measurement). The highest-risk implementation gaps
are invariants 30 and 31 (admission envelope, model leases), which are
architecture-level absences rather than measurement gaps. A focused session
should pick one cluster, not both.

## Non-claims

- PR84A does not claim V3.2 readiness; the current derived verdict is NOT READY.
- Proven statuses are scoped to the environments recorded in the report
  (predominantly sqlite-dev lanes plus frozen measurement artifacts; the
  strict PostgreSQL/S3 industrial lanes validate their own CI jobs and appear
  here as environment-gated bindings where bound).
- The readiness ledger is evaluation metadata; product truth remains the
  transactional truth kernel.
