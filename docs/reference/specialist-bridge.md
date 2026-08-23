# Specialist Candidate Bridge - Hybrid Extraction Evidence

**Benchmark:** `marker.specialist_bridge_evidence.v1`
**Machine-readable evidence:** `docs/reference/measurements/specialist-bridge-hybrid.json`
**Replay cache (shared with PR80B):** `docs/reference/measurements/pr80b-llm-cache.json`
**Regenerate offline:** `python backend/scripts/bench_specialist_bridge.py --write`
**Evaluated commit:** recorded in the artifact's `git_sha` (branch `markerui-v2`).

## 1. Question

PR80B measured that a trained specialist has higher raw normalized-field
coverage than the deterministic route but fabricates plausible values,
resolves conflicts confidently, and carries no evidence lineage - and it
recorded the safe follow-up: a trained specialist may contribute
**non-authoritative candidates** into Marker UI's existing reconciliation
and proof process. This slice executes that recommendation and measures
the result:

When a trained specialist proposes values through the production
extraction path - with every proposal attributable, never evidence, and
acceptable only under deterministic corroboration - what does the hybrid
path gain over PR80A, and does a single false-authority event occur?

## 2. Design

### 2.1 Two different facts, never collapsed

- **Source evidence** (`EvidenceCitation`) still means a real kernel
  record at a real revision, served through the one authorized query
  path. Unchanged from PR80A.
- **A specialist proposal** (`ProposalView` on `FieldOutcome`,
  `SpecialistProposal` in the lane) is attributable model output: who
  produced it (producer id + family + stable configuration identity),
  what authorized context it saw (`SpecialistProvenance` - a disclosure,
  never entailment), its flags, and OUR independent typed parse. It
  carries no citation and never creates one.

Runtime observations (latency, attempts, tokens, cache hits) are
recorded on the lane report but excluded from result identity: a
replayed response is semantically identical to the live one.

### 2.2 The lane (opt-in, bounded, untrusted)

`ExtractionService` accepts an optional `SpecialistLane`. The lane sees
only the run's own authorized packet evidence, bounded to a character
budget, wrapped in `<document>` markers with an explicit
data-not-instructions contract; the provider payload never carries
tools. Model output is parsed as untrusted data under the versioned
`marker.specialist.output.v1` contract - unknown shapes, wrong
versions, non-string values, and oversized row sets fail closed into
typed lane statuses (`output_contract_failure`, `provider_failure`,
`replay_cache_miss`, `context_refused`). One provider boundary
(`SpecialistProvider`) with two implementations: `OpenAICompatProvider`
(injectable transport, bounded retries, 401/403 fast-fail, no hardcoded
model - callers pass the exact model they selected) and `ReplayProvider`
(deterministic recorded responses; a miss is an explicit refusal).

### 2.3 The authority policy (`marker.extraction.hybrid/v1`)

The grounded PR80A policy runs first and unchanged. Proposals merge on
top under one invariant - **a proposal is attributable input, never
evidence**:

| Situation | Outcome |
|---|---|
| Proposal-only field | `review_required` with the proposal intact; never valued, never `missing`-collapsed |
| Proposal agrees with accepted source value | stays accepted; agreement recorded, explicitly not a witness |
| Proposal conflicts with source | source stands; conflict disposition recorded and visible |
| Proposal vs live grounded conflict | conflict preserved; a model cannot pick a winner |
| Proposal + failed strict parse of cited raw text | accepted ONLY if the deterministic normalizer (`app.extraction.normalization.v1`) reproduces the proposal's typed value from that raw text (`hybrid.corroboration.deterministic_normalization.v1`) |
| Proposal row with no grounded counterpart | `review_required` proposal-only row |

Corroboration is deliberately strict: the model must propose the
CANONICAL typed value and the normalizer must independently derive the
same value from the cited raw text. A model that merely echoes the raw
separator-preserving text (as the recorded PR80B model does for
decimal-comma documents) does NOT trigger corroboration - those fields
become reviewable proposals instead. Relaxing that would be a routing
decision, not a bridge decision, and is out of scope.

Committed corroborated assessments carry the hybrid policy revision
(`v1+v1`), workflow class `marker.extraction.hybrid.v1`, the hybrid rule
in `declared_context`, and the
`marker.extraction.reconcile/v1:deterministic-normalization` authority
rule on every proof support - the kernel graph itself names how each
value was proved, and the proof chain terminates at source records, at
the model never.

Stale/cross-workspace defense: proposal provenance binds
workspace/publication/packet; a lane result bound elsewhere is refused
wholesale (`context_refused`), and the replay prompt embeds a
content+schema fingerprint so changed source means changed prompt means
explicit cache miss - never a stale attach.

## 3. Results (24-document PR80B corpus, offline replay)

| Metric | PR80A | Hybrid bridge |
|---|---|---|
| Exact documents | 17 / 24 | **18 / 24** |
| Authoritative accepted fields | 454 | **457** (3 corroborated) |
| Reviewable fields with a specialist proposal | 0 | **24** |
| False-authority events | 0 | **0** |
| Fabricated values reaching authority | 0 | **0** |
| Accepted values with source/proof lineage | 100% | **100%** |
| Lane failures | - | 0 (full replay coverage) |

Corroboration fired exactly where the recorded model normalized and the
normalizer agreed: `inv-006` (US date), `inv-007` and `inv-018`
(currency synonyms). The decimal-comma documents (`inv-008`, `inv-009`)
became reviewable-with-candidate rather than auto-accepted, per the
strict rule above. The known fabrication case (`inv-013`: derived
`unit_price` 29.99 for the structurally broken row) remains a
proposal-only review row with zero committed authority.

Interpretation: the bridge's measured win is not mass auto-acceptance -
it is (a) zero false authority, (b) normalized fields becoming
source-authoritative when a deterministic rule proves them, and (c)
previously-lost fields becoming well-attributed review candidates.
Whether that reduces human review minutes is the invariant-26
measurement, not this one.

## 4. Security posture

- Document text is untrusted data: prompt-injection content cannot gain
  tools (none exist), extend the schema (unknown fields rejected and
  recorded), or push values into authority (tested with a model that
  OBEYS the injection).
- The specialist receives no more than the extraction's authorized,
  bounded context; no workspace identifiers travel in the prompt.
- Secrets never enter prompts, envelopes, replay artifacts, or result
  identities (provider tests assert the key's absence).
- Provider outcomes are typed and bounded: 401/403 fail fast (no retry
  storm), 429/5xx/transport faults retry a bounded count with
  injectable backoff, and lane failures never erase deterministic
  evidence that already succeeded.

## 5. Limitations and non-claims

- No held-out distribution-shift or catastrophic-utility evaluation:
  **invariant 25 is not closed** and no automatic routing promotion is
  claimed or enabled. The lane is opt-in by construction.
- One specialist, one provider boundary. Multi-provider correlation is
  future work; the provenance already records producer family so future
  false independence is not the easy path.
- Production review capacity/usability (invariant 26) is unmeasured;
  the corpus is synthetic and chose the implementation.
- The recorded corpus responses are from PR80B's free-tier model; the
  bridge makes no claim about other models' behavior.
- The deterministic corroboration set is the normalization ruleset;
  other deterministic proof rules (e.g. sum-derivation) are future
  policy work.

## 6. Verification

Focused suites (from `backend/`):

```text
python -m pytest tests/test_specialist_contract.py tests/test_specialist_provider.py tests/test_hybrid_reconciliation.py tests/test_hybrid_service.py tests/test_hybrid_adversarial.py tests/test_eval_bridge.py -q
python -m pytest tests/test_extraction_contract.py tests/test_extraction_service.py tests/test_extraction_review.py tests/test_extraction_proof_integrity.py tests/test_eval_pr80b_llm.py -q
```

Full regression: `python -m pytest tests conformance -q` (from
`backend/`). Offline benchmark rerun:
`python backend/scripts/bench_specialist_bridge.py --write`.

## 7. Implementation map

- `backend/app/extraction/normalization.py` - production deterministic
  normalization ruleset (single authority; PR80B eval re-exports it)
- `backend/app/extraction/provider.py` - provider-neutral boundary,
  live + replay implementations
- `backend/app/extraction/specialist.py` - provenance model, versioned
  output contract, bounded lane orchestration
- `backend/app/extraction/hybrid.py` - authority-aware reconciliation
  policy and deterministic corroboration
- `backend/app/extraction/service.py` - opt-in lane integration,
  hybrid-attributed claim/proof persistence
- `backend/app/eval/bridge/` - benchmark harness (prompt rekeying,
  contract translation, authority metrics)
- `backend/scripts/bench_specialist_bridge.py` - deterministic offline
  rerun and artifact writer
