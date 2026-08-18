# Authorization-First Retrieval (PR78)

Status: implemented (V3.2 PR78). Modules:
`backend/app/context_runtime/authorization.py` (trusted resolver),
`backend/app/context_runtime/executor.py` (enforcement),
`backend/app/services/query_policy.py` (operator policy writes),
`backend/app/kernel/records.py` (`SecurityDomainRecord`,
`AccessDenialRecord`), `backend/app/kernel/publications.py`
(security-domain lexical partitions). Substrate: PR76 publication sets,
PR70 source/access truth, PR77 typed query contract. No new migration
(pure runtime layer; domain assignments and deny events are kernel
records on the existing spine). Measurements:
`docs/reference/measurements/pr78-authorization-retrieval.json`.

## What changed

A bounded query (`marker.query.v1`) is no longer just publication-
pinned — it is **authorization-first**. Before any protected retrieval:

1. the server resolves an **effective authorization** from committed
   truth only: the workspace's current `AuthorizationEpochRecord`, the
   latest `SecurityDomainRecord` per source, the latest
   `AccessDenialRecord` event per target (record / source / domain),
   and the latest PR70 `AccessPolicyRevisionRecord` per source;
2. `record_get` resolves the record's source lineage through verified
   pinned reads and refuses unauthorized material with the exact
   caller-visible shape of a missing record;
3. `lexical_search` walks the pinned generation's deterministic rank
   order keeping only authorized candidates, until the probe target is
   reached or the corpus is exhausted — counts, budgets, and
   more-matches signals describe the **authorized universe** only;
4. authorization is re-resolved before every operation: content stays
   pinned to one PublicationSet, **policy does not** — a deny committed
   mid-query linearizes before the next operation.

Caller-supplied `QuerySecurityContext` fields remain identity seams for
packet reuse (as in PR77) but grant nothing: the resolver never reads
them.

## Security model (honest, local v1)

There is no identity provider in this slice, so the profile does not
pretend to have one. `local_v1` states exactly what it defends:

- **Base grant:** the workspace boundary. Every record in a workspace's
  published set is readable unless a live deny excludes it (by record
  id, source id, or security domain).
- **Deny overlay:** an append-only event chain keyed to stable identity
  (`marker.kernel.access_denial.v1`). The latest event per target wins;
  `denied=false` is the explicit lift. A deny outruns reindexing: it is
  checked per candidate at delivery time, so stale FTS/materialized
  rows can never resurface revoked content (proven in tests and the
  benchmark while the stale rows are shown to still exist).
- **Epoch:** workspace-level `AuthorizationEpochRecord` (PR70) advances
  on local-domain facts. It invalidates packet identity but is not a
  per-document ACL.
- **Security domains:** `marker.kernel.security_domain.v1` assigns a
  source to a domain. Assignment is policy, not content: reassigning a
  source mints a policy record and touches no content revision.

## Assurance modes

The request gains `assurance: "standard" | "high"` (default
`standard`).

| | standard | high |
|---|---|---|
| Corpus | shared publication profile (`request.profile`) | derived `ha.` partition profile over the authorization-visible domains |
| Forbidden-domain rows | filtered **during** rank traversal; never candidates, never counted | **physically absent** from the corpus (separate FTS5 table per partition) |
| bm25 score basis | shared-index corpus statistics (declared residual: rank *values* a caller sees can shift when hidden-domain content changes) | isolated: forbidden growth provably leaves authorized order and scores byte-identical |
| Revocation | live deny overlay, immediate | live deny overlay, immediate (inside the partition too) |
| Missing corpus | honest `unpublished` empty packet | **fails closed** (`QueryAuthorizationError`); no shared-index fallback |

Partition profiles (`ha.<digest>`) are derived from trusted state and
reserved: the contract rejects them if a caller names one, and high
assurance routes by the resolver's derivation regardless of the
request's `profile` field. Publishing a partition is an operator
action (`PublicationService.publish_high_assurance(...,
partition_domains={...})`); the partition corpus contains only records
whose committed lineage (view → content revision → source → latest
in-generation assignment) resolves into the declared domains —
unresolvable lineage is excluded, never guessed in.

## Nondisclosure contract

- Unauthorized and nonexistent exact reads share one outcome template
  (`not_found`, same detail shape, same status) — no existence oracle.
- A lexical query matching only forbidden content returns the same
  honest `no_hit` as a term matching nothing.
- Probe-one-beyond-limit and candidate counts operate over the
  authorized universe; hidden matches never surface as "more matches
  exist".
- Packet `authorization` identity view is digests and counters only —
  no domain names, denied ids, or denial reasons are serialized.
- Timing: allowed / unauthorized / nonexistent exact paths follow the
  same code path by design; the benchmark records percentiles as
  characterization. This is **not** a constant-time claim; dedicated
  timing isolation is explicitly deferred.

## EvidencePacket identity

Identity dimensions now include the trusted authorization view:
`profile`, `assurance`, `epoch_number`, `epoch_fingerprint`,
`deny_revision`, `policy_digest` (a digest over assignments, deny
sets, and PR70 access-policy revision identities). Reuse is therefore
invalidated by any authorization change that can change what evidence
is legally visible — including lifts that restore an earlier set state
(`deny_revision` still moves) — while unrelated content commits do not
churn identity.

## Why not a final-row filter (architecture note)

The simpler alternative — rank globally, then drop forbidden rows —
fails three ways, each demonstrated in the adversarial suites and the
benchmark:

1. **Authorized recall dies under top-K pressure.** A fixed over-fetch
   loses authorized hits when forbidden matches crowd the shared
   ranking; the implementation keeps walking the deterministic order
   and the benchmark proves recall of a deliberately low-ranked
   authorized hit behind 30+ forbidden documents.
2. **bm25 statistics leak and distort.** `bm25()` uses corpus-level
   statistics (total rows, per-phrase document frequency). Filtering
   delivered rows does not stop forbidden content from shifting
   authorized scores — the benchmark records the shared index shifting
   rank values while the partition stays byte-identical.
3. **Revocation cannot wait for a rebuild.** Deletion from the index is
   a background cleanup job, not a security linearization point. The
   live overlay is checked per candidate at delivery time and proven
   effective while the stale FTS rows still physically exist.

## Operations

```python
# Restrict a source's domain (policy-only; content untouched)
await policy.assign_source_domain("src.1", "dom-finance")

# Revoke immediately — no reindex, no republish
await policy.deny_record("view.invoice-7", basis={"reason": "revoked"})
await policy.deny_domain("dom-finance")
await policy.deny_source("src.1")

# Explicit re-authorization
await policy.allow_domain("dom-finance")

# Publish the high-assurance partition for a domain set
await pubs.publish_high_assurance(
    materialized_generation_id=gen.generation_id,
    partition_domains=frozenset({"dom-alpha"}),
)
```

## Residual risks / deferred work

- Standard mode's shared-index rank-value residual (declared above);
  full removal requires per-domain serving isolation everywhere.
- Timing isolation is characterization-only; constant-time behavior
  would need dedicated resources (masterplan's future industrial
  profiles).
- Team/group/connector ACL richness, per-principal grants, and IdP
  integration (the resolver's base grant is workspace-wide).
- PR79A: signed continuation stores the authorization identity
  (`policy_digest`, `deny_revision`, epoch) server-side and invalidates on
  change. See [`snapshot-safe-query-continuation.md`](snapshot-safe-query-continuation.md).
- Vector/visual retrieval partitioning (PR81+) must adopt the same
  domain-partition discipline before those operators ship.

## Reproducing the evidence

```
python backend/scripts/bench_pr78_authorization.py --write
python -m pytest backend/tests/test_context_runtime_authorization.py \
  backend/tests/test_context_runtime_authz_retrieval.py \
  backend/tests/test_context_runtime_high_assurance.py \
  backend/tests/test_kernel_publication_partition.py \
  backend/tests/test_query_policy_service.py -q
```

The benchmark exits non-zero if any structural acceptance invariant
fails. Wall-clock numbers are evidence only, and machine-dependent.
