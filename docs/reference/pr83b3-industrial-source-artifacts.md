# PR83B3 — Industrial Source-Artifact Topology and Stable Source Availability

**Session date:** 2026-08-22
**Implementation start SHA:** `4b29850907e8e3e5ef8907affce30f1c78273dca` (PR83B2 evidence head)
**Evidence head:** see `docs/reference/measurements/pr83b3-industrial-source-artifacts.json`
**Plan:** `planning/v2/marker-ui-v2-PR83B3-industrial-source-artifact-topology-plan-20260822.md`

## What this slice proves

The PR83B2 evidence listed `source-artifact topology` as an explicit
non-claim: source bytes lived in the node-local `LocalSourceStore`
directory of the acquiring process. This slice closes that gap.

| Required truth (plan §7/§9/§11) | Proof |
|---|---|
| A committed industrial `ContentRevision` points to bytes durably available outside the originating node's source-store directory | `S3SourceStore` publishes content-addressed immutable objects into a shared S3-compatible namespace (`kernel-sources` prefix); process-boundary test destroys the acquiring node's directories and still resolves + verifies + consumes the exact revision (`test_kernel_source_industrial_topology.py`) |
| A fresh process/node resolves and verifies the exact bytes from durable truth alone | Process B starts with different empty `SOURCE_STORE_ROOT`/cache directories and only database + object-store configuration; env-driven `build_source_store()` reconstructs the store; dual-backend database (real PostgreSQL 16 when provisioned) |
| Existing path-based converters work without making an ephemeral path a second authority | `consumable_path_for()`: local profile consumes the owned artifact; industrial profile consumes a `VerifiedSourceMaterializer` working copy whose every use is re-verified by full content hash and whose corruption forces a rebuild from durable truth (service-level + end-to-end dispatch tests) |
| Original source mutation/deletion after acquisition cannot change the committed revision's bytes | Runtime tests mutate/delete the external file post-submission; conversion still executes revision A bytes (s3 profile); store-level conformance proves it on both profiles |
| Industrial object missing/corrupt/unreachable fail closed, no hidden path/local fallback | Missing → terminal `acquired source revision unavailable`; truncated → unresolvable (HEAD length) + refused materialization; same-size tampered → refused by content hash at verification and materialization; unreachable endpoint → `SourceStoreError`, never another authority; authorize refuses path-trust submissions without a committed revision; `ensure_source_revision` propagates failures instead of the legacy log-and-continue |
| Concurrent identical acquisition and ambiguous writer outcomes converge safely | 4-way and 8-way concurrent identical stages (conditional create `If-None-Match: *`); 412 loser verifies winner's bytes by full read-back; wrong bytes at a claimed identity are refused, never overwritten; ambiguous PUT (fault after PUT, before read-back) converges by re-observation with verified dedup |
| Crash after stage-before-commit leaves no false semantic truth; retry converges | after-verify fault: object staged, zero `content_revision` rows committed, healthy retry converges by dedup reuse — proven on S3 and re-proven on the local profile |
| Crash/restart after commit resumes from the shared revision | fresh-coordinator restart test: same DB + bucket, empty cache, original source deleted → dispatch materializes and completes |
| PR70/71 identity/content/policy semantics intact; local tests green | records untouched; `ContentRevisionRecord.blob_key` still `sha256:` content identity; all six historical source suites green in full regression |
| Source artifacts remain outside ordinary kernel-payload GC authority | `kernel-sources` prefix disjoint from `kernel-payloads` in the same bucket; payload listing/deletion cannot see or remove source objects (store-level); a source acquisition commits exactly the five source-record classes — no payload rows exist that could authorize payload GC against source bytes (kernel-truth level) |
| Industrial source tests run against real services in the strict matrix with zero skips | 5 new files added to `run_industrial_conformance.py` `TEST_TARGETS` and the CI `industrial-persistence` job; strict env fails at collection rather than skip; runner returns 3 on any skip |

## Design decisions (Workstream A record)

* **Capability boundary, not a subclass.** A `SourceArtifactStore`
  protocol (`stage_from_path` / `artifact_exists` / `available_length`
  / `verify_artifact` / `locator_for` / validation) extracted in front
  of the unchanged `LocalSourceStore`; a sibling `S3SourceStore` in
  `app/kernel/source_object_store.py`. Alternatives rejected: wrapping
  `S3PayloadStore` (payload ownership/GC semantics differ — suffix keys,
  no payload registry, no GC surface) and generalizing both into one
  mutable object-store abstraction (broader than the claim requires).
* **Reuse of proven machinery, not of the payload store.** PR83B1's
  SigV4 signing (`s3_request_headers`, extended with an optional
  precomputed `content_sha256`), `S3StoreConfig`, and conditional-create
  convergence discipline are reused; the namespace, keys, staging
  discipline, and consumption bridge are source-specific.
* **Single-open two-pass staging.** Pass 1 streams and hashes from one
  fd with pre/post identity stats (the PR70/71 discipline); pass 2
  streams the PUT from the *same* descriptor, signed with pass 1's
  SHA-256 (the server verifies the body end-to-end), re-hashed during
  upload, identity-checked again, then read-back-verified by streamed
  GET. A source mutating between or during passes is rejected as
  incoherent — never staged, never overwritten.
* **Profile declared on the revision envelope.** `AcquiredSourceRevision`
  carries `store_profile`; `to_config()` persists it; `resolve()`
  refuses cross-profile blocks before any kernel lookup. Pre-PR83B3
  blocks without the field are local-by-construction and are never
  reinterpreted as shared-topology-available. Kernel records and schema
  are untouched — identity semantics and migrations unchanged.
* **Availability model.** `resolve()` gates on HEAD presence + length
  (the exact analogue of the local `stat` check); acceptance and
  consumption are always content-verified (full-body hash on stage,
  read-back, `verify_artifact`, and materialize). Wrong bytes never
  reach conversion even when length matches.
* **Consumption bridge.** Local profile consumes the owned artifact
  path (zero behavior change). Industrial profile consumes verified
  node-local working copies under `MARKER_SOURCE_CACHE_ROOT` — a
  rebuildable cache, never an authority: every hit is re-hashed, every
  corruption is rebuilt from durable truth, and deleting cache files
  cannot affect the shared object.
* **Fail-closure boundary is the profile, not a flag.**
  `legacy_submit_fallback` is true only for the local profile; under
  industrial, acquisition failures propagate, missing files reject the
  submission, path-trust authorization is refused, and restart
  adoption logs the refusal per row instead of silently adopting
  unowned-path work.
* **Configuration is explicit.** `MARKER_SOURCE_STORE_PROFILE`
  (`local` default for compatibility, `s3` for industrial) +
  `MARKER_SOURCE_S3_{ENDPOINT,BUCKET,ACCESS_KEY,SECRET_KEY,REGION,PREFIX}`
  + `MARKER_SOURCE_CACHE_ROOT`. A missing industrial configuration
  raises — industrial source truth is never an accidental side effect
  of a local default, and there is no silent cross-profile fallback.

## Reproduction

```bash
# strict industrial matrix: real PostgreSQL 16 + real MinIO, zero skips
cd backend && python scripts/run_industrial_conformance.py

# focused industrial source suites (same services, strict env set by runner)
python -m pytest tests/test_source_store_conformance.py \
  tests/test_source_store_s3.py tests/test_kernel_source_acquisition_s3.py \
  tests/test_kernel_source_runtime_s3.py \
  tests/test_kernel_source_industrial_topology.py -q

# established PR70/71 local suites
python -m pytest tests/test_kernel_source_store.py tests/test_kernel_source_records.py \
  tests/test_kernel_source_acquisition.py tests/test_kernel_source_runtime.py \
  tests/test_kernel_source_snapshot.py tests/test_kernel_source_ingress.py -q

# full backend regression
python -m pytest tests conformance -q
```

## Results

| Gate | Result |
|---|---|
| Industrial matrix (one command, real PG 16.14 + real MinIO, strict) | **395 passed / 0 failed / 0 skipped** in 704.10 s (PR83B2 baseline 321 + 74 new industrial source tests) |
| Dual-profile store conformance | 24 tests (12 cases × {local_file, s3_minio}) |
| S3 store adversarial suite | 24 tests vs real MinIO |
| Industrial acquisition service suite | 11 tests vs real MinIO |
| Industrial runtime suite | 10 tests vs real MinIO |
| Process-boundary / crash-window / ownership proofs | dual-backend DB parametrization; sqlite + PostgreSQL 16.14 under the runner |
| Full backend regression | **3150 passed / 0 failed / 183 skipped** (baseline 3134/0/137; +16 passed env-free new tests, +46 skips are the new service-gated industrial tests executing green in the strict matrix instead) |

## Performance characterization (localhost MinIO, streamed 1 MiB chunks)

| Operation | Median |
|---|---|
| Fresh stage, small (~48 B) | 0.0064 s |
| Dedup stage, small | 0.0060 s |
| Fresh stage, 17 MiB | 0.1431 s (≈119 MiB/s) |
| Dedup check (HEAD + full GET hash), 17 MiB | 0.1375 s |
| Full verify (streamed GET + hash), 17 MiB | 0.1717 s |
| Verified materialization, 17 MiB (cache miss) | 0.1536 s |
| Verified reuse (cache hit, local re-hash) | 0.0245 s |

Fresh-stage transfer amplification is 3× logical bytes (hash pass read
+ PUT write + read-back verify), mirroring the local profile's
deliberate read-back verification cost; dedup costs one logical read
and zero writes; staging/materialization/verification hold one 1 MiB
chunk — no full-body buffering (PR70/71's bounded-memory property
carries over). Execution reuses a verified local working copy instead
of re-downloading per attempt; per-node cache eviction policy is a
documented non-claim. Source and payload traffic are distinguishable
by bucket prefix (`kernel-sources` vs `kernel-payloads`).

## Known deviations and non-claims

* No HA/failover, leader election, or multi-node scheduler claims
  (PR83C); the process-boundary proof shows a *replacement* process
  can recover committed truth, not orchestrated failover.
* No backup/restore or measured RPO/RTO across database + object store.
* Single-PUT unversioned-bucket profile (MinIO-tested); no multipart,
  no S3 vendor certification beyond SigV4/path-style compatibility.
* Source-artifact retention/GC remains unclaimed (as in PR70/71):
  artifacts accumulate; a source-specific tombstone authority is
  future work. Payload GC provably cannot touch them.
* Node-local materialization cache has no eviction policy yet; it is
  rebuilt-on-demand and safe to delete at any time.
* Vector-index industrialization, PR69 dynamic admission, and PR84
  readiness remain open (unchanged from PR83B2).
* The industrial profile requires marker-owned ingress acquisition;
  legacy path-trust submissions are refused rather than migrated —
  pre-PR83B3 local rows are not adopted by an industrial runtime.

## Handoff

Next candidates unchanged from the plan: PR83C HA/failover +
backup/restore + measured RPO/RTO (now testable against shared source
and payload artifacts), or vector-index industrialization.
