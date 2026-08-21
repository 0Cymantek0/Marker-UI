# PR83B1 — Industrial Persistence Parity: Lifecycle Closure and Strict CI (Checkpoints F + G)

> Continuation of the PR83B1 expanded session (plan:
> `planning/v2/marker-ui-v2-PR83B1-control-plane-postgres-parity-plan-20260821.md`).
> Checkpoints A–E landed earlier on the branch (through `6f95e4b`).
> This report covers **Checkpoint F** (snapshot/reconciliation/GC
> store-neutrality, PostgreSQL deletion linearization, physical sweep,
> lexical fail-closure) and **Checkpoint G** (strict CI, one-command
> reproduction, regression, evidence). Machine-readable bundle:
> `docs/reference/measurements/pr83b1-industrial-persistence.json`.

## What this slice proves

The payload lifecycle is now **profile-symmetric**: the same
production modules run on SQLite and PostgreSQL over the local-file and
S3-compatible stores, with the deletion decision linearized against
every writer that can protect or adopt bytes.

| Invariant | Proof |
|---|---|
| Maintenance capability is a declared contract | `PayloadMaintenanceStore` (read/list/stat/delete) + `stat_object` on both stores; 24 shared conformance cases ×2 stores vs real MinIO |
| Snapshot/reconciliation verification is store-neutral | protocol typing + 56-case Gate 6 matrix |
| GC accounting needs no local paths | orphan age/size through `stat_object`; zero `object_path().stat()` in `gc.py` |
| Deletion decision linearizes with roots/pins/adoption | `PAYLOAD_DECISION_LOCK_SCOPE` advisory-xact lock in GC recheck, retirement transactions, `declare_hold`, `acquire/renew_reader_pin`, generation activation, payload-carrying commits (SQLite: writer lock equivalent) |
| Root-before-decision rescues; decision-first gives honest retired + heal | both §29.4 orderings proven with barrier pause hooks, including the blocked-writer mid-decision state |
| Physical delete requires a durable tombstone; failures retry, never fake success | crash windows `gc-after-recheck` / `gc-after-unlink` reconciled from reopened engines; injected transport failure → `failed` → clean retry → `deleted` |
| PostgreSQL never executes SQLite FTS DDL | `LexicalRetirementUnsupportedError` raised **pre-write**; dormant metadata stays inspectable; SQLite FTS suite unchanged |
| One command, zero skips, real services | `run_industrial_conformance.py`: **275 passed / 0 failed / 0 skipped** vs real PostgreSQL 16.14 + real MinIO |
| CI cannot hide industrial regressions | `industrial-persistence` job: health-checked PG+MinIO service containers, strict env, skip-guard step |

## Gate 6 matrix

`tests/test_kernel_lifecycle_conformance.py` — 14 scenarios ×
{sqlite, postgresql} × {local_file, s3_minio} = **56/56, strict, zero
skips**. Every combination asserts engine dialect and store profile;
PostgreSQL cases capture the real server banner. Race scenarios
(concurrent collectors, barrier orderings) were re-run for stability.

Reproduce:

```bash
python backend/scripts/run_industrial_conformance.py
# or targeted, with the services' env vars set:
cd backend && python -m pytest tests/test_kernel_lifecycle_conformance.py -q
```

## Linearization design (reviewer note)

SQLite serialized every writer, which made the old write-first
trick sufficient. On PostgreSQL the equivalent is one advisory
transaction-lock scope — `("kernel-payloads", "gc-decision")` — taken
by: the GC recheck/tombstone transaction, generation/publication
retirement, retention hold/pin creation and renewal, generation
activation, and payload-carrying commits. The commit path takes the
advisory **before** its head-row lock so activation (advisory → row)
and commit share one global order and cannot deadlock. Lock-key
collisions are irrelevant here: mutual serialization is the only
requirement. On SQLite `advisory_xact_lock` is a no-op and the writer
lock provides the same serialization.

## Incidental fix (pre-existing, not caused by the port)

`test_two_identical_builds_converge_idempotently` flaked ~2/5 on the
parent commit: `_stage_transaction` raised `GenerationStateError` when
a sibling builder activated the same deterministic identity between
the staged-state read and the residue purge. The purge's rollback was
correct; the public outcome was wrong — identical inputs must
converge. It now raises `_ConcurrentPointerMove`, the established
retry-and-converge signal, and the retry re-reads the durable
generation idempotently. 8/8 isolated + full 96-test lifecycle cluster
green.

## Known deviations / non-claims

- Industrial lexical index / PublicationSet query serving on PostgreSQL
  does not exist; FTS5 retirement fails closed on PG and dormant
  metadata stays inspectable (next slice owns it).
- Source artifacts remain local (`LocalSourceStore`); no
  stateless-replica claim.
- No HA/failover/backup-restore/RPO-RTO claims (PR83C).
- S3 profile: single PUT, unversioned, path-style, ETag untrusted;
  MinIO-tested — not a certification of every S3-compatible vendor.
- PR69 dynamic admission, PR84 readiness/compatibility: untouched.

## Full regression

Recorded in the JSON bundle (`results.full_backend_regression`);
baseline to beat: PR83A's 3,008 passed / 0 failed / 17 skipped. Every
count delta is attributable to named session additions (dialects +2,
payload-store conformance +3×2 stores, lifecycle conformance +56,
industrial runner coverage documented separately).

## Handoff

Next slices: industrial PublicationSet/query serving; industrial
source-artifact topology (if stateless replicas are required); PR83C
durability/failover drills with measured RPO/RTO.
