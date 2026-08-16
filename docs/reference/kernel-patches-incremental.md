# PR73 — Conflict-Aware Patches & Dependency-Complete Incremental Rebuild

**Evidence bundle.** Implementation head: `a4efe6c930a4e1a947ba9fffbd0eaff9a5627d79`
(branch `markerui-v2`, eight commits `0de5ea5..a4efe6c` on top of the PR72 head
`a6a98eb5f6b1b882f284790dca8bfbf8be548739`). Environment: Windows 11 x64,
CPython 3.11, local SQLite (aiosqlite), pytest. Schema/migration head:
`20260817_0010` (new table `kernel_view_heads`). All measurements from
`backend/scripts/bench_pr73_patches.py` on this machine are recorded in
`docs/reference/measurements/pr73-patches-incremental.json`.

## What changed, by file

| File | Change |
|---|---|
| `backend/app/kernel/patches.py` | new — view revision / patch proposal / patch outcome records, operation envelope (`replace_text`, `split_node`, `rebase_source`), preconditions, pure application + rebase replay, transactional advancement evaluator |
| `backend/app/kernel/patching.py` | new — patch service: genesis init, `submit_patch`, `rebase_proposal`, reversal (`build_reversal_proposal` / `reverse_patch`), `load_view_history`, `clean_rebuild_view` (the oracle) |
| `backend/app/kernel/dependencies.py` | new — `DependencyDeclarationRecord` + `compute_invalidation` (exact / conservative / semantic-candidate contract) |
| `backend/app/kernel/rebuild.py` | new — `submit_rebase`, `incremental_rebuild` with declared-source carry |
| `backend/app/kernel/commit.py` | `KernelCommitBatch.view_advancement` seam: in-transaction precondition evaluation + independent recomputation + conditional head flip; fault phases `view-checked` / `view-advanced` |
| `backend/app/kernel/models.py` | `KernelViewHead` ORM model |
| `backend/app/kernel/errors.py` | `InvalidViewAdvancementError`, `StaleBaseRevisionError`, `BeforeHashMismatchError`, `SourceRevisionMismatchError`, `MissingViewTargetError` |
| `backend/alembic/versions/20260817_0010_add_kernel_view_heads.py` | new migration (inspect-and-skip, downgrade documented destructive) |
| `backend/tests/test_kernel_patches.py`, `test_kernel_view_advancement.py`, `test_kernel_patching.py`, `test_kernel_patch_conflicts.py`, `test_kernel_dependencies.py`, `test_kernel_incremental_rebuild.py`, `test_pr73_durability.py` | new — 137 PR73 tests (see matrix below) |
| `backend/tests/test_kernel_migration.py` | PR73 head constants + upgrade/fail-closed/downgrade tests |
| `backend/tests/conftest.py` | `KernelViewHead` registration |
| `backend/conformance/fixtures/canonical_vectors_v1.json` | 3 PR73 vectors (proposal identity, view identity, float rejection) |
| `backend/scripts/bench_pr73_patches.py` | benchmark |

## Authority & revision flow

There is exactly one commit authority. A patch, its accepted outcome, its
resulting view revision, the lineage edges, and the current-view head flip
are **one kernel commit transaction**: the writer lock is taken up front
(the existing write-first `KernelCommitHead` upsert), preconditions are
evaluated against durable current state under that lock, the proposed
revision is *independently recomputed* (operation replay for view patches,
declared-facts replay for rebases) and must equal the advanced revision
exactly, and the head flips conditionally on the observed base.
`kernel_view_heads` ordering is subordinate to `kernel_commit_id` by
construction — there is no second current-document-truth store, no
TOCTOU window, and nothing between "transaction committed" and "revision
current" that a crash could split.

## Patch identity & precondition model

* Base view identity: `ViewDocumentRecord.view_revision_id()` — the
  canonical identity hash over (source content revision, reading graph,
  every node text); it is simultaneously the declared view digest.
* Enforced preconditions today: base revision identity (strong-validator
  `If-Match` discipline), per-node canonical before-value hashes, and
  required source/content revisions. The proposal's operations are
  re-executed transactionally; a mismatch with the claimed result rejects
  the whole batch (`InvalidViewAdvancementError`).
* Deliberately deferred to PR74: claim-assessment preconditions. The
  `required_claim_refs` field exists and **fails closed** — non-empty
  values are rejected at construction and on rematerialization. Nothing
  pretends to verify claims before PR74 exists.
* Rejected patches raise typed conflicts (`StaleBaseRevisionError`,
  `BeforeHashMismatchError`, `SourceRevisionMismatchError`,
  `MissingViewTargetError`) with structured expected/observed attributes;
  nothing durable is written for a rejection (no accepted partial state,
  verified by record-count assertions).

## Conflict matrix (all reproducible tests)

| Adversary | Outcome |
|---|---|
| B1 stale base | typed stale conflict; record count unchanged |
| B2 before-value changed (fresh base) | typed hash conflict; node-id existence never sufficient |
| B3 source revision mismatch | typed conflict (patch level); replay drop (rebuild level) |
| Missing target | fail closed, no partial application |
| B4 overlapping replaces | exactly one accepted; re-targeted loser conflicts on value |
| B5 split vs replace (sequential, both orders) | one direction loses its target (`rebase_proposal` → `None`), the other loses its before-value |
| B5 split vs replace (real `asyncio.gather` race) | exactly one acceptance; loser typed-conflicted; 4 records total (genesis + one batch) |
| B5 three-way same-target race | exactly one of three accepted |
| B6 disjoint patches | compose only through the explicit, tested rebase rule (`rebase_proposal`); value-clobbering and cross-source retargeting refused unless explicitly chosen |
| B7 disjoint order-independence | both application orders produce the identical view identity |
| B7 overlapping falsification | the non-commutative pair conflicts; no merge is claimed anywhere |
| Duplicate/no-op identity | `DuplicateRecordIdentityError`; supersession requires a new record |

Arrival order can never silently choose truth: every advancement is
conditional on the exact base revision under the writer lock.

## Dependency completeness & invalidation

`DependencyDeclarationRecord` carries per-input completeness
(`exact_native`, `exact_operator`, `conservative_scope`,
`semantic_candidate`) plus operator lineage; identity is
version-sensitive, so a changed operator supersedes old assumptions.
`compute_invalidation`:

* exact knowledge localizes; multi-input subjects stay stale until every
  changed input is reconciled (`pending_inputs`);
* a changed conservative input widens its declared scope;
* a change covered by **no** exact knowledge widens every conservative
  boundary and, at the rebuild layer, forces full derivation (uncertainty
  expands; it never disappears);
* semantic-candidate edges only ever surface as `recall_candidates` —
  they can neither narrow nor enter the correctness set.

## Clean-vs-incremental equivalence

The oracle is double-independent per run:

1. `clean_rebuild_view` replays every committed proposal from the genesis
   revision (view patches by operation replay, rebases by declared-facts
   replay, reversals by restore reproduction) and hard-fails if any step
   disagrees with the revision that commit recorded;
2. an independently **full-derived** source view (derive called for every
   node) is replayed purely and must equal the committed incremental
   result.

The incremental path carries from the **last declared source** (genesis
texts or the previous rebase's facts — never the patched view, so replay
never meets its own effects), derives only the invalidated scope, and
falls back to full derivation on any widening, uncovered change,
document-level subject, or structural divergence outside the invalidated
scope. Covered by 16 seeded randomized scenarios (`seeds 0–15`, recorded
in test ids; random graph sizes 3–6, patch chains 0–4, source deltas 0–2
nodes, ~30% runs with conservative declarations) plus the targeted
scenarios in `test_kernel_incremental_rebuild.py`.

**Fault injection:** `view-checked` / `view-advanced` phases roll the
whole batch back — head old-valid, zero partial records (parametrized
tests, including the rebase path).

## Reversal

Only single `replace_text` patches are declared reversible (a split
consumed structure reversal must not guess back). Reversal is new
history: an inverse proposal asserting the exact value the original patch
produced (an intervening change conflicts instead of being clobbered),
committed with an outcome naming the restored revision; the head moves
back to the prior revision's exact committed identity —
content-digest-for-content-digest — while the original patch event and
every revision remain inspectable. Reversal commits are first-class
lineage steps the oracle must reproduce.

## What survives restart / what GC may delete

Everything durable is committed truth: accepted lineage, reversals, and
the head recover from a fresh engine on the same file (tested), and
`verify_history` passes over patch workspaces. GC never deletes committed
records — the revision lineage stays reconstructable after generations
are retired; a `generation_hold` protects a materialized generation
through `collect` while active (tested), and after release + collection
the view history rebuilds identically. Downgrade of `20260817_0010`
drops only current-pointer truth; committed view records survive and
re-committing the same genesis fails as a duplicate identity rather than
resurrecting silently.

## Measurements (this machine; JSON has full precision)

* Identity: ~0.6 ms per proposal+view identity pair (1000 iterations).
* Conflict-check: ~1.5 µs per node precondition check.
* Derivation locality for a local edit: incremental derives **1** node at
  200 and at 2000 nodes (200× / 2000× fewer derive calls than clean);
  resulting revision identical at both scales. Replay-verification cost
  is identical for both paths by design — the win is derivation work,
  not skipped verification.
* Worst invalidation amplification: the same local edit invalidates 1
  node under exact knowledge and the full 2000-node scope under
  conservative knowledge (widening is the price of honesty).

## Rejected alternatives

* **Change pruning (Bazel-style resurrection).** Declined: the measured
  win is derivation locality, which does not need downstream
  resurrection machinery; the complexity was not earned.
* **Generation activation as the acceptance linearization point.**
  Rejected: activation is a separate transaction from the record commit,
  which opens a ghost/pollution window between committed patch records
  and head movement. The in-commit conditional head flip removes it.
* **Committing rejection outcomes.** Rejected for this slice: a typed
  exception with structured attrs is machine-detectable and leaves
  nothing ambiguous; durable rejection records can arrive with review
  workflows (PR74+) if they earn their storage.
* **Carrying patched view values into rebase declarations.** Rejected
  during implementation (caught by the oracle design): declared source
  facts must be pure; carry from the last declared source instead.

## Residual limits

* One named view (`document`) per workspace; the head table is keyed for
  named views but services pin the single id.
* `record_id`s minted by the service are workspace-scoped because
  `kernel_records.id` is a global primary key; cross-workspace
  uniqueness relies on that scoping.
* `load_view_history`/`clean_rebuild_view` are O(lineage) — acceptable
  at tracer scale; a bounded-incremental oracle is future work.
* Conformance vectors cannot express array-order-insensitivity at the
  JCS layer (arrays are order-significant there); that property is
  proven in unit tests where the record class normalizes before
  serialization.
* Rebase replay of the full patch set re-evaluates every proposal —
  intentional: equivalence by construction beats clever partial replay.

## Test commands & results (final code)

```
python -m pytest backend/tests backend/conformance   # full suite
python -m pytest backend/tests/test_kernel_patches.py \
  backend/tests/test_kernel_view_advancement.py \
  backend/tests/test_kernel_patching.py \
  backend/tests/test_kernel_patch_conflicts.py \
  backend/tests/test_kernel_dependencies.py \
  backend/tests/test_kernel_incremental_rebuild.py \
  backend/tests/test_pr73_durability.py             # PR73 slice
```

PR73 slice: 107 targeted tests (29 contract, 11 advancement seam, 12
service, 12 conflict matrix, 11 dependencies, 24 incremental rebuild
incl. 16 randomized seeds, 5 durability, 3 migration-slice additions)
plus 3 new conformance vectors. Final full-suite result on the final
code: `python -m pytest backend/tests backend/conformance` →
**2081 passed, 0 failed, 3 skipped** (PR72 baseline was 1970 passed /
3 skipped; the +111 delta is PR73). The 3 skips are the pre-existing
platform-gated skips, unchanged by this slice.

## Next dependency-complete slice

**PR74** (ClaimAssertion/ClaimAssessment, proof DAG, cycle rejection,
proof-input integrity) — the `required_claim_refs` seam is already
versioned and fails closed. PR69 (model admission/leases) and the
remote/connector remainder of PR71 remain parallel unfinished lanes and
are not falsely marked complete.
