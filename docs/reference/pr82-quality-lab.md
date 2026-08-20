# PR82A: Adversarial Quality Lab and Release-Evidence Closure

**Evidence schema:** `marker.pr82_release_evidence.v1`
**Machine-readable evidence:** `docs/reference/measurements/pr82-quality-lab.json`
**Planning head:** `fbea4c31e688d47a615eace8c97b30b06e5de491`
**Evaluated commit:** see `git_sha` in the evidence artifact
**Preregistration identity:** see `preregistration_identity` in the evidence artifact — the twelve questions and their decision rules were frozen and committed before any suite result was interpreted
**Decision:** `ready_with_scoped_non_promotions` — the local V3.2 stack survives adversarial cross-cutting evaluation; PR83 is authorized with the scoped non-promotions below carried explicitly

---

## 1. Question

Can the current local Marker UI v2/V3.2 stack survive a deliberately adversarial, cross-cutting evaluation with enough reproducible evidence to authorize the next industrial-topology phase, and if not, what exactly blocks it?

Twelve preregistered questions (Q1–Q12) with pre-committed decision rules answer this; see `backend/app/eval/pr82/preregistration.py`. The rules were frozen at commit `b7dd038`, before any suite ran.

## 2. Evidence boundary

- Every suite's execution mode is declared: `deterministic`, `replay`, or `machine_dependent`. Nothing live/model-dependent was executed this session; unavailable rows name their exact prerequisite instead of being faked.
- Consumed prior evidence and its lifecycle is recorded in the artifact: PR81B model sensitivity (current), PR79B transport (current), PR75 verification risk (superseded by this phase's correlation fixes), PR68A data-plane timing (stale, environment-scoped).
- Runtime metadata (python/platform/machine) lives only in the environment block and never enters semantic identity.

## 3. Repository changes made by PR82A

| Commit | Intent |
|---|---|
| `e509ea2` | fix(ci): install `invoice2data` in CI so the PR80B suite collects — every recent `markerui-v2` backend CI run was red at collection, so the full-suite gate had stopped being evidence |
| `d5a6458` | feat(kernel): cross-revision anchor mapping contract (`app/kernel/anchor_mapping.py`) |
| `b7dd038` | feat(eval): preregistration, decision vocabulary, release-evidence bundle contract |
| `266099b` | feat(eval): adversarial mapping corpus (12 revision-pair cases) |
| `bd68329` | feat(eval): adversarial dependence evaluation + three correctness fixes (any-shared-dimension correlation, `model_family` dimension, non-finite prediction rejection) |
| `acff04d` | feat(eval): adversarial incremental rebuild with mixed change sequences and mapping composition |
| `be1ab39` | feat(eval): runtime fault matrix (nine faults on real seams) |
| `a833649` | feat(eval): hostile-document, mid-task revision/deny, and MCP-era evaluation |
| (this phase) | feat(eval): release-evidence runner `scripts/bench_pr82_quality_lab.py` + measurement artifact |

No production behavior changed except the two contained eval-side correctness fixes in `bd68329` (both exposed directly by adversarial tests, both independently tested, both recorded pre-fix failure → post-fix evidence in the test suite).

## 4. Mapping results (Q1, Q2)

The PR72 deferral is closed. `backend/app/kernel/anchor_mapping.py` implements a deterministic cascade with the closed disposition vocabulary `exact | mapped_deterministic | mapped_reviewed | mapped_semantic_candidate | stale | unresolved`:

- Only authoritative native-selector agreement mints `exact` — a bookmark preserving its identity across a value edit is exact; paraphrase, normalized, fuzzy, containment, duplicate, and geometry evidence can never produce `exact` or `mapped_deterministic`.
- Mapping a content revision onto itself is refused outright, so ACL-only changes cannot remint content identity.
- Mapping records are append-only with identity over the full semantic payload: replay across shuffled inputs mints identical ids, and re-committing an identical mapping is refused as already-committed truth.
- `mapped_reviewed` exists only through a human-review `AnchorMappingDecisionRecord`; the cascade structurally cannot mint it. Decision chains supersede by reference; forks and foreign decisions fail closed.

The frozen adversarial corpus (`app/eval/pr82/mapping.py`) covers the plan's twelve hardest cases. Result: 12/12 in expectation — 1 exact, 4 deterministic, 3 candidates, 2 stale, 1 unresolved, 1 refused (policy-only). Zero silent identity changes; similarity never promoted. Negative controls prove the evaluator fails loudly on corrupted expectations, so zero violations is evidence, not vacuity.

## 5. Dependence results (Q5, Q6)

A fresh held-out corpus — separate from the PR75 fixture that defined the thresholds — attacks the dependency-aware policy:

- **Base-lineage masking attack:** two witnesses sharing renderer+cropper while carrying different base lineages would, under the old selector, be admitted as independent and outvote the source-native evidence 2-1, fabricating three false verifications on the held-out slice. The evaluator's negative control demonstrates exactly this under the buggy selector. Fixed: correlation is now any-shared-dimension.
- On the committed PR75 corpus the fix newly dedupes `model-b`/`model-c` (shared renderer/cropper/detector) while matched-slice results are unchanged — PR75's recorded two false verifications stand as honest evidence of correlated model error, not a classification artifact. The PR75 measurement is marked superseded for the selection semantics only.
- **Non-finite predictions:** `NaN` is truthy in Python and previously loaded cleanly, counting as a verifying vote inside majority baselines. Now rejected at the corpus load boundary.
- `model_family` is a first-class dependency dimension.
- Held-out outcomes: matched slice 9 accepted / 0 false verifications / 3 honest abstentions (tie votes); shifted distribution breaks the risk bound into full abstention; two flawless samples stay `insufficient_support` (zero observed failures never becomes zero risk); high-risk model-only consensus never verifies.

**Non-promotions (first-class findings):**

- The dependency-aware policy stays `shadow`. The held-out slice is clean, but PR75's promotion prerequisites (frozen held-out data under change control, review capacity, expiry/retest rules, new evidence identity) remain unmet.
- Kernel-side evidence expiry is still optional: `expires_at = None` remains authority-bearing indefinitely. This is the one Q6 dimension that does not fail closed (PR82B candidate).
- Kernel `DependencyDisclosureRecord` lacks a cropper dimension, asymmetric with the eval side (PR82B candidate).

## 6. Incremental results (Q3)

24 frozen seeds of 4–10-op mixed sequences interleaving accepted patches, stale-before-hash conflicts (all rejected, zero merged), structural insert/delete, multi-rebase chains with patch replay, and exact vs conservative declarations. Both PR73 oracles hold on every seed: committed-history replay and an independent full-derivation pure replay equal the incremental result. Localized rebuilds derive exactly the changed node set; conservative declarations widen to full rebuilds.

Each rebase also computes and commits anchor-mapping dispositions into the same history the oracles replay — revision propagation now composes with the mapping contract (carried nodes deterministic, edited nodes stale, quote evidence never exact).

## 7. Runtime results (Q7, Q8)

Nine deterministic faults against the real seams (outbox, fencing, publications, durable events, artifact handles):

- crash before/after the acceptance linearization point: no publication before the point, exactly one after, post-commit crashes converge on retry;
- stale superseded owner and self-cancelled owner: `StaleFenceError`, accepted truth untouched;
- divergent results: `PublicationConflictError`; duplicate execution converges to one publication id;
- slow event consumer: never blocks truth, progress coalesces to one row per work, durable events never drop;
- restart on a fresh engine over the same database: sequence identity preserved, in-flight work honestly reset to pending;
- on-disk tampering of handle-backed bytes: rejected fail-closed; corrupted bytes never reconstruct valid output.

**Absence finding:** PR69 dynamic admission/model-lease machinery does not exist in this branch (confirmed by search and by the runtime-integration doc). Only the static `max_in_flight` cap is testable. Recorded as a finding, not tested. Performance characterization stays hardware-scoped; PR68A's timing numbers were not refreshed this session.

## 8. Agent and hostile-document results (Q9, Q10, Q4)

Seven OWASP LLM01-derived injection classes embedded as published record content and retrieved through the real bounded-query path. Per payload: the hostile record is retrievable as data with honest source-resolvable citations; the payload never surfaces in protocol-controlled envelope fields; the authorization identity view stays digest-only — content cannot name itself into privilege.

Mid-task composition: a publication head switch between operations leaves the in-flight packet pinned to the original set (revised text only appears in new queries); a domain deny between operations filters later operations live, and fresh queries on the denied domain are `no_hit` — unauthorized equals nonexistent, never stale rows.

**Q10 (bounded vs full-document work):** on the frozen seven-record corpus, the bounded path returns the cited target with 1 evidence unit versus 7 for full-document context — recorded as `promote_narrow`, scoped to this corpus class.

**Honest limit:** Marker invalidates its own cursors and routes; it cannot revoke bytes already delivered into an external model's context.

## 9. MCP 2026-07-28 era check

The 2026-07-28 revision retires the initialize exchange and `Mcp-Session-Id` for self-describing stateless requests with explicit state handles, and deprecates HTTP+SSE with a twelve-month offramp (official announcement; Tier 1 SDKs including Python support it). Marker's transport is already aligned in design: stateless JSON streamable-HTTP FastMCP, no hidden session assumptions, durable server-side cursor state exposed as an opaque `next_cursor` handle. The installed SDK (1.29.0 locally; requirements pin `mcp>=1.13.0,<2.0.0`) speaks protocol revision 2025-11-25 — deprecated-but-working inside the offramp window. Verdict: `aligned_deprecated_era`; the SDK bump is a PR84 compatibility item, not a blocker. Note the recorded PR79B evidence (SDK 1.28.0 / 2025-06-18) is now environment-stale while remaining semantically valid.

## 10. Carried PR80/PR81 evidence (Q11, Q12)

- PR80A/PR80B: deterministic evidence-backed extraction remains the truth path; the hosted specialist stays candidate-generation only. Unchanged.
- PR81B: the `rerank_vision` gain remains a model-gated reranker-selection result — per-model control gate, zero-danger constraints, replayability, and the rate-limit exclusion history all carried. Not generalized.
- External validity probe (Q11): designed — smallest public ViDoRe V3 subset, NDCG@10, text-easy control, no retuning on the external slice — and deferred. The blog does not state dataset license terms for subset redistribution, and a bounded session must not adopt an unstable benchmark dependency. Recorded `inconclusive` with the exact prerequisite: license confirmation for a frozen redistributable subset.

## 11. Full regression

Command: `python -m pytest tests conformance -q` (from `backend/`, the CI-authoritative invocation).

**Result: 2987 passed / 0 failed / 3 skipped in 25:19** (exit code 0).

Count reconciliation against the planning head: the recorded baseline of 2884 was measured at `97b55a5`, before the 24 model-catalog tests that landed in `fbea4c3`; the true planning-head count is therefore 2908. PR82A adds exactly 79 tests (31 anchor-mapping kernel, 16 preregistration/evidence, 8 mapping corpus, 11 dependence, 7 incremental, 3 runtime, 3 agent): 2908 + 79 = 2987, zero unexplained deltas. The skip delta 4 → 3 is a pre-existing environment-conditional skip (one `importorskip` target present on this machine), unrelated to PR82A. The 19 warnings are pre-existing classes; PR82A introduces no new warning class.

Additionally, the CI full-suite gate itself was repaired this phase (`e509ea2`): every recent `markerui-v2` backend CI run had failed at collection (`ModuleNotFoundError: invoice2data`), so the branch's strongest automated evidence had silently stopped being evidence. Deterministic per-commit CI plus the committed release bundle now form the two-tier evidence story: fast gate blocks ordinary changes; research/release evidence is regenerated by the documented runner command rather than per-commit jobs.

## 12. Readiness-invariant matrix

| Invariant | Status | Evidence |
|---|---|---|
| Citations never change source identity silently across revisions | pass | suite:mapping |
| Semantic similarity never promotes to exact identity | pass | suite:mapping |
| Incremental rebuild equals clean rebuild under declared outputs | pass | suite:incremental |
| Unknown dependency scope widens, never narrows | pass | suite:incremental |
| No fault creates false completion or stale accepted publication | pass | suite:runtime |
| Pathological evidence fails closed | non_promotion | suite:dependence (kernel expiry-optional gap) |
| Hostile content cannot manufacture authorization or truth | pass | suite:agent |
| Mid-task revision/policy change yields structured invalidation | pass | suite:agent |
| Bounded query beats full-document work on the frozen corpus | pass | suite:agent |
| Release evidence reproducible from documented inputs | pass | this artifact + reproduce block |

## 13. Non-promotions, kills, and follow-ups

| Item | Decision | Owner |
|---|---|---|
| Dependency-aware verification policy | `shadow` | PR82B: promotion prerequisites (change-controlled held-out data, review capacity, expiry/retest) |
| Kernel evidence expiry enforcement | `non_promoted` (gap) | PR82B: require `expires_at` on authority-bearing evidence |
| Kernel cropper dependency dimension | `non_promoted` (asymmetry) | PR82B: extend `DependencyDisclosureRecord` |
| External ViDoRe V3 validity probe | `inconclusive` (deferred) | next research session: confirm license, freeze subset |
| MCP SDK protocol-era bump | `aligned_deprecated_era` | PR84 compatibility item |
| PR69 admission/model leases | absence finding | PR83/PR84 scope decision: implement or explicitly descope |
| PR68A target-hardware rerun | `characterization_only` (stale) | release engineering: rerun before any latency claim |

No `kill_or_simplify` findings: nothing measured this phase earned deletion; the shared-memory and mmap lanes remain rejected per PR68A and were not revisited.

## 14. Reproduction

```
# focused suites
python -m pytest tests/test_kernel_anchor_mapping.py tests/test_eval_pr82_mapping.py -q
python -m pytest tests/test_eval_pr82_dependence.py -q
python -m pytest tests/test_eval_pr82_incremental.py -q
python -m pytest tests/test_eval_pr82_runtime.py tests/test_eval_pr82_agent.py -q
# full regression
python -m pytest tests conformance -q
# release bundle
python scripts/bench_pr82_quality_lab.py --write
```

Deterministic/replay portions must reproduce identical semantic results; the runtime suite is machine-scoped truth evidence, not a latency claim.

## 15. Recommendation

**PR83: `ready_with_scoped_non_promotions`.** Every local semantic gate that PR83 must preserve — source identity across revisions, incremental equivalence, runtime truth under faults, authorization-first retrieval under hostile content — passed adversarially. The non-promotions are optional-research or bounded follow-ups (PR82B items, a PR84 SDK bump, a license-gated external probe) and none is an industrial-topology blocker. PR83 must prove PostgreSQL/object-store/topology preserves these semantics, not redefine them.

**PR84 can already consume:** the release bundle, the readiness matrix, the claim ledger, and the regression gate repaired in CI. **PR84 cannot yet consume:** an external-validity number for visual retrieval and refreshed data-plane latency on target hardware — both carry named prerequisites above.
