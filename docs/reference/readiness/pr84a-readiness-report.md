# PR84A V3.2 Readiness Report

**Overall verdict: NOT_READY** (mechanically derived; never hand-set)

- Audited source head: `2bd30d78bb46d6f3d7ef3bd55bb5965f3f8bd17b`
- Invariants proven: **49 / 62**
- Failed: **0**
- No acceptable evidence: **13**

## Group summary

| Group | Name | Proven | Failed | No evidence |
|---|---|---:|---:|---:|
| 23C.1 | Truth and persistence | 5 | 0 | 4 |
| 23C.2 | Geometry, patches, and incrementality | 8 | 0 | 1 |
| 23C.3 | Verification and routing | 8 | 0 | 1 |
| 23C.4 | Runtime and jobs | 8 | 0 | 3 |
| 23C.5 | Source, authorization, and retrieval | 9 | 0 | 1 |
| 23C.6 | Agent and product behavior | 8 | 0 | 0 |
| 23C.7 | Economics and claim language | 3 | 0 | 3 |

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
| 18 | 23C.2 | proven | sqlite-dev |
| 19 | 23C.3 | proven | sqlite-dev |
| 20 | 23C.3 | proven | sqlite-dev |
| 21 | 23C.3 | proven | sqlite-dev |
| 22 | 23C.3 | proven | sqlite-dev |
| 23 | 23C.3 | proven | sqlite-dev |
| 24 | 23C.3 | proven | sqlite-dev |
| 25 | 23C.3 | no-evidence | executed proof covers only part of the invariant wording |
| 26 | 23C.3 | proven | sqlite-dev, sqlite-dev deterministic tracer (injected clocks, real seams) |
| 27 | 23C.3 | proven | deterministic local lane (real kernel/publication/query authorities per document; specialist responses replayed offline from the committed PR80B cache - no network, no credentials) |
| 28 | 23C.4 | proven | sqlite-dev, offline-artifact (PR68A measured comparison, frozen) |
| 29 | 23C.4 | proven | sqlite-dev |
| 30 | 23C.4 | no-evidence | executed proof covers only part of the invariant wording |
| 31 | 23C.4 | proven | cpu-test-env (real threads/locks/conditions; the lease semantics are process-level and make no VRAM claim) |
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
| 42 | 23C.5 | proven | deterministic local sqlite lane (migrated file DB, real kernel commit spine, real content-addressed store, real authorization overlay; scripted provider) |
| 43 | 23C.5 | no-evidence | executed proof covers only part of the invariant wording |
| 44 | 23C.5 | proven | sqlite-dev |
| 45 | 23C.5 | proven | sqlite-dev |
| 46 | 23C.5 | proven | sqlite-dev |
| 47 | 23C.5 | proven | sqlite-dev |
| 48 | 23C.5 | proven | deterministic local lane (canonical reference doc, MCP guide, runtime agent-guide resource, and live tool description read from the working tree) |
| 49 | 23C.6 | proven | sqlite-dev |
| 50 | 23C.6 | proven | sqlite-dev |
| 51 | 23C.6 | proven | sqlite-dev |
| 52 | 23C.6 | proven | sqlite-dev |
| 53 | 23C.6 | proven | sqlite-dev lane: real kernel commit spine, real immutable publication sets/generations, trusted local_v1 authorization resolution, durable Alembic-migrated cursors through head 20260823_0016, and the run_agent_query delivery seam |
| 54 | 23C.6 | proven | deterministic local sqlite lane (migrated file DB through Alembic head 20260823_0015, real kernel commit spine, real publications/authorization, real EvidencePacket delivery chains) |
| 55 | 23C.6 | proven | sqlite-dev |
| 56 | 23C.6 | proven | sqlite-dev, sqlite-dev + vitest jsdom, sqlite-dev + chromium |
| 57 | 23C.7 | proven | local sqlite + docker postgres16/minio industrial + offline VLM replay |
| 58 | 23C.7 | proven | offline decision-rule + artifact-honesty tests, offline same-workload OFF/ON + ACL experiment (VLM replay cache) |
| 59 | 23C.7 | no-evidence | executed proof covers only part of the invariant wording |
| 60 | 23C.7 | no-evidence | only non-executable context (docs/prose) bound |
| 61 | 23C.7 | proven | offline decision-rule tests, offline-artifact (PR81A; VLM cache replay), offline-artifact (PR80B displacement replay) |
| 62 | 23C.7 | no-evidence | executed proof covers only part of the invariant wording |

## Residual gap map

Gap types: **A** — implementation missing; **B** — behavior appears present, executable proof missing; **C** — proof exists but cannot currently be trusted (stale/corrupt/unsupported); **D** — evidence valid but narrower than the invariant; **E** — compatibility/public boundary unresolved; **F** — measurement/economics/operations closure missing; **G** — governing applicability needs clarification

### Type B — behavior appears present, executable proof missing

- **Inv 1** (23C.1, single-transactional-commit-authority): Single transactional authority and atomicity are proven; the clause 'per-document JSONL files are not the serving authority' has no executed negative test asserting JSONL non-authority (no serving path may read per-document ledgers). Reason: executed proof covers only part of the invariant wording.
- **Inv 4** (23C.1, blob-vs-observation-identity): Separation is demonstrated indirectly (GC rescue keys on blob_key; evidence classes are distinct record identities); a direct dedup-collision test naming both identities is missing. Reason: executed proof covers only part of the invariant wording.
- **Inv 13** (23C.2, cross-page-fragment-preservation): Continuation-edge and alternative-preservation semantics exist; the all-fragments + full-provenance preservation property for cross-page continuations and multi-page tables has no executable assertion. Reason: executed proof covers only part of the invariant wording.
- **Inv 30** (23C.4, admission-memory-envelope): PR69 landed the admission subsystem keyed to the pinned preprocessor's visual-token/memory envelope (runtime_capacity.py + worker/thread gates) with deterministic, race, worker-integration, and OOM-injection suites green and a committed estimate-mode characterization artifact; the invariant's full wording additionally requires dynamic-resolution GPU OOM-stress evidence on real CUDA hardware, which has not been executed yet, so coverage stays partial. Reason: executed proof covers only part of the invariant wording.
- **Inv 37** (23C.4, external-effect-semantics-declared): Local exactly-once acceptance and truthful refusal/reconciliation are proven; the per-destination external-effect semantics declaration driven by real destination primitives is absent. Reason: proof exists but was environment-gated in the recorded run.
- **Inv 43** (23C.5, revocation-slo): Revocation effectiveness without content events is proven and measured; a declared numeric SLO (bound/latency) is neither declared nor asserted. Reason: executed proof covers only part of the invariant wording.

### Type D — evidence valid but narrower than the invariant

- **Inv 3** (23C.1, crash-injection-no-partial-state): Crash atomicity for mutations/decisions/publication pointers/effects is proven on SQLite; source-cursor and full recovery crash injection runs in the PG+S3 industrial environment (env-gated here). No cross-topology crash claim beyond the CI-grade lab. Reason: proof exists but was environment-gated in the recorded run.
- **Inv 6** (23C.1, canonical-id-cross-platform): Determinism proven multi-OS × multi-Python on x86_64 with committed golden constants; the invariant's literal cross-language and ARM64 clauses have no executable fixture (no second-language implementation, no ARM64 runner). Reason: executed proof covers only part of the invariant wording.
- **Inv 25** (23C.3, routing-stays-shadow-until-proven): Routing demonstrably stays shadow/offline, but the promotion condition has not been evaluated against truly held-out shift + catastrophic utility (same fixture trains and evaluates); evidence is fixture-scoped. Reason: executed proof covers only part of the invariant wording.
- **Inv 38** (23C.4, failure-injection-truthful-outcomes): Cancellation/failover/disk/model-service(lease-lapse) classes are covered on the SQLite lane and DB-outage/WAL semantics on the PG failover lab; shared-memory pressure is not injected anywhere (the shared-memory lane was measured and rejected in PR68A), and no literal model-service crash injection exists. Reason: proof exists but was environment-gated in the recorded run.

### Type F — measurement/economics/operations closure missing

- **Inv 59** (23C.7, subsystem-owner-rollback-kill): Utility is measured per capability slice; the required per-subsystem support owner, rollback, expiry, and kill-condition matrix does not exist in any artifact. Reason: executed proof covers only part of the invariant wording.
- **Inv 60** (23C.7, leadership-claim-discipline): No leadership-claim completeness fields or validators exist: workflow, competitors, catastrophic budget, and review burden are not named in any artifact schema or test. Reason: only non-executable context (docs/prose) bound.
- **Inv 62** (23C.7, final-displacement-test): Concession language and specialist comparisons exist; the final rational-user displacement test (better accepted end-to-end outcome by leaving Marker UI) has never been executed as such. Reason: executed proof covers only part of the invariant wording.

### Next-slice ranking (groups with most non-proven invariants)

- 23C.1 Truth and persistence: 4 non-proven
- 23C.4 Runtime and jobs: 3 non-proven
- 23C.7 Economics and claim language: 3 non-proven
- 23C.2 Geometry, patches, and incrementality: 1 non-proven
- 23C.3 Verification and routing: 1 non-proven
- 23C.5 Source, authorization, and retrieval: 1 non-proven

## Reproduction

```bash
# from repository root
python backend/scripts/readiness_audit.py --mode integrity
# re-execute bound evidence and regenerate the snapshot:
python backend/scripts/readiness_audit.py --mode run-evidence
```

This report is generated from the canonical ledger and executed evidence; manual edits are detected by `--mode integrity`. An honest NOT READY verdict with valid evidence integrity is an accepted repository state.
