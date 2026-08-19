# PR80B - Direct-Specialist Displacement Report

**Benchmark:** `marker.pr80b_displacement_evidence.v1`
**Machine-readable evidence:** `docs/reference/measurements/pr80b-direct-specialist-displacement.json`
**Replay cache:** `docs/reference/measurements/pr80b-llm-cache.json`
**Regenerate offline:** `python backend/scripts/bench_pr80b_displacement.py --write`
**Evaluated commit:** recorded in the artifact's `git_sha` (branch `markerui-v2`).

## 1. Question

On a declared invoice-style extraction slice exercising scalar fields and
repeated line items, does Marker UI's PR80A evidence-backed extraction path
provide better practical workflow utility than a credible direct
specialist - and if not, what slice should be delegated or routed?

## 2. Systems compared

All three routes received the **same user-level input**: the identical
document text for each of the 24 corpus documents.

| System | Identity | Input path |
|---|---|---|
| `marker-pr80a` | current evidence-backed route (`app.extraction` pr80a.1, anchor route over `marker.query.v1`) | text published as kernel view documents; extraction via `execute_query` over the active PublicationSet |
| `invoice2data` | invoice2data 1.0.1 (PyPI), canonical per-vendor templates authored once | same text as `.txt` files via the library's text reader |
| `llm` | poolside/laguna-s-2.1:free via a local OpenAI-compatible gateway, structured-output extraction prompt, temperature 0 | same text as the user message; system prompt declares the task normalization rules |

**Comparator rationale.** An LLM with a structured invoice-extraction
prompt is the dominant deployed direct-specialist approach and is the
displacement question that actually faces the product; invoice2data is the
canonical specialized open-source invoice extractor and anchors the
"deterministic specialist" reading. OpenRouter free-tier models were
attempted first; `z-ai/glm-5.2:free` was upstream-rate-limited at benchmark
time (429 bursts, 17/24 documents lost), so the uniform run used the local
gateway's laguna model for all 24 documents. Every LLM answer is replayable
from the committed cache; routine tests never touch the network.

**Adapter policies (declared).** invoice2data: first regex match wins for
multi-match arrays; an empty extraction maps to a lane error (integrator
semantics). LLM: the model may flag conflicts (`<field>_conflict`), which
map to honest abstentions. PR80A: only values the route actually accepted
are emitted; everything else is absent with a review flag.

## 3. Corpus and task

24 synthetic invoices (fictional vendors, no personal data) with auditable
gold covering: happy path, missing optional/required fields, zero-vs-absent,
US/EU date-currency-decimal normalization, many/near-duplicate rows,
identical/conflicting duplicate SKUs, broken short/long rows, non-numeric
amount, total mismatch, label variants, noise with a decoy anchor,
two-witness agreement/conflict, negative credit, fullwidth punctuation, and
pagination. The manifest (`backend/eval_data/pr80b/manifest.json`) declares
the task-level normalization rules applied identically to gold and to every
system; the loader recomputes every declared invariant from gold before a
run is allowed to start.

## 4. Scoring

Deterministic pure-function scoring (`app.eval.pr80b.scoring`) over
(gold, system output), double-scored per run for byte-identical
repeatability. Scalar and row outcomes are scored separately; dangerous
classes (fabrication, confident conflict resolution, cross-row
contamination, silent contradictions, duplicate/hallucinated rows) stay
individually countable rather than being averaged into accuracy.

## 5. Results

| Metric | marker-pr80a | invoice2data | llm (laguna) |
|---|---|---|---|
| Exact documents | **17**/24 | 12/24 | **20**/24 |
| Scalar accuracy on gold-present fields | 91.3% | 82.5% | **99.0%** |
| Absent-field rejection rate | 100% | 100% | 100% |
| Lane/provider errors | 0 | 4 (whole-document) | 0 |
| Fabricated values | **0** | 1 | 1 |
| Confident conflict resolutions | **0** | 3 | 2 |
| Silent total/row contradictions | **0** | 8 | 3 |
| Duplicate rows emitted | **0** | 6 | 2 |
| Emitted values with evidence lineage | **454/454 (100%)** | 0/359 | 0/412 |
| Self-flagged review outcomes | 61 | 0 | 2 |
| Invariant machinery | reports on every doc; 5 mismatches all flagged | none (24 not reported) | none (24 not reported) |

Workflow reading: PR80A's review burden is **self-declared** (61 flagged
outcomes a reviewer triages with citations in hand); both specialists push
the entire burden to unverifiable spot-checks (359 and 412 uncited
emissions). LLM usage for the run: 24,918 prompt + 8,816 completion tokens,
no chargeable cost (free tier).

## 6. Failure walk-through (manually inspected)

- **LLM fabrication (inv-013, the decisive case).** The document's
  SKU-7002 row omits its `unit_price` column. The model delivered a derived
  `unit_price` (89.97 / 3 = 29.99) the document never states, shifted
  `amount` to null, and raised no flag. PR80A dropped the broken row but
  its invariant surfaced the loss (row sum 60.03 ≠ total 150.00 →
  `violated` → review). One route misses visibly; the other invents
  plausibly.
- **Conflict handling splits.** inv-021 (two documents disagree only on
  total): PR80A unresolved → review; the LLM flagged it honestly
  (`total_due_conflict`, null). inv-012 (same-document duplicate SKU with
  different qty/amount): both specialists emitted both conflicting rows
  (duplicates, confident); PR80A collapsed the identity and flagged the
  row for review while keeping first-seen values - an honest-but-lossy
  same-witness policy worth revisiting in a later PR.
- **Decoy asymmetry (inv-019).** "Estimated Total Due: 999.99"
  substring-matches the anchor. invoice2data took the decoy (wrong total
  999.99). The LLM abstained (false conflict - over-cautious but safe).
  PR80A accepted the real total via same-witness first-seen semantics -
  correct here, but fragile: scalar-level same-witness conflicts are not
  review-escalated the way row-level ones are.
- **PR80A normalization blindness.** inv-006/007/008/009 (US date, `$`,
  comma decimals, EU decimals) all escalate to review with
  `missing_flagged` instead of guessing; inv-018's semantic label variants
  ("Invoice No.", "Date of Issue", "Grand Total", "US Dollars") are simply
  absent. The LLM normalized every one of these correctly.
- **invoice2data whole-document fragility.** A single missing required
  regex (inv-003/004/017/018) returns an empty extraction for the entire
  document, rows included.

## 7. Decision

**Hybrid routing condition** (full text in the artifact's `decision` block):

1. **PR80A retains the authoritative slice.** It is the only route with
   evidence lineage, conflict honesty, invariant surfacing, and zero
   dangerous-failure classes. Displacing it with any measured specialist
   would trade review-visible misses for silent fabrications.
2. **A later routing phase is justified, narrowly.** The LLM's 99.0%
   normalized-field coverage on layout-variant and normalization slices is
   real and repeatable from cache. The safe integration shape is a
   NON-authoritative candidate generator feeding PR80A's existing
   reconciliation/proof machinery (synthetic specialist witness, honest
   provenance, acceptance only with independent corroboration) - not a
   model swap.
3. **invoice2data is not promoted.** It wins no axis and fails whole
   documents on this corpus.

Strongest observed failure mode overall: the specialist's plausible
fabricated value with confident delivery and no lineage (inv-013).

## 8. Limitations

- 24 synthetic documents, one schema, one task declaration; no claim beyond
  this slice or these system versions.
- One hosted model (free-tier laguna via local gateway), temperature 0,
  single live pass per document; variance across models/runs is not
  characterized. glm-5.2:free and gemma-4-31b were unavailable (upstream
  429) at measurement time; nemotron-3-super-120b answered a smoke test
  correctly but was not run to completion.
- Scoring normalization (US-first dates, EU-decimal pattern rules) is
  declared task convention, not universal truth; ambiguous tokens are
  rejected rather than guessed.
- `doc_exact` measures extraction exactness only; contradiction surfacing
  is reported separately (§5, §6).
- PR80A timings are runtime observations on the implementer's Windows
  machine; they are not latency promises.

## 9. Verification

- Focused PR80B tests (`test_eval_pr80b_*`): 217 passed across the
  normalization, corpus, scoring, LLM-adapter, invoice2data, and PR80A-lane
  matrices - all offline, no credentials.
- Full backend + conformance suite (`python -m pytest tests conformance -q`
  from `backend/`): first run this session returned 2717 passed / 2 failed /
  3 skipped; both failures were `ImportError: streamable_http_client` in
  `test_pr79b_transport_conformance.py` caused by a stale local `mcp`
  1.13.0 (the alias exists in newer 1.x releases that CI resolves).
  Upgrading the environment to `mcp>=1.29.0,<2.0.0` - already within the
  repo's pin - made the file pass 4/4 in isolation, and the clean full-suite
  rerun returned **2719 passed / 0 failed / 3 skipped**. No PR80B change
  was involved in either the failure or the fix.

## 10. Reproduction

```bash
# full offline measurement from committed fixtures + cache (no network)
python backend/scripts/bench_pr80b_displacement.py --write

# refresh the LLM cache from a gateway (requires LLM_BASE_URL/LLM_API_KEY)
LLM_BASE_URL=... LLM_API_KEY=... LLM_MODELS=... \
  python backend/scripts/refill_pr80b_llm_cache.py

# focused tests (scoring, adapters, lanes; no credentials)
python -m pytest backend/tests/test_eval_pr80b_* -q
```
