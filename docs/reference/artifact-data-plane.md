# Local Artifact Data Plane (PR68A)

**Status:** active (default on) · **Scope:** local process-worker result handoff · **Parent plan:** V3.2 PR68, Runtime/Data Plane lane · **Module:** `backend/app/services/artifact_handles.py`

## What this is

A versioned, verified file-reference seam that moves large immutable
conversion-result fields out of the pickled `WorkerEvent.payload` control
message. The process worker stages eligible fields (long text, image bytes,
pickled PIL images, asset byte payloads) into a store-managed blob, and emits a
compact wire envelope whose handles carry `{slot, kind, encoding, name, length,
sha256, job_id}`. The parent's drain thread resolves each handle — read, verify
length and SHA-256, decode, re-insert — before the existing `_finalize_job`
contract runs, so finalization sees exactly the logical result it saw before
PR68A.

What it is **not**: not a second truth authority (the Truth Kernel PR64 payload
store remains the only durable blob authority), not a distributed transport,
not a tensor ABI. The handle is a way to locate and verify bytes; the
conversion result remains the only logical truth.

## Wire contract

```
WorkerEvent.payload = {
  "artifact_handles": {
    "version": 1,
    "inline": <the payload with eligible leaves removed>,
    "handles": [ {slot, kind, encoding, name, length, sha256, job_id}, ... ]
  }
}
```

* `slot` — key/index path from the payload root to the field
  (`("result", "images", "p3.png")`, `("formats_payload", "html", "text")`,
  `("result", "assets", 0, "data")`).
* `kind` — `text | image_bytes | image_pil | asset_data | asset_pil` (decides
  re-insertion shape).
* `encoding` — `utf8` (text), `raw` (bytes), `pickle` (PIL objects, and the
  lone-surrogate text fallback).
* `name` — `uuid4().hex`; the only string allowed to become a path component.
  Strict hex validation plus an `os.path.abspath`-based containment check make
  root escape impossible; the containment check never touches the filesystem
  (a `Path.resolve()`-based check flipped forms across the directory-create
  boundary on Windows and false-rejected concurrent stagers).
* Envelope version mismatch, unknown kind/encoding, malformed slot, non-hex
  name, bad digest format, or a `job_id` different from the event's job are
  each rejected before any attach.
* A payload without the wire key is a classic inline payload and passes
  through untouched — the pre-PR68A contract is a strict subset.

## Lifecycle and ownership

* **Ephemeral by design.** Blobs are written with flush-and-close but no fsync
  (an `fsync=True` store profile exists for characterization). Rationale: a
  crash kills both producer and consumer of the handoff and the job is retried
  from source; a machine-level power loss can lose an unconsumed blob, which
  surfaces as an honest failure, never as wrong bytes.
* **Unique names, no dedup.** Every stage creates a fresh `uuid4().hex` blob.
  Physical dedup would let one job's consume-unlink race another job sharing
  the same bytes; unique names make premature deletion structurally impossible
  and keep evidence identities distinct.
* **One consumer per handoff.** `consume()` = read + verify + unlink. Duplicate
  delivery finds the blob missing and fails closed instead of rebuilding
  anything twice; retries converge (no multiplying permanent resources).
* **Orphan reclamation.** Producer crash mid-stage, consumer crash before
  consume, and cancelled jobs leave blobs that an age-based sweep removes —
  at parent startup (before the drain loop runs) and after every 25 process
  results. The sweep only unlinks blobs older than
  `MARKER_ARTIFACT_HANDLE_SWEEP_SECONDS` (default 3600), which comfortably
  exceeds the seconds-scale stage→consume window, so it can never race a live
  reader.
* **Unlink failure is observable** (`failed_unlinks` counter + warning log),
  never silently treated as successful cleanup; the sweep is the backstop.

## Failure semantics (asymmetric by design)

* **Producer degrades, consumer is strict.** If staging fails (disk full,
  store error, any unexpected exception) the worker keeps remaining fields
  inline and emits a truthful mixed envelope — or a pure inline payload when
  nothing staged. The data plane can never fail a conversion.
* **Consumer fails closed.** Missing, truncated, tampered (hash mismatch),
  oversized, cross-job, or version-incompatible handles raise
  `ArtifactHandleError`; `TaskManager._finalize_proc_job` routes that to the
  failure path, so a broken handoff can never produce a completed job built
  from wrong bytes.
* **Bounds.** Reads are capped at `MARKER_ARTIFACT_HANDLE_MAX_BYTES`
  (default 1 GiB) before any I/O. Slow or crashed consumers cannot create
  producer-side growth: the producer writes once and never blocks on
  consumption; leftovers are bounded by the sweep.

## Configuration

| Knob | Default | Meaning |
|---|---|---|
| `MARKER_ARTIFACT_HANDLES` | `true` | Kill switch; `false` restores pure queue-inline transport |
| `MARKER_ARTIFACT_HANDLE_ROOT` | `data/artifact_handles` | Store root (same value must be visible to workers and parent) |
| `MARKER_ARTIFACT_HANDLE_INLINE_LIMIT` | `1048576` | Per-field encoded-size cutoff for handle eligibility |
| `MARKER_ARTIFACT_HANDLE_SWEEP_SECONDS` | `3600` | Orphan sweep age threshold |
| `MARKER_ARTIFACT_HANDLE_MAX_BYTES` | `1073741824` | Hard single-read bound |

The thread backend is untouched: handles exist only on the process-worker
result path. Progress/log/status/error events stay queue-inline forever —
the seam is not a new IPC platform.

## Measured behavior and the promotion decision

Evidence: `backend/scripts/artifact_dataplane_benchmark.py`
(reproducible; spawn start method; reports under
`docs/reference/measurements/`). Environment: Windows 11, Python 3.11.9,
12 logical CPUs, single NVMe with real-time antivirus active.

Single producer (`pr68a-full-comparison.json`), per-result p50:

| Lane | 256 KiB | 4 MiB | 32 MiB | Control bytes @32 MiB |
|---|---|---|---|---|
| queue_inline (baseline) | 17 ms | 101 ms | 820 ms | 47.7 MiB |
| file_handle | 26 ms | 188 ms | 1562 ms | ~0 |
| file_handle_fsync | 30 ms | 320 ms | 1591 ms (p95 4008 ms) | ~0 |
| shared_memory | 19 ms | 116 ms | 815 ms | name only |

Four concurrent producers sharing one parent queue
(`pr68a-concurrent-4-workers.json`), per-result p50 at 32 MiB:
queue_inline 541 ms vs file_handle 1291 ms.

Findings and the resulting decisions:

1. **Pickle serialization dominates, not the pipe.** The shared-memory lane
   ties queue-inline (815 vs 820 ms p50) because both pickle the full object
   graph; moving the copy off the pipe buys nothing. **Shared memory is
   rejected** — no measured benefit, plus `unlink`/resource-tracker lifecycle
   complexity that is genuinely painful on Windows. It remains a benchmark
   lane, not production code.
2. **The file-backed handle is promoted as the default seam** — not for raw
   latency (it is ~1.9–2.4× slower than inline at 32 MiB on this machine,
   disk + antivirus bound) but because it is the simplest mechanism that
   satisfies the V3.2 readiness requirement with a compact verified control
   channel (47.7 MiB → ~0 through the one shared queue per 32 MiB result),
   bounded verified lifecycle, and crash-truthful cleanup. Queue head-of-line
   blocking by one giant pickle and transient parent heap spikes disappear on
   the handle path.
3. **Small payloads stay inline.** At ≤4 MiB results, inline is both faster
   and simpler, and its control-byte cost is insignificant. The default
   per-field cutoff is therefore 1 MiB: typical page images and texts stay
   inline; genuinely large fields (heavy scans, huge texts, big assets)
   travel by handle where control-channel relief matters.
4. **No-fsync ephemeral profile is the default.** The fsync variant adds
   measurable cost (p95 4 s at 32 MiB under concurrency) and durability is
   not part of this handoff's truth contract.
5. **mmap was not implemented as a production lane.** The consumer needs the
   full bytes anyway (hash + unpickle), so page-cache-backed mapping saves no
   first-read I/O versus `read_bytes`; it adds platform-specific handle and
   alignment behavior. Rejected by analysis; revisit only if a future consumer
   streams partial views.

What would change these decisions: rerun the benchmark on target hardware
(.fast local disk, antivirus exclusions, Linux). If file_handle p50 lands
within ~1.2× of queue_inline at ≥32 MiB, the current defaults stand; if it
stays 2×+ slower AND realistic result distributions rarely exceed 1 MiB
fields, raise `MARKER_ARTIFACT_HANDLE_INLINE_LIMIT` (or set
`MARKER_ARTIFACT_HANDLES=false`) until hardware catches up. The rollback
surface is exactly one boundary — the kill switch — with no output-contract
change either way.

## Windows notes

* Spawn start method: every worker builds its store from the same environment,
  which is what makes the file reference resolvable across the boundary.
* Blob writes use exclusive create (`xb`); partial writes are detected by an
  on-disk size check and refused honestly.
* Path derivation avoids `Path.resolve()` (existing vs not-yet-existing paths
  realize to different forms, which raced concurrent creators during
  testing); `os.path.abspath` + strict hex name validation is race-free.
* The sweep's age threshold makes it safe against readers that hold no open
  descriptors — consumers read-then-unlink within one call.

## Evidence index

* Contract/tamper/concurrency/leak/crash-window tests:
  `backend/tests/test_artifact_handles.py` (63 tests, incl. a real
  spawn-process handoff).
* Worker emission + fallback tests: `backend/tests/test_gpu_worker.py`.
* Parent resolution + fail-closed tests: `backend/tests/test_task_manager.py`.
* Benchmarks: `docs/reference/measurements/pr68a-baseline-queue-inline.json`,
  `pr68a-full-comparison.json`, `pr68a-concurrent-4-workers.json`.
