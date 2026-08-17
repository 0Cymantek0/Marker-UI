# Publication Sets and Lexical Generations (PR76)

Status: implemented (V3.2 PR76). Module: `backend/app/kernel/publications.py`.
Migration: `20260817_0011` (five tables, arriving empty). Measurements:
`docs/reference/measurements/pr76-publication-sets.json`.

## What this layer is

A publication set is the one object that means *"these exact queryable
products belong together and are the currently accepted published
state."* It names immutable member generations — the required
materialized kernel generation and lexical (FTS5) index generation,
plus an optional vector slot whose `NULL` is an explicit **absent** —
and it becomes current through exactly one transactional pointer switch
on `kernel_publication_heads` (one row per `(workspace, profile)`).

Everything here is rebuildable derived serving state over kernel
truth, never a second truth authority: `kernel_commit_id` ordering
remains the only document truth, and every row can be discarded and
rebuilt from its pinned cut.

## Invariants

- **I1 — one published pointer.** `resolve_published_set` is the single
  authoritative resolution; there are no independent per-layer
  "current" pointers. A head naming a missing set fails closed.
- **I2 — immutability after acceptance.** Set and lexical manifests are
  written once; rebuilding the same declared inputs reproduces the same
  deterministic identity or fails closed on digest disagreement.
- **I3 — compatibility by construction + validation.** Members must
  agree on workspace, kernel cut, snapshot, and source-generation
  lineage at staging; validation additionally enforces member presence
  and lifecycle, lexical deep integrity, per-row locator membership in
  the materialized generation, and manifest digest agreement.
- **I4 — optional means absent.** A set without a vector layer carries
  `vector_generation_id = NULL`; resolution never borrows an older
  publication's optional layer. Sneaking a value in post-acceptance is
  detectable (the digest covers the slot).
- **I5 — staging is invisible.** Only a *published* set names queryable
  state; staged/validated candidates and lexical staging residue are
  never resolved by readers.
- **I6 — failed validation displaces nothing.** A failed candidate set
  is marked `failed`; the prior published set stays head.
- **I7 — reindex is generational.** A lexical generation is immutable;
  a reindex or corpus change produces a new generation and a new set
  candidate. v1 supports exactly the `unicode61` tokenizer with empty
  config; anything else fails closed rather than silently reindexing.
- **I8 — hits are source-resolvable.** Every FTS row maps through
  `kernel_lexical_rows` to (record id, view id, node id, view revision
  ref, text hash). Reads re-verify each hit's locator and text hash;
  orphan or tampered hits fail closed.
- **I9 — readers pin once.** A reader resolves the published set once
  and uses that identity for its whole lifetime; a mid-read
  publication switch cannot change any layer underneath it.
- **I11 — crash behavior is binary.** See the linearization point
  below: recovery sees the old complete set or the new complete set.

## Lifecycle and the linearization point

1. `build_lexical(source_generation_id)` — one transaction creates the
   generation-scoped FTS5 table, inserts its rows and locator rows,
   and writes the manifest in state `staged`; a second transaction
   validates by recomputing the digest from stored rows **and** the
   FTS read-back (plus FTS5 `integrity-check`). Crash residue is
   resumable via `validate_lexical`.
2. `stage_publication_set(...)` — records the immutable member
   manifest in state `staged` (building the lexical member when not
   supplied).
3. `validate_publication_set(id)` — enforces the full compatibility
   key; failure marks the set `failed` and raises.
4. `activate_publication_set(id)` — one transaction supersedes the
   previous published set, conditionally flips
   `kernel_publication_heads`, and marks the set `published`. **That
   transaction's commit is the publication linearization point.**
   `publish(...)` chains steps 1–4.

Fault phases (`PUBLICATION_FAULT_PHASES`) bracket every step; the
fault tests prove no pre-linearization failure displaces the accepted
set and a post-commit fault still exposes the complete new set.

## FTS5 storage mode

One **self-contained** (non-external-content) FTS5 virtual table per
lexical generation, named `kernel_fts_<hex>` and created at build
time — never by Alembic. SQLite documents that external-content FTS
consistency is the application's responsibility; per-generation
self-contained tables make the index/content/generation relationship
provable by construction instead (the index is never shared across
generations and never mutated after staging). Dropping the generation
drops its table transactionally (GC and the migration downgrade both
do this). The migration contract comparison excludes the
`kernel_fts_` prefix symmetrically (`app.db_migration`): these tables
are rebuildable derived serving state, not schema authority, and a
head database carrying built indexes still classifies as `CURRENT`.

## Reads and retention

`open_published_reader` resolves + pins the current set;
`open_pinned_publication` pins a named (possibly superseded) set for
long reads. Pins are bounded wall-clock leases
(`kernel_publication_pins`); an unexpired pin protects the set and
every member from GC, which now treats live sets as retention roots.
Retirement order per collection pass: materialized generations (with
live-set member rescue) → publication sets → unreferenced lexical
generations (dropping their FTS tables).

## Known limits (deliberate)

- Single tokenizer (`unicode61`), empty config, single view per
  workspace (v1 kernel reality); a tokenizer change must arrive as a
  new projection version.
- The vector layer is a slot, not an implementation (PR81); absence is
  recorded explicitly.
- Idempotent rebuilds trust immutable rows (mirroring materialized
  generations): deep verification (`verify_lexical_generation`,
  `verify_publication_set`) and per-hit read checks are the tamper
  detectors.
- Lexical validation re-reads every indexed row — linear in corpus
  size per build, characterized in the measurements.

## Reproduce

```bash
cd backend
python -m pytest tests/test_kernel_publication.py \
          tests/test_kernel_publication_faults.py \
          tests/test_kernel_publication_concurrency.py \
          tests/test_kernel_publication_gc.py -q
python -m pytest tests/test_kernel_migration.py tests/test_database_migration.py -q
# from repository root:
python backend/scripts/bench_pr76_publications.py --write
```

PR77 (typed query planner, EvidencePacket) builds directly on this
substrate: one resolved set → one pinned lexical generation →
source-resolvable hits.
