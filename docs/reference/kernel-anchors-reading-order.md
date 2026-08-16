# PR72 — Layered SourceAnchor + Partial-Order Reading Order

**Date:** 2026-08-17
**Branch:** `markerui-v2`
**Code base for all recorded results:** `06bb2f3` (anchor/graph commits `3bfa0f3`..`06bb2f3` on top of `b857f5f`)
**Environment:** Windows 11 (win32 10.0.26200), CPython 3.11.9, SQLite 3.45.1
**Schema/migration head:** Alembic `20260816_0009` — **no new tables**; anchors and graphs are kernel records on the existing commit spine
**Plan:** `planning/v2/marker-ui-v2-pr72-source-anchor-reading-order-implementation-plan.md` (master-plan authority: amendments 8C, 18C.4, 19C row PR72, 23C items 10–13)

Reproducible evidence bundle for the PR72 slice: every claim below is verifiable from code, tests, and the exact commands recorded in §7.

---

## 1. What changed

| Area | File | Responsibility |
|---|---|---|
| Anchor contract | `backend/app/kernel/anchors.py` | Coordinate-space registry, four selector families, `SourceAnchorRecord`, fail-closed rematerialization |
| Reading order | `backend/app/kernel/reading_order.py` | `ReadingOrderGraph` (contains/before/member_of/continues), deterministic serialization, `linearize` view, bounded `split_node`, `ReadingOrderRecord` |
| Kernel error | `backend/app/kernel/errors.py` | `OrderConflictError` (contradictions are explicit, never silent) |
| Native extraction | `backend/app/conversion/native_sources.py` | pypdf content-stream facts; OOXML package/bookmark/EMU facts (stdlib zip + ElementTree) |
| Fixtures | `backend/tests/pr72_fixtures.py` | Byte-deterministic two-column PDF (computed xref) and minimal OOXML package (fixed zip metadata) |
| Identity tests | `backend/tests/test_kernel_source_anchors.py` | 27 identity-matrix tests (§9.1/§9.2) |
| Graph tests | `backend/tests/test_kernel_reading_order.py` | 33 adversarial graph/restitch tests (§9.3/§9.4) |
| Tracer tests | `backend/tests/test_pr72_native_tracer.py` | 9 PDF/Office tracer tests (§9.5) |
| Durability tests | `backend/tests/test_pr72_durability.py` | 4 commit/replay/rematerialize tests (§9.6) |
| Conformance | `backend/conformance/fixtures/canonical_vectors_v1.json` | 4 anchor-domain golden vectors (35 total cases) |
| Benchmark | `backend/scripts/bench_pr72_anchors.py` + `docs/reference/measurements/pr72-anchor-reading-order.json` | §8 characterization |

## 2. Anchor → graph → view flow

```
artifact bytes ──(native_sources extraction: pypdf / zip+xml)──> selector facts
                                                                    │
ContentRevisionRecord (PR70/71, blob_key = sha256(bytes)) ────────▶ SourceAnchorRecord
                                                                    │  identity = framed hash of
                                                                    │  {content_revision_ref, locator,
                                                                    │   selectors by family}
                                                                    ▼
                                       layout producer ──▶ ReadingOrderGraph
                                                                    │  identity = framed hash of
                                                                    │  canonical graph payload
                                                                    ▼
                                     KernelCommitService.commit(batch) ──▶ kernel_records rows
                                                                    │
                                     replay() ──▶ RecordView.payload ──▶ from_payload()
                                                                    │  (same anchor/graph ids)
                                                                    ▼
                                     linearize(graph, policy) — derived VIEW, ambiguity reported
```

## 3. Identity model

**What is the exact semantic identity of a SourceAnchor?** The PR61 framed identity hash (`marker.kernel.source_anchor.v1`, schema `1.0.0`) over `{content_revision_ref, locator, selectors}` where each selector contributes its canonical value: native `{provider, native_kind, native_id, package_path?}`, quote `{quote, prefix, suffix}` (Unicode-exact, never NFC-folded), position `{scope, start, end}`, geometry `{space, boundary, quantized geometry, render_state?, approximate}`.

**Identity-bearing vs evidence-only.** Identity: everything above — revision ref, locator, all selector facts, coordinate space, render state, approximate flag. Evidence-only (excluded from `identity_payload()`): producer/lineage, observed timestamps, seam metadata. Consequences: two producers reporting identical facts under one revision converge to one anchor (kernel `DuplicateRecordIdentityError` on re-commit); evidence changes never remint.

**How is ContentRevision binding enforced?** `content_revision_ref` is a validated kernel record reference and the first identity field; there is no anchor-without-revision construction path. A changed revision changes the hash by construction; historical anchors stay inspectable (append-only records).

**Why does an ACL-only change not remint anchors?** `AccessPolicyRevisionRecord` is a separate record family; the anchor payload structurally contains no policy field (asserted by test). Authorization epochs change authorization state, not content addresses.

**How are PDF points, Office EMU, and render pixels kept distinct?** `GeometrySelector.space` is a registry-validated `CoordinateSpace` (`pdf.page_points.v1` y-up pt / `office.emu.v1` y-down emu / `render.pixel.v1`) carried inside identity; identical integer tuples under different spaces hash differently (unit test + conformance vector). Render space requires non-empty `render_state` and forces `approximate=True`; native space rejects render state. Coordinates are PR61 fixed-point integers only — `geometry_from_canonical` rejects floats even at rematerialization.

## 4. Reading-order model

**Relationship types:** `contains` (single-parent tree; overlapping containment and cycles rejected), `before` and `continues` (producer + canonical-decimal confidence + evidence state `asserted|unresolved`), `member_of` (region/column membership, target must be a region node). Every edge carries producer lineage.

**Authoritative vs derived vs unresolved:** graph edges as committed are the evidence layer; `linearize(graph, policy="canonical_id")` is a visibly derived view — only `asserted` edges constrain it and every policy tie-break is reported in `ambiguous_groups`. `unresolved` edges (and unresolved opposite hypotheses alongside asserted ones) remain representable without promotion.

**What happens when constraints conflict?** Asserted cycles, both-direction asserted pairs, ordering between a containment ancestor and descendant, duplicate edges, and overlapping containment all fail closed (`OrderConflictError`/`KernelError`) — never a last-write-wins sequence.

**Bounded neighborhood for a specialist split:** `split_node` touches exactly the split node, its parent, its replacement children, and the endpoints of incident before/continues/member_of edges. Reconnection is sound, not fabricated: `X before node` becomes `X before each child` and `node before Y` becomes `each child before Y` (children partition the region); internal order exists only where the specialist declared it. Conservative widening: splitting a node that itself contains children abstains with an explicit error (re-parenting disposition required). A soundness test proves no child_order permutation can manufacture an asserted cycle. Crop-local order is never promoted to document-global order: the split writes only edges inside the neighborhood (measured: 7 edges rewritten, 1996 preserved on a 1000-node graph).

**Why one record, not kernel edges?** `kernel_record_edges` has no payload column — producer/confidence/state cannot live there. One `ReadingOrderRecord` per graph reuses the existing commit/replay/manifest machinery transactionally; PR73 can mint graph-record revisions and PR74 can assess them without a new persistence authority.

## 5. Failure matrix (adversarial cases from plan §13)

| Attack/failure | Observed outcome | Test |
|---|---|---|
| Same geometry+quote, different content revision | distinct anchor ids; old anchor still rematerializes | `test_content_revision_change_mints_new_anchor`, `test_new_content_revision_rebinds_every_anchor` |
| Identical text twice on one page | two valid distinct anchors via geometry; same quote with no distinguishing selector is one id (ambiguity is resolution-time) | `test_identical_quote_twice_is_two_valid_distinct_anchors`, `test_same_quote_different_column_is_two_anchors` |
| Same native/package path under changed revision | new revision → new anchor id | revision-binding class |
| PDF vs Office coordinates, coincident numbers | spaces part of identity → different ids | `test_pdf_points_and_office_emu_cannot_collide` + conformance vector variant |
| Quantization collapses a tiny box | `CanonicalValueError` (degenerate boxes are not identity geometry) | `test_degenerate_box_rejected_by_geometry_profile` |
| Malformed/unknown selector extension | fail-closed `KernelError` (unknown families, unknown fields, unknown spaces) | `test_unknown_family_fails_closed`, `test_unknown_selector_fields_fail_closed`, `test_unknown_space_fails_closed` |
| Missing native id, valid quote/geometry | anchor valid, classified `quote_context` | `test_missing_native_id_leaves_anchor_valid` |
| Two-column page, no justified cross-column order | graph keeps only within-column edges; linearization reports cross-column ambiguity | `test_two_columns_stay_partially_ordered`, `test_two_columns_remain_partially_ordered_from_real_facts` |
| Specialist children overlapping/unsafe inputs | child-id collision, non-permutation order, region children, container re-split → explicit rejection, original graph unchanged | `TestSpecialistSplit` |
| Contradictory before/after constraints | `OrderConflictError` (cycle / both-directions); asserted+unresolved opposite stays representable | `TestContradictions` |
| Shuffled insertion / dict order | byte-identical canonical payload + same graph id | `test_shuffled_insertion_produces_identical_bytes` |
| Replay/materialization after restart | same anchor/graph ids from replayed payloads; re-extraction reaches same ids | `test_verify_history_and_replay_rematerialize`, `test_recommit_after_simulated_restart_keeps_ids` |
| ACL-only change, stable content revision | anchor payload has no policy field; ids unchanged | `test_acl_only_change_does_not_remint` |
| Old anchor inspection after newer revision | historical record rematerializes with identical id | `test_historical_anchor_stays_inspectable_after_new_revision` |

## 6. Fixtures and canonical hashes

**PDF fixture** (`pr72_fixtures.build_two_column_pdf`): hand-assembled one-page PDF, MediaBox `[0 0 612 792]`, four text runs (two columns at x=72/x=316, y=720/700), two `re` rectangles. Byte-deterministic (computed xref). Extraction via pypdf content-stream parsing keeps coordinates as exact decimal text.

**Office fixture** (`pr72_fixtures.build_native_docx`): minimal OOXML package (`[Content_Types].xml`, `_rels/.rels`, `word/document.xml`) with three bookmarked paragraphs (`w:bookmarkStart` ids 0/1/2) and one `wp:anchor` drawing at EMU offsets `(1828800, 457200)` with extent `(914400, 457200)`. Fixed zip metadata → byte-identical regeneration.

**Canonical anchor fixture hashes** (conformance corpus, `marker.kernel.source_anchor.v1` / `1.0.0`):

- `pr72-anchor-pdf-point-geometry-quote` → `sha256:44401df3fa786e99274043dce50af2ed22c7711231c653bf29c5047426c1eea6` (office.emu.v1 variant differs)
- `pr72-anchor-emu-drawing` → `sha256:dd6e28aef1a5bad522f6940547b82b85c9a862f38430fd612987a088302b16cb`
- `pr72-anchor-unicode-quote-exact` → `sha256:ad4388641bf965fa61c8c92dda0fb4d7237844105c8d9cfb549a9dd3bc6cba64` (decomposed variant differs)
- benchmark anchor example → `sha256:b55b485d0bc6f7d486e10f2c33565799f18fdfb6ed77f7bfd769ae776be57205`

## 7. Test commands and results (exact final code)

All commands run from `backend/` on the recorded code base.

| Command | Result |
|---|---|
| `python -m pytest tests/test_kernel_source_anchors.py -q` | 27 passed |
| `python -m pytest tests/test_kernel_reading_order.py -q` | 33 passed |
| `python -m pytest tests/test_pr72_native_tracer.py -q` | 9 passed |
| `python -m pytest tests/test_pr72_durability.py -q` | 4 passed |
| `python -m pytest tests/test_kernel_source_records.py tests/test_canonical_geometry.py -q` | 78 passed (PR61/PR70 base unchanged) |
| `python -m pytest conformance -q` | 40 passed (35-case corpus incl. 4 PR72 vectors) |
| `python -m pytest tests/test_dependency_truth.py tests/test_dockerfile.py -q` | 40 passed |
| `python -m pytest tests/test_database_migration.py -q` | 33 passed |
| `python -m pytest tests conformance -q` | **1970 passed, 3 skipped** |
| `python -m app.cli provenance --verify` | local env reports `ok:false` (81 pin drift vs `requirements-cpu.lock`) — **pre-existing local GPU-env skew**, PR72 adds no dependencies and touches no lockfile; CI installs from the lock and gates there |
| `python scripts/bench_pr72_anchors.py` | wrote `docs/reference/measurements/pr72-anchor-reading-order.json` |

PR70/71 regression surface is inside the full suite (source records/acquisition/store/snapshot/ingress/runtime tests all green).

## 8. Performance characterization

| Measurement | Value (this machine) |
|---|---|
| Canonical payload bytes per anchor (3-selector) | ~550 B |
| Anchor identity time | ~315 µs each; 1000 anchors in 0.32 s |
| Graph payload bytes per order edge | ~147 B (1000-node/1999-edge graph: 294 KB, 75 ms serialize) |
| Local split on 1000-node graph | 5.3 ms; **7 edges rewritten, 1996 preserved, 6-node neighborhood** |
| Same split on 10,000-node graph | same 6-node neighborhood (graph-size independent) |
| Split vs manual full reconstruction | ~1.8× — fail-closed validation dominates; the benefit is bounded touched structure and sound transfer, not raw time |

No operation performs document-wide all-pairs comparisons; the only O(V+E) step is whole-graph validation at construction, which is the fail-closed contract.

## 9. Design decisions and rejected alternatives

- **Selectors in family-keyed slots (at most one per family), not an open list.** Ordering of semantically-unordered selector collections can never leak into identity because there is no order to leak; a list would have needed `CanonicalSet` machinery for no semantic gain.
- **Reading order as one kernel record, not per-edge kernel records and not a new table.** Edge table has no payload; a new table would be a second persistence authority (plan §14 explicitly rejects that). One record gives transactional commit, replay, manifest coverage, and PR73/PR74 hooks.
- **No record-type registry added to `records.py`.** The commit path is generic over `identity_payload()`; the two new record classes live beside their semantics (`anchors.py`, `reading_order.py`) and reuse `KernelRecord` unchanged.
- **Simultaneous-Kahn-readiness as the ambiguity test.** Two nodes ready at once provably have no asserted path either way, so ambiguity reporting needs no transitive-closure computation.
- **Native extraction instead of Markdown laundering.** PDF facts come from the real content stream via pypdf; Office facts from the real package via stdlib zip/XML. python-docx/python-pptx were considered and rejected for the fixture — stdlib assembly is deterministic and dependency-free.
- **Simpler alternative considered:** anchors as bare dicts inside `NativeFactRecord.anchor`. Rejected: untyped, no revision binding, no fail-closed selector grammar, and it would leak one parser's shape into the public contract.

## 10. What was deliberately not built

Cross-revision mapping cascade and mapping decisions (8C.6/8C.7 — later PR); patch language/preconditions/conflicts (PR73); proof DAG/verification authorization (PR74); remote connector inbox (PR71 continuation); production converter migration — `native_sources.py` is the tracer seam, not a converted pipeline; multi-page table reconciliation; semantic/embedding anything; UI.

## 11. Residual limits (owned by later PRs)

1. **Cross-architecture identity:** evidence here is cross-OS/runtime (CI conformance matrix ubuntu/windows/macos × Py 3.11/3.13). x86_64/ARM64 equivalence is the master plan's stronger claim and remains a named residual gate — no ARM run was performed in this session (local machine is x64).
2. Anchor selector content is fixed at the four v1 families; adding a family is an intentional, identity-affecting contract change (fail-closed by design).
3. `linearize` ships one declared policy (`canonical_id`); region-major/column policies are a rendering-policy concern for a later slice.
4. `native_sources.py` parses the operators the fixtures use (`Tm`/`Tj`/`re`, `wp:anchor`); full PDF graphics-state text (`Td`, `TJ` arrays, font-metric extents) and wider OOXML coverage belong to the production parser migration.
5. Local-env provenance drift (81 pins) pre-dates this slice; unchanged by it.

## 12. Next dependency-complete slice

**PR73 — patch language, optimistic patch conflicts, view revisions, dependency/invalidation** on top of this slice's immutable anchor ids, explicit reading-order neighborhoods, and replayable graph records. (PR71 remote/connector continuation is the alternative dependency-complete path.)
