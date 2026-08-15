# Truth Kernel — Commit Spine (PR63A)

Status: implemented (V3.2 amendment 4C / PR63 slice A). This page is the
authoritative description of what the local Truth Kernel commit spine
guarantees, what it deliberately does not guarantee yet, and how PR64/65/66
are expected to attach. The canonical identity contract it consumes is
documented separately in [canonical-identity.md](canonical-identity.md);
the migration authority in [../development/database.md](../development/database.md).

## Overview

PR63A establishes the first production-shaped local Truth Kernel
persistence layer:

- an Alembic-owned schema (revision `20260815_0004`, previous head
  `20260709_0003`) with four tables — `kernel_commit_heads`,
  `kernel_commit_manifests`, `kernel_records`, `kernel_record_edges`;
- one internal commit authority, `app.kernel.commit.KernelCommitService`,
  through which every kernel mutation enters SQLite;
- per-workspace causal commit chains ordered by `kernel_commit_id`
  (wall-clock timestamps are audit metadata only and never order truth);
- atomic multi-record batches: head, records, edges, and one immutable
  manifest become durable in a single transaction;
- PR61 canonical identity as the record identity boundary;
- a read-side replay/verification surface (`app.kernel.replay`).

Existing GUI/REST/CLI/MCP conversion behavior is untouched: nothing in
production calls the kernel yet. Legacy job/settings/audit tables remain
migration substrate until their later integration phases.

## Commit protocol and the linearization point

One `KernelCommitService.commit(batch)` call performs the SQLite portion
of the V3.2 commit protocol:

1. pre-transaction canonicalization/validation of every record, edge, and
   producer metadata (rejected canonical values fail here, before any
   lock is taken);
2. begin a transaction and immediately upsert the workspace
   `kernel_commit_heads` row (insert-or-ignore) — the first write takes
   the SQLite writer lock, so concurrent committers serialize at the
   database instead of racing through a read snapshot;
3. read the committed head, derive `kernel_commit_id = head + 1` and
   `parent_kernel_commit_id = head`;
4. resolve external record references against visible committed state
   (unknown or cross-workspace references are rejected);
5. insert records and edges;
6. insert the immutable manifest (counts, per-class counts, deterministic
   record/edge roots, manifest identity hash);
7. advance the head with a conditional update
   (`WHERE head_kernel_commit_id = <observed>`) — a lost update cannot
   be silently accepted;
8. COMMIT. **The database commit while holding the head row is the
   linearization point.** Before it: nothing of the batch is visible.
   After it: the whole manifest-declared batch is visible together.

`SQLITE_BUSY`/lock errors and concurrent head movement are treated as
expected retryable conditions with a bounded retry budget (8 attempts,
exponential backoff capped at 500 ms). A free-running database sequence
is never used for order.

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

## What PR63A does NOT guarantee (deliberate non-goals)

- No durable payload staging/availability repair/outbox — PR64 owns the
  blob side. Payload bytes supplied to a commit are hashed, not stored.
- No `KernelSnapshot` API, materialized generations, or GC — PR65.
- No fencing tokens or exactly-once publication — PR66.
- No scheduler/job-state changes, no source/content/access identity,
  no patch semantics, no verification policy resolution, no UI.
- Fault-injection evidence covers **database-transaction** behavior
  only; it does not claim filesystem/object-store crash safety.
- FK constraints are declared RESTRICT for documentation, but the app
  engine does not enable SQLite FK enforcement — referential visibility
  is enforced inside the commit transaction by the service, and the
  verifier double-checks it on read.

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
  service retry budget is the backstop beyond the driver timeout.

## Measurement baseline (synthetic, metadata + 512-byte payload hashes)

`backend/scripts/kernel_characterization.py` (rerun for updated numbers):

| shape | commits×records | db bytes | WAL | p50 / p95 commit | replay | verify | retries |
|---|---|---|---|---|---|---|---|
| 4 writers | 100×12 (1200 records, 100 edges) | 1,056,768 | 0 | 15.6 / 294.7 ms | 0.089 s | 0.280 s | 0 busy / 0 head |
| 8 writers | 200×8 (1600 records, 200 edges) | 1,404,928 | 0 | 14.7 / 680.3 ms | 0.131 s | 0.383 s | 0 busy / 0 head |

Verification passed (`verification_ok: true`) on both workloads. p95
growth under 8 writers is queueing at the single writer, as designed.
This is the baseline PR64/65 measurements compare against; no threshold
is imposed by the plan.

## Design decision note (simplest-design test)

Kept: write-first head upsert (serialization), conditional head update
(lost-update guard), canonicalization before the transaction (short lock
hold), per-record composite root entries, and a single generic
`kernel_records` table with a typed envelope (class + schema version +
canonical payload) rather than six per-entity tables — the envelope
keeps manifest/identity/replay logic uniform while per-class semantics
live in `app.kernel.records`.

Considered and rejected: an in-process `asyncio.Lock` as the primary
serialization (proves nothing across processes or against direct DB
writers); a Merkle tree over records (no measurable need — sorted-list
sha256 roots give the same tamper detection with less machinery);
six normalized tables (more joins, no additional enforceable invariant
for this slice); storing raw payload bytes (PR64).

Deleting any of the kept mechanisms fails an acceptance test:
write-first upsert + conditional update → fork test; canonical
pre-validation → boundary-rejection tests; composite roots → tamper
tests; envelope identity → duplicate/dedup tests.

## Extension points for later PRs

- **PR64 (payload staging):** records already carry
  `payload_byte_hash`/`payload_length`; staged-blob manifests attach
  alongside the commit transaction without schema change to the spine.
- **PR65 (snapshots/materialization):** `replay(to_commit=N)` already
  defines the committed-cut semantics a snapshot builder consumes.
- **PR66 (fencing/publication):** `KernelCommitReceipt` is the
  commit-identity token later fencing work can chain from.
- **PR74+ (verification):** `ClaimAssessmentRecord.declared_context` is
  the versionable seam for policy resolution without rewriting history.
