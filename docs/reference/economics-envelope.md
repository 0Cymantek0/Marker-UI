# Economics Envelope — Scale Cost and Visual Retrieval Operational Delta

**Session:** 2026-08-23, invariants 57 and 58 (masterplan 23C.7)
**Artifacts:**
`pr87a-local-economics-envelope.json` · `pr87b-industrial-economics-envelope.json` · `pr87c-visual-economics.json` · composed by `pr87-economics-envelope-index.json`

---

## 1. What this is

A reproducible, machine-checkable economics envelope for a
representative Marker UI workload, plus a controlled visual
OFF-vs-selective-ON comparison with a measured ACL complexity vector.
Every number comes from an executed run against the real kernel
authorities; every dimension that cannot be measured on a profile is
recorded as `unavailable` or `not_applicable` with a reason — never as
zero.

The shared contract is `marker.economics_envelope.v1`
(`backend/app/eval/economics/`): statuses separate
measured/derived from unavailable/not-applicable, units come from a
closed vocabulary (including signed `delta_*` units for OFF/ON
differences), derived ratios must reference raw same-window counters,
timing claims carry sample counts, and every dimension of the declared
set must be present. `validate_envelope` fails closed on each of these;
the adversarial cases are pinned in
`backend/tests/test_economics_contract.py`.

## 2. Workload and topology

| Profile | Database | Object store | Workload |
|---|---|---|---|
| local (`pr87a`) | SQLite (rollback-journal) via temp workspace | local content-addressed stores | PR81A corpus (15 PDFs / 27 pages) ingest+publish → `doc-rev-01` v3→v4 revision → publication-pin GC lifecycle → conflicting-candidate review scenario → 3 cold-start samples |
| industrial (`pr87b`) | PostgreSQL 16 (`postgres:16-alpine`, docker) | MinIO (S3-compatible, docker) | same corpus via `seed_workspace` with S3 source artifacts, same revision, 3 fresh-database cold-start samples |
| visual (`pr87c`) | SQLite (same corpus, separate arms) | local render cache + npz visual generation | OFF arm (lexical lanes only) vs ON arm (+ CLIP dense + hybrid VLM rerank) on identical queries; ACL arm (deny propagation, partitioned publication, partitioned visual matrix); revision in both arms. VLM answers from the committed replay cache — no network, no credentials. |

## 3. Headline measurements

### Invariant 57 — scale envelope

| Dimension | local | industrial |
|---|---:|---:|
| database rows (full window, exact counts) | 1,659 | 961 |
| — logical authority / derived / lexical / publication | 79 / 64 / 1,514 / 2 | 79 / 125 / 754 / 3 |
| payload + source objects | 16 sources | 16 sources (S3) |
| committed source bytes (logical) | 44,243 | 44,243 |
| WAL bytes / write amplification | n/a (rollback-journal) | 1,052,808 B → **23.8x** |
| revision-window WAL / changed bytes | — | 491,664 B / 2,510 B → **195.9x** |
| FTS storage | unavailable (no DBSTAT in this build; logical rows/chars measured) | 745,472 B (`pg_total_relation_size`) |
| vector storage | not_applicable (no implementation exists) | not_applicable |
| retained generations after revision + GC | active 1 (pinned old gen survived GC; release retired it) | active 1 + superseded 1 |
| cold start (fresh DB → first lexical query, p50 of 3) | ~1.5 s | ~1.0–1.3 s |
| review burden | 1 review-required run, 1 field, 1 decision record, 36 assessment records, 62 rows | not_applicable (local-profile dimension) |
| reprocessing | 1 doc changed, 14 unchanged docs added **zero** rows, 1 generation rebuild | same shape: 444 revision rows for 1 changed doc |

The revision rows are the story: one 2.5 KB document change rebuilds
the full materialized generation and lexical publication (377 lexical
rows industrial, 63 generation rows) — rebuild-per-revision semantics,
not incremental patching. The envelope makes that cost visible instead
of leaving it as an inference.

### Invariant 58 — visual OFF/ON + ACL

| Measure | OFF | ON (selective) |
|---|---:|---:|
| visual-hard task success | 0.6567 | 0.960 (hybrid rerank) / 0.3867 (dense) |
| embedding bytes | 0 | 47,104 (CLIP v3) |
| render bytes | baseline delivery renders | +678,127 |
| VLM calls (replay) | 60 | 172 (+112) |
| revision rebuild | no visual state | + visual rebuild (measured in `build_delta`) |

Hybrid rerank gains **+0.3033** on visual-hard slices over the OFF
baseline — reproducing the committed PR81A conclusion on the same
corpus/cache. Dense stays below baseline (−0.27). Recorded
disposition: **`narrow_only`** — the narrow hybrid route pays, the
dense route does not, and no broad `visual_search` promotion is
justified. keep_disabled remains a first-class outcome of the rule.

ACL complexity is a measured vector, not prose: 2 visual partitions,
43,008 duplicated matrix bytes for high assurance, 0.2 ms partition
build, **38.5 ms deny-to-effective with ZERO visual rebuilds required**
(deny outruns reindex by design), 146 authorized-universe filter calls
through a pass-through counting wrapper that never alters the
production authorize-before-competition semantics. Zero forbidden or
stale delivery across all arms.

## 4. What this does and does not claim

Claims: row/storage/WAL/cold/review/reprocessing economics for the
declared workload on the declared topologies; a same-workload OFF/ON
visual delta with ACL cost; a disposition from a predeclared rule.

Non-claims (also recorded in each artifact's `non_claims`):
- No human review time or queue dwell — review burden is item/row counts.
- Provider wall time is replay-cache time; call counts and usage totals
  are exact, live latency is not claimed.
- Local FTS physical bytes are unavailable (this SQLite build lacks
  DBSTAT); logical rows/chars are measured. Industrial FTS bytes are
  real `pg_total_relation_size` sums.
- `pg_stat_wal` record/FPI counters were not observable under the async
  driver (asynchronous stats flush observed a zero delta while rows
  committed); WAL is claimed as exact LSN byte volume, cluster-scoped to
  a benchmark-only cluster.
- Vector storage: no implementation exists on any profile; absence is
  declared, not zero-filled.

## 5. Reproduction

```bash
cd backend
python scripts/bench_economics_local.py --samples 3 --write        # local
python scripts/bench_economics_visual.py --write                   # visual (offline replay)
python scripts/economics_envelope_index.py                         # composed checks
# industrial: requires Docker (provisions postgres:16-alpine + minio) or
# MARKER_TEST_POSTGRES_ADMIN_URL + MARKER_TEST_S3_ENDPOINT{,_ACCESS_KEY,_SECRET_KEY}
python scripts/bench_economics_industrial.py --samples 3 --write

python -m pytest tests/test_economics_contract.py tests/test_economics_collectors.py \
  tests/test_economics_pgprobe.py tests/test_economics_visual_envelope.py \
  tests/test_economics_envelope_index.py tests/test_eval_pr81a_economics_decision.py
```

Row counts and FTS bytes are deterministic across runs; wall-time
fields (cold start, WAL byte volume, deny latency) vary with hardware —
acceptance asserts structure and sample counts, not exact timings.

## 6. Audit note for future agents

Blind spots and intentionally-left-unavailable metrics:

1. **Local physical FTS bytes** — needs a SQLite build with
   `SQLITE_ENABLE_DBSTAT_VTAB` (or an equivalent page-accounting
   technique); the collector already degrades truthfully.
2. **`pg_stat_wal` record/FPI counters** — revisit if the kernel ever
   runs a synchronous driver or after a `pg_stat_clear_snapshot()`-based
   approach is validated; LSN byte volume is the honest numerator today.
3. **WAL amplification variance** — 12–24x observed across runs
   (checkpoint timing dominates at this tiny corpus size); the raw
   numerator/denominator are retained per run. Repeat on a larger
   corpus before quoting a single number in any claim.
4. **Payload read-back on S3** — `S3PayloadStore` has no
   `bytes_read_back` counter; `copy_bytes` documents the gap rather
   than approximating.
5. **Review burden scope** — one deterministic conflict scenario; it
   closes invariant 57's "review is reported" clause, not invariant 26's
   production review-policy usability claim.
6. **Hardware/topology** — all timings are from one Windows dev machine
   + Docker Linux containers; re-run the industrial bench on the CI
   industrial job topology before any production-capacity claim.
7. **Thresholds** — no new numeric thresholds were invented; the visual
   disposition imports the committed PR81A margins, and ACL acceptance
   is structural (complete vector, zero forbidden delivery, zero-rebuild
   deny propagation) so it cannot be tuned after the fact.
