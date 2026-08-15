# Truth Kernel — Commit Spine + Local Payload Durability (PR63A/PR64)

Status: implemented (V3.2 amendment 4C / PR63 slice A, then amendment
18C/19C PR64). This page is the authoritative description of what the
local Truth Kernel guarantees, what it deliberately does not guarantee
yet, and how PR65/66 are expected to attach. The canonical identity
contract it consumes is documented separately in
[canonical-identity.md](canonical-identity.md); the migration authority
in [../development/database.md](../development/database.md).

## Overview

PR63A established the first production-shaped local Truth Kernel
persistence layer, and PR64 extended it with durable payload staging,
availability truth, and the transactional outbox:

- an Alembic-owned schema — revision `20260815_0004` (commit spine:
  `kernel_commit_heads`, `kernel_commit_manifests`, `kernel_records`,
  `kernel_record_edges`) plus revision `20260815_0005` (PR64:
  `kernel_payload_objects`, `kernel_outbox`);
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
- a read-side replay/verification surface (`app.kernel.replay`).

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

## What PR63A+PR64 do NOT guarantee (deliberate non-goals)

- No `KernelSnapshot` API, materialized generations, atomic generation
  serving, proof-closure traversal, retention/GC, pack-segment
  compaction, or materialized indexes — **PR65**. Orphan objects are
  classified and reused, never garbage-collected.
- No lease/fencing for workers, no exactly-once accepted publication,
  no stable/provisional publication namespaces, no external effect
  ledger — **PR66**. Outbox delivery is at-least-once by declaration.
- No scheduler/job-state changes, no source/content/access identity,
  no patch semantics, no verification policy resolution, no UI.
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

Considered and rejected: an in-process `asyncio.Lock` as the primary
*commit* serialization (proves nothing across processes or against
direct DB writers — the SQLite writer lock remains the authority); a
Merkle tree over records (no measurable need — sorted-list sha256 roots
give the same tamper detection with less machinery); six normalized
tables (more joins, no additional enforceable invariant for this
slice); a pending/outbox dispatcher (PR66); per-workspace blob
namespacing (bytes carry no evidence identity, so content-only keys
maximize dedup without merging evidence).

Deleting any of the kept mechanisms fails an acceptance test:
write-first upsert + conditional update → fork test; canonical
pre-validation → boundary-rejection tests; composite roots → tamper
tests; envelope identity → duplicate/dedup tests; staged-before-
registered ordering → the fault matrix; registry-in-transaction →
rollback tests; outbox dedupe → duplicate-intent tests.

## Extension points for later PRs

- **PR65 (snapshots/materialization):** `replay(to_commit=N)` already
  defines the committed-cut semantics a snapshot builder consumes;
  `LocalPayloadStore` and the availability classes are the storage
  substrate for materialized generations; orphan classification is the
  mark phase input for GC.
- **PR66 (fencing/publication):** `KernelCommitReceipt` is the
  commit-identity token later fencing work can chain from;
  `kernel_outbox` rows (dedupe key + attempts + states) are the claim
  surface leases/fencing will extend.
- **PR74+ (verification):** `ClaimAssessmentRecord.declared_context` is
  the versionable seam for policy resolution without rewriting history.
