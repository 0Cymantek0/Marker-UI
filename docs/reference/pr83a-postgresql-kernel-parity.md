# PR83A — PostgreSQL Kernel Parity & Persistence-Portability Foundation

**Session date:** 2026-08-20/21 · **Branch:** `markerui-v2`
**Start head:** `508bc2b` (PR82 release evidence) · **Machine-readable evidence:** `docs/reference/measurements/pr83a-kernel-parity.json`

## What this session claims

Two completed, validated workstreams:

1. **Real PostgreSQL kernel parity.** The authoritative Truth Kernel commit path — the one code path that creates commits — now executes with identical semantics on PostgreSQL and SQLite. The application engine bootstrap, the Alembic migration authority, and the readiness gate all run against a real server, and a dual-backend conformance harness proves the semantic matrix (ordering, idempotency, conflict, rollback, concurrency, orphan safety) on both backends through the *same* `KernelCommitService`.
2. **A backend-neutral payload-store boundary.** The kernel commit path depends on a three-operation `KernelPayloadStore` protocol (`stage`, `check_object`, `object_exists`) instead of the concrete local-filesystem store. `LocalPayloadStore` is the unchanged reference implementation; a reusable behavioral suite covers the contract so the PR83B object-store implementation inherits it.

**Not claimed (explicitly):** the optional third workstream — no S3/object-store code was added; no generation publication, backup/restore, failover, or RPO/RTO work; and kernel subsystems beyond the commit path (events, fencing, GC, generations, liveness, publications, retention, scheduler) still use the SQLite-native upsert import and remain exercised on SQLite only.

## Environment exercised

Real PostgreSQL 16.14 (`postgres:16-alpine`, Docker 29.1.3, `127.0.0.1:55432`), Python 3.11.9, SQLAlchemy 2.0.36, Alembic 1.14.0, asyncpg 0.30.0, pytest 8.3.4.

## How to reproduce

| Question | Answer |
|---|---|
| Start/provide PostgreSQL | `python backend/scripts/run_kernel_pg_conformance.py` (reuses or Docker-starts a server; or `--external-url postgresql+asyncpg://user:pass@host:5432/postgres`) |
| Switch the app to PostgreSQL | `MARKER_DATABASE_URL=postgresql+asyncpg://...` (SQLite stays the default local profile) |
| Initialize/migrate the database | `cd backend && python -m app.db_migration --url <pg-url> upgrade` — same entrypoint, both backends |
| Run the dual-backend conformance tests | the runner above, or `MARKER_TEST_POSTGRES_ADMIN_URL=<server-url> python -m pytest tests/test_kernel_dual_backend_conformance.py` |
| Know PostgreSQL was not skipped | strict mode: `MARKER_TEST_POSTGRES_STRICT=1` turns missing provisioning into FAILURES; the fixture also asserts `engine.dialect.name == "postgresql"` and captures the server banner; the runner additionally scans for any skip in the summary |
| Wider regression | `cd backend && python -m pytest tests conformance -q` |
| No-fork concurrency proof | `test_concurrent_same_head_writers_never_fork` — 12 concurrent same-head commits, contiguous ids 1..12, parent links `i-1`, one head, no duplicate manifest identities |

## Results

- **Dual-backend conformance:** 26/26 passed, 0 skipped — 13 scenarios × {SQLite, real PostgreSQL 16.14}. Observed busy/head retries under the 12-writer contention: 0 (the head-row lock serializes without serialization failures).
- **Payload-store conformance (new, reusable):** 9/9 passed for the local implementation.
- **SQLite migration authority gates:** 61/61 (`test_database_migration`, `test_kernel_migration`, `test_context_runtime_cursor_migration`) — the portability changes did not move local schema semantics.
- **Kernel commit/concurrency/fault/payload-commit tests:** 51/51.
- **Lockfile drift gate:** in sync after adding `asyncpg==0.30.0` (CPU + GPU locks regenerated with `scripts/lock_dependencies.py`, `--check` green).
- **Full backend regression:** see the run recorded below.

## Design decisions a reviewer should know

- **Serialization.** Step 1 of the commit protocol is now a write-first head upsert plus a locked re-read: SQLite takes its database writer lock on the upsert; PostgreSQL takes `SELECT ... FOR UPDATE` on the `kernel_commit_heads` row and holds it to commit. One lock, acquired first, in the same order for every committer — no committer deadlock, and all check-then-act phases (tombstone rescue, view advancement, proof checks) keep a no-TOCTOU guarantee on both backends.
- **Contention vocabulary.** `app/kernel/dialects.py` unifies SQLite lock/busy text and PostgreSQL SQLSTATEs 40001/40P01/55P03 into one retryable-contention answer feeding the existing bounded retry budget. Integrity errors map through the violated constraint name when the driver provides one (asyncpg does), with the SQLite text match retained.
- **Timestamps.** Every `DateTime` column is now timezone-aware: PostgreSQL gets `TIMESTAMPTZ` (asyncpg rejects aware values against naive columns, and the kernel stamps aware UTC); SQLite rendering and contracts are byte-identical (`DATETIME`).
- **Migration authority.** `app/db_migration_postgres.py` implements the same contract/states with `pg_catalog`/`information_schema` introspection mapped into the shared affinity vocabulary, `pg_advisory_lock` writer serialization, and an at-head ORM-contract verification. The migrations-vs-ORM reference diff stays on the SQLite profile; both profiles consume the same revision chain and metadata. Pre-Alembic legacy adoption is SQLite-only by design — a PostgreSQL database must be born from Alembic.
- **Async introspection.** PostgreSQL introspection needs a loop; sync `inspect_database(pg_url)` must be called from the CLI/worker thread (documented failure otherwise), with `inspect_database_async` for async callers.
- **Payload boundary.** Minimal three-operation protocol — exactly what the commit path uses. Read/delete/list stay implementation-owned until PR83B's object store forces the question.

## Known deviations between the profiles

- Sync `inspect_database` on PostgreSQL requires a no-running-loop context (above).
- Sub-commit-path kernel subsystems are SQLite-exercised only, pending the same `dialect_insert` treatment (PR83B).

## PR83 follow-ups handed to the next session

- PR83B: port the remaining kernel subsystems to the dialect helper; implement the industrial object store behind `KernelPayloadStore` (register one factory in `tests/test_payload_store_conformance.py` and inherit the suite); DB-to-object consistency and upload/finalization failure injection.
- PR83C: generation publication/reader pinning, backup/restore, failover drill, RPO/RTO evidence.
- CI: add a PostgreSQL service container running the strict conformance runner.

## Commits in this session

| Commit | Content |
|---|---|
| `feat(kernel): dialect-portable commit authority…` | dialects module, commit port, engine bootstrap |
| `feat(db): PostgreSQL migration profile…` | introspection, advisory lock, timestamptz |
| `test(kernel): dual-backend conformance harness…` | matrix suite + strict runner script |
| `feat(kernel): backend-neutral payload-store boundary…` | protocol + reusable conformance |
| `deps(backend): asyncpg…` | requirements + both lockfiles |
| `docs(reference): PR83A evidence…` | this report + JSON bundle |

## Full regression run

Recorded from this machine after all changes (see JSON bundle for the same numbers):

- `python -m pytest tests conformance -q` → **3008 passed, 0 failed, 17 skipped** (1858.77s / 31 min).
- Skip breakdown: 13 are the PostgreSQL conformance parameters without a provisioned server — each skips with the actionable provisioning reason and cannot skip under the strict runner; the remaining 4 are pre-existing local-environment skips unrelated to this session (the branch's prior release note recorded 3; this machine's run observes 4, with the same 0 failures).
- Session additions account exactly: +13 SQLite conformance parameters, +9 payload-store conformance tests = the 22 new passing tests; 2986 prior passes + 22 = 3008.
