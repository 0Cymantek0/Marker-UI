# Packet representation reuse semantics (PR86)

**Status:** implemented and test-proven (`backend/tests/test_context_runtime_representation.py`).
**Scope:** closes readiness amendment 23C invariant 53 — *"EvidencePacket
reuse is invalidated by source, ACL, policy, verification, redaction,
citation, or renderer/tokenizer changes."*

## The rule

An EvidencePacket (or a stored packet representation) is reusable only
while **every** semantic dimension that can change its legally visible
evidence or its caller-visible citation/render interpretation still
matches. Unrelated runtime noise must never churn the reusable identity.
There is no packet cache in Marker UI: reuse soundness is enforced
structurally, through deterministic `identity_id` recomputation, durable
continuation bindings, and disclosure rows that store delivered truth
verbatim. Correctness therefore derives from identity/binding
semantics, never from operator memory ("clear the cache during deploy").

The cache-soundness principle is the one RFC 9110/9111 borrow that
applies here (§12.5.5 `Vary`, §4.1 cache-key matching): *if a semantic
input can select a different caller-visible representation, it
participates in reuse selection or reuse across the change is
prohibited.* Marker UI is not an HTTP cache and implements no HTTP
mechanics — the principle is applied to citation/rendering/tokenizer
semantics on top of the existing content/provenance and authorization
identities.

## Dimension ownership (who may set what)

| Dimension | Owner | Reuse effect |
|---|---|---|
| Normalized query (operations, output, budget) | caller, validated fail-closed | in `identity_id` |
| Context seams (`security_context_id`, `verifier_policy_id`, `redaction_profile_id`, `serialization_profile`) | caller, opaque identity seams | in `identity_id`; grant nothing |
| Publication identity (set id, generation ids, **tokenizer**, content digest) | server, pinned immutable set | in `identity_id` |
| Evidence locators + content hashes | server | in `identity_id` |
| Effective authorization (profile, assurance, epoch, deny revision, policy digest) | server, from committed kernel truth | in `identity_id` |
| **Representation semantics** (packet schema, citation locator scheme, identity framing, canonicalization) | **server, deployed code constants** | in `identity_id` (PR86) |
| Transport envelope schema (`marker.query_result.v1`, `marker.answer_evidence.v1`) | server | **not** packet identity — envelopes wrap packets without reinterpreting packet content |
| Timing, pin ids, process state, field order in scheme tuples | runtime noise | never in identity |

## Representation semantics

`app/context_runtime/packets.py:representation_semantics()` derives the
representation identity from the deployed constants that actually define
caller-visible representation behavior:

- `EVIDENCE_PACKET_SCHEMA_VERSION` — the public packet shape;
- `CITATION_LOCATOR_FIELDS` — the authoritative fields that constitute
  one citation to an evidence unit;
- the packet identity framing record type + schema version;
- the canonicalization identity (`marker.record_identity.v1`).

Nothing request-derived enters this view, callers cannot name or
influence it, and it contains no build timestamps or environment noise,
so it is deterministic across restarts. A deployment that changes any of
these constants rotates every packet identity automatically; unchanged
constants (including a semantically null reorder of the citation field
tuple) leave identity stable.

### Citation semantics

`CITATION_LOCATOR_FIELDS` is the single source of truth for citations.
Answer-evidence `EvidenceRef.locator_view()` and claim validation
(`_unit_exists`) derive from it at call time, so a deployed
citation-scheme change alters citation construction, citation
validation, and packet reuse identity **together** — they cannot drift
apart. A reference or delivered unit that does not carry every scheme
field is not citable: citation construction fails closed and validation
refuses partial matches rather than weakening citation identity.

### Tokenizer semantics

There is exactly one tokenizer role today: the retrieval tokenizer of a
lexical generation. It is bound at its real source —
`compute_lexical_identity` includes the tokenizer and its config, so a
tokenizer change can only ever materialize as a **new lexical
generation id**, which flows into publication identity and packet
identity. The supported set is backend-pinned (`unicode61` on SQLite):
requesting a foreign tokenizer fails closed at build time instead of
silently reindexing under a borrowed identity. Continuation
defense-in-depth additionally compares `tokenizer` inside
`PUBLICATION_BINDING_KEYS`. There is no separate output/context
tokenizer; if one is ever introduced it must participate in
representation semantics explicitly.

## Continuation coherence

Cursor rows durably record the representation semantics the chain was
created under (`kernel_query_cursors.representation_json`, migration
`20260823_0016`). On resume the stored binding is compared against the
deployed semantics:

- match → the chain continues coherently;
- mismatch → the chain ends explicitly with status `invalidated` and
  error code `representation_changed` **before** any page is emitted —
  a single chain can never silently mix incompatible citation/renderer
  semantics;
- `NULL` binding (a row that predates PR86) → the row cannot be
  verified against any deployed semantics and fails closed the same
  way. Cursor TTLs are short, bounding the one-time operational cost.

Publication-set rotation mid-chain keeps the pre-existing PR79A
behavior: the chain stays pinned to its immutable set (no mixing, no
invalidation), while fresh queries resolve the new set and necessarily
produce a different identity.

## Disclosure and answer-trace truth

Disclosures (`kernel_context_disclosures`) store the delivered packet
JSON and its `identity_id` as `packet_id`. A representation rotation
therefore yields **distinct** `packet_id`s for the same logical request
— a packet delivered under v1 semantics can never masquerade as the
same reusable representation as one delivered under v2. Historical rows
are never rewritten or backfilled: they remain readable, answer traces
keep fingerprinting exactly the disclosed `(disclosure_id, packet_id)`
set that was delivered, and stored packet JSON remains interpretable
through its own `schema_version`.

## Non-claims

- No claim that `identity_id` equality proves answer correctness or
  entailment — see `pr85-answer-evidence.md`.
- Transport envelope schema versions are deliberately outside packet
  identity; changing an envelope rewraps packets without stale-packet
  risk.
- A renderer change that alters no caller-visible field set, schema
  constant, framing record, or canonicalization is semantically a
  no-op and intentionally does not churn identity.
- SQLite deployments cannot demonstrate a live tokenizer rotation (the
  supported set is pinned); the rotation contract is proven at the
  identity function, the fail-closed build path, and the publication
  projection that feeds packet identity.

## Proof map

`backend/tests/test_context_runtime_representation.py` (adversarial
matrix): stability + citation-scheme rotation, field-order negative
control, renderer/framing rotation across restarts, caller spoofing
rejection, tokenizer identity source + fail-closed rotation +
publication binding, continuation control vs rotation vs legacy-row
invalidation, citation single-source fail-closed construction, and
disclosure/trace immutability across rotation. Source, ACL/policy,
verification, redaction, revision, budget, and determinism dimensions
are proven by the pre-existing suites bound alongside this invariant.
