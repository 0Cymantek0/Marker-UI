# Bounded Typed Queries and EvidencePackets (PR77)

Status: implemented (V3.2 PR77). Module:
`backend/app/context_runtime/` (`contract.py`, `executor.py`,
`packets.py`, `errors.py`). Substrate: PR76 publication sets. No new
migration (pure runtime layer over the PR76 serving schema).
Measurements: `docs/reference/measurements/pr77-bounded-query.json`.

## What this layer is

A transport-agnostic, server-side query core standing on the PR76
publication substrate. A caller hands over a **typed request object**
(`marker.query.v1`) — never SQL, never FTS5 syntax — the server
validates it against a finite operator algebra and structural budgets,
executes it against **exactly one pinned PublicationSet**, and returns
an **EvidencePacket**: bounded retrieval/context evidence with explicit
provenance, omissions, budget accounting, and a deterministic reuse
identity.

An EvidencePacket proves what evidence was selected and delivered. It
does **not** claim answer correctness or entailment.

## Supported operators (the complete finite set)

| Operator | Typed shape | Semantics |
|---|---|---|
| `lexical_search` | `{op, text, mode: all_terms \| any_term \| phrase, limit}` | Plain-text search through the pinned lexical generation. The server NFC-normalizes and whitespace-collapses `text`, rejects tokens with no searchable characters, and compiles every token into a doubled-quote FTS5 phrase — user bytes can never become MATCH grammar (`OR`, `NEAR(...)`, column filters, bareword operators in text are literal content). `limit` ≤ 200. |
| `record_get` | `{op, record_id, node_id?}` | Exact selection of one record from the pinned set's materialized generation (identity-hash re-verified). With `node_id`: one content node with its text; without: the whole record with its payload hash and no text-body claim. |

Everything else in the masterplan's bounded query algebra —
`vector_search`, `visual_search`, `structural_traverse`,
`relation_traverse`, `field_predicate`, `aggregate`,
`compare_revisions`, `rerank`, `evidence_select` — is a **named future
operator**: requesting it raises `UnsupportedOperatorError` with the
supported list. There is no fallback from an unsupported operator to
lexical/text search.

Unknown operators, unknown fields, wrong schema version, malformed
values, and over-hard-cap shapes raise `QueryContractError`. A valid
request whose operation count exceeds its budget raises
`QueryBudgetError` **before any execution**.

## Invariants

- **One pinned set per query.** `execute_query` resolves the published
  head once, pins it, and serves every operation — lexical and exact —
  from that set's members. A publication-head switch mid-execution
  cannot mix generations inside one packet (deterministic test hook
  `_after_operation` proves this; see the benchmark's
  `across_publication_switch` slice).
- **PR76 integrity stays authoritative.** Lexical retrieval runs only
  through `PublicationReader.search` (locator re-verification, text
  hash alignment, orphan-hit refusal). Exact reads re-verify the
  record's identity hash. Tampered state fails closed as
  `PublicationIntegrityError`; this layer never substitutes a stale or
  fallback generation.
- **Execution is bounded.** Budgets: `max_operations` (default 8, hard
  cap 32), `max_candidates` (200), `max_evidence_units` (50),
  `max_output_chars` (100,000). Candidate accumulation spans the whole
  query (repeating an operator cannot reset accounting); lexical
  retrieval probes one row beyond the requested limit so a truncated
  page reports `candidate_budget` instead of presenting itself as
  exhaustive.
- **Output stays structurally whole.** The indivisible unit is one
  evidence unit (locator + text). Under budget pressure whole units are
  omitted with explicit reasons — `output_budget`, `unit_budget`,
  `unit_too_large` (a single unit larger than the whole output budget
  is refused, never cut mid-structure). Any budget-driven omission
  marks the packet `partial`.
- **Duplicates are explicit.** The same source locator + content hash
  selected through multiple operations yields one unit plus `duplicate`
  omissions.
- **Pins never leak.** The publication pin is released in `finally` on
  success, error, budget termination, and cancellation. After release,
  PR76 GC rules apply normally.
- **Empty is honest.** No lexical hits → `no_hit`; absent record →
  `not_found`; absent node → `node_not_found`; unpublished workspace →
  `unpublished`. All are explicit omissions in a valid, attributed (or
  explicitly unattributed) packet — never fabricated evidence.

## EvidencePacket identity

`identity_id` is `record_identity_hash` over canonical dimensions
(`marker.evidence_packet.v1`):

- the normalized query (schema version, workspace, profile, canonical
  operations, output directive, budget profile);
- publication attribution (set id, member generation ids, tokenizer,
  kernel cut, snapshot, set content digest);
- evidence locators + content hashes, in selection order;
- omission reasons;
- the caller-supplied context: `security_context_id`,
  `verifier_policy_id`, `redaction_profile_id`,
  `serialization_profile`.

Runtime-only values (timing, pin ids) never enter identity, so
identical request + identical published state reproduce the identical
packet (including across process restart). Any relevant change — new
publication set, moved content revision, changed security/verifier/
redaction/serialization dimension, changed budget or output directive —
changes the identity.

**The context fields are identity seams, not authorization proof.**
They are carried and hashed exactly as supplied; PR78 owns real policy
semantics and pre-retrieval scope constraints. Nothing in this layer
claims an access decision.

## Known limits (deliberate)

- No externally reachable retrieval endpoint. `execute_query` is the
  internal application-service callable; a future agent/MCP transport
  (and PR78's authorization-first layer) wraps it. Publishing a
  retrieval endpoint before authorization exists would permanently
  bypass PR78.
- No signed/portable cursors or cross-client continuation (PR79). A
  `partial` packet is the whole continuation story in this slice.
- No vector/visual retrieval (the PR76 vector slot stays an explicit
  absent until PR81); no natural-language/model query planning — the
  typed server contract comes first, a later model can draft typed
  plans into this validator.
- The authorization seam is opaque caller-supplied ids only.
- One lexical generation per set (unicode61 tokenizer), as in PR76.

## Reproduce

```
python -m pytest backend/tests/test_context_runtime_contract.py \
  backend/tests/test_context_runtime_execution.py \
  backend/tests/test_context_runtime_packets.py \
  backend/tests/test_context_runtime_lifecycle.py \
  backend/tests/test_kernel_publication_reader_records.py -q

python backend/scripts/bench_pr77_bounded_query.py --write
```

The benchmark exit code is nonzero if any structural acceptance check
fails (operation count bounded, units within caps, single-set
attribution, explicit partial under budget pressure, pinned across a
mid-query publication switch, stable identity over unchanged state).
