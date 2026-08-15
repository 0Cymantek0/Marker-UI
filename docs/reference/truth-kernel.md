# Truth Kernel — Commit Spine + Payload Durability + Snapshots & Generations (PR63A/PR64/PR65A)

Status: implemented (V3.2 amendment 4C / PR63 slice A, then amendment
18C/19C PR64, then PR65 slice A). This page is the authoritative
description of what the local Truth Kernel guarantees, what it
deliberately does not guarantee yet, and how PR65B/PR66 are expected to
attach. The canonical identity contract it consumes is documented
separately in [canonical-identity.md](canonical-identity.md); the
migration authority in [../development/database.md](../development/database.md).

## Overview

PR63A established the first production-shaped local Truth Kernel
persistence layer, PR64 extended it with durable payload staging,
availability truth, and the transactional outbox, and PR65A adds
snapshot-pinned immutable materialized read generations:

- an Alembic-owned schema — revision `20260815_0004` (commit spine:
  `kernel_commit_heads`, `kernel_commit_manifests`, `kernel_records`,
  `kernel_record_edges`), revision `20260815_0005` (PR64:
  `kernel_payload_objects`, `kernel_outbox`), and revision
  `20260815_0006` (PR65A: `kernel_generations`,
  `kernel_generation_records`, `kernel_generation_edges`,
  `kernel_generation_heads`);
- one internal commit authority, `app.kernel.commit.KernelCommitService`,
  through which every kernel mutation enters SQLite;
- per-workspace causal commit chains ordered by `kernel_commit_id`
  (wall-clock timestamps are audit metadata only and never order truth);
- atomic multi-record batches: head, records, edges, one immutable
  manifest, payload-registry rows, and outbox intent become durable in a
  single transaction;
- a content-addressed immutable blob store
  (`app.kernel.payloads.LocalPayloadStore`) that durably publishes and
  verifies payload bytes **before** the database may reference them;
- a durable at-least-once outbox (`app.kernel.outbox`) whose rows appear
  exactly with the commit that authorizes them;
- availability classification and conservative repair
  (`app.kernel.reconcile`);
- PR61 canonical identity as the record identity boundary;
- a read-side replay/verification surface (`app.kernel.replay`) — a
  correctness tool, explicitly not the serving path;
- **PR65A**: `app.kernel.snapshots.resolve_snapshot` pins a workspace to
  one committed cut with an honest completeness verdict, and
  `app.kernel.generations` builds deterministic immutable read
  generations from that cut (build → validate → activate) with a
  bounded, generation-pinned reader that never replays kernel history.

Existing GUI/REST/CLI/MCP conversion behavior is untouched: nothing in
production calls the kernel yet. Legacy job/settings/audit tables remain
migration substrate until their later integration phases.

## Commit protocol and the linearization point

One `KernelCommitService.commit(batch)` call performs the SQLite portion
of the V3.2 commit protocol:

1. pre-transaction canonicalization/validation of every record, edge,
   outbox intent, and producer metadata (rejected canonical values fail
   here, before any lock is taken);
2. **durable payload staging (PR64)** — when the service is constructed
   with a payload store, every record's `payload_bytes` is published
   once, before the transaction: write-to-scratch, `fsync`, atomic
   rename to the content-addressed final path, directory `fsync` where
   the platform allows, then re-open and re-verify the final bytes.
   Declared-hash records whose object is already staged and verified
   reuse it. Staging runs once per `commit()` call; database retries
   never re-publish bytes;
3. begin a transaction and immediately upsert the workspace
   `kernel_commit_heads` row (insert-or-ignore) — the first write takes
   the SQLite writer lock, so concurrent committers serialize at the
   database instead of racing through a read snapshot;
4. read the committed head, derive `kernel_commit_id = head + 1` and
   `parent_kernel_commit_id = head`;
5. resolve external record references against visible committed state
   (unknown or cross-workspace references are rejected);
6. insert records and edges;
7. insert `kernel_payload_objects` registry rows for the staged blobs
   (insert-or-ignore on the blob key: identical bytes back any number of
   records with one registry row);
8. insert the immutable manifest (counts, per-class counts, deterministic
   record/edge roots, manifest identity hash);
9. insert the batch's `kernel_outbox` rows with deterministic dedupe
   keys — successor work becomes visible exactly when the authorizing
   commit does;
10. advance the head with a conditional update
    (`WHERE head_kernel_commit_id = <observed>`) — a lost update cannot
    be silently accepted;
11. COMMIT. **The database commit while holding the head row is the
    linearization point.** Before it: nothing of the batch is visible.
    After it: records, edges, manifest, payload references, and outbox
    intent are visible together.

`SQLITE_BUSY`/lock errors and concurrent head movement are treated as
expected retryable conditions with a bounded retry budget (8 attempts,
exponential backoff capped at 500 ms). A free-running database sequence
is never used for order.

## Payload durability model (PR64)

The store root (default `<data>/kernel_payloads`, override
`MARKER_KERNEL_PAYLOAD_ROOT`) holds:

- `objects/<hex[0:2]>/<hex>` — immutable final objects, named by the
  hex sha256 of the exact bytes (a partial write can never impersonate
  a valid object: the name itself is the integrity claim);
- `tmp/<uuid>.tmp` — staging scratch, never referenced by truth;
- `quarantine/<hex>.<n>` — corrupt objects displaced when exact verified
  bytes are re-supplied (tamper evidence, never reused).

Invariants the implementation proves by test:

- **Durable-before-reference.** A commit can only reference a payload
  as available if the immutable final object was published and
  re-verified first. The registry row that makes the reference
  "available" lives in the same transaction as the record — it appears
  or disappears with the commit.
- **Crash ordering is one-sided.** A pre-commit crash may leave an
  unreachable complete object (classified `orphan` by reconciliation,
  reusable byte-for-byte by a retry); it can never leave committed
  truth that depends on bytes never durably published. The full fault
  matrix (`tests/test_kernel_payload_faults.py`) injects failures at
  six storage phases and nine transaction phases and asserts exactly
  these two outcomes.
- **Blob identity is not evidence identity.** Identical bytes are
  physically stored once and shared by any number of distinct records;
  semantic record identity (`identity_hash`) never merges.
- **Tampering is detectable.** Truncation or mutation of a committed
  object makes availability verification report `corrupt`; deletion
  reports `missing`; neither may be presented as complete.
- **Storage paths are hex-derived only.** Record ids and caller strings
  never reach path construction; persisted locators are validated
  against a strict grammar before resolution.

Availability classes reported by
`app.kernel.reconcile.verify_payload_availability` per committed
payload-bearing record: `available`, `missing`, `corrupt`, and
`metadata_only` (hash declared but bytes never durably staged in the
local profile — honest, and not payload-backed-complete). Database
chain integrity (`verify_history`) and payload availability are
separate dimensions: a database can verify green while availability is
degraded, and the surfaces never conflate them.

`reconcile_after_restart` reconstructs everything from disk and
database: committed payloads re-verify, pending outbox stays pending,
stuck in-flight outbox returns to pending (at-least-once), orphans and
stale tmp scratch are classified; only tmp scratch past an explicit age
threshold is ever deleted. Repair never deletes objects (GC is PR65),
never manufactures bytes, and never rewrites evidence identity. Healing
a corrupt object requires the exact bytes to be re-supplied and
re-verified through staging; the displaced tampered object is kept as
quarantine evidence.

## Outbox semantics (PR64)

- Rows are created only inside the authorizing commit transaction:
  rollback removes intent, commit makes it durable.
- Delivery is honestly **at-least-once**: `claim` (pending → in_flight,
  single-claim per item), `ack` (in_flight → done), `release`
  (in_flight → pending, attempts+1), `reset_in_flight` (crash recovery).
  Consumers must be idempotent across redelivery.
- `dedupe_key` deterministically derives from the authorizing commit
  and the intent content (`marker.kernel.outbox_intent.v1` framing), so
  a retried commit protocol cannot duplicate an intent; duplicate
  intents inside one batch collapse to one row.
- No dispatcher exists yet; PR66 owns scheduling, leases/fencing, and
  exactly-once accepted publication.

## Identity rules

- Record semantic identity = `app.utils.canonical.record_identity_hash`
  over the per-class framing domain (`marker.kernel.<class>.v1` +
  schema version). Mapping order, key order, and set-member order never
  change identity; floats, sets, datetimes, and bytes are rejected at
  the kernel boundary; raw Unicode is preserved exactly (no NFC/NFKC
  folding); decimals and geometry enter as canonical strings and
  fixed-point integers respectively.
- `record_id` is a caller-visible event id and is **not** part of
  semantic identity. Semantically identical records are rejected as
  duplicates within a batch and across commits (unique constraint on
  `(workspace_id, identity_hash)`); supersession requires a new record.
- Payload byte hash (`payload_byte_hash`, exact stored bytes) is stored
  separately from identity. Two observations with identical payload
  bytes but different derivations are two distinct evidence records that
  share one payload hash — payload dedup never collapses or inflates
  evidence.
- Manifest identity hashes the manifest payload (workspace, commit ids,
  counts, roots, schema/canonicalization versions) under
  `marker.kernel.commit_manifest.v1`; the audit timestamp is excluded.

## Record classes established

`NativeObject`, `NativeFact`, `ClaimAssertion` (immutable meaning +
stable subject identity), `ClaimAssessment` (append-only, references its
assertion, declares the policy/evidence/snapshot context fields it
knows), `Observation` (witness record demonstrating the
record-vs-payload identity split), and a storage-envelope `Decision`.
Edges are a small fixed vocabulary (`depends_on`, `derived_from`,
`assesses`, `evidence_for`, `observes`) with in-workspace, visible-target
enforcement. Proof DAG/authority/cycle semantics are explicitly PR74+.

## Verification and replay

`app.kernel.replay.verify_history` recomputes, for every workspace:
record identities from stored payloads, canonical payload form,
manifest counts/roots/identity, chain contiguity and parent linkage,
head agreement, edge endpoint visibility, and orphan rows (records/edges
whose creating commit has no manifest). Problems are reported as
`[kernel] workspace=... commit=N: <violated expectation>` — never a
generic database error. `replay()` reconstructs a deterministic
metadata-only view in commit order with a replay digest; replaying a
range twice must produce identical digests, and membership is decided
exclusively by `kernel_commit_id`.

Payload availability is verified by the separate
`verify_payload_availability` surface (see above); the two dimensions
are intentionally not merged.

## Snapshots and materialized generations (PR65A)

### Snapshot contract

`app.kernel.snapshots.resolve_snapshot(factory, workspace_id,
at_commit=None, required_payload_state=..., payload_store=...)` pins one
committed cut of one workspace chain:

- membership is exclusively `kernel_commit_id <= K`; `at_commit=None`
  pins the current head and `K=0` is the valid empty cut;
- future (`K > head`), negative, and non-integer cuts are rejected
  explicitly; a cut below head without a manifest fails closed as chain
  corruption, as do manifest/record count disagreements;
- the resolver returns a deterministic `snapshot_id` (framing
  `marker.kernel.snapshot.v1`) over exactly the declared deterministic
  fields, so resolving the same cut repeatedly is stable and different
  cuts never collide;
- completeness is honest against the requested payload requirement:
  `metadata_only` is complete when the cut's metadata is coherent;
  `inspectable`/`replayable` are complete only when every payload-bearing
  record in the cut verifies `available` in the local store (the scan is
  bounded to the cut — later payloads never participate). Missing,
  corrupt, and metadata-only references keep the snapshot `degraded`
  with per-state counts and a bounded offending-record sample; an
  inspectable/replayable requirement without a payload store is refused
  rather than guessed;
- the master plan's future snapshot bindings (`content_revision_ids`,
  `access_policy_set_id`, `verifier_policy_revision_id`,
  `schema_registry_revision`) are **unbound by declaration**: the
  subsystems that own them (PR70+) do not exist yet, no resolver
  parameter can supply them, and their unbound names are hashed into the
  snapshot identity. Absence is machine-detectable, never fabricated.

### Generation lifecycle

`app.kernel.generations.GenerationService` turns a pinned snapshot into
the current read model in three durable steps:

1. **build → staged.** One transaction reads the committed cut, inserts
   materialized record/edge rows (`kernel_generation_records` /
   `kernel_generation_edges`), and writes the immutable manifest row
   (`kernel_generations`, state `staged`) with the content digest
   computed from the source cut.
2. **validate → validated.** The digest is recomputed *from the
   materialized rows*; counts, workspace bounds, and cut bounds are
   re-checked. Divergence marks the generation `failed` and raises — the
   previously accepted generation is untouched. An explicit
   `validate()` step can resume a generation left staged by a crash
   between staging and validation.
3. **activate.** One transaction flips `kernel_generation_heads` under a
   conditional update, superseding the previous active generation. That
   commit is the linearization point: readers observe the old accepted
   generation or the complete new one — never a mix.

Guarantees proven by test (`tests/test_kernel_snapshot.py`,
`tests/test_kernel_generation*.py`):

- **deterministic identity.** `generation_id` derives from (workspace,
  cut, snapshot id, materializer id/version, schema version, canonical
  config). Rebuilding the same declared inputs reproduces the same
  `content_digest` and reuses the immutable rows; a diverging digest for
  the same declared inputs fails closed as tampering/nondeterminism.
- **cut isolation.** Records committed after the pinned cut never appear
  in the generation; rebuilding a historical generation after newer
  commits reproduces the original digest.
- **atomic activation + pinning.** A reader that resolved generation A
  keeps reading A (immutable rows) after B activates; new readers see B.
  A failed or faulted build leaves the prior accepted generation current
  — for every injected lifecycle fault the outcome is binary (prior
  authoritative, or new fully-valid current), with no third state.
- **restart truth.** `resolve_current_generation` recovers the current
  generation from durable state alone; half-built generations are never
  selected; `staged`/`failed` residue is listable for later cleanup.
- **tamper detection.** Validation rejects tampered staged content;
  bounded reads re-verify each record's semantic identity hash (tampered
  rows fail loudly); `verify_generation` recomputes the full digest and
  manifest counts for deep verification.
- **replay-free reads.** The `GenerationReader` surface (summary, record
  lookup, class-filtered enumeration, counts, edges) queries only
  materialized rows — `replay()` is never invoked on the read path
  (asserted by instrumentation).
- **no second truth authority.** Every generation row is derived from
  the committed cut named in its manifest; dropping the generation
  tables and rebuilding reproduces the same identity and digest.

### Deliberate PR65A limits (owned by PR65B/PR66)

- No proof-closure roots, reader/retention pins, or mark/recheck/sweep
  GC; no kernel payload object is ever deleted — **PR65B**. Its
  attachment points are `GenerationService.list_generations(state=...)`
  (stale staging identification), `kernel_generation_heads` (active
  roots), and snapshot watermarks.
- No pack-segment compaction or materialized FTS/vector indexes —
  **PR65B/later**.
- No worker leases/fencing, exactly-once accepted publication, or
  dispatcher; the builder is a plain in-process service — **PR66**
  remains untouched.

## What this kernel does NOT guarantee (deliberate non-goals)

- No proof-closure garbage collection, physical blob deletion, retention
  pins, or pack-segment compaction — **PR65B**. Orphan objects and
  stale staged generations are classified and listable, never deleted.
- No lease/fencing for workers, no exactly-once accepted publication,
  no stable/provisional publication namespaces, no external effect
  ledger — **PR66**. Outbox delivery is at-least-once by declaration.
- No scheduler/job-state changes, no source/content/access identity
  (snapshot future bindings stay explicitly unbound), no patch
  semantics, no verification policy resolution, no materialized
  FTS/vector/visual indexes, no server-side query engine, no UI.
- Production GUI/REST/CLI/MCP paths are still not wired to the kernel.
- FK constraints are declared RESTRICT for documentation, but the app
  engine does not enable SQLite FK enforcement — referential visibility
  is enforced inside the commit transaction by the service, and the
  verifier double-checks it on read.
- Platform caveat: Windows cannot `fsync` directories, so the rename
  durability barrier is file-`fsync`-plus-atomic-rename there; the
  post-rename read-back verification closes the gap for the guarantees
  claimed. The blob store serializes object mutations within one
  process (single-process local profile, mirroring SQLite's own
  single-writer model); concurrent multi-process staging into one store
  root is not a supported topology yet.
- No PostgreSQL/S3/object-store/multi-tenant/distributed outbox
  implementations — future industrial topology; the store/outbox seams
  are the attachment points.

## SQLite notes

- The production engine uses SQLite's default rollback journal
  (`journal_mode=delete`); WAL is not enabled in this slice. The
  master-plan floor **SQLite 3.51.3 (2026 WAL-reset fix) therefore does
  not gate this topology yet** — it becomes binding when WAL is adopted.
- Local validation ran against Python 3.11.9 / SQLite 3.45.1
  (journal_mode=delete): below the 3.51.3 floor, acceptable because no
  WAL mode is exercised. Any later WAL adoption must raise the floor or
  document a fixed backport.
- Contention behavior observed: with Python sqlite3's built-in 5 s busy
  timeout, 8 concurrent same-database writers produced zero surfaced
  `SQLITE_BUSY` errors (queues show up as latency, not failures); the
  service retry budget is the backstop beyond the driver timeout. PR64
  concurrency tests add payload staging into the race and keep the same
  guarantee: bounded retries, linear chain, immutable bytes.

## Measurement baseline

`backend/scripts/kernel_characterization.py` (rerun for updated numbers).

PR63A baseline (metadata + 512-byte payload hashes, before durable
staging):

| shape | commits×records | db bytes | WAL | p50 / p95 commit | replay | verify | retries |
|---|---|---|---|---|---|---|---|
| 4 writers | 100×12 (1200 records, 100 edges) | 1,056,768 | 0 | 15.6 / 294.7 ms | 0.089 s | 0.280 s | 0 busy / 0 head |
| 8 writers | 200×8 (1600 records, 200 edges) | 1,404,928 | 0 | 14.7 / 680.3 ms | 0.131 s | 0.383 s | 0 busy / 0 head |

PR64 delta (Windows dev box, Python 3.11.9, 40 commits × 8 records,
mixed metadata-only/payload-bearing, 520-byte payloads, 4 writers):

- commit latency p50: metadata-only ≈ 28 ms vs payload-bearing ≈ 133 ms
  — the delta is one durable stage (write+fsync+rename+fsync-dir+
  read-back-verify) plus the registry row, all before/inside the same
  transaction window; the store serializes staging within the process;
- staging latency p50 ≈ 20–24 ms and nearly flat from 1 KiB to 1 MiB
  (fixed per-object overhead dominates at these sizes);
- write amplification ≈ 0.58 physical bytes per logical byte in the
  mixed workload (dedup reuse subtracts); every written byte is also
  read back once for verification by design;
- availability scan ≈ 90 ms for 40 payload-bearing records (full
  re-hash); restart reconstruction scans cost the same order;
- 5 injected pre-commit failures produced exactly 5 orphan objects and
  0 tmp residue; 0 busy/head retries at 4 writers.

No threshold is imposed by the plan; this is the measured operating
envelope PR65 may optimize.

PR65A delta (same Windows dev box, Python 3.11.9, SQLite 3.45.1,
journal_mode=delete; default fixture: 100 commits × 12 records, 1200
records / 100 edges in the workspace, generation built at cut 100 with
the full-hash inspectable snapshot):

- snapshot resolution p50: metadata-only ≈ 32 ms; inspectable with full
  payload re-hash ≈ 326 ms (≈300 payload-bearing records re-hashed; the
  availability classes from PR64 are reused unchanged, scan bounded to
  the cut);
- generation build (stage + validate, 1200 records materialized) ≈
  0.57 s; atomic activation ≈ 47 ms; deterministic rebuild (digest
  equal) ≈ 0.18 s; full `verify_generation` ≈ 0.53 s;
- ready generation reads (never replay kernel history): current-
  generation resolution p50 ≈ 9.5 ms, manifest summary ≈ 7.0 ms, record
  lookup ≈ 7.4 ms, 20-record page ≈ 12.9 ms — p95 ≤ 26 ms, comfortably
  inside the master-plan sub-200 ms local goal for this fixture;
- storage: ≈ 377 bytes per source kernel record materialized
  (payload-JSON + hash + row overhead), 1200 generation record rows for
  1200 kernel records;
- restart: current-generation recovery from a fresh engine ≈ 15 ms;
- one injected `gen-staged` build fault left exactly 1 identifiable
  staged generation, the prior generation stayed current, and rebuild
  digest equality held.

These are single-machine observations with the runtime profile recorded
beside them, not universal claims; rerun
`backend/scripts/kernel_characterization.py` to reproduce.

## Design decision note (simplest-design test)

Kept: write-first head upsert (serialization), conditional head update
(lost-update guard), canonicalization before the transaction (short lock
hold), per-record composite root entries, and a single generic
`kernel_records` table with a typed envelope (class + schema version +
canonical payload) rather than six per-entity tables — the envelope
keeps manifest/identity/replay logic uniform while per-class semantics
live in `app.kernel.records`.

PR64 kept: content-addressed flat `objects/<xx>/<hash>` files with
tmp+rename publication (simplest layout that makes partial writes
structurally un-nameable); registry rows inside the commit transaction
(one authority remains one authority — the filesystem never decides
truth); an in-process store lock plus atomic replace for Windows
replace-vs-open semantics; outbox dedupe by deterministic identity
hash instead of a dispatcher framework.

PR65A kept: snapshots as **resolved immutable views** (a dataclass plus
deterministic `snapshot_id`, not a persisted table — nothing new can
drift from the commit chain because every field is recomputed from it
on demand); relational derived tables for generations in the same
SQLite database (indexed bounded lookups, `PK (generation_id,
record_id)` makes duplicate materialization structurally impossible);
one deterministic `generation_id` over declared inputs so rebuild
idempotence and tamper rejection fall out of identity; staged →
validated → active as three separate transactions with a single
conditional pointer flip; per-record identity re-verification on
bounded reads instead of whole-generation hashing per request.

Considered and rejected: a persisted `kernel_snapshots` table (a second
copy of derivable truth with drift risk — resolution is cheap and
deterministic); one JSON blob column per generation (simplest rebuild,
but record lookup/enumeration becomes O(generation) parsing and
un-indexed); a separate generation database file (two-file durability
and no atomic pointer switch); hashing the whole generation on every
read (replay-scale cost — per-record identity checks plus explicit
`verify_generation` give bounded honesty instead); purging any
generation rows beyond never-activated `staged`/`failed` residue
(retirement belongs to PR65B retention, not the builder).

Deleting any of the kept mechanisms fails an acceptance test:
deterministic generation id → rebuild-equality/idempotence tests;
digest-from-materialized-rows validation → tamper/fault matrix;
conditional pointer update → activation race tests; cut-bounded source
reads → isolation tests; unbound future fields → snapshot honesty tests;
never-activated-only purge → residue tests.

## Extension points for later PRs

- **PR65B (proof-closure/GC/pack):** active generations and
  `kernel_generation_heads` are the durable reader roots; snapshot
  watermarks (`kernel_commit_id` cuts) bound reachability;
  `GenerationService.list_generations(state="staged"/"failed")` names
  reclaimable residue; superseded-but-readable generations are the
  pin model retention must respect. No physical blob deletion exists
  yet, by design.
- **PR66 (fencing/publication):** `KernelCommitReceipt` is the
  commit-identity token later fencing work can chain from;
  `kernel_outbox` rows (dedupe key + attempts + states) are the claim
  surface leases/fencing will extend; generation activation is
  idempotent and safe to drive from an at-least-once dispatcher.
- **PR70+ (source identity):** `KernelSnapshot.UNBOUND_FIELDS` is the
  exact seam where content/access/verifier identities attach — binding
  them changes the snapshot identity domain by construction, so old
  snapshots cannot be silently reinterpreted.
- **PR74+ (verification):** `ClaimAssessmentRecord.declared_context` is
  the versionable seam for policy resolution without rewriting history.
