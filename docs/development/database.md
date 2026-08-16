# Database Schema & Migrations

Marker UI uses **SQLite** as its storage engine, managed through **SQLAlchemy** (asynchronous `aiosqlite`).

**Alembic is the sole persistent schema authority** (V3.2 PR62):

- Application startup **never creates, repairs, or mutates schema**. The runtime
  only *validates* that the database is at the current migration head and
  structurally compatible; if it is not, startup fails closed with an
  actionable diagnostic instead of attempting repairs.
- Every persistent schema change is an Alembic revision under
  `backend/alembic/versions/`.
- All supported launch paths (`start.sh`, `start.ps1`, the container entrypoint
  via `supervisord.conf`) run the migration phase **before** Uvicorn starts.
- Concurrent launchers are serialized by a lock file
  (`<database>.migration.lock`); one migration writer at a time.

## Operational commands

Run from the `backend/` directory:

```bash
# Bring the database to the migration head (the ONLY schema-mutating command)
python -m app.db_migration upgrade

# Inspect migration state (revision, head, problems)
python -m app.db_migration status

# Exit 0 if the database is ready for app runtime, 1 otherwise
python -m app.db_migration check

# Override the target database (defaults to MARKER_DATABASE_URL / app config)
python -m app.db_migration upgrade --url "sqlite+aiosqlite:////path/to/db.sqlite"
```

Raw Alembic remains available for development workflows:

```bash
alembic -c backend/alembic.ini upgrade head   # from the repository root
```

## Database states and how they are handled

| Database state | Behavior |
|---|---|
| No database / empty database | `upgrade` initializes it to the migration head. |
| At head and structurally valid | `upgrade` is a no-op; startup proceeds (zero schema churn). |
| At a known older revision | `upgrade` applies revisions to head; existing rows are preserved. |
| Legacy database without `alembic_version` | Shape is validated (no unknown tables/columns, compatible type affinities), then the guarded revision chain is replayed from the base revision. Rows are preserved. |
| Claims head but physically broken (missing table/column) | **Fails closed.** Nothing is repaired; the diagnostic names the missing object. |
| Unknown revision, foreign schema, or partially-equivalent shape | **Fails closed** with an actionable description of the divergence. |
| Another migration writer holds the lock | `upgrade` waits (default 60 s), then fails with instructions; stale locks from dead processes are recovered automatically. |

## Making a schema change

1. Update the model in `backend/app/models/` or `backend/app/kernel/`
   (and register any new model module in `backend/alembic/env.py`).
2. Generate and hand-check a revision:

   ```bash
   cd backend
   alembic revision --autogenerate -m "Describe your change"
   ```

3. `python -m app.db_migration upgrade` to apply locally, then run the test
   suite. `tests/test_database_migration.py` fails if ORM metadata and the
   migration head drift apart (a model change without a revision fails CI).

Note for SQLite: only a constrained set of `ALTER TABLE` operations exist
natively; use Alembic batch operations (`op.batch_alter_table`) for drops,
renames, or type changes.

## Schema models

- `ConversionJob` → `conversion_jobs` — document conversion jobs (status,
  formats, config, results, durable-queue/lease fields).
- `Setting` → `settings` — key-value configuration.
- `AuditEvent` → `audit_events` — redacted audit trail.
- `JobEvent` → `job_events` — per-job progress/event log.
- Truth Kernel spine (V3.2 PR63A, revision `20260815_0004`; see
  [../reference/truth-kernel.md](../reference/truth-kernel.md)):
  - `KernelCommitHead` → `kernel_commit_heads` — per-workspace commit
    head (the commit serialization point).
  - `KernelCommitManifest` → `kernel_commit_manifests` — one immutable
    manifest per accepted commit.
  - `KernelRecord` → `kernel_records` — append-only committed logical
    record metadata with canonical semantic identity.
  - `KernelRecordEdge` → `kernel_record_edges` — dependency/reference
    edges between records.
- Truth Kernel payload durability + outbox (V3.2 PR64, revision
  `20260815_0005`; see
  [../reference/truth-kernel.md](../reference/truth-kernel.md)):
  - `KernelPayloadObject` → `kernel_payload_objects` — registry of
    durably published content-addressed payload objects (blob key,
    length, store profile, locator). Rows are inserted in the same
    transaction as the records referencing them, so a visible row
    implies the bytes were staged and verified before acceptance.
  - `KernelOutbox` → `kernel_outbox` — durable at-least-once
    successor-work intent, enqueued atomically with its authorizing
    commit and identified by a deterministic dedupe key.
- Truth Kernel materialized generations (V3.2 PR65A, revision
  `20260815_0006`; see
  [../reference/truth-kernel.md](../reference/truth-kernel.md)):
  - `KernelGeneration` → `kernel_generations` — immutable manifest row
    per materialized generation: pinned snapshot identity,
    materializer/schema/config identity, lifecycle state
    (staged/validated/active/superseded/failed), and the deterministic
    content digest. Derived, rebuildable state — never a second truth
    authority.
  - `KernelGenerationRecord` → `kernel_generation_records` — committed
    record metadata materialized into a generation, bounded to the
    generation's pinned cut.
  - `KernelGenerationEdge` → `kernel_generation_edges` — dependency
    edges materialized into a generation, bounded to the pinned cut.
  - `KernelGenerationHead` → `kernel_generation_heads` — per-workspace
    current accepted read generation; the atomic pointer switch happens
    on this row. Downgrade of this revision discards generation state
    (a rebuild restores the derived content, not the activation
    history).
- Truth Kernel retention contract (V3.2 PR65B, revision `20260815_0007`;
  see [../reference/truth-kernel.md](../reference/truth-kernel.md)):
  - `KernelRetentionRoot` → `kernel_retention_roots` — declared durable
    retention holds over a cut and a required payload class; roots are
    what collection must treat as live (the intrinsic current-generation
    roots are read from `kernel_generation_heads` and are not stored
    here).
  - `KernelReaderPin` → `kernel_reader_pins` — bounded wall-clock read
    leases over one generation; an unexpired pin is an active root and a
    crashed reader's pin lapses when its lease expires.
  - `KernelPayloadRetirement` → `kernel_payload_retirements` — durable
    GC tombstones (pending/deleted/failed) so crash recovery converges
    idempotently; the payload registry row is deliberately kept as an
    honest availability fact.
- Truth Kernel fenced work authority (V3.2 PR66, revision
  `20260816_0008`; see
  [../reference/truth-kernel.md](../reference/truth-kernel.md)):
  - `KernelWorkLease` → `kernel_work_leases` — one row per outbox work
    item holding the current fenced ownership: a monotonically
    increasing fencing token (advanced inside every ownership
    transition transaction), the current owner, a wall-clock lease
    expiry (takeover eligibility only, never authority), and the
    leased/released/accepted lifecycle state.
  - `KernelPublication` → `kernel_publications` — the exactly-once
    accepted result for one work identity, uniquely scoped by
    `(workspace_id, work_id)` so the database itself enforces "at most
    one accepted publication"; deterministic publication id and result
    hash make same-result retries converge and different results fail
    as classified conflicts. Downgrade of this revision discards
    fencing and accepted-publication truth irreversibly.
