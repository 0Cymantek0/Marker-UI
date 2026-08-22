# PR71B — Connector Convergence Core (Readiness Invariant 42)

**Slice:** Connector event ingestion / convergence core — V3.2 amendment 16B.7
**Branch:** `markerui-v2`
**Implemented against head:** `94c0d466a119667e731c3712f23c702ab0bb79e2` (branch was clean; all work sits on top)
**Governing plan:** `planning/v2/marker-ui-v2-connector-convergence-inv42-implementation-plan-20260822.md`
**Readiness invariant 42:** “Connector events are idempotent and gap-aware; source state and cursor advancement commit together locally; token expiry/reset triggers reconciliation.”

---

## 1. What this slice delivers

A provider-neutral, durable **connector ingestion core** that turns a remote
provider’s unreliable incremental change stream (duplicate, out-of-order,
gapped, reset) into the existing local source truth, with an atomic
checkpoint. No real SaaS connector ships in this slice; a deterministic
scripted provider (`ScriptedProvider`) reproduces every failure mode so the
proofs are reproducible from a clean checkout.

Remote-source changes become the **existing** truth records
(`SourceIdentityRecord` / `ContentRevisionRecord` /
`AccessPolicyRevisionRecord` / `AccessDenialRecord` /
`SourceObservationRecord`) — there is no parallel connector-only truth
store, and no correctness-critical in-process state.

## 2. Architecture chosen, and why it preserves the fixed properties

### 2.1 One kernel transaction per application unit

`KernelCommitBatch` gained exactly one optional field — `connector:
ConnectorEffects | None` — mirroring the PR73 `view_advancement` seam:

* **check** (phase `connector-checked`, under the head-row writer lock,
  before any insert): `app.kernel.connector_state.check_connector_effects`
  validates stream state, cursor expectations, and durable event dedupe
  against current authoritative stream state.
* **apply** (phase `connector-applied`, after outbox insert, before head
  advance): `apply_connector_effects` inserts inbox rows and flips the
  stream checkpoint as a compare-and-set on the exact expected token.

A connector application unit therefore commits source records + edges +
`source.invalidated` outbox intent + inbox receipts + checkpoint movement
in ONE transaction whose database commit is the linearization point (P3,
P4). The kernel commit service stays a narrow authority — no arbitrary
callback surface was introduced.

### 2.2 Durable state (migration `20260823_0014`)

* `kernel_connector_streams` — one row per provider stream: the opaque
  cursor token that was last **durably applied** (never merely fetched),
  the provider sequence at that checkpoint (gap detection), and the
  explicit health state `consuming` / `reconciliation_required` with the
  recorded reason.
* `kernel_connector_inbox` — append-only receipt + classification evidence
  per provider event. `UNIQUE(stream_id, provider_event_id)` is the
  durable redelivery-dedupe authority; the committing transaction
  re-checks it under the writer lock, so a redelivered event can only be
  refused (`DuplicateConnectorEventError`) and converged, never
  double-applied.

### 2.3 Provider-neutral adapter contract (`app/services/connector_adapter.py`)

Adapters map one provider’s real mechanics (Drive change feeds, Graph
delta, webhook+poll) onto: `fetch_changes(cursor)` → `ChangePage`,
`fetch_item(item_id)` → authoritative current truth, and
`full_scan(resume)` → deterministic restartable `ScanPage`s. Provider
vocabulary stops at this boundary; the core never sees pagination or
notification details.

### 2.4 Convergence rules (why the traps are avoided)

* **Dedupe is layered, never single-key** (Traps C/D): event-id dedupe
  (inbox unique key) *and* source/revision identity convergence
  (`commit_converging`) *and* strictly-older sequence refusal. Equal
  sequence under a different delivery identity converges through record
  identity — state truth, not notification truth.
* **Arrival order is never causal order** (Trap B): sequenced providers
  are guarded by the inbox-recorded per-item `max(applied seq)`;
  ordering-free providers (`ordering="none"`) are resolved through
  `fetch_item` authoritative queries, never local timestamps.
* **Missing source is a security event** (Trap E): `removed` events
  (deletion *or* loss-of-access) mint an `AccessDenialRecord`
  (`target_kind="source"`) plus an `access_lost` observation; the real
  authorization overlay (`resolve_effective_authorization`) denies live
  reads immediately while immutable history stays inspectable (Trap F).
* **Reset is never “newest token”** (Trap G): token expiry / invalidity /
  provider reset / detected sequence gap park the stream in
  `reconciliation_required` with the cursor frozen; only a reconciliation
  scan’s final page may install a fresh checkpoint
  (`completes_reconciliation` is the only exit from that state, enforced
  by the kernel check).
* **Restartable reconciliation** (P6): scans apply page-wise with
  tentative checkpoints; a crash mid-scan restarts the scan (already
  applied pages converge as duplicates; the no-op guard skips checkpoint
  re-acknowledgement); an incomplete scan is never blessed as complete.

### 2.5 Source lifecycle semantics supported

New source · content update · repeated identical state · policy/ACL-only
update (no content revision minted) · deletion · loss-of-access · restore
(deny lifted via the append-only denial chain, content re-acquired) ·
move/rename under stable provider identity (metadata-only observation;
logical identity untouched) · delete+create with equal bytes stays two
logical sources (provider-qualified keys, never byte identity).

Connector sources use `SOURCE_KIND_CONNECTOR` with provider-qualified
`source_key`s (`connector:<provider>:<account>:<item>`); consistency class
is `version_pinned` when the adapter supplies a revision token, honestly
`best_effort_consistent` otherwise.

## 3. Verification (exact commands, run from repository root)

Focused suites (deterministic local SQLite lane — migrated file DB, real
kernel spine, real content-addressed store, real authorization overlay):

```bash
cd backend
python -m pytest tests/test_connector_state.py -q       # 9 passed
python -m pytest tests/test_connector_migration.py -q   # 5 passed
python -m pytest tests/test_connector_ingestion.py -q   # 27 passed
```

Adjacent regression set (kernel commit authority, fault injection,
concurrency, source acquisition, access records, authorization overlay,
outbox):

```bash
cd backend
python -m pytest tests/test_kernel_commit.py tests/test_kernel_faults.py \
  tests/test_kernel_concurrency.py tests/test_kernel_source_acquisition.py \
  tests/test_kernel_access_records.py tests/test_context_runtime_authorization.py \
  tests/test_kernel_outbox.py \
  tests/test_connector_state.py tests/test_connector_migration.py \
  tests/test_connector_ingestion.py -q
# 141 passed
```

Full CI lane:

```bash
cd backend && python -m pytest tests conformance -q
```

(Result recorded in the handoff commit message of this document.)

Industrial PostgreSQL lane (strict: real Docker PostgreSQL, failures on
missing prerequisites, no silent skips):

```bash
cd backend && python scripts/run_kernel_pg_conformance.py
# 109 passed, 6:50 — "PASS: dual-backend kernel conformance green with
#  real PostgreSQL", including the connector effects class on both
#  backend params (T29).
```

Readiness machinery:

```bash
python backend/scripts/readiness_audit.py --mode integrity
python backend/scripts/readiness_audit.py --mode run-evidence
```

## 4. Fault cases exercised

| Case | Mechanism | Proof |
|---|---|---|
| Before transaction | `_inject_fault_at="begin"` | T7: nothing visible |
| Bytes staged, pre-truth | `"records-inserted"` | T8: residue ≠ truth; no checkpoint |
| After outbox insert | `"outbox-inserted"` | T12: no orphan intent |
| After connector apply | `"connector-applied"` | T9: full rollback; retry converges |
| Immediately pre-commit | `"pre-commit"` | T10: all-or-nothing |
| Crash after commit, pre-ack | restart + replay | T11: duplicate convergence, head unmoved |
| Crash mid-reconciliation | `page_limit` restart | T17: resume; no partial-scan blessing |
| Concurrent duplicate workers | `asyncio.gather` | T25: one application, one inbox row |
| Overlapping polls | `asyncio.gather` | T26: one checkpoint; no fork |

## 5. Readiness outcome

Invariant 42 is bound as **proven** (full-coverage test binding) in
`docs/reference/readiness/readiness-ledger.json`, derived mechanically by
the PR84A auditor from the executed evidence run — never hand-marked. The
ledger’s honest residual classifications elsewhere are untouched.

## 6. Explicit nonclaims

* No claim that any real provider transport (Google Drive, Microsoft
  Graph, GitHub, Slack, …), OAuth flow, credential vault, or webhook
  deployment is production-ready. The shared convergence core and the
  deterministic adapter contract are the deliverable.
* No downstream incremental recomputation for every view/index/citation
  system is implemented; the durable `source.invalidated` intent is the
  authorization boundary future consumers attach to.
* The connector transaction path is proven on the strict PostgreSQL
  dual-backend lane (see §3), but the *full* connector behavioral matrix
  (T1–T26) executes on the deterministic SQLite lane; the equivalent
  full-matrix PG drill would follow the same one-service/one-protocol
  argument the dual-backend lane already exercises.
* Inv43 revocation SLO, Inv48 disclosure doc, and Inv54 remain open gaps.

## 7. Suggested next slice

Re-audit the new branch head from scratch (the ledger now shows one fewer
Type-A gap). Candidates by value: **Inv54 AnswerContextTrace** (last
remaining Type-A implementation gap after this slice) or the economics /
scale-envelope closure group (57–62) — selection should follow the new
readiness state, not this document.
