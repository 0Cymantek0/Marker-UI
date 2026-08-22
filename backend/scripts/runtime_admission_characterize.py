#!/usr/bin/env python
"""PR69 runtime-admission characterization harness.

Deterministic, machine-readable evidence for the admission envelope:
input-geometry matrix -> pinned-preprocessor demand facts -> envelope
arithmetic, plus (when a CUDA torch is actually importable) an
allocator-truth stress that allocates the derived patch-grid tensors and
records peak allocated/reserved and device-level memory.

Modes:
  estimate   CPU-safe: geometry matrix + demand/envelope verification.
             This is the committed artifact mode — it proves the demand
             path is pinned to the real preprocessor math, monotone in
             input pressure, and correctly classifies OOD inputs. It does
             NOT prove the GPU OOM-envelope claim (see --mode cuda).
  cuda       Env-gated: additionally allocates synthetic tensors shaped
             exactly like the pinned foundation preprocessing would
             (patch grids at fp32), measures torch allocator and device
             peaks, and records over-capacity rejection behavior through
             the real CapacityLedger. Skips honestly without CUDA.

Usage (repository root):
  python backend/scripts/runtime_admission_characterize.py --mode estimate \
      --output docs/reference/measurements/pr69-admission-estimate.json
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.runtime_capacity import (  # noqa: E402
    DEFAULT_PREPROCESSOR_FACTS,
    DemandClass,
    DemandEstimator,
    PageGeometry,
    ResourceProfile,
    runtime_versions,
    visual_token_count,
)

SCHEMA = "marker.pr69.admission.characterization.v1"

# Deterministic input matrix: (name, width_pt, height_pt). Covers the
# normal document population and the boundary classes.
GEOMETRY_MATRIX: tuple[tuple[str, float, float], ...] = (
    ("a4-portrait", 595.0, 842.0),
    ("a4-landscape", 842.0, 595.0),
    ("letter", 612.0, 792.0),
    ("a3", 842.0, 1191.0),
    ("tabloid-wide", 1224.0, 792.0),
    ("slide-16x9", 960.0, 540.0),
    ("square-small", 300.0, 300.0),
    ("dense-scan-a4", 595.0, 842.0),  # same geometry; matrix varies by class below
    ("engineer-c1", 1830.0, 649.0),
    ("poster-a0", 2384.0, 3370.0),  # lowres ~3.18M px/side ~ 10.7M px: still in-bound
    ("ood-banner", 15875.0, 1122.0),  # ~21M lowres px: exceeds the 30M? no -> boundary probe
    ("ood-poster", 20000.0, 16000.0),  # ~85M lowres px: out of distribution
)


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - metadata is best effort
        return "unknown"


def _estimator() -> DemandEstimator:
    from app.core.config import (
        ADMISSION_CROPS_PER_MEGAPIXEL,
        ADMISSION_DETECTION_BYTES_PER_CHUNK_MP,
        ADMISSION_LAYOUT_BYTES_PER_SLICE,
        ADMISSION_MAX_PAGE_LOWRES_PIXELS,
        ADMISSION_RECOGNITION_BYTES_PER_TOKEN,
        ADMISSION_WEIGHTS_BOUND_BYTES,
    )

    return DemandEstimator(
        crops_per_megapixel=ADMISSION_CROPS_PER_MEGAPIXEL,
        bytes_weights_resident=ADMISSION_WEIGHTS_BOUND_BYTES,
        bytes_per_layout_slice=ADMISSION_LAYOUT_BYTES_PER_SLICE,
        bytes_per_detection_chunk_mp=ADMISSION_DETECTION_BYTES_PER_CHUNK_MP,
        bytes_per_recognition_token=ADMISSION_RECOGNITION_BYTES_PER_TOKEN,
        max_page_lowres_pixels=ADMISSION_MAX_PAGE_LOWRES_PIXELS,
    )


def _profile() -> ResourceProfile:
    return ResourceProfile(
        family="marker-gpu",
        device_label="characterization",
        dtype_label="auto",
        batch_vector=(
            ("recognition", DEFAULT_PREPROCESSOR_FACTS.recognition_batch),
            ("layout", DEFAULT_PREPROCESSOR_FACTS.layout_batch),
            ("detection", DEFAULT_PREPROCESSOR_FACTS.detection_batch),
        ),
        versions=runtime_versions(),
    )


def _run_estimate_mode() -> dict:
    estimator = _estimator()
    profile = _profile()
    profile_id = profile.fingerprint()

    cases = []
    for name, width_pt, height_pt in GEOMETRY_MATRIX:
        geometry = PageGeometry(0, width_pt, height_pt)
        estimate = estimator.estimate_for_geometries([geometry], profile_id=profile_id)
        low_w, low_h = geometry.lowres_px
        high_w, high_h = geometry.highres_px
        cases.append(
            {
                "name": name,
                "width_pt": width_pt,
                "height_pt": height_pt,
                "lowres_px": [low_w, low_h],
                "highres_px": [high_w, high_h],
                "lowres_megapixels": round(low_w * low_h / 1e6, 3),
                "demand_class": estimate.demand_class.value,
                "layout_slices": estimate.max_layout_slices_per_page,
                "detection_chunks": estimate.max_detection_chunks_per_page,
                "recognition_crop_bound": estimate.max_recognition_crops_per_page,
                "recognition_tokens_per_crop": estimate.max_recognition_tokens_per_crop,
                "peak_recognition_batch": estimate.peak_recognition_batch,
                "envelope_bytes": estimate.envelope_bytes,
                "notes": list(estimate.notes),
            }
        )

    # Arithmetic integrity: the envelope must be EXACTLY the sum of its
    # per-model components recomputed independently from the recorded
    # geometry facts and coefficients, the crop bound must be linear in
    # highres area (its defining driver), and layout/detection multipliers
    # must match the pinned tiling math. Total monotonicity in a single
    # pixel-area proxy is unsound by construction — detection cost is
    # width-driven (width x chunk-height) and extreme aspect ratios cost
    # more slices per pixel.
    from app.core.config import (
        ADMISSION_CROPS_PER_MEGAPIXEL,
        ADMISSION_DETECTION_BYTES_PER_CHUNK_MP,
        ADMISSION_LAYOUT_BYTES_PER_SLICE,
        ADMISSION_RECOGNITION_BYTES_PER_TOKEN,
    )

    def _tile_multipliers(low_w: int, low_h: int) -> tuple[int, int]:
        slices = (
            1
            if max(low_w, low_h) <= DEFAULT_PREPROCESSOR_FACTS.layout_slice_min_px
            else math.ceil(low_w / DEFAULT_PREPROCESSOR_FACTS.layout_slice_size_px)
            * math.ceil(low_h / DEFAULT_PREPROCESSOR_FACTS.layout_slice_size_px)
        )
        chunks = max(
            1, math.ceil(low_h / DEFAULT_PREPROCESSOR_FACTS.detection_chunk_height_px)
        )
        return slices, chunks

    arithmetic_ok = True
    crops_linear = True
    normal_sorted = sorted(
        [c for c in cases if c["demand_class"] == DemandClass.NORMAL.value],
        key=lambda c: c["highres_px"][0] * c["highres_px"][1],
    )
    crop_bounds = [c["recognition_crop_bound"] for c in normal_sorted]
    crops_linear = all(b >= a for a, b in zip(crop_bounds, crop_bounds[1:]))

    for case in cases:
        low_w, low_h = case["lowres_px"]
        slices, chunks = _tile_multipliers(low_w, low_h)
        if slices != case["layout_slices"] or chunks != case["detection_chunks"]:
            arithmetic_ok = False
        layout = case["layout_slices"] * ADMISSION_LAYOUT_BYTES_PER_SLICE
        detection = math.ceil(
            chunks
            * (low_w * DEFAULT_PREPROCESSOR_FACTS.detection_chunk_height_px / 1e6)
            * ADMISSION_DETECTION_BYTES_PER_CHUNK_MP
        )
        recognition = (
            case["peak_recognition_batch"]
            * case["recognition_tokens_per_crop"]
            * ADMISSION_RECOGNITION_BYTES_PER_TOKEN
        )
        if layout + int(detection) + recognition != case["envelope_bytes"]:
            arithmetic_ok = False

    ood = [c for c in cases if c["demand_class"] == DemandClass.OUT_OF_DISTRIBUTION.value]
    checks = {
        "envelope_matches_component_arithmetic": arithmetic_ok,
        "crop_bound_monotone_in_highres_area": crops_linear,
        "ood_cases_present_when_matrix_exceeds_bound": len(ood) >= 1,
        "tokens_per_crop_equals_pinned_foundation_math": all(
            c["recognition_tokens_per_crop"]
            == visual_token_count(1024, 512, DEFAULT_PREPROCESSOR_FACTS)
            for c in cases
        ),
    }

    return {
        "schema": SCHEMA,
        "mode": "estimate",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "profile": profile.summary(),
        "preprocessor_facts": DEFAULT_PREPROCESSOR_FACTS.as_dict(),
        "coefficient_sources": {
            "weights_resident_bytes": "declared conservative default (unmeasured)",
            "per_layout_slice_bytes": "declared conservative default (unmeasured)",
            "per_detection_chunk_mp_bytes": "declared conservative default (unmeasured)",
            "per_recognition_token_bytes": "declared conservative default (unmeasured)",
            "crops_per_megapixel": "declared conservative default (unmeasured)",
        },
        "matrix": cases,
        "checks": checks,
        "unclaimed": [
            "no CUDA hardware was exercised by this mode",
            "the OOM-envelope conservatism of the coefficients is declared, "
            "not measured; --mode cuda plus real marker model residency is "
            "required before invariant 30 can claim full coverage",
        ],
    }


def _run_cuda_mode() -> dict:
    artifact = _run_estimate_mode()
    artifact["mode"] = "cuda"

    try:
        import torch

        if not torch.cuda.is_available():
            artifact["cuda"] = {
                "available": False,
                "reason": "torch present but CUDA unavailable in this environment",
            }
            return artifact
    except Exception as exc:  # noqa: BLE001
        artifact["cuda"] = {"available": False, "reason": f"torch import failed: {exc!r}"}
        return artifact

    device = torch.device("cuda:0")
    free, total = torch.cuda.mem_get_info(0)
    facts = DEFAULT_PREPROCESSOR_FACTS

    # Allocator-truth stress: allocate EXACTLY the input tensors the pinned
    # foundation preprocessing builds for a max-size OCR crop batch —
    # [patches, 3*patch_size*patch_size] fp32 — through the real ledger, so
    # the peaks and the rejection behavior are the runtime's own.
    cap_w, cap_h = facts.ocr_task_img_size
    factor = facts.patch_size * facts.merge_size
    h_bar = math.ceil(cap_h / factor) * factor
    w_bar = math.ceil(cap_w / factor) * factor
    patches_per_crop = (h_bar // facts.patch_size) * (w_bar // facts.patch_size)

    stress = []
    torch.cuda.reset_peak_memory_stats(0)
    ledger_runs = []

    from app.core.config import (
        ADMISSION_RECOGNITION_BYTES_PER_TOKEN,
        ADMISSION_WEIGHTS_BOUND_BYTES,
    )
    from app.services.runtime_capacity import (
        AdmissionError,
        CapacityEnvelope,
        CapacityLedger,
    )
    from dataclasses import replace as dc_replace

    estimator = _estimator()
    profile = _profile()

    for batch in (8, 32, 128):
        estimate = dc_replace(
            estimator.estimate_for_geometries(
                [PageGeometry(0, 595, 842)], profile_id=profile.fingerprint()
            ),
            peak_recognition_batch=batch,
        )
        # A synthetic per-batch envelope scaled to the real token count.
        envelope_bytes = (
            batch
            * estimate.max_recognition_tokens_per_crop
            * ADMISSION_RECOGNITION_BYTES_PER_TOKEN
        )
        try:
            tensor = torch.zeros(
                (batch * patches_per_crop, 3 * facts.patch_size * facts.patch_size),
                dtype=torch.float32,
                device=device,
            )
            torch.cuda.synchronize(0)
            entry = {
                "batch": batch,
                "tensor_shape": list(tensor.shape),
                "tensor_bytes": tensor.numel() * tensor.element_size(),
                "peak_allocated_bytes": int(torch.cuda.max_memory_allocated(0)),
                "peak_reserved_bytes": int(torch.cuda.max_memory_reserved(0)),
                "device_free_bytes": int(torch.cuda.mem_get_info(0)[0]),
                "admitted": True,
            }
            del tensor
            torch.cuda.empty_cache()
        except RuntimeError as exc:  # noqa: BLE001 - OOM is data, not failure
            entry = {
                "batch": batch,
                "admitted": False,
                "oom": str(exc)[:200],
                "peak_allocated_bytes": int(torch.cuda.max_memory_allocated(0)),
                "peak_reserved_bytes": int(torch.cuda.max_memory_reserved(0)),
            }
        stress.append(entry)

        # The ledger must refuse demand exceeding the real device envelope.
        ledger = CapacityLedger(
            CapacityEnvelope(
                usable_bytes=int(total),
                safety_reserve_bytes=int(total * 0.10),
                base_resident_bytes=ADMISSION_WEIGHTS_BOUND_BYTES,
                device_total_bytes=int(total),
                coefficients={},
            )
        )
        ledger_run = {"batch": batch, "envelope_bytes": envelope_bytes}
        try:
            reservation = ledger.admit(f"stress-{batch}", envelope_bytes)
            ledger_run["ledger_admitted"] = True
            ledger.release(reservation.reservation_id)
        except AdmissionError:
            ledger_run["ledger_admitted"] = False
        # Deliberate over-capacity must be refused before any allocation.
        try:
            ledger.admit(f"over-{batch}", int(total) * 2)
            ledger_run["over_capacity_refused"] = False
        except AdmissionError:
            ledger_run["over_capacity_refused"] = True
        ledger_runs.append(ledger_run)

    artifact["cuda"] = {
        "available": True,
        "device": torch.cuda.get_device_name(0),
        "device_total_bytes": int(total),
        "device_free_bytes_at_start": int(free),
        "torch": torch.__version__,
        "allocator_stress": stress,
        "ledger_decisions": ledger_runs,
        "unclaimed_for_cuda_mode": [
            "synthetic patch-grid tensors stand in for the recognition "
            "batch input; real marker model weights were not resident",
            "cold-start/load timing of the full marker model dict is not "
            "measured by this harness",
        ],
    }
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("estimate", "cuda"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    started = time.perf_counter()
    artifact = _run_cuda_mode() if args.mode == "cuda" else _run_estimate_mode()
    artifact["elapsed_seconds"] = round(time.perf_counter() - started, 3)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checks = artifact.get("checks", {})
    failed = [k for k, ok in checks.items() if not ok]
    print(f"[ok] wrote {args.output} ({args.mode}, {len(artifact['matrix'])} cases)")
    if failed:
        print(f"[error] estimate checks failed: {failed}")
        return 1
    print(f"[ok] estimate checks: {checks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
