# PR84A V3.2 Readiness Report

**Overall verdict: NOT_READY** (mechanically derived; never hand-set)

- Audited source head: `31464e7121b1dcdf217159504c9faf3cd9c66350`
- Invariants proven: **36 / 62**
- Failed: **0**
- No acceptable evidence: **26**

## Group summary

| Group | Name | Proven | Failed | No evidence |
|---|---|---:|---:|---:|
| 23C.1 | Truth and persistence | 5 | 0 | 4 |
| 23C.2 | Geometry, patches, and incrementality | 7 | 0 | 2 |
| 23C.3 | Verification and routing | 4 | 0 | 5 |
| 23C.4 | Runtime and jobs | 7 | 0 | 4 |
| 23C.5 | Source, authorization, and retrieval | 7 | 0 | 3 |
| 23C.6 | Agent and product behavior | 5 | 0 | 3 |
| 23C.7 | Economics and claim language | 1 | 0 | 5 |

## Invariant statuses

| ID | Group | Status | Evidence environments / reason |
|---:|---|---|---|
| 1 | 23C.1 | no-evidence | executed proof covers only part of the invariant wording |
| 2 | 23C.1 | proven | sqlite-dev |
| 3 | 23C.1 | no-evidence | proof exists but was environment-gated in the recorded run |
| 4 | 23C.1 | no-evidence | executed proof covers only part of the invariant wording |
| 5 | 23C.1 | proven | sqlite-dev + conformance-local (single CI matrix cell) |
| 6 | 23C.1 | no-evidence | executed proof covers only part of the invariant wording |
| 7 | 23C.1 | proven | sqlite-dev |
| 8 | 23C.1 | proven | sqlite-dev |
| 9 | 23C.1 | proven | sqlite-dev |
| 10 | 23C.2 | proven | sqlite-dev |
| 11 | 23C.2 | proven | sqlite-dev |
| 12 | 23C.2 | proven | sqlite-dev |
| 13 | 23C.2 | no-evidence | executed proof covers only part of the invariant wording |
| 14 | 23C.2 | proven | sqlite-dev |
| 15 | 23C.2 | proven | sqlite-dev |
| 16 | 23C.2 | proven | sqlite-dev |
| 17 | 23C.2 | proven | sqlite-dev |
| 18 | 23C.2 | no-evidence | executed proof covers only part of the invariant wording |
| 19 | 23C.3 | proven | sqlite-dev |
| 20 | 23C.3 | proven | sqlite-dev |
| 21 | 23C.3 | proven | sqlite-dev |
| 22 | 23C.3 | no-evidence | executed proof covers only part of the invariant wording |
| 23 | 23C.3 | no-evidence | executed proof covers only part of the invariant wording |
| 24 | 23C.3 | proven | sqlite-dev |
| 25 | 23C.3 | no-evidence | executed proof covers only part of the invariant wording |
| 26 | 23C.3 | no-evidence | executed proof covers only part of the invariant wording |
| 27 | 23C.3 | no-evidence | executed proof covers only part of the invariant wording |
| 28 | 23C.4 | proven | sqlite-dev, offline-artifact (PR68A measured comparison, frozen) |
| 29 | 23C.4 | proven | sqlite-dev |
| 30 | 23C.4 | no-evidence | no executable evidence bound |
| 31 | 23C.4 | no-evidence | no executable evidence bound |
| 32 | 23C.4 | proven | sqlite-dev |
| 33 | 23C.4 | proven | sqlite-dev |
| 34 | 23C.4 | proven | sqlite-dev |
| 35 | 23C.4 | proven | sqlite-dev |
| 36 | 23C.4 | proven | sqlite-dev |
| 37 | 23C.4 | no-evidence | proof exists but was environment-gated in the recorded run |
| 38 | 23C.4 | no-evidence | proof exists but was environment-gated in the recorded run |
| 39 | 23C.5 | proven | sqlite-dev |
| 40 | 23C.5 | proven | sqlite-dev |
| 41 | 23C.5 | proven | sqlite-dev |
| 42 | 23C.5 | no-evidence | no executable evidence bound |
| 43 | 23C.5 | no-evidence | executed proof covers only part of the invariant wording |
| 44 | 23C.5 | proven | sqlite-dev |
| 45 | 23C.5 | proven | sqlite-dev |
| 46 | 23C.5 | proven | sqlite-dev |
| 47 | 23C.5 | proven | sqlite-dev |
| 48 | 23C.5 | no-evidence | no executable evidence bound |
| 49 | 23C.6 | proven | sqlite-dev |
| 50 | 23C.6 | proven | sqlite-dev |
| 51 | 23C.6 | proven | sqlite-dev |
| 52 | 23C.6 | proven | sqlite-dev |
| 53 | 23C.6 | no-evidence | executed proof covers only part of the invariant wording |
| 54 | 23C.6 | no-evidence | no executable evidence bound |
| 55 | 23C.6 | proven | sqlite-dev |
| 56 | 23C.6 | no-evidence | executed proof covers only part of the invariant wording |
| 57 | 23C.7 | no-evidence | executed proof covers only part of the invariant wording |
| 58 | 23C.7 | no-evidence | executed proof covers only part of the invariant wording |
| 59 | 23C.7 | no-evidence | executed proof covers only part of the invariant wording |
| 60 | 23C.7 | no-evidence | only non-executable context (docs/prose) bound |
| 61 | 23C.7 | proven | offline decision-rule tests, offline-artifact (PR81A; VLM cache replay), offline-artifact (PR80B displacement replay) |
| 62 | 23C.7 | no-evidence | executed proof covers only part of the invariant wording |

## Residual gap map

Gap types: **A** — implementation missing; **B** — behavior appears present, executable proof missing; **C** — proof exists but cannot currently be trusted (stale/corrupt/unsupported); **D** — evidence valid but narrower than the invariant; **E** — compatibility/public boundary unresolved; **F** — measurement/economics/operations closure missing; **G** — governing applicability needs clarification

### Type A — implementation missing

- **Inv 30** (23C.4, admission-memory-envelope): No admission subsystem keyed to a pinned preprocessor's visual-token/memory envelope exists (no admission-envelope symbols anywhere in backend/); dynamic-resolution OOM stress is untested. Reason: no executable evidence bound.
- **Inv 31** (23C.4, model-lease-anti-eviction): No model-lease/anti-eviction mechanism exists (no ModelLease symbols); cold-start/queue/load cost does not surface in routing or user-visible outcomes. Reason: no executable evidence bound.
- **Inv 42** (23C.5, connector-idempotency-cursor-atomicity): No connector event ingestion subsystem exists: no idempotent/gap-aware connector event handling, no source-state+cursor local atomic commit, no token-expiry/reset reconciliation. (Byte-staging dedup in source_store is a different concern.) Reason: no executable evidence bound.
- **Inv 48** (23C.5, disclosed-context-non-revocability-doc): The required documentation statement — already disclosed external-agent context cannot be revoked — appears nowhere in Marker UI docs and no test links or checks it. Reason: no executable evidence bound.
- **Inv 54** (23C.6, trace-not-entailment-proof): AnswerContextTrace does not exist in the codebase; no entailment-representation separation or material-answer-claim assessment path is implemented or tested (the EvidencePacket docstring disclaims entailment, but that is prose). Reason: no executable evidence bound.

### Type B — behavior appears present, executable proof missing

- **Inv 1** (23C.1, single-transactional-commit-authority): Single transactional authority and atomicity are proven; the clause 'per-document JSONL files are not the serving authority' has no executed negative test asserting JSONL non-authority (no serving path may read per-document ledgers). Reason: executed proof covers only part of the invariant wording.
- **Inv 4** (23C.1, blob-vs-observation-identity): Separation is demonstrated indirectly (GC rescue keys on blob_key; evidence classes are distinct record identities); a direct dedup-collision test naming both identities is missing. Reason: executed proof covers only part of the invariant wording.
- **Inv 13** (23C.2, cross-page-fragment-preservation): Continuation-edge and alternative-preservation semantics exist; the all-fragments + full-provenance preservation property for cross-page continuations and multi-page tables has no executable assertion. Reason: executed proof covers only part of the invariant wording.
- **Inv 18** (23C.2, redaction-all-paths): Redaction policy exists only as audit-text redaction plus retrieval identity rotation; the invariant's enumerated surface (image/cache/index/visual vector/export/cursor) is untested and may be partially unimplemented. Reason: executed proof covers only part of the invariant wording.
- **Inv 22** (23C.3, verification-status-relative): Relativity is structural (every dimension is matched before an outcome), yet the 'one unresolved region does not make the entire document unusable' property has no direct two-region assertion. Reason: executed proof covers only part of the invariant wording.
- **Inv 23** (23C.3, calibration-artifact-discipline): Calibration artifact schema carries method/version/sample/support/CI/shift but not named population/assumptions/expiry fields; zero observed catastrophic failures are reported as counts only. Reason: executed proof covers only part of the invariant wording.
- **Inv 26** (23C.3, review-policy-operational): Verification-policy operational usability (review coverage, queue time, bypass rate) is an acknowledged open follow-up in docs/reference/verification-risk.md; only the extraction review lane is tested. Reason: executed proof covers only part of the invariant wording.
- **Inv 27** (23C.3, no-training-routing-transparency): Displacement comparison is measurement-only; routing of trained specialists as non-authoritative candidates is a declared condition, not an executed behavior. Reason: executed proof covers only part of the invariant wording.
- **Inv 37** (23C.4, external-effect-semantics-declared): Local exactly-once acceptance and truthful refusal/reconciliation are proven; the per-destination external-effect semantics declaration driven by real destination primitives is absent. Reason: proof exists but was environment-gated in the recorded run.
- **Inv 43** (23C.5, revocation-slo): Revocation effectiveness without content events is proven and measured; a declared numeric SLO (bound/latency) is neither declared nor asserted. Reason: executed proof covers only part of the invariant wording.
- **Inv 53** (23C.6, packet-reuse-invalidation): Most named triggers are explicit identity dimensions with tests; 'citation change' has no identity dimension and renderer/tokenizer rotation is covered only via serialization_profile + publication tokenizer fields. Reason: executed proof covers only part of the invariant wording.

### Type D — evidence valid but narrower than the invariant

- **Inv 3** (23C.1, crash-injection-no-partial-state): Crash atomicity for mutations/decisions/publication pointers/effects is proven on SQLite; source-cursor and full recovery crash injection runs in the PG+S3 industrial environment (env-gated here). No cross-topology crash claim beyond the CI-grade lab. Reason: proof exists but was environment-gated in the recorded run.
- **Inv 6** (23C.1, canonical-id-cross-platform): Determinism proven multi-OS × multi-Python on x86_64 with committed golden constants; the invariant's literal cross-language and ARM64 clauses have no executable fixture (no second-language implementation, no ARM64 runner). Reason: executed proof covers only part of the invariant wording.
- **Inv 25** (23C.3, routing-stays-shadow-until-proven): Routing demonstrably stays shadow/offline, but the promotion condition has not been evaluated against truly held-out shift + catastrophic utility (same fixture trains and evaluates); evidence is fixture-scoped. Reason: executed proof covers only part of the invariant wording.
- **Inv 38** (23C.4, failure-injection-truthful-outcomes): Cancellation/failover/disk/model-service(lease-lapse) classes are covered on the SQLite lane and DB-outage/WAL semantics on the PG failover lab; shared-memory pressure is not injected anywhere (the shared-memory lane was measured and rejected in PR68A), and no literal model-service crash injection exists. Reason: proof exists but was environment-gated in the recorded run.
- **Inv 56** (23C.6, stale-review-rejection): Backend review-commit staleness is proven; UI screens, approvals, exports, and operational status exposing as-of revision/policy/completeness end-to-end (including frontend) is unproven. Reason: executed proof covers only part of the invariant wording.

### Type F — measurement/economics/operations closure missing

- **Inv 57** (23C.7, scale-envelope-economics): Measured dimensions: object counts, copy bytes, transfer amplification, failover RTO components. Missing as measured fields: WAL/write amplification, retained-generation accounting, FTS/vector/visual storage, cold starts, review burden, reprocessing cost, database-row envelopes. Reason: executed proof covers only part of the invariant wording.
- **Inv 58** (23C.7, visual-retrieval-selective-economics): Selectivity + disable proven and storage/update costs partially measured; ACL complexity is unmeasured and no enabled-vs-disabled operational-load delta exists. Reason: executed proof covers only part of the invariant wording.
- **Inv 59** (23C.7, subsystem-owner-rollback-kill): Utility is measured per capability slice; the required per-subsystem support owner, rollback, expiry, and kill-condition matrix does not exist in any artifact. Reason: executed proof covers only part of the invariant wording.
- **Inv 60** (23C.7, leadership-claim-discipline): No leadership-claim completeness fields or validators exist: workflow, competitors, catastrophic budget, and review burden are not named in any artifact schema or test. Reason: only non-executable context (docs/prose) bound.
- **Inv 62** (23C.7, final-displacement-test): Concession language and specialist comparisons exist; the final rational-user displacement test (better accepted end-to-end outcome by leaving Marker UI) has never been executed as such. Reason: executed proof covers only part of the invariant wording.

### Next-slice ranking (groups with most non-proven invariants)

- 23C.3 Verification and routing: 5 non-proven
- 23C.7 Economics and claim language: 5 non-proven
- 23C.1 Truth and persistence: 4 non-proven
- 23C.4 Runtime and jobs: 4 non-proven
- 23C.5 Source, authorization, and retrieval: 3 non-proven
- 23C.6 Agent and product behavior: 3 non-proven
- 23C.2 Geometry, patches, and incrementality: 2 non-proven

## Reproduction

```bash
# from repository root
python backend/scripts/readiness_audit.py --mode integrity
# re-execute bound evidence and regenerate the snapshot:
python backend/scripts/readiness_audit.py --mode run-evidence
```

This report is generated from the canonical ledger and executed evidence; manual edits are detected by `--mode integrity`. An honest NOT READY verdict with valid evidence integrity is an accepted repository state.
