# PR74 — Claim Proof Authority & Integrity

**Start SHA:** `46165b663d3eab9cdef76b5c750d7344cae9d4fe` (PR73 evidence head)
**Final SHA:** see the commit carrying this document (`git log -1 docs/reference/kernel-claims-proofs.md`).
**Slice:** ClaimAssertion/ClaimAssessment semantics, proof DAG integrity, cycle rejection, proof-input integrity, PR73 claim-precondition closure.

## What changed, by file

| File | Purpose |
|---|---|
| `backend/app/kernel/records.py` | Assessment v2 contract: `snapshot_commit_id`, `workflow_class`, versioned outcome vocabulary (`CLAIM_ASSESSMENT_OUTCOMES`, `AUTHORITY_BEARING_OUTCOMES`), `from_payload` rematerialization for assertion/assessment with PR63 legacy-shape compatibility. No migration: every new construct lives in the existing record/edge tables. |
| `backend/app/kernel/proofs.py` (new) | `ProofSupportRecord` (authority-bearing support relation), `check_batch_proof_integrity` (the commit-boundary validator), `ClaimRequirement` + `evaluate_claim_requirements` (patch seam), pure conformance probes `detect_proof_cycle` / `proof_closure_path_to_authority_consumer`. |
| `backend/app/kernel/commit.py` | Step 2.8: proof integrity validated inside the commit transaction before any insert; new `proof-checked` fault phase. |
| `backend/app/kernel/patches.py` | PR73 `required_claim_refs` placeholder replaced by typed `required_claims`; in-transaction evaluation in `check_view_advancement`; legacy empty key still rematerializes. |
| `backend/app/kernel/patching.py` | Advisory claim-precondition pre-check in `submit_patch`. |
| `backend/app/kernel/errors.py` | `ProofCycleError`, `ProofInputIntegrityError`, `InvalidClaimAssessmentError`, `ClaimPreconditionUnmetError`. |
| `backend/app/kernel/__init__.py` | Package exports for the proof surface. |
| `backend/conformance/fixtures/claim_proof_vectors_v1.json` + `backend/conformance/test_claim_proof_conformance.py` + `backend/scripts/generate_claim_proof_fixtures.py` | Deterministic identity/topology/grounding vectors (committed hashes, drift-checked). |
| `backend/scripts/bench_pr74_claims.py` | Operational benchmark; writes `docs/reference/measurements/pr74-claims-proofs.json`. |
| `backend/tests/test_kernel_claims.py`, `test_kernel_proofs.py`, `test_kernel_proof_inputs.py`, `test_kernel_claim_patches.py`, `test_kernel_claim_assessment_states.py`, `test_pr74_durability.py`, `test_kernel_proof_randomized.py` | The adversarial matrix (§9-equivalent or stronger). |
| `backend/tests/test_kernel_patches.py` | Updated the two PR73 placeholder tests to the live contract (test currency). |

No Alembic migration: PR74 is representable in the existing `kernel_records` / `kernel_record_edges` tables, and a migration "because PR74 has a number" was explicitly out of scope.

## Semantic contract

### ClaimAssertion identity

`claim_key` **is** part of semantic identity — an explicit, fixture-pinned decision. It is the caller's stable external key scoping the assertion's referent: a different key is a different claim even when subject/predicate/value coincide; renaming mints a new claim rather than relabeling. This preserves every PR63-era stored assertion identity (no payload/identity split needed). Changing evidence, policy, or assessment outcomes never touches the assertion record.

### ClaimAssessment identity & context

Append-only historical evaluation event, never a mutable status bit. Identity covers: assertion ref, outcome, policy id + revision, the **unordered** evidence set, `snapshot_commit_id` (the committed head the assessment was computed against — validated `<= current head` and, for evidence committed in earlier commits, `>=` the evidence's landing commit), `workflow_class` (PR75 hook), and the retained PR63 `declared_context`. There is no policy-free global claim status anywhere in the schema (`kernel_records` has no verified/status column — tested).

Outcome vocabulary: `source_exact`, `verified`, `accepted_with_warning`, `uncertain`, `unavailable`, `abstained`, `failed`. Authority-bearing: `source_exact`, `verified` only. Unknown/legacy outcome strings (PR63 records) stay committable and readable as explicitly non-authority-bearing history — the kernel fail-closes by never treating an outcome it does not know as authority.

### Proof support & authority boundaries

`ProofSupportRecord` = the authority-bearing relation: `holder_ref` (assessment or decision) relies on `evidence_ref` under a declared `role` and a mandatory `authority_rule` (which rule allows this relation to raise authority). Generic lineage/navigation edges (`depends_on`, `assesses`, `observes`, `evidence_for`) stay non-authoritative. `derived_from` edges join the authority graph as the laundering channel: a derived record implicitly relies on its ancestor.

### Cycle rule

The reliance graph (proof supports + `derived_from`, committed state overlaid with the batch) must stay acyclic through every batch-introduced relation. Committed history is acyclic by induction, so DFS runs from new-edge tails only; any new cycle passes through one and is rejected with the offending path before any insert. Non-authoritative navigation cycles remain legal (tested).

### Grounding rule (anti-laundering)

The reliance closure of an authority-bearing assessment (or authority-rule-bearing decision) may never reach a `claim_assertion`, `claim_assessment`, or `decision`: reaching the assessed claim is self-support; reaching any other claim/assessment/decision is laundering through unresolved authority. This is what rejects "summary/reconciliation of a claim supporting that claim" in all indirect shapes.

### Input-integrity rules

* declared `evidence_refs` must agree **exactly** with the support graph (both directions);
* `witness` = structural independence: no derivation lineage;
* `derived` = derivation path exposed (≥1 `derived_from` edge);
* `input` = structural dependency (crop/topology/source revision) that enters the closure;
* authority consumers can never act as evidence; one support per (holder, evidence);
* authority-bearing outcomes require ≥1 support; authority-rule decisions require support in the same commit;
* all assertion/evidence/holder references must resolve in-workspace (typed errors, same as edge references);
* snapshot honesty: an assessment cannot rely on state its declared cut does not contain.

### Claim-precondition rule (PR73 seam closure)

`PatchPreconditions.required_claims: tuple[ClaimRequirement, ...]` — each requirement names assertion, policy id/revision, accepted outcomes (default: authority-bearing set), optional pinned `assessment_ref`, and a freshness floor `min_snapshot_commit_id`. Evaluated authoritatively inside the commit transaction (before inserts, under the writer lock) and advisively in `submit_patch`: missing, wrong-assertion, policy-mismatched, stale-snapshot, or proof-invalid assessments raise `ClaimPreconditionUnmetError` and roll back the entire all-or-conflict patch commit. Unpinned resolution is deterministic: the latest committed assessment under the exact policy ask (causal order). The PR73-era `required_claim_refs` key rematerializes only when empty (no non-empty value was ever committable, so nothing stored depends on it).

## Adversarial test matrix (all reproducible)

* **Identity/append-only** (`test_kernel_claims.py`): qualifier/key-order convergence; record-id irrelevance; claim_key scoping; value/subject/predicate/qualifier separation; raw Unicode (no folding); evidence change → new assessment, same assertion; policy/snapshot/workflow identity separation; construction validation; legacy PR63 payload rematerialization; fail-closed unknown/missing fields.
* **Cycles** (`test_kernel_proofs.py`): two-node, three-node, five-node mixed-kind loops; same-batch and cross-commit closure; self-support through derivation; support reaching another claim; navigation-cycle legality; fault at `proof-checked`; invalid proof with view advancement moves no head.
* **Input integrity** (`test_kernel_proof_inputs.py`): derivation-less derived evidence; derived material presented as witness; validator input absent from the evidence set; declared evidence without support; incomplete derivation metadata; claim/assessment as evidence; non-assessment holder; duplicate pair; authority-bearing without support; future snapshot; unresolved references; honest non-authority outcomes; authority-rule decision support.
* **Policy/snapshot-relative states** (`test_kernel_claim_assessment_states.py`): two policies one claim, both auditable; two claims independent states; no global verified field (schema-level); newer policy does not rewrite history; evidence beyond the declared snapshot rejected.
* **PR73 integration** (`test_kernel_claim_patches.py`): satisfied precondition admits; missing/wrong-assertion/policy-mismatch/stale-snapshot/proof-invalid/tainted-closure rejections with full rollback and untouched view head; deterministic latest-wins resolution; restart + rematerialization preserve identical behavior; clean rebuild reproduces the gated revision.
* **Durability/fault** (`test_pr74_durability.py`): faults at every post-proof phase leave nothing; rejected proofs leave no outbox side effects; restart replay determinism; every rematerialized PR74 record re-derives its stored identity.
* **Randomized** (`test_kernel_proof_randomized.py`): seeded (20260817) multi-commit DAG growth always accepted; randomly chosen cycle-closing relation always rejected with zero partial state; replay digests identical.

## Canonical/conformance vectors

`backend/conformance/fixtures/claim_proof_vectors_v1.json` (schema `marker.claim_proof.fixtures.v1`): committed identity hashes for assertion (claim_key scoping, qualifier order, raw Unicode), assessment (evidence order, policy revision, snapshot, workflow class, context order), proof support (role/rule); topology vectors (acyclic chains/wide fans vs two-/three-node and cross-consumer cycles); grounding vectors (grounded chains vs summary-of-claim and peer-assessment laundering). Regenerate deterministically with `python backend/scripts/generate_claim_proof_fixtures.py --write`; the conformance suite recomputes everything through the real constructors and probes and fails on drift.

## Measurements (this machine; JSON has full precision)

`python backend/scripts/bench_pr74_claims.py` (best-of-5, `time.perf_counter`):

| Probe | 100 | 1,000 | 10,000 |
|---|---|---|---|
| pure cycle detection (chain) | 0.000049 s | 0.000495 s | 0.005234 s |
| pure cycle detection (worst-case far cycle) | 0.000040 s | 0.000404 s | 0.004624 s |
| in-transaction commit check (committed history + batch) | 0.00894 s | 0.014508 s | 0.072807 s |
| claim-precondition evaluation | 10: 0.004623 s | 100: 0.004690 s | 1,000: 0.010185 s |

Growth is linear in edges (100× edges → ~100× pure time; the DB-backed check is dominated by SQLite row loading at 10k edges, ~73 ms total). Normal claim commits stay in single-digit milliseconds. No pathological shape hides behind the fixtures: the far-cycle worst case matches the accepted-chain cost. **The simple visible-cut graph check is kept; no reachability cache, closure table, or secondary graph store is justified by these numbers.**

## Rejected / simplified alternatives

* **Separate proof-edge table with rule payloads** — rejected: a record class (`proof_support`) reuses the immutable-record machinery (identity, manifests, replay, GC) with zero migration; an edge-payload hybrid would need schema + manifest changes for no additional invariant.
* **Global graph-cycle ban** — rejected: the V3.2 contract explicitly allows non-authoritative navigation cycles; only proof-support/derivation reliance participates in the authority graph (tested legal navigation cycle).
* **Payload/identity split to demote `claim_key`/`declared_context` to audit-only fields** — rejected: identity changes for zero stored records vs. new-field additions; documented decision keeps the identity rule unambiguous without a remap.
* **Recursive-CTE SQL validation** — rejected for now: in-memory DFS over the loaded cut is simpler and measured linear; SQL recursion can replace it behind the same function if scale ever demands.
* **Requiring ≥1 witness for authority** — simplified away: derivation-exposed chains that terminate on non-claim ground are structurally honest; requiring a witness would forbid legitimately layered proofs without adding an invariant.

## Residual limits (what PR74 intentionally does not prove)

* Statistical/empirical sufficiency of proofs (calibration, joint-error, risk bounds) — **PR75**.
* Domain validators (table totals, formulas, units, currency semantics) — later slices build them ON this substrate.
* An author who simply omits a `derived_from` edge for a derivation the kernel cannot observe: PR74 makes hidden inputs unrepresentable **as authority** (witness independence is structural), it cannot make authors declare everything; policy/risk layers decide trust. Payload-level derivation-map cross-checks are a possible later hardening.
* Precondition revalidation re-walks committed proof supports per check (linear scan over workspace assessments); measured at ~10 ms for 1,000 assessments — an index is justified only when a workspace exceeds that by orders of magnitude.
* Committed assessments are revalidated lazily (at commit for new proofs, at use for preconditions); a tainting commit itself lands, but the assessment it taints can no longer gate patches.

## Test commands & results (final code)

```
cd backend
python -m pytest tests conformance -q
```

Full-suite result on the PR74 head (this machine, 2026-08-17):

* **2155 passed, 8 failed, 4 skipped.**

The 8 failures are **not PR74 regressions** — they reproduce byte-identically on the pre-PR74 baseline (`46165b6`) run in a clean worktree on the same machine: **2071 passed, 8 failed (the same 8), 5 skipped**. The failing set is `test_kernel_source_ingress.py` (7 real-PDF-conversion E2E tests whose 30 s `_wait_status` poll budget times out under full-suite load) plus `test_database_migration.py::test_live_migration_lock_is_never_stolen_by_age` (a Windows OS-lock subprocess race). Both files pass green in isolation on the same tree.

* PR73 recorded baseline (repo evidence): 2081 passed / 3 skipped.
* PR74 delta vs today's baseline rerun: **+84 passed, zero new failures** (skip count fluctuates 3–5 with environment gates — ffmpeg availability, POSIX permission bits, benchmark condition — none of them PR74 tests; PR74 adds zero skips).

## Next dependency-complete slice

PR75 can assume: stable claim/assessment identities; explicit assessment context; acyclic, integrity-checked proof topology; non-launderable source/derived lineage; structurally invalid authority rejected at the boundary; claim-dependent patches gated on authoritative assessment state. PR75 owns: dependency-risk disclosure, empirical joint-error behavior, calibration artifacts, risk bounds, correlation-aware policy, benchmarked promotion/abstention decisions.
