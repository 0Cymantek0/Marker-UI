# PR69 — Runtime admission and active model leases

Resource-aware admission and model-residency safety for the Marker GPU
runtime family: pre-execution demand from the pinned preprocessor,
atomic capacity reservation, active model execution leases, safe unload
transitions, OOM containment with profile feedback, and caller-visible
cold/warm/queue cost. Governing V3.2 targets: invariants 30 and 31.

## The resource lifecycle

```
eligible work (kernel claim/dispatch — the ONLY durable authority)
  -> runtime profile + demand estimate (pinned preprocessor math)
  -> admission: capacity reservation + model execution lease (atomic)
  -> execution (converter entered ONLY past this gate)
  -> release: reservation + lease settled on every terminal path
  -> capacity/health state updated (observations, OOM feedback)
```

Nothing here is a second job-truth authority: authorization, fair claim,
fencing, and accepted publication remain the kernel runtime's. Admission
answers exactly one question — can THIS runtime instance safely start
THIS scarce execution now.

## Where each piece lives

| Concern | Owner |
|---|---|
| Profile identity, demand math, ledger, leases, OOM feedback | `backend/app/services/runtime_capacity.py` |
| Worker-side gate (converter entry, runtime events) | `backend/app/services/gpu_worker.py` |
| Residency observations, safe `release_models`, OOM retry hook | `backend/app/services/marker_service.py` |
| Thread-backend gate, ticket ownership, status surfacing | `backend/app/services/task_manager.py` |
| Configuration knobs | `backend/app/core/config.py` (`MARKER_ADMISSION*`) |

## What the demand estimate is pinned to

The estimator replicates the pinned runtime's own arithmetic (verified
against the locked environment, marker-pdf 1.10.0 + surya-ocr 0.17.x):

- marker renders pages at 96 DPI (layout + line detection) and 192 DPI
  (OCR crops) — `DocumentBuilder.lowres_image_dpi` / `highres_image_dpi`;
- page geometry is read metadata-only via pypdfium2 (no render, no
  models) before the converter is entered;
- the recognition crop cap is the foundation OCR task image size
  (1024x512); token counts use the patch/merge grid (patch 14, merge 2,
  round-up to 28 px multiples), exactly mirroring the foundation
  predictor's own math;
- layout slicing uses `LAYOUT_SLICE_MIN`/`LAYOUT_SLICE_SIZE` (1500/1200);
- detection chunking uses `DETECTOR_IMAGE_CHUNK_HEIGHT` (1400);
- the per-page line-crop COUNT is the detection model's output and cannot
  be known pre-execution, so it is bounded by a conservative
  crops-per-highres-megapixel coefficient (declared, not measured).

Unknown or out-of-distribution inputs never ride the normal
high-throughput class: they take the declared exclusive safe path
(serialized against the whole activation budget) or are rejected by
policy. Missing files, non-marker converter kinds, and documents whose
geometry cannot be parsed skip admission entirely — there is either no
execution to admit or the converter fails truthfully before any GPU
allocation.

## What is measured vs declared

**Measured (executable evidence):**

- the full test suites in `tests/test_runtime_capacity.py`,
  `tests/test_runtime_capacity_races.py`, and
  `tests/test_runtime_admission_integration.py` (deterministic demand,
  races, worker-seam integration, OOM injection);
- the committed characterization artifact
  `docs/reference/measurements/pr69-admission-estimate.json`
  (regenerate with the command below): a 12-case geometry matrix with
  independently recomputed envelope arithmetic, crop-bound linearity,
  OOD classification, and the pinned foundation token math.

**Declared conservative (unmeasured) until a CUDA characterization run
lands on real hardware:**

- `ADMISSION_WEIGHTS_BOUND_BYTES` (shared model residency, 3 GiB),
- `ADMISSION_LAYOUT_BYTES_PER_SLICE`, per-slice layout activation,
- `ADMISSION_DETECTION_BYTES_PER_CHUNK_MP`, detection activation,
- `ADMISSION_RECOGNITION_BYTES_PER_TOKEN` (24 KiB), recognition
  activation per visual token,
- `ADMISSION_CROPS_PER_MEGAPIXEL` (250), the crop-count bound.

These defaults overestimate rather than underestimate: on a 6 GB card
the declared envelope refuses dense full-batch A4 work — which today is
exactly the population the after-the-fact OOM retry loop catches. A
`--mode cuda` run of the characterization harness on real hardware
measures allocator/device peaks for the derived patch grids and tightens
the coefficients per profile; until then invariant 30's readiness claim
stays deliberately partial.

## Model leases and safe unload

Model residency is a generation with execution ownership:

- every admitted execution holds a lease on the current generation;
- while any lease is active, that generation cannot be unloaded
  (the drain waits, or refuses on timeout — it never evicts);
- draining blocks NEW leases for the generation (no late attach);
- a borrower may voluntarily surrender ITS OWN lease through the
  explicit unload protocol (the hybrid-OCR low-VRAM phase) — never
  another borrower's;
- a crashed worker's leases die with its process; kernel fencing and
  recovery reconcile the work, no cross-process lease resurrection
  exists (deliberately).

`MarkerService.release_models()` is the safe unload transition when a
coordinator is attached, and keeps its legacy behavior otherwise.

## What callers see

Structured `WorkerEventType.runtime` events (and thread-backend status)
now distinguish: admitted (with demand class + envelope), admission
refused (with reason), cold model load vs warm reuse vs unload (with
elapsed seconds), and OOM containment. `TaskManager.get_status()`
exposes the latest observation as `runtime`, and status text says why
work is waiting instead of a generic "processing".

## Reproduction

```bash
# deterministic estimate-mode artifact (CPU-safe, committed)
python backend/scripts/runtime_admission_characterize.py --mode estimate \
  --output docs/reference/measurements/pr69-admission-estimate.json

# env-gated CUDA allocator-stress (skips honestly without CUDA torch)
python backend/scripts/runtime_admission_characterize.py --mode cuda \
  --output /tmp/pr69-admission-cuda.json

# focused suites
python -m pytest backend/tests/test_runtime_capacity.py \
  backend/tests/test_runtime_capacity_races.py \
  backend/tests/test_runtime_admission_integration.py
```

## Explicitly unclaimed after this session

- No real-CUDA dynamic-resolution OOM-envelope stress has been executed;
  invariant 30 is NOT fully proven by this session's evidence.
- Cold-start/load timing of the full marker model dict on real hardware
  is observed structurally (events + profile observations) but not
  measured into a committed artifact.
- Multi-node or remote-provider capacity, learned routing, and the
  economics cluster (invariants 57-62) are untouched; the observations
  exported here are the inputs that cluster will need.
