# Snapshot-Safe Query Continuation (PR79A)

Status: implemented backend core (V3.2 PR79A). Modules:
`backend/app/context_runtime/service.py`,
`backend/app/context_runtime/continuation.py`,
`backend/app/context_runtime/continuation_state.py`,
`backend/app/context_runtime/continuation_paging.py`,
`backend/app/context_runtime/continuation_store.py`, and
`backend/app/context_runtime/cursor.py`. Measurements:
`docs/reference/measurements/pr79-snapshot-safe-query-continuation.json`.

PR79A adds bounded, cross-call continuation to PR77 typed queries and PR78
authorization-first retrieval. `ContinuationService` returns a structured
outcome: a complete page, a partial page with one opaque cursor, or a terminal
status such as invalidated, stale, loop-limited, policy-fail-closed, or
execution-failure. Evidence remains an `EvidencePacket`; cursor lifecycle is
not inferred from packet prose or exception text.

## Guarantees

Each continuation chain has one immutable content snapshot. The first page
opens and pins one published set. The durable cursor row stores the exact
publication and generation identity, and every later page opens that pinned
set. A publication-head switch therefore cannot mix page generations. The
checked-in benchmark proves a four-page tied-rank lexical traversal with an
ordered locator digest, zero duplicates, and zero skips, plus an independent
head-switch check.

Authorization is live. Before a continuation is delivered, the service
resolves trusted `local_v1` authorization again and compares its identity
view. A domain deny, record deny, access-policy revision, or authorization
epoch advance invalidates the old cursor. Domain/record/epoch/policy changes
do not expose the hidden target or denial basis through the outcome. The
benchmark checks both deny granularities, both policy identity dimensions, and
serialized nondisclosure.

Lexical keyset state uses the publication reader's deterministic
`(bm25 rank ASC, row_index ASC)` order plus publication-set, lexical-generation,
and query-hash bindings. Exact `record_get` state uses durable operation
positions. Cursor budget state is cumulative: candidate work, evidence units,
output characters, operations, pages, raw traversal work, and emitted locator
keys survive page rotation. A page-size choice cannot reset the original
request caps. `max_chain_pages`, lexical traversal caps, and atomic nonce
claims bound excessive or oscillating work.

## Cursor and retention design

The client token is a signed reference, not a serialized query. Its only
claims are protocol version, signing-key id, random state handle, and random
replay nonce. Query text, publication identity, authorization identity,
keyset, budget, expiry, and pin metadata stay in canonical JSON in the
`kernel_query_cursors` row. HMAC-SHA256 signing material is injected through a
`CursorKeyring`; no secret is generated or hardcoded by the cursor module.

The cursor row starts with a fresh nonce. Continuation atomically claims that
nonce, runs one page, and rotates it before issuing the next token. Reusing a
nonce fails closed. A second concurrent claimant cannot revoke the in-flight
claim; an abandoned claim remains bounded by the cursor expiry and sweeper
claim timeout. Terminal, invalidated, expired, or reclaimed rows release their
publication pin. A valid cursor retains its exact set, and its pin lease is
clamped to the cursor lifetime. The benchmark observes pin retention during a
valid cursor, release after terminal completion, and release after expiry
reclaim.

Expiry is server-side row state, checked against the service clock. Expiry is
strict at the boundary. It is not a client-controlled token claim. The
reclaimer removes expired, terminal, and abandoned-claimed cursor rows and
releases any returned pin ids.

## Security and correctness answers

1. **What binds a cursor to its query and publication?** The durable row binds
   normalized query and workspace to snapshot id, materialized-generation id,
   publication-set id, lexical-generation id, profile, kernel cut, tokenizer,
   row count, keyset, cumulative budget, and a replay nonce. The signed token
   references only that row.

2. **What authorization is checked again?** The trusted resolver recomputes
   profile, assurance, epoch number/fingerprint, deny revision, and policy
   digest. Continuation compares this complete caller-safe identity view with
   the row before reading the next page, and checks it again after page work.

3. **What prevents forgery, and how do keys rotate?** HMAC-SHA256 covers the
   canonical version/key-id/handle/nonce envelope. New tokens use the current
   injected key. A rotation window can retain old verification keys; once an
   old key is retired, its token fails closed. The benchmark exercises both
   overlap verification and retired-key rejection.

4. **Does the token reveal topology?** No query, record, domain, denial,
   snapshot, or budget fields enter the token. The handle, nonce, version, and
   key id are opaque protocol values. Durable server rows are sensitive state
   and must not be logged or exposed as a transport response.

5. **What ordering is deterministic?** Lexical pages use `(bm25 rank,
   row_index)` ascending, with row index as the tie-breaker and bindings for
   publication set, lexical generation, and normalized query hash. Exact reads
   advance each operation's one-shot position. Unsupported operators do not
   get a cursor fallback.

6. **How do cumulative budgets work?** The row stores a versioned budget
   object containing candidates considered, evidence units, output characters,
   operations executed, pages, raw work units, and emitted locator keys. Each
   page copies and advances those counters; hard request caps are checked
   before another cursor is issued. The benchmark reaches an explicit unit
   budget terminal and a separately configured low traversal-work terminal.

7. **How are retention pins extended and released?** A fresh query
   acquires exactly one durable publication pin up front, leased to the
   candidate cursor lifetime; it protects the first-page read and, when
   the page stays partial, becomes the cursor row's pin unchanged. Every
   continuation opens the pinned set through that same pin — no second
   transient pin is acquired per page, and page rotation never extends the
   lease beyond the original cursor expiry. Completion, invalidation,
   policy failure, expiry, and reclaim clear/release the pin; release is
   also attempted during cleanup failure paths.

8. **What is replay/loop defense?** A one-time durable nonce claim and nonce
   rotation stop token replay, including concurrent reuse. A maximum chain-page
   count stops deliberate continuation loops. Cursor, pin, and claim leases
   bound abandoned work after process failure.

9. **What caller binding is trustworthy today?** `local_v1` has a workspace
   base grant and trusted deny overlay, but no identity provider, session
   principal, group ACL, or connector identity. `workspace_id` supplied to
   `continue_query` is checked against the stored workspace, but is not a
   substitute for authenticated principal binding. Possession of a valid
   token and knowledge of its workspace remains a local-product residual.
   Caller context hints never grant access.

10. **Which invalidation reasons are collapsed?** Malformed, unsupported,
    tampered, unknown-key, missing-row, wrong-workspace, query-mismatch,
    replay, nonce-race, and rotation-race cases return the broad
    `cursor_invalid` category — the structured outcome's `error_code`
    matches that category exactly, so a caller cannot distinguish replay
    from tamper from internal state races. Domain, source, record,
    policy-revision, and epoch changes return the broad
    `authorization_changed` category. They do not name the hidden target.
    Missing/expired pinned state and unusable row state are collapsed the
    same way into `pinned_state_unavailable`/stale, and a missing
    high-assurance partition is policy-fail-closed. Server logs retain the
    finer internal cause at info level; caller outcomes stay bounded.

11. **What happens after key rotation or restart?** During key overlap, old
    tokens verify and new tokens use the new key. After retirement, old tokens
    fail closed. A restart can resume rows when the durable cursor database,
    publication state, and required verification key are shared with the new
    process. A process-local database, missing key, or different keyring does
    not resume the chain; it fails closed. PR79A does not provide a distributed
    key service or an in-memory-only continuation mode.

12. **What was deferred to PR79B — now landed.** The `marker_query` MCP
    registration, agent query contract, authenticated transport-edge
    principal binding (`kernel_query_cursors.principal_id`), structured
    `marker.query_result.v1` adapter, and durable event reconnect/client
    conformance are implemented in PR79B; see
    `docs/reference/agent-query-transport.md` and
    `docs/reference/measurements/pr79b-agent-query-transport.json`. This
    document remains the authority for the continuation backend semantics
    underneath that transport.

## High assurance and nondisclosure

`assurance: "high"` routes from trusted authorization-derived partition
profiles. It never accepts a caller-named partition and never falls back to
the shared profile. A missing partition returns structured
`policy_fail_closed`; a domain change invalidates a high-assurance cursor.
The benchmark checks both conditions.

The token boundary does not make this a principal ACL system. Standard-mode
shared-index score changes caused by hidden corpus growth remain the PR78
declared residual. Timing measurements remain characterization only. No
constant-time, timing-isolation, or side-channel elimination claim follows
from the local benchmark.

## Reproduce

```text
python backend/scripts/bench_pr79_continuation.py --write
python -m pytest backend/tests/test_context_runtime_continuation.py \
  backend/tests/test_context_runtime_service.py \
  backend/tests/test_context_runtime_cursor_migration.py -q
```

The benchmark exits non-zero if any scenario is blocked or any structural
acceptance boolean is false. Its JSON records producing git SHA, schema
versions, runtime metadata, page/generation bindings, counts, booleans, and
timing characterization. It does not turn wall-clock measurements into a
security claim.

## Deliberate deferrals

PR79A does not add PR80 extraction/reconciliation/reviewer workflows,
`review_required`, specialist displacement, or a new per-principal ACL
system. It also does not add vector/visual retrieval or new query algebra.
Those remain future slices; no producer is fabricated here.
