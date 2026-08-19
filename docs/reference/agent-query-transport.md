# Agent Query Transport and Durable Reconnect (PR79B)

**Status:** implemented on `markerui-v2` (2026-08-19)
**Builds on:** `docs/reference/snapshot-safe-query-continuation.md` (PR79A), `app/kernel/events.py` (PR67A durable events)
**Evidence:** `docs/reference/measurements/pr79b-agent-query-transport.json`

## What this phase adds

PR79A delivered a transport-agnostic, authorization-first, snapshot-safe
query and continuation backend with deliberately no externally reachable
surface. PR79B crosses that core to the supported agent boundary without
changing its authority:

- `marker_query` — canonical v2 MCP tool (minimal profile, `queries:read`
  scope, read-only annotations) running the real `ContinuationService`.
- `marker_events` — canonical v2 MCP tool (minimal profile, `events:read`
  scope) exposing the durable per-workspace semantic event log for
  disconnect-safe resume.
- `backend/app/agent_query.py` and `backend/app/agent_events.py` — thin
  transport-neutral adapters between the MCP tools and the context
  runtime / kernel event log. There is no second query executor and no
  second event history.
- Authenticated transport caller binding: continuation cursors issued on
  authenticated streamable HTTP are bound to the server-validated
  principal and cannot be resumed by a different principal.

## The agent contract

### marker_query

One tool, two mutually exclusive modes:

- **fresh**: pass a `marker.query.v1` request object (`query` argument).
  The adapter pre-validates through the authoritative contract parser, so
  unsupported operators, budget violations, and malformed requests are
  explicit usage errors (MCP tool errors), never silent downgrades.
- **continuation**: pass the opaque `next_cursor` from a partial result
  plus the `workspace_id` that owns it.

The result is the machine-readable `marker.query_result.v1` envelope:

| Field | Meaning |
|---|---|
| `schema_version` | `marker.query_result.v1` |
| `status` | `complete`, `partial`, `invalidated`, `stale`, `loop_limit`, `policy_fail_closed`, `execution_failure` |
| `result` | `cumulative_budget` (server-authoritative pages/work/output) plus the serialized `marker.evidence_packet.v1` packet when protected output is released |
| `next_cursor` | present exactly when `status` is `partial` |
| `reason` / `error_code` | stable class strings; capability failures stay in the coarse `cursor_invalid` class PR79A established |

The envelope is forward-compatible: future statuses (for example PR80
`review_required`) extend the `status` field without redesigning the
envelope. Domain outcomes are results, not protocol errors; only contract
misuse raises tool errors.

### marker_events

`marker_events(workspace_id, stream="work", after_sequence=0, limit=N)`
returns one ordered page of the authoritative `(workspace, stream,
semantic_sequence)` log plus `latest_sequence`, `next_after_sequence`,
and `has_more`. Resume semantics:

- the client records the last delivered `semantic_sequence`;
- after any disconnect it calls again with that value as `after_sequence`;
- it receives exactly the missing tail in authoritative order;
- duplicate transport redelivery is de-duplicable by `semantic_sequence`.

Ordering is the durable sequence, never arrival time or timestamps. Reads
are isolated per workspace and per stream.

## Trust model and principal binding

- **Authenticated streamable HTTP**: the caller principal derives from the
  server-validated MCP access token (`client_id` plus a token digest) in
  `mcp_server._mcp_caller_principal_id()`. It is stamped into the durable
  cursor row (`kernel_query_cursors.principal_id`, migration
  `20260819_0013`) at issuance and rechecked on every continuation. A
  cursor issued to principal A fails closed for principal B or for an
  unauthenticated caller, through the collapsed `cursor_invalid` class,
  without revoking A's row. Caller-supplied `QuerySecurityContext` fields
  remain packet-identity metadata and never become authentication proof.
- **stdio / loopback no-auth**: there is no server-validated identity, so
  cursors stay explicitly unbound (`principal_id` NULL). This is the
  documented local trust model, not a multi-user claim.
- `run()` now enables bearer auth whenever `MARKER_AUTH_TOKENS` is
  configured (per-token scopes), not only when a single
  `MARKER_MCP_AUTH_TOKEN` exists. Non-loopback binding without any
  configured token still refuses to start.
- This phase adds **no** OIDC verifier, group membership, enterprise
  document ACL, or per-principal workspace ACL. Authorization inside the
  query path remains the PR78 `local_v1` committed-policy model.

## Durable reconnect design decision

The MCP Python SDK's streamable-HTTP `event_store` seam (SSE
`Last-Event-ID` replay of JSON-RPC messages) is incompatible with this
server's supported configuration: Marker runs FastMCP in stateless +
JSON-response mode, where the SDK never wires an event store into
transports. Switching to stateful SSE sessions would change the deployment
contract far beyond this phase.

PR79B therefore exposes durable reconnect as a **typed pull surface**:
`marker_events` with server-issued sequence identity, backed directly by
the durable `kernel_events` log. This satisfies the required properties —
no lost semantic truth, deterministic de-duplication, restart-safe resume,
slow or absent consumers cannot influence execution — and works
identically over stdio and streamable HTTP. The conformance suite proves
resume across client disconnect, cross-stream isolation, and a full server
process restart over the same database.

**Explicit non-claim:** protocol-level SSE `Last-Event-ID` replay of
JSON-RPC message streams is not implemented and not claimed in this
phase.

## Cursor key management

Cursor HMAC keys resolve in order:

1. `MARKER_QUERY_CURSOR_KEY` (dedicated override);
2. the deployment encryption key (`ENCRYPTION_KEY` env or the generated
   `data/.encryption_key` file), domain-separated via HKDF-style
   derivation;
3. an ephemeral process key, logged loudly. Chains then die with the
   process, which the 60-second cursor TTL already bounds operationally.

Restart-safe continuation requires stable key material; the conformance
suite pins `ENCRYPTION_KEY` to prove the shared-key restart path.

## Residuals and non-claims

- `principal_id` is a transport binding, not an authorization model.
  Within one configured token, workspace-level `local_v1` policy still
  governs everything.
- Pre-PR79B cursor rows (NULL `principal_id`) and stdio cursors remain
  caller-agnostic by design.
- No exactly-once network delivery claim; resume is at-least-once with
  stable sequence identity for de-duplication.
- PR80 extraction/review statuses, PR81 visual/vector retrieval, and
  PR83 PostgreSQL/object-store work remain deferred.

## Where to look

- Adapters: `backend/app/agent_query.py`, `backend/app/agent_events.py`
- MCP tools: `backend/app/mcp_server.py` (`marker_query`, `marker_events`,
  `_mcp_caller_principal_id`)
- Registry/scopes: `backend/app/agent_surface.py`,
  `backend/app/security/scopes.py`
- Binding core: `backend/app/context_runtime/service.py`
  (`principal_id`), `backend/app/kernel/models.py`
  (`KernelQueryCursor.principal_id`), migration `20260819_0013`
- Tests: `backend/tests/test_agent_query_adapter.py`,
  `backend/tests/test_agent_events_adapter.py`,
  `backend/tests/test_mcp_query_tools.py`,
  `backend/tests/test_pr79b_transport_conformance.py`
