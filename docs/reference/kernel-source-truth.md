# PR70/71 Local Slice — Source Identity + Stable Local Acquisition

**Date:** 2026-08-16
**Branch:** `markerui-v2`
**Code base for all recorded results:** `2c3a2eac65938dca2c369f326ef4f8c9456a7e37` (docs-only commits may follow)
**Environment:** Windows 10 (10.0.26200), CPython 3.11.9, SQLite 3.45.1 (aiosqlite)
**Schema/migration head:** `20260816_0009` (unchanged — this slice adds **no** database tables; all new durable state rides the existing kernel record/commit spine)

This document is the reproducible evidence bundle for the TB2 Slice A plan (`planning/v2/marker-ui-v2-tb2-source-identity-stable-acquisition-plan.md`). Every claim is verifiable from code, tests, and the commands recorded here.

---

## 1. What changed

| Area | File | Responsibility |
|---|---|---|
| Source record types | `backend/app/kernel/records.py` | Five typed kernel records: `SourceIdentityRecord` (logical identity — never a content hash), `ContentRevisionRecord` (exact acquired bytes + consistency class), `AccessPolicyRevisionRecord` (per-source policy snapshot), `AuthorizationEpochRecord` (workspace authorization-domain epoch), `SourceObservationRecord` (append-only acquisition evidence incl. rejected outcomes). Consistency-class constants per masterplan 16B.2. |
| Source artifact store | `backend/app/kernel/source_store.py` | `LocalSourceStore`: content-addressed immutable artifacts (`objects/<aa>/<hex>.<suffix>` — suffix kept because converter routing is extension-based) with the payload-store publish discipline (tmp → fsync → atomic replace → read-back verify → read-only). Single-open streamed acquisition: hash + stage in one 1 MiB-chunked pass over one descriptor, pre/post `fstat` identity comparison. Deterministic fault phases + mutation-hook test seams. |
| Acquisition service | `backend/app/services/source_acquisition.py` | `SourceAcquisitionService`: policy-fact capture (resolved-path permitted-root check; marker-owned basis for uploads/URLs), epoch advancement, convergent commit (identity-hash pre-resolution reuses committed records; bounded duplicate retry), rejected-acquisition observations, `resolve()` validation of config blocks against committed records + owned bytes. `AcquiredSourceRevision` config block (`source_revision`) + process-wide default service. |
| Runtime binding | `backend/app/services/kernel_runtime.py` | `authorize()` validates the source-revision block before authorizing work; request record carries revision refs + `depends_on` edge onto the ContentRevision; locator becomes the artifact path. `_launch()` resolves execution input from the revision block only and terminal-fails honestly on missing/truncated artifacts — never falls back to the external path. `ensure_source_revision` is the single acquisition chokepoint for direct submissions. |
| Submission | `backend/app/services/task_manager.py` | `submit_conversion` routes through `ensure_source_revision` before authorization. |
| Ingress | `backend/app/routes/convert.py`, `backend/app/agent_api.py` | Kernel-mode acquisition **before** the PDF probe (probe/config/execution observe one revision); `durable_filepath` = artifact; error cleanup unlinks the marker-owned upload copy, never a shared artifact; incoherent acquisition → REST 409 / agent `UsageError`. `_job_source_path` prefers the committed revision artifact (async); kernel-mode retry reuses the owned revision or re-acquires a NEW revision with a fresh probe when the artifact is destroyed. |
| Snapshot binding | `backend/app/kernel/snapshots.py` | Schema **1.1.0**: `content_revision_ids` (sorted identity hashes visible in the cut) and `access_policy_set_id` (framed root over the access-revision set) are bound into the identity preimage, derived only from committed records ≤ cut. Verifier-policy and schema-registry stay honestly unbound. Pre-source cuts report empty bindings explicitly. |
| Config | `backend/app/core/config.py` | `MARKER_SOURCE_STORE_ROOT` (default `<data>/source_store`). |
| Benchmark | `backend/scripts/bench_source_acquisition.py` | Operational characterization (Section 8). |

No new tables, no new Alembic revision, no second scheduler, no second terminal state machine. PR66/67B fencing/publication/liveness semantics untouched.

## 2. Source authority flow

```text
permitted local source (or marker-owned upload / fetched URL copy)
  -> SourceAcquisitionService.acquire()
       1. policy facts on the RESOLVED path (symlinks cannot re-point the open)
       2. LocalSourceStore.stage_from_path()  — one open descriptor:
          pre-fstat -> stream(hash+write) -> post-fstat -> compare
          -> atomic content-addressed publish -> read-back verify
       3. one kernel commit:
            SourceIdentity + ContentRevision + AccessPolicyRevision
            (+ AuthorizationEpoch when the domain fingerprint changed)
            + SourceObservation, edges derived_from/observes
  -> config["source_revision"] block + durable_filepath = artifact
  -> probe_pdf(artifact)                     (same revision)
  -> ConversionJob row (queue_backend='kernel')
  -> coordinator.authorize(): block re-validated against committed truth;
     request record + depends_on(content_revision) + outbox intent
  -> fair claim -> fenced execution reading the artifact ONLY
  -> accepted publication -> source-bound KernelSnapshot (schema 1.1.0)
```

## 3. Identity model (local profile rules)

- **Logical source key**: `local:<normcase(resolved path)>` for local paths; `upload:<job_id>` per upload occurrence; `url:<url>` for fetched origins. Moving/renaming a local file therefore starts a new logical source — documented profile choice (provider-native stable identity is future connector work).
- **Content revision identity**: `(source_ref, blob_key, byte_length, media_type, consistency_class, suffix)`. Acquisition evidence (timestamps, handle stats) lives in observations, so re-acquiring identical bytes **converges to one revision**.
- **Access policy identity**: per-source facts actually observed (`permitted_root`, `unrestricted`, `roots_configured`, `declared_acl_knowledge: "none"`). No fabricated ACL/group knowledge.
- **AuthorizationEpoch**: advances when the effective local domain facts (roots set + unrestricted flag) change; structurally separate from content identity.
- **Consistency classes**: local path and marker-owned upload → `stable_handle` (single-open verified handle + immutable staged copy); URL origin → `best_effort_consistent` (no provider validators; staged bytes immutable once owned). `incoherent_rejected` is an observation outcome only — it never mints a revision.

## 4. Failure matrix (observed outcomes, all test-proven)

| Attack / failure | Observed outcome | Test |
|---|---|---|
| Truncation during streamed read | `IncoherentSourceError` ("changed size"); rejected observation committed; **no** revision, no artifact, no tmp residue | `test_kernel_source_store.py::test_truncation_...`, `test_kernel_source_acquisition.py::...records_rejection_only` |
| Append during read | post-fstat size mismatch → incoherent | `...::test_append_...` |
| Same-size in-place mutation during read | mtime_ns identity change → incoherent | `...::test_inplace_mutation_...` |
| Path replaced between resolve and open | acquisition opens what the path currently resolves to; all evidence from that one open — coherent acquisition of current bytes, no splice | `...::test_replacement_after_resolve_...` |
| External file replaced after acquisition | execution continues on revision A's artifact; B never parsed | `test_kernel_source_runtime.py::test_worker_parses_artifact_not_external_path`, `test_kernel_source_ingress.py::test_probe_and_execution_consume_one_acquired_revision` |
| External file deleted after acquisition | job completes from owned bytes; retry works | `...::test_external_source_disappearing_...`, `test_kernel_source_ingress.py::test_external_source_deleting_...` |
| Artifact destroyed before launch | terminal failure "acquired source revision unavailable"; **no external-path fallback** | `test_kernel_source_runtime.py::test_missing_artifact_...` / `..._truncated_...` |
| Artifact truncated | size check at launch → same honest terminal failure | `..._truncated_...` |
| Crash after staging, before commit | bytes exist unreferenced (residue, never truth); retry converges to one revision | `test_kernel_source_acquisition.py::test_crash_between_staging_and_commit_...` (commit fault `pre-commit`) |
| Concurrent duplicate acquisition (4-way) | one revision, one source identity, four observations | `...::test_concurrent_duplicate_...` |
| Duplicate submission of one job | same work id; no forked truth | `test_kernel_source_runtime.py::test_duplicate_submission_converges` |
| Restart with external source changed | recovery + dispatch execute revision A | `...::test_restart_executes_acquired_revision_...` |
| Forged/unresolvable source block at authorize | `KernelError` (work not authorized against fiction); through submission the block is re-acquired honestly | `...::test_authorize_rejects_unresolvable_...`, `...::test_forged_block_reacquires_...` |
| Retry with dead artifact + changed external | NEW revision with fresh probe; old probe never reused | `test_kernel_source_ingress.py::test_retry_reacquires_new_revision_...` |

## 5. Identity separation examples (from the committed suites)

- same logical source + same bytes (re-submitted): identical `source_id`/`content_revision_id`/`access_policy_id`; observation count grows (audit history).
- same logical source + changed bytes: same `source_id`, new `content_revision_id`.
- same source + policy-only change (permitted root widened): **same** `content_revision_id`, new `access_policy_id`, epoch +1.
- two logical sources + identical bytes: distinct `source_id` and `content_revision_id`, one shared artifact (`dedup_hits`), snapshot `content_revision_ids` grows by two distinct hashes.

## 6. Snapshot binding

- `content_revision_ids` and `access_policy_set_id` are computed from records visible in the cut — deterministic for the same committed state, change on revision/policy membership change, and keep historical pinned cuts stable.
- A cut that predates source truth yields empty bindings under schema 1.1.0 (explicit, machine-detectable); v1.0.0 identities remain historical facts — the schema version in the framing domain separates the two interpretation domains.

## 7. Test commands and results (exact final code)

| Command | Result |
|---|---|
| `python -m pytest tests/test_kernel_source_records.py tests/test_kernel_source_store.py -q` | 38 passed |
| `python -m pytest tests/test_kernel_source_acquisition.py -q` | 16 passed |
| `python -m pytest tests/test_kernel_source_runtime.py tests/test_kernel_source_snapshot.py -q` | 13 passed |
| `python -m pytest tests/test_kernel_source_ingress.py -q` | 7 passed |
| `python -m pytest tests/test_kernel_snapshot.py tests/test_kernel_runtime.py -q` (PR67B regression) | 43 passed |
| `python -m pytest tests/test_kernel_snapshot.py tests/test_kernel_runtime.py tests/test_kernel_generation.py tests/test_kernel_replay.py tests/test_kernel_reconcile.py tests/test_kernel_commit.py tests/test_kernel_migration.py -q` | 134 passed |
| `python -m pytest tests/test_convert.py tests/test_convert_retry.py tests/test_agent_contract.py tests/test_cli_mcp.py -q` | 158 passed |
| `python -m pytest tests -q` (full backend) | **1857 passed, 3 skipped** (685.66s; baseline before this slice was 1819/3) |
| Race/TOCTOU subset ×3 (§13.8) | 3× consecutive: **54 passed** each run (45.8s / 62.2s / 43.6s) |

No new migration was added; the existing migration suite (`test_kernel_migration.py`, `test_database_migration.py`) passes unchanged from `20260816_0009`.

## 8. Performance characterization (this machine)

`python scripts/bench_source_acquisition.py`:

| Measurement | Value |
|---|---|
| Small doc (~4.6 KB) acquisition | ~0.04 s |
| Large doc (~17 MB) acquisition | ~0.11 s (≈160 MB/s) |
| Duplicate acquisition (dedup, small) | ~0.03 s |
| `resolve()` reuse path (17 MB revision) | ~0.007 s — no external read |
| Write amplification (fresh acquisition) | 3× logical bytes: external read + tmp write→final + read-back verify |
| Write amplification (dedup hit) | 0 bytes written (external read + existing-artifact re-hash) |
| Durable rows per fresh revision | 5 records (source, content, access, epoch†, observation) + 3 edges |
| Durable rows per duplicate acquisition | 1 observation |
| Peak memory (32 MB source, streamed) | < 16 MB traced (1 MiB chunks) — `test_large_source_streams_with_bounded_memory` |

† epoch record only when the authorization-domain fingerprint actually changed.

The dominant cost is proportional to source bytes (hash + copy + verify), not to revision count; the fixed per-acquisition commit cost is the small-doc floor. Legacy comparison: the old path performed zero extra I/O but offered no revision guarantee — the 3× byte tax is the recorded price of path-based converter compatibility (plan §11 approach family 2, explicitly chosen over a handle-abstraction rewrite).

## 9. Design decisions and rejected alternatives

- **Immutable staging over stable-handle plumbing** (plan §20.3): converters consume paths; a descriptor-based handle abstraction would need cross-process/platform machinery (Windows/POSIX divergence) for equal correctness. Staging gives restart-proof, content-addressed, dedup-able truth. Kill-criterion respected: converters immediately require paths anyway.
- **Kernel records over dedicated tables** (§20.1): the commit spine already provides canonical identity, atomicity, and append-only history; source state needs no current-pointer table at this scale (resolution queries `kernel_records` by identity; a materialized read model can come later).
- **Separate source store namespace over PR64 payload registry** (§20.2): same blob discipline, but source artifacts must keep routing suffixes and must stay outside PR65B payload GC — a revision's blob key is committed record state, not a payload-object reference.
- **Acquisition before job-row commit at ingress** (§20.4): the config block exists only after the source commit returns, so no row can reference uncommitted truth; restart adoption re-validates blocks instead of guessing.
- **Rejected**: deriving `SourceIdentity` from content hash (merges distinct sources), from raw path case (Windows case-folds), or reminting content on policy change — each has a dedicated falsification test.

## 10. Residual limits (owned by later PRs)

- Remote/object/HTTP version-pinned acquisition (strong ETags, version IDs, range consistency) — URL origins stay `best_effort_consistent`.
- Connector inbox/cursor transactions, event dedup, gap recovery (PR71 remote half).
- Enterprise authorization: group/ACL resolution and per-principal grants. (PR78 shipped the local-v1 slice — security domains, live deny/lift overlay, and retrieval revocation — see `authorization-retrieval.md`; richer multi-principal models remain open.) The local epoch advances only on permitted-roots/unrestricted changes.
- Source artifact retention/GC: artifacts accumulate like uploads (content-addressed, deduped); job deletion does not reclaim them. A retention story needs a PR65B-style tombstone authority for source blobs.
- PR72+ anchors/patches/verification remain unbound snapshot fields (`verifier_policy_revision_id`, `schema_registry_revision`).
- Move/rename continuity for local sources is a new logical source under the documented profile rule.

## 11. Next dependency-complete slice

TB2 Slice B — SourceAnchor + reading-order primitives (PR72) rooted on the now-durable `ContentRevision`; or the PR71 remote/connector continuation if ingestion convergence takes priority.
