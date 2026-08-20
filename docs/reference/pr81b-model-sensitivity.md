# PR81B — VLM model-sensitivity matrix over the PR81A experiment

**Evidence schema:** `marker.pr81b_model_sensitivity.v1`
**Machine-readable evidence:** `docs/reference/measurements/pr81b-model-sensitivity.json`
**Per-model artifacts:** `docs/reference/measurements/pr81b-model-{sonnet,haiku,gptluna,gemflash}.json`
**Per-model replay caches:** `docs/reference/measurements/pr81b-vlm-cache-{sonnet,haiku,gptluna,gemflash}.json`
**Capability probe table:** `docs/reference/measurements/pr81b-capability-probe.json`
**Corpus:** unchanged `backend/eval_data/pr81a` (manifest `marker.pr81a_corpus.v1`, fingerprint in every artifact)
**Decision:** `confirmed` with attribution `rerank_vision` — the PR81A `narrow_rerank_only` promotion holds across VLM quality tiers, and the gain is now correctly *scoped*: it lives in VLM rerank **selection**, not in pixel **answering**.

## 1. Question

> PR81A promoted `narrow_rerank_only` from a single VLM identity
> (gemma-4-26b). Does that promotion survive other vision-capable
> models across quality tiers — and if it does, does the gain actually
> come from *vision*, or from something a text-capable reranker would
> also deliver?

Two sub-questions, both pre-registered:

1. **Confirmation** — is the hybrid route's task-success gain over the
   targeted-rendering baseline stable across models, with security and
   the text-easy control intact?
2. **Attribution** — decompose the hybrid at its two VLM touchpoints
   (rerank selection, answer reading): does removing pixels from the
   *answer* prompt, or removing the *rerank*, erase the gain?

## 2. Systems compared

Five models were declared on the user's local OpenAI-compatible
gateway, by exact gateway id: `kr/claude-sonnet-4.5`,
`kr/claude-haiku-4.5`, `cx/gpt-5.6-luna` (frontier tier),
`free/bbl/gemini-3.0-flash` (economy tier), and `oc/mimo-v2.5-free`
(dropped before benchmarking — §5). Every model ran the same PR81A
lanes on the same corpus with the same scoring:

| Lane | Role in this experiment |
|---|---|
| `lexical-render` (B2) | per-model baseline anchor |
| `visual-hybrid-rerank` (V2) | the promoted route: contact-sheet rerank + image answer |
| `visual-hybrid-rerank-text` | **ablation**: same rerank, transcript-only answer |
| `visual-hybrid-rerank-joint` | **ablation**: same rerank, render + transcript answer |
| `visual-hybrid-union-only` | **ablation**: same candidate union, lexical order, no rerank |
| `visual-hybrid-rerank:ha` | the promoted route under the partitioned HA publication + partitioned visual generation |
| `lexical-text`, `visual-dense:*` (full runs only) | PR81A-parity columns for sonnet/haiku |

Sonnet and haiku ran the full lane set; gptluna and gemflash ran the
declared lean set (B2, V2, text/union ablations, HA probe — the exact
lanes the decision rule and attribution consume). The lean set halves
live VLM calls per model while leaving every rule input computable;
`--lean-lanes` is committed runner surface, and the per-model artifacts
record which lanes ran.

## 3. Corpus and task

Identical to PR81A: 15 documents / 27 pages / 35 judged queries / 9
slices, deterministic reportlab PDFs, gold drawn from the drawing
constants, corpus fingerprint asserted by the fail-closed loader in
every run. `task_success` = correct page at rank 1 AND normalized
correct answer AND no danger class. Nothing about the task changed;
only the answerer/reranker identity varied, which is the point.

## 4. Capability probe and the declared rule (fixed before results)

**Capability probe.** Before any benchmark, each model answered three
known questions against freshly rendered pages (tallest bar value `4.0`
and region `West` on `doc-fin-01` page 2; approved budget `95000` on
`doc-ops-01` page 1), graded by the PR81A normalizer. Pass bar: ≥ 2 of
3. Probe golds are provably printed on the probe pages (verified
against the oracle text layer by test).

**Declared rule** (module `app/eval/pr81b/decision.py`, committed at
`781cff6` with its tests, days before any matrix artifact existed;
thresholds restated in every artifact's `confirmation.thresholds`):

- A model **holds** iff it passed the probe, its per-model run of the
  *committed PR81A rule* returns `narrow_rerank_only`/`promote_narrow`,
  it has zero security dangers, and all no-delivery probes are clean —
  or the pre-declared **ceiling clause** applies (frontier model lifts
  its own baseline so high that a ≥ 0.10 gain is arithmetically
  unreachable; it still holds at hybrid visual-hard ≥ 0.90 without
  regressing its baseline, control and security intact).
- **Confirmed** iff ≥ 3 holders including ≥ 2 frontier-tier;
  1–2 holders → `model_gated_experimental` behind an explicit
  model-quality gate; 0 holders → `do_not_promote`; systemic security
  failure (≥ 3 models with dangers) → `do_not_promote` outright.
- **Attribution** (independent of confirmation): `answer_vision` if the
  text-answer ablation costs ≥ 0.10 visual-hard task-success for any
  holder; else `rerank_vision` if the union-only ablation costs ≥ 0.10
  for any holder; else `retrieval_only` and the promotion claim must be
  re-scoped.

## 5. Results

Capability probe (live gateway; graded by PR81A normalization):

| Model | probe | bar value | bar region | budget | verdict |
|---|---|---|---|---|---|
| `kr/claude-sonnet-4.5` | 3/3 | 4.0 | West | 95000 | pass |
| `kr/claude-haiku-4.5` | 3/3 | 4.0 | West | 95000 | pass |
| `cx/gpt-5.6-luna` | 3/3 | 4.0 | West | 95000 | pass |
| `free/bbl/gemini-3.0-flash` | 2/3 | 4.0 | miss | 95000 | pass (≥2) |
| `oc/mimo-v2.5-free` | 3/3* | 4.0 | West | 95000 | pass, then **dropped** |

\* mimo's probe initially lost two cases to 429 rate-limit exhaustion;
stronger committed retry knobs healed it to 3/3. Its benchmark route
then proved operationally unusable (3 completed responses in 20 minutes
under sustained 429s), and the model was dropped from the matrix by
user decision. No mimo benchmark numbers exist; its partial cache was
discarded rather than committed.

Matrix (34 judged queries per lane; visual-hard = mean task-success
over the five PR81A visual-hard slices; deltas are ablation costs):

| Model (tier) | B2 hard | V2 hard | gain | V2 easy | control | text-answer Δ | union-only Δ | holds |
|---|---:|---:|---:|---:|---|---:|---:|---|
| sonnet-4.5 (frontier) | 0.657 | **0.960** | +0.303 | 1.000 | ok | +0.067 | +0.303 | **yes** |
| haiku-4.5 (frontier) | 0.657 | 0.893 | +0.237 | **0.500** | **breach** | +0.067 | +0.237 | no |
| gpt-5.6-luna (frontier) | 0.607 | **0.910** | +0.303 | 0.833 | ok | +0.067 | +0.303 | **yes** |
| gemini-3.0-flash (economy) | 0.657 | **0.920** | +0.263 | 1.000 | ok | +0.027 | +0.263 | **yes** |
| gemma-4-26b (PR81A reference) | 0.657 | 0.960 | +0.303 | 1.000 | ok | — | — | (reference row) |

Per-model PR81A-rule outcomes: sonnet/gptluna/gemflash
`narrow_rerank_only`; haiku `do_not_promote` *by its own control
breach* — the only model whose rerank actively hurt the text-easy
control. The ceiling clause never fired: no frontier model lifted its
baseline near 0.90, so every holder earned the plain ≥ 0.10 gain.

Security held everywhere: **zero** forbidden/stale/unresolvable
dangers across all four models and every lane; all no-delivery probes
clean (including the hybrid under high assurance); the HA hybrid lane
served the public-domain gold at 1.000 from the partitioned
publication and partitioned visual generation, with restricted-domain
pages never entering its candidate set.

## 6. Ablation attribution — where the gain actually lives

Three findings, each consistent across every model that held:

1. **Removing the rerank erases the entire gain.** `union-only` (same
   lexical ∪ visual candidates, lexical order, no VLM rerank) lands
   exactly on the B2 baseline in all four models (0.657/0.657/0.607/
   0.657). The rerank delta equals the whole gain (+0.24..+0.30).
   Union recall alone contributes nothing beyond lexical top-1 here —
   the candidate union's value is realized only when a competent VLM
   orders it.
2. **Removing pixels from the answer prompt does not erase the gain.**
   The text-answer ablation costs +0.067 (sonnet, haiku, gptluna) and
   +0.027 (gemflash) — every model below the declared 0.10 material
   margin. A reranker-selected page read *as text* retains ≥ 78% of the
   gain. The answer step benefits from pixels (the residual is real,
   and joint image+text never beats image-only by ≥ 0.10 either —
   `joint_over_image_delta` 0.0 where measured) but is not where the
   promotion is earned.
3. **Attribution: `rerank_vision`.** Vision contributes through
   *selection* — reading thumbnails well enough to order candidates —
   not through pixel reading in the answer step. The PR81A phrase
   "VLM reranker" was the load-bearing part of that promotion all
   along; this experiment measured it.

Re-scope (now normative): the promotion claim is **a bounded VLM
*reranker* after lexical retrieval**, not visual answering. A deployed
route may hand the answer step text, image, or both without materially
moving task success, but its reranker model must clear the quality bar
this matrix measured (§8).

## 7. Failure walk-through (manually inspected)

- **haiku's control breach is a rerank-quality failure, not an answer
  failure.** Its hybrid lost text-easy queries by *selecting the wrong
  page* (q01/q02/q04: page_miss with null answers — the answer step
  honestly refused to answer from the wrong page). Rerank misranking
  on easy queries dropped easy to 0.500 while visual-hard still gained
  +0.237. The declared rule correctly refuses to promote a route whose
  reranker trades easy-query correctness for hard-query gains.
- **Answer-form verbosity is scored, not forgiven.** gpt-5.6-luna
  answered `18.5 thousand` and `15 days` where gold normalizes `18.5`
  and (a date kind) rejected the phrase — recorded as
  `answer_unparseable` for every lane equally, baseline included. Its
  B2 easy rate (0.667) absorbs the same penalty, so the gain comparison
  stays fair.
- **One chart-reading error repeats across models.** q10 (line-chart
  final value) drew `11.55` from both Claude models with the correct
  page delivered — a genuine chart-reading difficulty, not a selection
  or scoring artifact.
- **Text-mode region confusions.** q12/q25 (`South` instead of `West`)
  appear only in text-answer ablation rows for sonnet/haiku — the small
  residual that makes `answer_vision_delta` positive: reading the chart
  from the transcript is slightly harder than from pixels.
- **Honest unavailability, healed and recorded.** gemflash's B2 run had
  one `unavailable` row (429 retries exhausted on q09) in its final
  artifact; every other row is scored. mimo's dropped run is documented
  in §5 rather than averaged away.

## 8. Decision

**`confirmed` (attribution `rerank_vision`).** Three capable models —
`kr/claude-sonnet-4.5` and `cx/gpt-5.6-luna` (frontier) and
`free/bbl/gemini-3.0-flash` (economy) — hold the hybrid route under the
committed PR81A rule applied per model, each with ≥ +0.26 task-success
on the visual-hard slices, the text-easy control held, zero security
dangers, and clean no-delivery probes. The gain band (+0.26..+0.30) is
tight across tiers and matches the PR81A gemma reference (+0.303); the
promotion is not a single-model artifact.

Two scoped consequences, both new in PR81B:

1. **The claim is a reranker claim.** Productization (still gated;
   nothing here extends `marker.query.v1`) should spec the *reranker*
   as the quality-critical component: candidate budget ≤ 6, one VLM
   call, authorization-filtered before the sheet is built. The answer
   step's modality is a free implementation choice.
2. **Reranker model-quality gate.** haiku-4.5 — a capable image QA
   model by probe (3/3) — fails the gate by control breach. A promoted
   deployment must pin a reranker identity from the measured-holding
   set or re-run this matrix for a new one; "any vision model" is
   explicitly falsified by the haiku row.

## 9. Security, lifecycle, and economics verification

- **Authorization:** zero `forbidden_delivered` in 100% of scored rows
  across four models. The denied-profile probe ended `no_delivery_ok`
  for every lane including the hybrid under high assurance; HA hybrid
  queries served only from the partitioned publication and partitioned
  visual generation (restricted-domain vectors physically absent).
- **Revision lifecycle:** pre/post-revision attribution and the pinned
  v3 probe behaved identically to PR81A in every per-model run; zero
  `stale_revision_delivered` anywhere.
- **Economics (per model, lean set):** 146 scored pairs, ~150 live VLM
  calls, no additional render or embedding cost — PR81A's measured
  envelope (55 KB avg render/page, 2–3 KB embeddings/page, 0.14 ms warm
  visual query) is untouched by model choice; only rerank latency and
  token spend scale with the model, and the rerank is one call per
  query.
- **Repeatability:** every artifact is replayable offline from its
  committed cache (the aggregate is a pure function of committed
  artifacts); runs are resumable and transient gateway failures (429
  windows, a 502 stretch, SSE-vs-JSON response shapes on some routes)
  were absorbed by committed client behavior, never by discarding
  rows.

## 10. Reproduction

```bash
# offline: aggregate the committed matrix, no key, no network:
cd backend && python scripts/bench_pr81b_model_sensitivity.py

# per-model replay of a full artifact:
cd backend && python scripts/bench_pr81a_visual_retrieval.py --ablations \
  --vlm-cache ../docs/reference/measurements/pr81b-vlm-cache-sonnet.json

# fresh live matrix (needs a vision-capable OpenAI-compatible gateway):
cd backend && PR81A_VLM_API_KEY=<key> PR81A_VLM_BASE_URL=<base-url> \
  PR81A_VLM_MODELS=<exact-model-id> python scripts/bench_pr81b_model_sensitivity.py --live --lean
```

Scope claims: this matrix covers four vision-capable gateway models on
one 15-document / 35-query corpus with CLIP-B/32 candidates. mimo was
probed (3/3 after retry hardening) but not benchmarked (route
rate-limits; user decision). The haiku control breach is a property of
*that model as reranker on this corpus*; the confirmation is a property
of the declared holder set. Negative and excluded results above are
first-class evidence, kept so a future session cannot quietly widen the
claim.
