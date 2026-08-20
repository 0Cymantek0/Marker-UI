# PR81A — Selective visual retrieval promotion experiment

**Evidence schema:** `marker.pr81a_visual_retrieval_evidence.v1`
**Machine-readable evidence:** `docs/reference/measurements/pr81a-visual-retrieval.json`
**VLM replay cache:** `docs/reference/measurements/pr81a-vlm-cache.json`
**Visual generations:** `docs/reference/measurements/pr81a-visual-index-{clip,siglip}-{v3,v4}.npz`
**Corpus:** `backend/eval_data/pr81a` (manifest `marker.pr81a_corpus.v1`, fingerprint in evidence)
**Evaluated commit:** see `git_sha` in the evidence artifact
**Decision:** `narrow_rerank_only` — dense page-image indexing does not pay; a VLM reranker over lexical ∪ visual candidates does.

## 1. Question

> Does one credible selective visual retrieval route beat the strongest
> non-visual alternative on a declared visual-hard downstream task enough
> to pay its measured generation, storage, update, runtime, and
> authorization cost?

This is the PR81 experiment gate. Nothing here extends `marker.query.v1`:
`visual_search` remains an explicit `UnsupportedOperatorError`, proven by
test. The whole experiment lives behind the evaluation boundary and reads
production authorities (kernel records, publications, PR78 authorization)
without minting any.

## 2. Systems compared

| System | Identity | Evidence selection | Evidence handed to answerer |
|---|---|---|---|
| `lexical-text` (B1) | production FTS5 executor, `any_term` + bm25 | lexical node ranks aggregated to page ranks | oracle text transcript of top page |
| `lexical-render` (B2) | same executor + targeted page rendering | same lexical ranking | rendered PNG of top page |
| `visual-dense:openai/clip-vit-base-patch32` (V1) | dense page-image embeddings, CPU | cosine ranking over admitted pages | rendered PNG of top visual page |
| `visual-dense:google/siglip-base-patch16-224` (V1b) | model-sensitivity lane | same | same |
| `visual-hybrid-rerank` (V2) | lexical top-3 ∪ visual top-3, VLM contact-sheet rerank | reranker-chosen page | rendered PNG of chosen page |
| `:ha` variants | high-assurance lanes (partitioned publication / partitioned visual index) | — | — |

One hosted VLM (`google/gemma-4-26b-a4b-it` served via the user's local
OpenAI-compatible gateway; replay-cached) is the answerer — and the V2
reranker. Every route gets the same answerer; routes differ **only** in
evidence selection, so measured differences isolate selection quality.

Declared fairness bias: lexical lanes receive **oracle-quality** text
transcripts (perfect characters, table rows joined, chart labels
included). Any measured visual gain therefore survives a text baseline
that is stronger than production extraction.

## 3. Corpus and task

15 documents / 27 pages / 35 judged queries / 9 slices (see
`manifest.json`). Documents are deterministic reportlab PDFs regenerated
byte-identically by `backend/scripts/gen_pr81a_corpus.py`; gold answers
are drawn from the same constants that draw the pixels. The corpus
contains: grouped bar charts, a pie chart, a line chart, an org chart, a
KPI dashboard, dense grid tables, a duplicate-label form, a two-column
page committed in draw order (extraction-linearization trap), three
near-duplicate template pairs (decoys), one document with two content
revisions, and a restricted security domain holding the best evidence
for two authorization probes.

Task per query: select one page, answer one extractive question from it
(`task_success` = correct page at rank 1 AND normalized-correct answer
AND no danger class).

## 4. Scoring and the declared promotion rule

Retrieval (page hit, rank, MRR), downstream (answer correctness), and
danger classes (forbidden delivery, stale revision, unresolvable source,
decoy-twin confusion) are scored independently and never averaged into
one number. The rule was committed before interpretation:

- `promote_narrow` iff the best dense visual route gains ≥ 0.10
  task-success over `lexical-render` on the five visual-hard slices while
  holding the text-easy control within 0.10, with zero
  forbidden/stale/unresolvable dangers, all no-delivery probes clean,
  ≤ 1.5 MB render storage per page, ≤ 8 KB embeddings per page, and
  warm visual query p50 ≤ 250 ms;
- else `narrow_rerank_only` when `visual-hybrid-rerank` gains ≥ 0.10 with
  its own control held;
- else `experimental` at ≥ 0.05; else `do_not_promote`.

## 5. Results

| System | task_success | page_hit@1 | MRR | dangers |
|---|---:|---:|---:|---|
| `lexical-text` | 0.676 | 0.676 | 0.810 | 1 decoy |
| `lexical-render` (baseline) | 0.676 | 0.676 | 0.810 | 1 decoy |
| `visual-dense:clip` | 0.265 | 0.294 | 0.625 | 2 decoys |
| `visual-dense:siglip` | 0.176 | 0.176 | 0.543 | 1 decoy |
| `visual-hybrid-rerank` | **0.971** | **1.000** | **1.000** | **0** |

Slice detail (task success; page hits in parentheses where different):

| Slice | lexical-render | dense clip | hybrid rerank |
|---|---:|---:|---:|
| chart.appearance (n=5) | 0.20 | 0.60 (hits 0.80) | **0.80** (hits 1.00) |
| chart.value_read (n=3) | 0.67 | **1.00** | **1.00** |
| table.cell_grid (n=4) | 0.75 | 0.00 | **1.00** |
| form.label_placement (n=3) | 0.67 | 0.33 | **1.00** |
| layout.column_bind (n=3) | 1.00 | 0.00 | **1.00** |
| near_duplicate.decoy (n=4) | 0.25 | 0.25 | **1.00** |
| text.easy_control (n=6) | 0.83 | 0.00* | **1.00** |
| revision.change (n=4) | 1.00 | 0.00 | **1.00** |
| authz.revocation (n=2) | 1.00 | 0.50 | **1.00** |

\* by construction: the declared admission policy embeds only
visual-heavy documents, so dense visual as a *standalone* index cannot
serve plain-text queries. That is the measured selectivity tradeoff, not
a scoring artifact — and it is exactly why the hybrid shape wins.

Readings worth keeping:

- **B1 = B2 everywhere.** With an oracle text layer, targeted rendering
  added zero answers over the transcript: every page lexical found, the
  answerer read as well from text as from pixels. The baseline's losses
  are all *selection* losses (appearance wording, decoy twins).
- **Dense visual wins exactly the slices it was predicted to win** —
  chart.appearance page hits 0.80 vs 0.20, chart.value_read 1.00 vs
  0.67 — and loses nearly everything else, including both
  decoy-sensitivity probes.
- **The hybrid wins by union + rerank**: every gold page was in the
  lexical ∪ visual candidate set, the reranker picked it at rank 1 on
  all 34 judged queries (page hits 1.00, MRR 1.00), and it erased all
  decoy confusions.

## 6. Failure walk-through (manually inspected)

- `q07` ("groups of vertical bars … tallest bar value"): lexical
  delivered the *table* page of the same document (token overlap), dense
  CLIP delivered the chart page and answered 4.0. Textbook appearance
  query — lexical wording cannot match page text that does not name the
  visual form.
- `q25` (2023 report tallest bar): `lexical-render` fell for the
  near-duplicate template and delivered the public payroll summary;
  `visual-dense:clip` delivered the 2024 twin — flagged
  `decoy_confusion`. The hybrid reranker read the year from both
  thumbnails and picked correctly. Near-duplicate template pairs are a
  real danger for *both* non-visual and dense-visual retrieval; rerank
  resolved it here.
- `visual-dense:siglip` underperforming CLIP (0.176 vs 0.265) was a
  genuine surprise; raw-question SigLIP text prompts are weak zero-shot
  on document pages. Recorded as model sensitivity, not noise: it
  reinforces that dense page embedding is the fragile part of the
  pipeline.
- Transient gateway outages during the first live run left 21 hybrid
  rows honestly `unavailable`; the resumable cache refill healed all of
  them and the final artifact contains zero unavailable rows. The
  offline replay of the final artifact is **byte-identical on all
  metrics** with zero network calls.

## 7. Decision

**`narrow_rerank_only`.** Dense page-image indexing (CLIP or SigLIP
single-vector, CPU) does not pay its way as a retrieval index: overall
task success 0.18–0.27 vs 0.68 baseline, decoy confusions, and a
selectivity floor that excludes plain-text documents. But the hybrid —
lexical candidates plus a small visual candidate set, resolved by a VLM
reranker over a labeled contact sheet — gained **+0.303 task success on
the visual-hard slices (0.960 vs 0.657)**, held the text-easy control at
1.00, produced **zero danger classes across all 179 scored pairs**, and
its cost fits the declared envelope (55 KB avg render/page, 2–3 KB
embeddings/page, 0.14 ms warm visual query, ~0.3 s per rerank call).

A PR81B-sized follow-up may productize only the proven shape: a bounded
visual reranker *after* lexical retrieval (candidate budget ≤ 6, one
VLM call, cacheable, authorization-filtered before the sheet is built),
kept behind the internal boundary until it earns a versioned
`PublicationSet`/`EvidencePacket` extension. Dense page embeddings stay
unpromoted; the committed negative evidence is the reason a future
session cannot quietly re-add them without new measurements.

## 8. Security, lifecycle, and economics verification

- **Authorization:** all lanes filter candidates *before* competition.
  The denied-profile probe (gold exists only in the restricted domain)
  ended `no_delivery_ok` for **every** lane including high-assurance;
  zero `forbidden_delivered` anywhere. High assurance uses the
  physically partitioned `ha.` publication (lexical) and a partitioned
  visual matrix whose generation id differs from the shared build.
- **Revision lifecycle:** pre-revision queries attribute to v3; after
  the v4 commit the same queries attribute to v4; the pinned probe
  served the superseded cut (v3 answer "3", correct revision label, no
  stale flag) through the pinned publication and the pinned visual
  generation. Update cost: kernel commit + publish ≈ 0.4 s, visual
  rebuild 2.0–4.7 s for 2 cold page renders + 21 warm reuses; new
  generation ids differ from the old ones by construction.
- **On-demand economics:** 29 renders total (23 admitted pages at the
  final cut plus the superseded revision's pages), 45.7 ms cold / 0.4 ms
  warm mean render, 1.6 MB total cache, 47–70 KB embedding matrices per
  model, not-admitted documents never rendered.

## 9. Verification

- Focused PR81A suite: 135 tests across corpus, visual state, VLM
  client, scoring, decision, and lanes (real kernel workspaces, scripted
  VLM transport, hash embedder) — all passing.
- Broad suite, clean end-to-end rerun at head:
  `python -m pytest tests conformance -q` →
  **2853 passed, 0 failed, 4 skipped** (planning baseline 2719/0/3;
  +134 from PR81A suites). The skip-count delta 3→4 is a runtime
  conditional skip, not a failure; naming it (via `pytest -rs`) is
  recorded as an open follow-up for the model-sensitivity session.
- Replay determinism: the final artifact was produced **offline**
  (214 cache hits, 0 live calls) and its metrics are byte-identical to
  the live run.
- Known open evidence limitation — single-VLM identity — was **resolved
  by PR81B** (`docs/reference/pr81b-model-sensitivity.md`): the
  promotion held across four gateway vision models (three holders
  including two frontier-tier), with the gain re-scoped to VLM rerank
  *selection* rather than pixel answering, and one capable model
  (haiku-4.5) excluded by its own text-easy control breach.

## 10. Reproduction

```bash
# offline replay of the committed evidence (no key, no network, no GPU):
cd backend && python scripts/bench_pr81a_visual_retrieval.py

# fresh live run (needs a vision-capable OpenAI-compatible gateway):
cd backend && PR81A_VLM_BASE_URL=http://<gateway>/v1 \
  PR81A_VLM_MODELS=<vision-model> \
  python scripts/bench_pr81a_visual_retrieval.py --live --write
```

Regenerate the corpus deterministically with
`python scripts/gen_pr81a_corpus.py` (byte-identical output asserted at
generation time). The experiment's scope claims are limited to: this
15-document / 35-query corpus, CLIP-B/32 and SigLIP-base on CPU, and the
gemma-4-26b answerer/reranker via the local gateway; the negative dense
result is a property of *this* measured slice, and the positive rerank
result is a property of *this* reranker and contact-sheet shape.
Model sensitivity across VLM quality tiers is measured by the PR81B
follow-up (`pr81b-model-sensitivity.md`), which confirmed the rerank
promotion and re-scoped its attribution.
