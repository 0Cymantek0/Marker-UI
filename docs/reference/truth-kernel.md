# Truth Kernel — Commit Spine + Payload Durability + Snapshots & Generations + Retention/GC + Fenced Publication (PR63A/PR64/PR65A/PR65B/PR66)

Status: implemented (V3.2 amendment 4C / PR63 slice A, then amendment
18C/19C PR64, then PR65 slice A, then PR65 slice B — retention roots,
reader pins, and safe local garbage collection — then PR66 — fenced
work ownership and exactly-once accepted publication). This page is the
authoritative description of what the local Truth Kernel guarantees,
what it deliberately does not guarantee yet, and how PR67 and later
subsystems are expected to attach. The canonical identity contract it
consumes is documented separately in
[canonical-identity.md](canonical-identity.md); the migration authority
in [../development/database.md](../development/database.md).

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
  `kernel_generation_heads`), revision `20260815_0007` (PR65B:
`kernel_retention_roots`, `kernel_reader_pins`,
`kernel_payload_retirements`), and revision `20260816_0008` (PR66:
`kernel_work_leases`, `kernel_publications`);
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
  reports `missing`; an object that exists but cannot be read
  (permission loss, sharing violation, filesystem error) is reported
  as present-but-unverifiable — the `corrupt` bucket — because an
  availability claim requires a verified read; none of these may be
  presented as complete or available.
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
- A deliberately minimal dispatch seam exists since PR66:
  `fencing.claim_next` claims the oldest pending item and binds it to a
  durable fenced lease. Since PR67A the fair policy lives one layer up
  in `scheduler.claim_fair` (with challenge liveness and semantic
  events); `claim_next` stays as the measured baseline/fallback. See
  *Fenced work ownership and accepted publication* and the PR67A
  section below.

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

### Deliberate limits at the PR65B boundary

- The builder and the collector are plain in-process services; work
  leases/fencing and accepted publication are now PR66 primitives
  layered *on top of* this seam, not part of it.
- No pack-segment compaction or materialized FTS/vector indexes — the
  local store is still one content-addressed file per object; PR65B
  measures object-count implications and leaves the pack seam open.

## Retention roots, reader pins, and garbage collection (PR65B)

PR65B adds the retention contract (Alembic revision `20260815_0007`):
`kernel_retention_roots` (declared holds), `kernel_reader_pins`
(bounded read leases), `kernel_payload_retirements` (GC tombstones).
The database decides what must survive; the filesystem never does.

### What is live

A **retention root** is `(workspace, commit cut, required payload
class)`. Three families, all read through one collector query:

1. **Intrinsic roots** — each workspace's current generation with the
   payload class that generation declared (read from
   `kernel_generation_heads`, not stored as rows);
2. **Declared holds** — `declare_hold(...)` rows. `snapshot_hold`
   protects a cut directly; `generation_hold` protects one materialized
   generation. Future producers (jobs, reviews, exports, legal holds,
   PublicationSets) attach by inserting rows with their own `root_kind`
   and producer context — the collector never knows them;
3. **Reader pins** — unexpired `kernel_reader_pins` leases over one
   generation (`open_pinned_generation` /
   `open_current_generation(pin_lease_seconds=...)`; `reader.renew()`
   extends, `reader.close()` releases, a crashed reader's pin lapses
   when the lease expires — restart safety never depends on process
   memory).

The **live payload closure** is the union over all roots of the
payload hashes of records `<= cut` in that root's workspace, for roots
whose class is not `metadata_only`. Reachability is always computed
store-wide (all workspaces union): blob keys are deduplicated across
workspaces, so a workspace-scoped view can never justify deleting a
shared object. (The workspace-scoped `orphan_objects` tuple in
reconciliation remains a known reporting quirk and is never a deletion
candidate list.)

Commits newer than the newest protected cut are **not** intrinsically
protected: the operating pattern is *materialize (or hold), then
collect*. If collection runs while committed truth has advanced past
every root, the uncovered bytes are collectible and later inspectable
snapshots over them resolve degraded — honest, never silent.

### What is collected, and how

Only two things are ever removed:

- **derived generation rows** — superseded/failed generations (and
  staged/validated residue older than `stale_staging_seconds`, default
  1 h) that no current pointer, active hold, or unexpired pin protects.
  Current generations are structurally never candidates. Records, edges,
  manifests, and registry rows are permanent metadata and are never
  deleted;
- **physical payload bytes** — content-addressed objects whose hashes
  are outside every live closure, plus pre-commit orphan objects the
  registry never accepted.

Lifecycle (`app.kernel.gc`): `plan_collection` (mark) builds a
read-only plan — evidence, never authorization. `execute_collection`
opens **one** write transaction, write-first (expired-pin purge) takes
the SQLite writer lock, recomputes the live closure from freshly read
roots, and inserts `pending` tombstones for what is still unreachable.
**That transaction's commit is the deletion linearization point.**
Every root, pin, or hold committed before it is honored; every one
committed after it is a post-decision root that sees honest `retired` /
degraded availability and heals by re-staging the exact bytes through
the normal publish path. The sweep then unlinks one object per short
write transaction, write-first claiming the tombstone row so a
concurrent commit's rescue (below) cannot interleave with the unlink.

The commit protocol participates in the rescue: inside every commit
transaction (which already holds the writer lock), any staged payload
hash found tombstoned is either physically present — the tombstone is
deleted; this commit is re-referencing the bytes — or absent, in which
case the commit aborts, re-stages, and retries. A commit can therefore
never land referencing retired bytes, regardless of interleaving with a
sweep.

### Crash and restart semantics

`reconcile_retirements` resumes unfinished tombstones from durable
state alone: `pending` + file present → unlink; `pending` + file
absent → record `deleted` (idempotent convergence); `failed` → retry.
Unlink failures (`OSError`) are recorded as retryable `failed` rows
with `last_error` — never a false success, never claimed reclaimed
bytes. Registry rows are deliberately kept after retirement so
historical identity, length, and locator stay interpretable; the
availability classification gains a distinct `retired` state
(reconciliation + snapshot payload histograms) that never overstates:
a historical inspectable cut over retired bytes resolves degraded with
`retired` counts; metadata-only views stay complete; re-supplied bytes
that verify are `available` again regardless of tombstone history.

### Deliberate PR65B boundaries

- The linearization point is the tombstone transaction, not the sweep:
  a hold declared *after* authorization does not retroactively rescue;
  it gets honest degradation plus the re-staging heal path.
- `metadata_only` roots protect metadata only (metadata is never
  collected anyway), not bytes.
- Deleting a tombstoned object does not remove its registry row;
  nothing about retirement is ever reported as `available`.
- Multi-process collection against one store root shares the
  single-process topology caveat of the payload store itself.


## Fenced work ownership and accepted publication (PR66)

PR66 adds the durable authority boundary the at-least-once outbox
always needed (Alembic revision `20260816_0008`: `kernel_work_leases`,
`kernel_publications`; module `app.kernel.fencing`). Duplicate
execution, redelivery, crash, and failover remain expected; what is new
is that the database can always answer *which ownership generation may
turn an executed result into accepted state*.

### What makes an owner current

- One lease row per outbox work item holds the current authority:
  a monotonically increasing `fencing_token`, the `owner_id`, a
  wall-clock `lease_expires_at`, and a lifecycle `state`
  (`leased` / `released` / `accepted`).
- Every ownership transition advances the token inside one conditional
  transaction: first acquire creates it at 1; takeover after lapse or
  vacate advances it; `release` vacates *and* advances, so a releasing
  owner is immediately stale. Re-acquisition by the still-current
  owner renews the lease without moving the token (safe duplicate
  delivery).
- **Wall-clock expiry is eligibility, never authority.** An expired but
  unsuperseded token may still accept; a superseded token is stale
  forever, even after restart, even if its worker is still running.
- Restart reconstructs nothing in memory: `get_lease` /
  `get_publication` read the durable rows.

### The acceptance linearization point

`fencing.accept(work_id, fencing_token, result)` runs one transaction
that, in order:

1. verifies the submitted token is the current lease authority
   (`leased`, or `accepted` when retrying) — otherwise
   `StaleFenceError`, and an unauthorized submission is never even
   compared against accepted state;
2. checks the `(workspace_id, work_id)` publication scope — an
   existing row with the same `result_hash` returns
   `already_accepted` (idempotent retry), a different one raises
   `PublicationConflictError` with accepted state unchanged;
3. inserts the immutable `kernel_publications` row (deterministic
   `publication_id` + `result_hash` under the
   `marker.kernel.work_result.v1` / `marker.kernel.publication.v1`
   framings) and flips the lease to `accepted` under the same
   conditional token check.

The commit of that transaction **is** the linearization point: crash
before it means no accepted publication and recoverable work; crash
after it means exactly one accepted publication the retry converges to.
The unique scope constraint is the database-enforced backstop — two
transactions can never both believe they won.

### Fenced acknowledgement and dispatch

- `fencing.complete_work` moves the outbox row to `done` only inside a
  transaction that also observes the accepted publication and the
  still-current accepting fence: acknowledgement can never become
  durable before the accepted result it represents, and a stale worker
  cannot acknowledge.
- `fencing.claim_next(owner_id)` is the deliberately minimal dispatch
  seam: oldest pending item, one outbox claim, one fenced acquire, no
  fairness/policy/heartbeat (the PR67A fair seam `scheduler.claim_fair`
  now owns policy; this oldest-first seam stays as baseline and
  emergency fallback). A claimed-but-unfenceable item is returned to
  pending so nothing gets stuck.
- `outbox.ack` remains the lower-level PR64 primitive; fenced
  dispatch must use `complete_work`.

### Workspace isolation and external honesty

- Leases and publications are scoped by the workspace derived from the
  outbox row itself; identical intents in different workspaces never
  share fencing or acceptance state.
- Exactly-once here is a **local database** claim only. Any webhook,
  notification, or remote upload driven from an accepted publication is
  at-least-once (or reconciliation-required) unless the destination
  supplies a real idempotency primitive; PR66 adds no external effect
  ledger and claims none.

### Deliberate PR66 boundaries

- No scheduler fairness, quotas, admission control, or challenge
  heartbeat inside the PR66 revision itself — the PR67A layer above
  (`app.kernel.scheduler` / `app.kernel.liveness` / `app.kernel.events`,
  revision `20260816_0009`) supplies them without moving any PR66
  authority seam.
- The accepted publication is a single-work primitive; the atomic
  multi-generation `PublicationSet` protocol is **PR76**, which can
  build on this fence-proof primitive without being constrained by it.
- Legacy `TaskManager`/GUI/API execution paths are untouched; wiring
  production dispatch to the fence is later runtime integration work.

## Fair scheduling, challenge liveness, and durable semantic events (PR67A)

PR67A adds the runtime-truth slice immediately above the fence (Alembic
revision `20260816_0009`: `kernel_scheduling_entries`,
`kernel_scheduling_groups`, `kernel_liveness`, `kernel_events`,
`kernel_progress`; modules `app.kernel.scheduler`, `app.kernel.liveness`,
`app.kernel.events`). It changes **policy and evidence**, never
authority: every claim still goes through the PR66 outbox claim + fenced
acquire, acceptance stays exactly-once in `kernel_publications`, and
acknowledgement still happens only behind accepted truth.

### Fair bounded dispatch (`scheduler.claim_fair`)

- Work is partitioned into **resource classes** (capacity separation)
  and, inside a class, **scheduling groups** — by default the workspace
  id, registerable finer (`register_work`, e.g. workspace:document).
- Priority is **weighted fair queuing by virtual finish**
  `served_count / weight` in exact rational arithmetic. Equal weights
  interleave strictly (measured prefix service gap ≤ 2 at every point
  of a 3-group mixed drain); a 2:1 weight ratio makes the groups finish
  together on 2:1 inventories instead of the light group draining early.
  `served_count` is deliberately **non-authoritative bookkeeping** —
  losing or reseeding it changes interleaving, never truth.
- **Age boost** (item older than `age_boost_after_seconds` divides
  virtual finish by `age_boost_factor`) and **deadline pressure**
  (×2 near, ×4 past) keep old eligible work from perpetual displacement.
- **Bounded fan-out**: a group at `max_in_flight` live leases is
  skipped — a parent flow's huge child backlog cannot monopolize the
  class, and backpressure reduces further fan-out rather than queueing
  unbounded work. A coordinator waiting on children holds no lease and
  consumes no slot. The cap is a **hard, database-observable
  invariant**: the winning claim's capacity check, delivery claim,
  fence acquire, served-count bump, challenge seed, and
  `work.claimed` event commit in one write-serialized transaction
  (`BEGIN IMMEDIATE` takes SQLite's single-writer lock before the
  live-lease count is read), so concurrent dispatchers can never
  oversubscribe a group — not even transiently. Losing the capacity
  check, delivery race, or fence race rolls the whole transaction
  back; no partial claim state ever commits.
- **Bounded look-ahead**: each pass scores the K oldest pending items
  *per group* (window-partitioned, never a global id-ordered scan that
  would keep late-arriving groups invisible). Items fenced by a still
  *valid* lease are unavailable and do not shadow their group's
  claimable work; expired leases free both the window slot and
  candidacy (takeover remains the PR66 eligibility path).
- `scheduler.accept_work` wraps `fencing.accept` and then records the
  `work.accepted` semantic event (idempotent for converged retries);
  `scheduler.reconcile_dispatch` deterministically returns orphan
  in-flight deliveries (claimed, never fenced) to pending and re-derives
  missing `work.claimed` / `work.accepted` events from the lease and
  publication authorities — repair, never invention.

Measured (characterization `scheduler_liveness_events` section): the
late interactive item is served at dispatch positions 6–8 of 39 while
three 12-item backlogs are still active, versus 6/12/**24** (dead last)
under the oldest-first `claim_next` baseline; per-item dispatch cost
77.9 ms p50 vs 55.0 ms baseline (~23 ms fairness tax at this scale).

### Challenge-backed liveness (`liveness.renew_lease`)

Renewal is **evidence-bearing, not timer-bearing**. One transaction
requires, in authority order:

1. the current fence (`owner_id` + `fencing_token` + `leased`) — a
   superseded worker fails here forever;
2. the **current challenge nonce** — issued to the claimer inside the
   claim bookkeeping transaction and rotated on every successful
   renewal, handed only to the responder; the nonce never appears in
   any read view (`get_liveness` excludes it), so a component that
   merely reads the database cannot forge renewal evidence;
3. **strictly advancing progress** over the durable high-water mark —
   a frozen or replayed counter is not a responsive control loop;
4. a coherent **active request/stage identity**: while a request is
   bound and unexpired, renewal must serve that same id; after it
   lapses, only a *new* id renews (an honest stage transition);
5. the **topology generation** the fence was issued under, when one was
   declared at claim time.

Durably observed cancellation (`report_cancellation`, fence-gated,
idempotent) defeats any later evidence. A wedged worker simply stops
renewing; its lease lapses and PR66 takeover eligibility applies — the
in-flight window ignores expired leases so a wedged group cannot jam
its own recovery. Renewal never advances the fencing token. Measured:
renew p50 12.6 ms; forced-wedge takeover ≈ lease + poll granularity
(0.47 s at a 0.4 s lease); stale-nonce rejection 5.3 ms.

### Durable semantic events and lossy progress (`app.kernel.events`)

- `kernel_events` is append-only with an authoritative
  per-(workspace, stream) `semantic_sequence` allocated inside the
  append transaction (writer-serialized MAX+1): the sequence cannot
  fork or regress under concurrent producers, and replay never depends
  on timestamps. Claim/accept/cancel transitions append events in the
  same transactions that record the bookkeeping; renewal events are
  opt-in (`emit_event`) to control write amplification.
- `kernel_progress` coalesces: exactly one row per (workspace, work),
  updated in place. A 500-tick flood measured **1** durable row and
  never forced a per-tick event; durable events are never dropped as a
  consequence of progress backpressure.
- Reading is pull-based: `replay(after_sequence)` answers from the
  database; `follow()` is a polling cursor adapter that opens a fresh
  short session per batch — a slow consumer slows only itself.
  Disconnecting ends the iteration without touching work; reconnecting
  resumes from the last delivered sequence. Measured: append p50
  10.3 ms, replay of 200 events 5.8 ms p50, slow-consumer completion
  delta ≈ 0.10 s over a 6-item batch, restart replay identical.

### Deliberate PR67A boundaries

- Scheduling groups are two-level (resource class → group); there is no
  general workflow DAG scheduler — deeper hierarchy attaches when a
  real runtime consumer needs it, via the same group/window contract.
- `claim_next` stays as the measured oldest-first baseline and
  emergency fallback; it creates no competing authority.
- The semantic log is local-database truth; transport adapters (SSE
  `Last-Event-ID` mapping, signed cursors, authorization epochs) are
  **PR79**. `follow()` is the transport-independent surface only.
- Production `TaskManager`/GUI/API/SSE paths remain unwired; attaching
  live dispatch to `claim_fair` + `renew_lease` + `follow` is the
  PR67B/runtime integration seam.
- Pre-`20260816_0009` history has no semantic events: the upgrade
  creates the tables empty, and nothing fabricates events for work that
  ran before them.

## What this kernel does NOT guarantee (deliberate non-goals)

- No pack-segment compaction or size-bounded pack storage — PR65B
  measures object-count/inode implications of the one-file-per-object
  store and leaves the pack seam open; compaction is a later experiment.
- No materialized FTS/vector/visual indexes — later work.
- Fenced work ownership and exactly-once accepted publication exist
  (PR66) as *local database* guarantees; no external effect ledger, no
  stable/provisional publication namespaces (**PR67+/PR76**), and
  outbox delivery itself is still at-least-once by declaration.
- No scheduler/job-state changes, no source/content/access identity
  (snapshot future bindings stay explicitly unbound), no patch
  semantics, no verification policy resolution, no server-side query
  engine, no UI.
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

PR65B delta (same box; default fixture 100×12 plus the retention block:
inspectable hold at the half-way cut, current generation rebuilt
metadata-only, 10 fresh unreachable payload commits + 1 staged orphan):

- plan (mark) over 211 registry objects / 206 physical objects: ≈ 58 ms
  small fixture, ≈ 124 ms at 400 commits × 12 records; live-closure
  queries are per-root cut-bounded `DISTINCT` scans;
- recheck+tombstone transaction: ≈ 75 ms small, ≈ 300 ms at scale
  (211 objects considered, 111 unreachable + 6 orphans tombstoned);
- sweep: one unlink per short write transaction ≈ 19 ms/object
  (Windows fsync-dir caveat + per-object writer-lock claim), 117
  objects in ≈ 2.3 s; `already_absent` convergence and `failed` retry
  paths measured in the fault matrix, not this run;
- reclaimed 61,359 bytes of 113,859 registered+orphan bytes; the hold's
  100 objects (52,000 bytes) survived; post-pass availability showed
  `available: 300 / retired: 311` and `verify_history` stayed green;
- memory: tracemalloc peak ≈ 475 KiB (small) / ≈ 610 KiB (400-commit
  fixture) — candidate/live sets are in-memory hash sets scaling with
  objects, not records;
- restart reconciliation (fresh engine, nothing pending): ≈ 5–7 ms.

PR66 delta (same box; fencing block: 1 residue item drained, 24
uncontended claim->accept->complete samples, 32 stress items across 4
contending workers, 1 stale-rejection probe, restart resolution on a
fresh engine):

- uncontended latency p50: `claim_next` incl. fenced acquire ≈ 29 ms,
  accepted publication ≈ 14 ms, fenced acknowledgement ≈ 13 ms
  (p95 ≤ 34 ms) — three short write transactions, in the same band as
  one payload-less commit;
- concurrent dispatch stress: end-to-end per item p50 ≈ 129 ms /
  p95 ≈ 398 ms wall across 4 workers (32 items in ≈ 2.1 s) — SQLite
  writer serialization dominates; every item still settled exactly one
  lease + one publication;
- stale-fence rejection after takeover ≈ 4 ms (a read + conditional
  check, no write); restart authority/publication resolution ≈ 10 ms;
- durable cost per work item: one `kernel_work_leases` row and, on
  acceptance, one `kernel_publications` row — no per-attempt rows.

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

PR65B kept: retention expressed as bare `(workspace, cut, payload
class)` root triples instead of a typed root-object hierarchy (future
producers attach by inserting rows; the collector stays generic);
durable GC tombstones separate from the payload registry (the registry
row remains honest history, the tombstone is the lifecycle); one
recheck+tombstone transaction with a write-first statement as the
single deletion linearization point (mirrors the commit protocol's
write-first head upsert); per-object sweep transactions that write-first
claim the tombstone so the unlink and a commit-side rescue cannot
interleave; the commit-side tombstone rescue as the only fix needed for
the in-flight-commit race (no grace-period guessing); expired-hold and
pin semantics as wall-clock facts on stored rows rather than lifecycle
state machines.

PR65B considered and rejected: reference counting as a deletion
criterion (the master plan forbids it; reachability is recomputed at
decision time); recomputing liveness again inside every sweep
transaction (the tombstone transaction already linearizes the decision;
a second pass would move, not remove, the boundary); a persisted
snapshot-plan table (plans are in-memory evidence; durable intent
begins at tombstones); persistent reader leases with heartbeats beyond
a plain expires_at column (a lapsed lease plus re-staging heal covers
the crash case with less machinery); deleting registry rows on
retirement (would fabricate "never referenced" history).

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

- **PR68A (local artifact data plane):** the ArtifactHandle seam that moves
  large process-worker result fields by verified file reference lives
  deliberately OUTSIDE the kernel (`app/services/artifact_handles.py`) — it
  is an ephemeral transport mechanism, not durable truth, and never feeds
  commit/payload references. See `docs/reference/artifact-data-plane.md`.
- **Future retention producers (PR67+):** `declare_hold(...)` with a
  new `root_kind` and producer context is the entire attachment
  surface — jobs, reviews, cursors, exports, legal holds, claim proof
  closures, and PublicationSets register retention without the
  collector changing. `kernel_payload_retirements` rows are the point
  later effect ledgers can observe reclamation from.
- **PR67B (runtime wiring — IMPLEMENTED):** the live conversion path now
  runs through the kernel authority. `app/services/kernel_runtime.py`
  (`KernelRuntimeCoordinator`) authorizes each submission as one kernel
  commit (`NativeObjectRecord` request object + `conversion.execute`
  outbox intent in the same transaction), dispatches exclusively through
  `scheduler.claim_fair`, renews leases only from real control-loop
  evidence (activity counter fed by tqdm progress, worker logs/status;
  no detached heartbeat), and completes work only through
  `scheduler.accept_work` → `fencing.complete_work` before the
  `ConversionJob` compatibility row may read `completed`. Failure,
  cancellation, retry, and lease-lapse are durably recorded as semantic
  events (`work.failed`, `work.cancelled`, `work.retry`) behind the
  current fence; a watchdog requeues lapsed leases (one lapse retry
  always allowed, then the row's retry budget) and repairs lost acks;
  startup `recover()` converges every crash boundary (adoption of
  pre-kernel rows, publication projection, terminal-event projection,
  non-durable sweep). Executors are generation-bound (`submit_job(...,
  claim=...)`) so a stale generation can neither renew, publish, nor
  finalize under its successor's fence. `MARKER_KERNEL_RUNTIME=0`
  restores the legacy direct-submission runtime; the legacy SQLite
  durable queue is dispatch-disabled in kernel mode (compatibility
  metadata only). PR68A ArtifactHandle transport is unchanged and is
  always resolved before acceptance — the accepted publication is a
  bounded descriptor over the resolved durable output, never an
  ephemeral handle pathname. See
  `docs/reference/kernel-runtime-integration.md` for the evidence
  bundle. Remaining for later slices: PR79 signed-cursor event surface
  (SSE remains a compatibility projection over in-memory state plus the
  durable row) and worker-side generation identity for the process
  backend.
- **PR76 (PublicationSet):** `kernel_publications` proves the
  one-fenced-accepted-outcome primitive; the multi-generation atomic
  bundle protocol wraps a seam like it rather than mutating it.
- **Pack compaction (later experiment):** the store remains
  one-file-per-object; `LocalPayloadStore.delete_object` and the
  tombstone sweep are the seam a pack builder would sit behind, with
  old-segment retirement reusing the same mark/recheck/tombstone
  discipline.
- **PR70+ (source identity):** `KernelSnapshot.UNBOUND_FIELDS` is the
  exact seam where content/access/verifier identities attach — binding
  them changes the snapshot identity domain by construction, so old
  snapshots cannot be silently reinterpreted.
- **PR74+ (verification):** `ClaimAssessmentRecord.declared_context` is
  the versionable seam for policy resolution without rewriting history.
