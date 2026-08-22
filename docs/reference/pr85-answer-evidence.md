# PR85 — Answer Evidence Boundary: Durable Context Trace + Independent Support Assessment

**Date:** 2026-08-23
**Scope:** masterplan §9C.11 (retrieval proof is not entailment proof), §16B.11 (external disclosure boundary); readiness invariants **54** (trace-not-entailment-proof) and **48** (disclosed-context non-revocability documentation).
**Migration:** `20260823_0015_add_answer_evidence` (head; tables start empty).

---

## 1. The boundary decision — what Marker UI can honestly observe

Marker UI's `/agent` path is a **retrieval** API. Answer generation happens
in an external agent (model host, coding agent, or human). Therefore the
strongest disclosure fact this architecture can observe is the **delivery of
a query-result page (an `EvidencePacket`) to the caller** at
`run_agent_query` return time.

The subsystem's honest claim vocabulary is, from weakest to strongest:

1. *retrieved candidates* — what the executor considered;
2. **delivered context** — what Marker UI actually put on the wire to the
   caller (this subsystem's record);
3. ~~model attention~~ — **not observable**; Marker UI does not host the
   generation model and must never phrase a trace as knowing which tokens
   the model used.

PR85 records level 2. Nothing in the storage, service, API, or
documentation claims level 3.

## 2. Three durable concepts (and what they are not)

| Concept | Table | Is | Is not |
|---|---|---|---|
| Context disclosure | `kernel_context_disclosures` | one immutable record per delivered packet page, carrying the full canonical packet JSON (ordered evidence, publication/snapshot identity, authorization view, budget, partial/complete status) | not a support verdict; not reusable as proof of anything beyond delivery |
| Answer context trace | `kernel_answer_traces` + `kernel_answer_trace_disclosures` | one immutable binding of an external answer (`answer_ref`, content, digest) to an **ordered** list of disclosure ids | not evidence the answer is *entailed*; not mutable after commit |
| Answer support assessment | `kernel_answer_support_assessments` | an append-only independent judgment (`supported` / `unsupported` / `uncertain`) with per-claim spans, quote digests, cited delivered evidence, and assessor provenance | not an answer edit; not a retrieval artifact; `unassessed` is absence of a record, never implicit support |

Retrieval provenance stays where it was: `EvidencePacket` /
`AgentRetrievalOutcome` remain pre-answer retrieval records and never
acquire a support field.

## 3. Workflow

1. **Query with disclosure.** `marker_query(..., disclose=true)` runs the
   normal snapshot-safe query; every delivered page that carries a packet
   is recorded as a disclosure **before the response is returned**, and the
   envelope carries the minted `disclosure_id`. If the disclosure row
   cannot be written, the page is not delivered as a disclosable result.
2. **Answer externally, then commit.** `marker_answer_trace(workspace_id,
   answer_ref, answer, disclosure_ids)` binds the answer to the ordered
   disclosure set in one transaction (trace + all links commit together).
3. **Assess independently.** `marker_answer_assessment(workspace_id,
   trace_id, verdict, claims, assessor, assessment_key[, rationale])`
   appends a judgment that validates against the immutable committed
   truth and never touches it.

## 4. Guarantees and how they are enforced

- **Order + answer-time fidelity.** The canonical packet JSON of each
  disclosure is frozen at delivery; trace link rows carry `position`.
  History is never recomputed from later retrieval, policy, or
  publication state (a later republish, deny-overlay change, or packet
  invalidation cannot rewrite a stored trace).
- **Idempotency and conflict truth.** `(workspace_id, answer_ref)` is
  unique. Identical replay (same answer digest + same context
  fingerprint — a deterministic hash over ordered `(disclosure_id,
  packet_id)` pairs) returns the committed trace; any different body or
  context set (including order-only changes) is an explicit
  `AnswerTraceConflictError`. Concurrent same-ref commits converge through
  the unique constraint, not through luck.
- **Assessment lifecycle.** `(trace_id, assessment_key)` unique: identical
  payload replay is idempotent (payload digest compare); key reuse with a
  different payload conflicts. Assessments append with per-trace `seq`;
  the current judgment is derived (highest seq), never denormalized onto
  the trace. Two racing assessors serialize on the unique constraints and
  both remain in history.
- **Assessment cannot mutate the answer.** There is no code path that
  updates `kernel_answer_traces` after commit; assessment validation
  *reads* the stored answer (spans must cover it; optional `quote_digest`
  must match the stored slice; cited evidence locators must exist inside
  a disclosure bound to the trace — fabricated citations fail closed).
- **Tenancy fails closed.** Every lookup is workspace-scoped. Linking and
  assessment tables carry composite tenant foreign keys
  (`(workspace_id, …)` → owning table) so a cross-workspace reference is
  structurally unrepresentable on PostgreSQL; the SQLite development lane
  enforces the same rule at the service layer with identical error
  shapes (a foreign id is indistinguishable from a nonexistent one).
- **Restart durability.** All three concepts are ordinary migrated
  database rows; a new process/session recovers the same trace,
  ordering, and assessment history.

## 5. External disclosure boundary — the non-revocability contract

This is the canonical statement required by readiness invariant 48; the
executable contract check lives in
`backend/tests/test_answer_evidence_docs.py` and fails if this section
loses its meaning.

1. **Future disclosure is revocable.** When access or policy changes,
   Marker UI stops disclosing context going forward: it denies new
   queries, invalidates live cursors and cached packets, terminates
   streams, and gates exports under the current authorization epoch.
2. **Past external disclosure is not revocable.** Marker UI **cannot
   retroactively revoke, unsend, or unsee context that was already
   disclosed (delivered) to an external agent**: bytes already
   transmitted to that external system may persist in its context
   windows, logs, finetuning or memory beyond Marker UI's reach. No
   Marker UI operation — cursor invalidation, packet reuse refusal,
   policy revocation, or deletion — reverses that external copy.
3. **Local retention is a different thing.** Marker UI may apply its own
   retention, redaction, or deletion rules to *its local* disclosure
   records and traces. That is record hygiene and audit-policy
   compliance; it must never be presented as, or mistaken for, remotely
   revoking what the external agent already received.

Documentation and product surfaces must not imply that "revoking access
removes all prior access." Revocation removes *future* access; the
disclosed past stays disclosed.

## 6. Crash and retry semantics

- **Crash before disclosure commit** → the page was never returned as
  disclosable; no durable fact. (Record-before-return ordering.)
- **Crash after disclosure, before answer commit** → the disclosure row
  is an honest "context was disclosed, no answer bound it yet" record;
  the answer commit is a separate caller-driven step, so there is no
  half-trace state. A trace never exists without its full link set
  (single transaction), so there is also no "answer without its context"
  state.
- **Client retry after timeout** → re-commit with the same `answer_ref`
  and identical payload converges to the same trace; a mutated retry
  conflicts loudly. Assessment retries behave identically via
  `assessment_key`.

## 7. Residual limits (what PR85 does not claim)

- Marker UI cannot observe which delivered tokens the external model
  actually attended to; a trace is delivery provenance, not an attention
  record and not an entailment proof.
- Assessment quality depends on the named assessor/procedure; storing a
  judgment does not certify the judgment (the evaluator's lineage is
  itself evidence, §5C.4 authority ordering still applies).
- Retention/GC policy for disclosure rows (they contain source-derived
  text under the packet's sensitivity rules) is deliberately not invented
  here; they are append-only until a later retention slice governs them.
- The SQLite development lane enforces tenant FK semantics at the
  service layer; the PostgreSQL industrial lane enforces them in the
  database (migration declares the composite FKs).

## 8. Evidence

- Migration authority: `backend/tests/test_database_migration.py`
  (expected head `20260823_0015`, chain, table set, sentinel survival),
  `backend/tests/test_kernel_migration.py`,
  `backend/tests/test_context_runtime_cursor_migration.py`.
- Domain + boundary semantics (acceptance matrix AE-01…AE-22):
  `backend/tests/test_answer_evidence.py` (service/boundary),
  `backend/tests/test_answer_evidence_boundary.py` (end-to-end through
  `run_agent_query` disclosure minting),
  `backend/tests/test_answer_evidence_docs.py` (executable documentation
  contract for invariant 48),
  `backend/tests/test_agent_answer_surface.py` (MCP registry/scope
  facts).
- Readiness ledger bindings for invariants 48 and 54 point at these
  nodes; the reports under `docs/reference/readiness/` are regenerated
  by `backend/scripts/readiness_audit.py --mode run-evidence`.
