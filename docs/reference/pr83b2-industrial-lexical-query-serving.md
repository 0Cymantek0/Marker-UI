# PR83B2 — Industrial PublicationSet & Lexical Query-Serving Parity

**Scope:** real PostgreSQL lexical publication generations, query
serving, continuation, authorization isolation, retirement, and strict
industrial conformance — the slice PR83B1 explicitly refused to claim.

**Branch:** `markerui-v2`
**Start SHA:** `4af4751a310d06f5ad14423fd699ea8b2eb00d6b` (PR83B1 head)
**Evidence head:** see machine-readable bundle (`docs/reference/measurements/pr83b2-industrial-lexical-query-serving.json`).

---

## What this slice proves

| Question (plan §19) | Proof |
|---|---|
| Can PostgreSQL build and serve a real lexical PublicationSet? | `test_kernel_publication_lexical_conformance.py` — build/validate/activate/open/serve on real PG 16.14; physical tsvector+GIN artifact inspected in the catalog |
| Is the generation immutable and atomically activated? | identity/immutability cases + `_activate_set` head upsert via `dialect_insert`; pre-activate fault rolls back and converges |
| Can old and new generations coexist safely? | continuation-across-activation + concurrent reader race: no hybrid page ever observed |
| Does a cursor stay bound to publication/generation/query/security context? | keyset binding matrix incl. legacy pre-PR83B2 hash rejection and head-switch pin coherence |
| Can tied relevance scores page without duplicates or gaps? | canonical vs page sizes 1/2/3/7 over a tie-heavy corpus, exact equality both backends |
| Does compilation preserve terms/phrase/prefix semantics without FTS5 universality? | typed logical (text, mode) API end to end; PG compiles through bound-parameter `phraseto_tsquery` only |
| Are tokenizer differences deliberate and recorded? | per-backend assertions (diacritics divergence) + decision record below |
| Does authorization constrain retrieval/ranking before forbidden content? | high-assurance partition generation on real PG; rank basis byte-stable under forbidden-corpus growth |
| Can the PG lexical artifact retire only after proof closure? | GC retires real artifact (table gone from catalog) with pin/set protection + idempotency |
| Do crash points recover without mixed-generation visibility? | staged-residue resume, in-transaction build failure leaves zero physical residue (transactional DDL), activation race |
| Does strict real-PG conformance run with zero skips? | both new suites in the industrial runner + CI job with no-skip guard |
| Does SQLite remain healthy? | full SQLite publication/context suites green, identities byte-identical |

## Design decisions (Work Area A record)

1. **Physical model (Q1/Q7):** one immutable plain table per generation
   (`kernel_fts_<digest-40>` — 40 hex keeps table + `_tsv_ix` index under
   PostgreSQL's 63-byte identifier limit; SQLite keeps the historical
   full-digest FTS5 name) with a STORED `tsvector` generated under the
   pinned `simple` configuration plus a GIN index, created *inside* the
   staging transaction. PostgreSQL DDL is transactional, so a failed or
   crashed build leaves no physical residue — there is no
   non-transactional window, no `CREATE INDEX CONCURRENTLY`, and no
   `INVALID`-index state to reconcile (I11 satisfied structurally; the
   residue test proves zero leftover tables on injected failure).
2. **Text-search configuration (Q3):** `simple` — lowercasing, no
   stemming, no stop words — the closest native match to FTS5
   `unicode61`'s deliberate minimalism. Pinned explicitly in every
   `to_tsvector`/`phraseto_tsquery` call and in generation identity;
   a server-varying `default_text_search_config` can never change
   semantics. Deliberate documented divergence: `unicode61` folds Latin
   diacritics, `simple` does not (asserted per backend in the
   semantics-matrix test).
3. **Identity (Q9/I9):** SQLite generations keep the byte-identical
   `marker.kernel.lexical.fts5.v1` identity; PostgreSQL generations
   carry `marker.kernel.lexical.pg_tsvector.v1` with tokenizer
   `pg_tsvector` + config `{"text_search_config": "simple"}`, so a
   generation can never be mistaken for one built under different
   physical semantics and config changes can only mint new generations.
4. **Query compilation (Q4/I7):** the reader takes typed logical
   `(text, mode)` input; FTS5 MATCH strings no longer cross any API.
   PG expressions are composed from bound parameters through
   `phraseto_tsquery` (parses document text, never query grammar), so
   operator injection is structurally impossible. One authority:
   `app.kernel/lexical.py`.
5. **Ranking parity contract (Q2):** deterministic within-backend
   best-first ordering only — FTS5 `bm25` ASC vs `ts_rank` DESC — never
   raw cross-backend score equality. No corpus-global statistics are
   used on either backend (FTS5 bm25 is per-table; ts_rank is
   document-local), preserving the high-assurance isolation contract.
6. **Continuation key (Q5):** `query_hash` is now the hash of the
   *logical* form (schema tag + mode + canonical tokens), identical
   across backends; rank stays a backend-native float with
   backend-owned direction interpreted by the generation's own reader;
   row_index tie-break unchanged. Pre-PR83B2 cursors fail closed as
   query-binding mismatches (60s cursor TTL makes this non-disruptive;
   tested).
7. **Retirement (Q8):** `DROP TABLE` is portable; both GC fail-closed
   guards and the boundary error type are removed; protection semantics
   (surviving set rows, unexpired pins, staging grace) unchanged and
   now proven on real PG with catalog-level verification.
8. **Migrations (Q9):** none required — the physical artifact is
   runtime-managed under the already-excluded `kernel_fts_` prefix on
   both backends; metadata lives in the existing
   `kernel_lexical_generations` manifest (`fts_table` names the
   artifact; `tokenizer`/`tokenizer_config_json` carry the projection
   identity).

## Reproduction

```bash
# strict industrial matrix (real PostgreSQL 16 + real MinIO, zero skips)
python backend/scripts/run_industrial_conformance.py

# focused suites
cd backend
pytest tests/test_kernel_publication_lexical_conformance.py -q   # + MARKER_TEST_POSTGRES_ADMIN_URL
pytest tests/test_context_runtime_lexical_conformance.py -q      # + MARKER_TEST_POSTGRES_ADMIN_URL
pytest tests/test_kernel_lexical.py -q                            # pure unit, no services

# full backend regression (documented fast path)
cd backend && python -m pytest tests conformance -q
```

## Results

All runs under the locked dev environment (pytest 8.3.4,
pytest-asyncio 0.24.0, asyncpg 0.30.0) against real PostgreSQL 16.14
and real MinIO via the one-command runner:

| Gate | Result |
|---|---|
| Lexical unit layer | 27 passed |
| Kernel lexical conformance (dual-backend) | 36 passed / 0 failed / 0 skipped |
| Context-runtime lexical conformance (dual-backend) | 10 passed / 0 failed / 0 skipped |
| Strict industrial one-command matrix | 321 passed / 0 failed / 0 skipped in 544.81s (locked env, real PostgreSQL 16.14 + real MinIO, zero skips enforced) |
| Full backend regression (fast path) | 3134 passed / 0 failed / 137 skipped |

Regression delta vs the PR83B1 baseline (3083/0/115): **+51 passed**
(the 27 lexical unit tests + the SQLite parameters of the new
dual-backend suites) and **+22 skips** (23 new PostgreSQL-provisioning
parameters, minus one pre-existing environment-conditional skip that
does not reproduce on this host — the optional unstructured/xlwt
dependencies import here). Every new skip is executed for real by the
strict industrial runner and the CI industrial job.

See `docs/reference/measurements/pr83b2-industrial-lexical-query-serving.json`
for exact counts, environment, and per-gate outcomes (bound to the
final evidence SHA).

## Known deviations and non-claims

- Ranking scores are **not** comparable across backends (by design;
  ordering contracts are per-backend and deterministic).
- Diacritics: `cafe` matches `café` on SQLite, not on PostgreSQL —
  deliberate, asserted, stable.
- Query-plan note: at small corpus sizes PostgreSQL's cost model
  prefers Seq Scan (tsvector has no MCV statistics); with sequential
  scans disabled the planner produces a Bitmap Index Scan over the
  generation's GIN index, proving the served predicate matches the
  indexed column. No latency claims.
- Vector index industrialization, source-artifact topology, HA/failover,
  backup/restore/RPO/RTO (PR83C), PR69, PR84 — untouched, still open.
