"""Resource-aware admission and active model leases for the Marker GPU runtime (PR69).

This module owns the ephemeral device-capacity truth for one execution
context (a pinned GPU worker process, or the parent thread backend):

* ``DemandEstimator`` derives pre-execution demand from the SAME pinned
  preprocessing semantics the converter will use (marker 1.10 render DPIs +
  surya 0.17 scale/patch/merge math), never from page count alone.
* ``CapacityLedger`` reserves usable capacity atomically before scarce
  execution; concurrent admissions can never collectively overcommit.
* ``ResidencyLeaseRegistry`` makes model residency a generation with
  execution ownership: while a lease is active, that generation cannot be
  unloaded underneath the borrower, and draining blocks new leases.
* ``RuntimeCapacityCoordinator`` ties admission, leases, cold/warm
  observations, and unexpected-OOM feedback into one lifecycle:
  eligible work -> profile/demand -> admission/reservation -> model lease ->
  execution -> release -> capacity update.

Durable work ownership (authorization, claims, fencing, publication) stays
with the kernel runtime; nothing here is a second job-truth authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pinned preprocessor facts (marker-pdf 1.10.0 + sura-ocr 0.17.x, verified
# against the locked environment; changing either dependency requires
# re-verifying every constant below).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PinnedPreprocessorFacts:
    """Preprocessing constants that materially change visual-token demand."""

    # marker.builders.document.DocumentBuilder render DPIs
    lowres_dpi: int = 96   # layout + line detection images
    highres_dpi: int = 192  # OCR line-crop images
    # sura.settings: DETECTOR_IMAGE_CHUNK_HEIGHT (vertical slicing)
    detection_chunk_height_px: int = 1400
    # sura.settings: LAYOUT_SLICE_MIN / LAYOUT_SLICE_SIZE / LAYOUT_IMAGE_SIZE
    layout_slice_min_px: int = 1500
    layout_slice_size_px: int = 1200
    layout_image_size_px: int = 768
    # surya FoundationPredictor.tasks[...]["img_size"] caps (max_width, max_height)
    foundation_task_img_sizes: tuple[tuple[int, int], ...] = (
        (1024, 512),   # ocr_with_boxes / ocr_without_boxes / block_without_boxes
        (1024, 1024),  # layout
        (1024, 512),   # table_structure
    )
    # OCR line crops are capped by the recognition task image size.
    ocr_task_img_size: tuple[int, int] = (1024, 512)
    # surya processor scale_to_fit lower bound
    scale_min_px: tuple[int, int] = (168, 168)
    # surya.common.surya.encoder.config defaults
    patch_size: int = 14
    merge_size: int = 2
    # surya default batch sizes on cuda (foundation / layout / detection)
    recognition_batch: int = 256
    layout_batch: int = 32
    detection_batch: int = 32

    def as_dict(self) -> dict[str, Any]:
        return {
            "lowres_dpi": self.lowres_dpi,
            "highres_dpi": self.highres_dpi,
            "detection_chunk_height_px": self.detection_chunk_height_px,
            "layout_slice_min_px": self.layout_slice_min_px,
            "layout_slice_size_px": self.layout_slice_size_px,
            "layout_image_size_px": self.layout_image_size_px,
            "foundation_task_img_sizes": [list(s) for s in self.foundation_task_img_sizes],
            "ocr_task_img_size": list(self.ocr_task_img_size),
            "scale_min_px": list(self.scale_min_px),
            "patch_size": self.patch_size,
            "merge_size": self.merge_size,
            "recognition_batch": self.recognition_batch,
            "layout_batch": self.layout_batch,
            "detection_batch": self.detection_batch,
        }


DEFAULT_PREPROCESSOR_FACTS = PinnedPreprocessorFacts()


def scaled_size(width: int, height: int, max_size: tuple[int, int], min_size: tuple[int, int]) -> tuple[int, int]:
    """Replicate surya processor ``scale_to_fit`` (area-preserving bounds).

    Same floor-on-downscale / ceil-on-upscale arithmetic as the pinned
    ``SuryaOCRProcessor.scale_to_fit`` so admission sees exactly the pixel
    geometry the runtime will build tensors for.
    """
    if width <= 0 or height <= 0:
        return width, height
    max_w, max_h = max_size
    min_w, min_h = min_size
    current = width * height
    if current > max_w * max_h:
        scale = (max_w * max_h / current) ** 0.5
        return math.floor(width * scale), math.floor(height * scale)
    if current < min_w * min_h:
        scale = (min_w * min_h / current) ** 0.5
        return math.ceil(width * scale), math.ceil(height * scale)
    return width, height


def visual_token_count(width: int, height: int, facts: PinnedPreprocessorFacts) -> int:
    """Replicate the pinned foundation token math for one image.

    Mirrors ``FoundationPredictor``: round up to a multiple of
    ``patch_size * merge_size``, build the patch grid, divide by
    ``merge_size**2``. For task-capped inputs callers pass the already
    scaled dimensions.
    """
    factor = facts.patch_size * facts.merge_size
    h_bar = math.ceil(height / factor) * factor
    w_bar = math.ceil(width / factor) * factor
    grid_h = h_bar // facts.patch_size
    grid_w = w_bar // facts.patch_size
    return (grid_h * grid_w) // (facts.merge_size**2)


# ---------------------------------------------------------------------------
# Demand estimation
# ---------------------------------------------------------------------------

class DemandClass(str, Enum):
    NORMAL = "normal"
    UNKNOWN = "unknown"
    OUT_OF_DISTRIBUTION = "out_of_distribution"


@dataclass(frozen=True)
class PageGeometry:
    page_number: int
    width_pt: float
    height_pt: float

    @property
    def lowres_px(self) -> tuple[int, int]:
        w = self.width_pt * DEFAULT_PREPROCESSOR_FACTS.lowres_dpi / 72.0
        h = self.height_pt * DEFAULT_PREPROCESSOR_FACTS.lowres_dpi / 72.0
        return max(1, math.ceil(w)), max(1, math.ceil(h))

    @property
    def highres_px(self) -> tuple[int, int]:
        w = self.width_pt * DEFAULT_PREPROCESSOR_FACTS.highres_dpi / 72.0
        h = self.height_pt * DEFAULT_PREPROCESSOR_FACTS.highres_dpi / 72.0
        return max(1, math.ceil(w)), max(1, math.ceil(h))


@dataclass(frozen=True)
class DemandEstimate:
    """Pre-execution demand facts derived from the pinned preprocessor."""

    demand_class: DemandClass
    page_count: int
    max_layout_slices_per_page: int
    max_detection_chunks_per_page: int
    max_recognition_crops_per_page: int
    max_recognition_tokens_per_crop: int
    peak_recognition_batch: int
    envelope_bytes: int
    profile_id: str
    notes: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "demand_class": self.demand_class.value,
            "page_count": self.page_count,
            "max_layout_slices_per_page": self.max_layout_slices_per_page,
            "max_detection_chunks_per_page": self.max_detection_chunks_per_page,
            "max_recognition_crops_per_page": self.max_recognition_crops_per_page,
            "max_recognition_tokens_per_crop": self.max_recognition_tokens_per_crop,
            "peak_recognition_batch": self.peak_recognition_batch,
            "envelope_bytes": self.envelope_bytes,
            "profile_id": self.profile_id,
            "notes": list(self.notes),
        }


def read_pdf_page_geometries(filepath: str | Path, max_pages: int = 10_000) -> list[PageGeometry]:
    """Read page sizes from PDF metadata without rendering or models.

    Uses pypdfium2 (already a marker dependency) in metadata-only mode; the
    cheapness is what makes pre-execution admission viable.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(filepath))
    try:
        geometries: list[PageGeometry] = []
        for index in range(min(len(doc), max_pages)):
            page = doc[index]
            w, h = page.get_size()
            geometries.append(PageGeometry(page_number=index, width_pt=float(w), height_pt=float(h)))
        return geometries
    finally:
        doc.close()


class DemandEstimator:
    """Turns input facts into a demand estimate for one runtime profile."""

    def __init__(
        self,
        *,
        facts: PinnedPreprocessorFacts = DEFAULT_PREPROCESSOR_FACTS,
        crops_per_megapixel: float,
        bytes_weights_resident: int,
        bytes_per_layout_slice: int,
        bytes_per_detection_chunk_mp: int,
        bytes_per_recognition_token: int,
        max_page_lowres_pixels: int,
    ) -> None:
        self.facts = facts
        self.crops_per_megapixel = crops_per_megapixel
        self.bytes_weights_resident = bytes_weights_resident
        self.bytes_per_layout_slice = bytes_per_layout_slice
        self.bytes_per_detection_chunk_mp = bytes_per_detection_chunk_mp
        self.bytes_per_recognition_token = bytes_per_recognition_token
        self.max_page_lowres_pixels = max_page_lowres_pixels

    # -- geometry -> per-model multipliers ---------------------------------

    def layout_slices(self, lowres_px: tuple[int, int]) -> int:
        w, h = lowres_px
        f = self.facts
        if max(w, h) <= f.layout_slice_min_px:
            return 1
        cols = math.ceil(w / f.layout_slice_size_px)
        rows = math.ceil(h / f.layout_slice_size_px)
        return cols * rows

    def detection_chunks(self, lowres_px: tuple[int, int]) -> int:
        _, h = lowres_px
        return max(1, math.ceil(h / self.facts.detection_chunk_height_px))

    def recognition_crop_bound(self, highres_px: tuple[int, int]) -> int:
        """Conservative crop-count bound for a page.

        The true count is data-dependent (it is the detection model's
        output), so admission bounds it with a documented conservative
        crops-per-megapixel coefficient instead of guessing optimistically.
        """
        w, h = highres_px
        return int(math.ceil((w * h) / 1_000_000.0 * self.crops_per_megapixel))

    def max_tokens_per_crop(self) -> int:
        """Token ceiling for one OCR crop at the pinned recognition cap."""
        f = self.facts
        cap_w, cap_h = f.ocr_task_img_size
        sw, sh = scaled_size(cap_w, cap_h, (cap_w, cap_h), f.scale_min_px)
        return visual_token_count(sw, sh, f)

    # -- full estimate ------------------------------------------------------

    def estimate_for_geometries(
        self,
        geometries: Iterable[PageGeometry],
        *,
        profile_id: str,
        ocr_enabled: bool = True,
    ) -> DemandEstimate:
        geometries = list(geometries)
        notes: list[str] = []
        max_slices = 1
        max_chunks = 1
        max_crops = 0
        max_lowres_pixels = 0
        for geo in geometries:
            low = geo.lowres_px
            high = geo.highres_px
            max_slices = max(max_slices, self.layout_slices(low))
            max_chunks = max(max_chunks, self.detection_chunks(low))
            max_crops = max(max_crops, self.recognition_crop_bound(high))
            max_lowres_pixels = max(max_lowres_pixels, low[0] * low[1])

        demand_class = DemandClass.NORMAL
        if not geometries:
            demand_class = DemandClass.UNKNOWN
            notes.append("page geometry unavailable")
        elif max_lowres_pixels > self.max_page_lowres_pixels:
            demand_class = DemandClass.OUT_OF_DISTRIBUTION
            notes.append(
                f"page pixels {max_lowres_pixels} exceed characterized bound "
                f"{self.max_page_lowres_pixels}"
            )

        tokens_per_crop = self.max_tokens_per_crop()
        batch_cap = self.facts.recognition_batch
        peak_batch = min(max_crops, batch_cap) if ocr_enabled else 0

        layout_bytes = max_slices * self.bytes_per_layout_slice
        detection_bytes = max(
            self.detection_chunk_bytes(geo.lowres_px) for geo in geometries
        ) if geometries else self.detection_chunk_bytes((1, 1))
        recognition_bytes = peak_batch * tokens_per_crop * self.bytes_per_recognition_token
        # Per-execution activation demand only: shared model residency is a
        # runtime-level cost the ledger's activation budget already nets out.
        envelope = layout_bytes + detection_bytes + recognition_bytes

        return DemandEstimate(
            demand_class=demand_class,
            page_count=len(geometries),
            max_layout_slices_per_page=max_slices,
            max_detection_chunks_per_page=max_chunks,
            max_recognition_crops_per_page=max_crops,
            max_recognition_tokens_per_crop=tokens_per_crop,
            peak_recognition_batch=peak_batch,
            envelope_bytes=envelope,
            profile_id=profile_id,
            notes=tuple(notes),
        )

    def detection_chunk_bytes(self, lowres_px: tuple[int, int]) -> int:
        chunks = self.detection_chunks(lowres_px)
        w, _ = lowres_px
        chunk_mp = (w * self.facts.detection_chunk_height_px) / 1_000_000.0
        return int(math.ceil(chunks * chunk_mp * self.bytes_per_detection_chunk_mp))

    def estimate(
        self,
        filepath: str | Path,
        *,
        profile_id: str,
        ocr_enabled: bool = True,
    ) -> DemandEstimate:
        """Estimate demand for one input document.

        Any failure to read geometry degrades to the declared UNKNOWN class
        (handled conservatively by admission) — never to optimistic NORMAL.
        """
        try:
            geometries = read_pdf_page_geometries(filepath)
        except Exception as exc:  # noqa: BLE001 - unknown demand, not a crash
            logger.warning("demand geometry read failed for %s: %r", filepath, exc)
            estimate = self.estimate_for_geometries(
                [], profile_id=profile_id, ocr_enabled=ocr_enabled
            )
            return replace(
                estimate, notes=estimate.notes + (f"geometry read failed: {exc}",)
            )
        return self.estimate_for_geometries(
            geometries, profile_id=profile_id, ocr_enabled=ocr_enabled
        )


# ---------------------------------------------------------------------------
# Runtime resource profile
# ---------------------------------------------------------------------------

def runtime_versions() -> dict[str, str]:
    """Best-effort pinned-runtime versions; part of profile identity."""
    versions: dict[str, str] = {}
    try:
        import torch

        versions["torch"] = torch.__version__
    except Exception:  # noqa: BLE001 - absent torch is a profile fact too
        versions["torch"] = "absent"
    try:
        from importlib.metadata import version

        versions["surya"] = version("surya-ocr")
        versions["marker"] = version("marker-pdf")
    except Exception:  # noqa: BLE001
        versions.setdefault("surya", "unknown")
        versions.setdefault("marker", "unknown")
    return versions


@dataclass(frozen=True)
class ResourceProfile:
    """Identity of one runtime resource behavior.

    Two profiles are compatible only if their fingerprints match: the
    fingerprint covers device, dtype, resolved batch sizes (including any
    post-OOM halvings), preprocessor facts, and dependency versions. Any
    material change produces a new identity, and envelopes measured for the
    old profile are not silently reused.
    """

    family: str
    device_label: str
    dtype_label: str
    batch_vector: tuple[tuple[str, int], ...]
    facts: PinnedPreprocessorFacts = DEFAULT_PREPROCESSOR_FACTS
    versions: dict[str, str] = field(default_factory=runtime_versions)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "family": self.family,
                "device": self.device_label,
                "dtype": self.dtype_label,
                "batches": [list(b) for b in self.batch_vector],
                "facts": self.facts.as_dict(),
                "versions": self.versions,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def with_batches(self, batch_vector: tuple[tuple[str, int], ...]) -> "ResourceProfile":
        return ResourceProfile(
            family=self.family,
            device_label=self.device_label,
            dtype_label=self.dtype_label,
            batch_vector=tuple(sorted(batch_vector)),
            facts=self.facts,
            versions=self.versions,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "device": self.device_label,
            "dtype": self.dtype_label,
            "batches": dict(self.batch_vector),
            "versions": self.versions,
            "fingerprint": self.fingerprint(),
        }


# ---------------------------------------------------------------------------
# Capacity ledger
# ---------------------------------------------------------------------------

class AdmissionError(RuntimeError):
    """Raised when admission is refused or a ticket is misused."""


@dataclass(frozen=True)
class CapacityEnvelope:
    """Declared usable-capacity policy for one profile.

    ``base_resident_bytes`` is the shared model-residency cost charged once
    against usable capacity (not per reservation — weights are shared
    residency, and per-job double counting would be pointlessly cruel to
    admission). Reservations hold per-execution activation bytes.
    """

    usable_bytes: int
    safety_reserve_bytes: int
    base_resident_bytes: int
    device_total_bytes: Optional[int]
    coefficients: dict[str, int]
    measured: bool = False

    @property
    def activation_budget_bytes(self) -> int:
        return max(0, self.usable_bytes - self.base_resident_bytes)

    def summary(self) -> dict[str, Any]:
        return {
            "usable_bytes": self.usable_bytes,
            "safety_reserve_bytes": self.safety_reserve_bytes,
            "base_resident_bytes": self.base_resident_bytes,
            "activation_budget_bytes": self.activation_budget_bytes,
            "device_total_bytes": self.device_total_bytes,
            "coefficients": self.coefficients,
            "measured": self.measured,
        }


@dataclass
class Reservation:
    reservation_id: str
    job_id: str
    bytes: int
    exclusive: bool
    created_at: float


class CapacityLedger:
    """Atomic reservation book for one runtime's usable capacity.

    All mutation happens under one lock, so two admissions racing for the
    last capacity cannot both succeed, totals can never go negative, and
    release is idempotent.
    """

    def __init__(self, envelope: CapacityEnvelope) -> None:
        self._envelope = envelope
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.Lock()

    @property
    def envelope(self) -> CapacityEnvelope:
        return self._envelope

    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(r.bytes for r in self._reservations.values())

    def available_bytes(self) -> int:
        with self._lock:
            return self._available_locked()

    def _available_locked(self) -> int:
        reserved = sum(r.bytes for r in self._reservations.values())
        return self._envelope.activation_budget_bytes - reserved

    def admit(
        self,
        job_id: str,
        demand_bytes: int,
        *,
        exclusive: bool = False,
    ) -> Reservation:
        """Reserve capacity or raise :class:`AdmissionError`.

        ``exclusive`` reservations (the declared safe path for unknown /
        out-of-distribution demand) require the runtime to be otherwise
        idle and consume the whole activation budget.
        """
        if demand_bytes < 0:
            raise AdmissionError("demand cannot be negative")
        with self._lock:
            if any(r.exclusive for r in self._reservations.values()):
                raise AdmissionError(
                    "an exclusive safe-path reservation is active; unknown-class "
                    "demand must wait for it to finish"
                )
            effective = self._envelope.activation_budget_bytes if exclusive else demand_bytes
            if exclusive and self._reservations:
                raise AdmissionError(
                    "exclusive safe-path admission requires an idle runtime"
                )
            if effective > self._available_locked():
                raise AdmissionError(
                    f"capacity refused: demand {effective} B exceeds available "
                    f"{self._available_locked()} B of activation budget "
                    f"{self._envelope.activation_budget_bytes} B"
                )
            reservation = Reservation(
                reservation_id=uuid.uuid4().hex,
                job_id=job_id,
                bytes=effective,
                exclusive=exclusive,
                created_at=time.time(),
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def release(self, reservation_id: str) -> bool:
        """Release a reservation; idempotent and safe for stale ids."""
        with self._lock:
            return self._reservations.pop(reservation_id, None) is not None

    def active_count(self) -> int:
        with self._lock:
            return len(self._reservations)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "usable_bytes": self._envelope.usable_bytes,
                "reserved_bytes": sum(r.bytes for r in self._reservations.values()),
                "available_bytes": self._available_locked(),
                "active_reservations": len(self._reservations),
                "envelope": self._envelope.summary(),
            }


# ---------------------------------------------------------------------------
# Residency leases
# ---------------------------------------------------------------------------

class ResidencyState(str, Enum):
    COLD = "cold"
    LOADING = "loading"
    WARM = "warm"
    DRAINING = "draining"
    RELEASED = "released"


@dataclass
class ResidencyLease:
    lease_id: str
    job_id: str
    generation: int
    created_at: float


class ResidencyLeaseRegistry:
    """Active-execution leases over model-residency generations.

    A lease pins the generation it was acquired against: while any lease on
    generation G is active, G cannot be unloaded. Draining blocks NEW
    leases on the current generation and waits for active ones, so an
    unload/switch transition can never evict a borrower mid-request.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._generation = 1
        self._state = ResidencyState.COLD
        self._leases: dict[str, ResidencyLease] = {}

    @property
    def state(self) -> ResidencyState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def active_count(self) -> int:
        with self._lock:
            return len(self._leases)

    def set_state(self, state: ResidencyState) -> None:
        with self._lock:
            self._state = state

    def begin_load(self) -> None:
        with self._lock:
            if self._state is ResidencyState.RELEASED or self._state is ResidencyState.COLD:
                self._generation += 1
            self._state = ResidencyState.LOADING

    def mark_warm(self) -> None:
        with self._lock:
            if self._state is ResidencyState.LOADING:
                self._state = ResidencyState.WARM

    def acquire(self, job_id: str) -> ResidencyLease:
        """Acquire an execution lease on the current generation."""
        with self._lock:
            if self._state is ResidencyState.DRAINING:
                raise AdmissionError(
                    "runtime is draining; no new execution leases on this generation"
                )
            lease = ResidencyLease(
                lease_id=uuid.uuid4().hex,
                job_id=job_id,
                generation=self._generation,
                created_at=time.time(),
            )
            self._leases[lease.lease_id] = lease
            return lease

    def release(self, lease_id: str) -> bool:
        """Release a lease; idempotent. Wakes drain waiters at zero."""
        with self._lock:
            removed = self._leases.pop(lease_id, None) is not None
            if removed and not self._leases:
                self._drained.notify_all()
            return removed

    def request_drain(self, timeout: float = 30.0) -> bool:
        """Stop new leases and wait for active ones to finish.

        Returns True when the generation is drained (callers may unload),
        False on timeout (unload must NOT proceed under the anti-eviction
        contract; callers choose an explicit cancellation policy instead).
        """
        deadline = time.monotonic() + timeout
        with self._drained:
            self._state = ResidencyState.DRAINING
            while self._leases:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Abort the drain: back to WARM, leases keep running.
                    self._state = ResidencyState.WARM
                    return False
                self._drained.wait(remaining)
            self._state = ResidencyState.RELEASED
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "generation": self._generation,
                "active_leases": len(self._leases),
                "leases": [
                    {"job_id": lease.job_id, "generation": lease.generation}
                    for lease in self._leases.values()
                ],
            }


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

@dataclass
class AdmissionTicket:
    job_id: str
    reservation: Reservation
    lease: ResidencyLease
    estimate: DemandEstimate
    admitted_at: float
    completed: bool = False


@dataclass
class ResidencyObservation:
    """Structured cold/warm observation (replaces log-only visibility)."""

    generation: int
    transition: str  # "cold_load" | "warm_reuse" | "unload"
    elapsed_seconds: float
    observed_at: float
    device_label: str


def probe_device_capacity(device_str: str | None) -> dict[str, Any]:
    """Best-effort device-level capacity observation.

    Reports allocator-visible AND device-level numbers separately (they are
    related but not interchangeable); failures degrade to ``available=False``
    rather than pretending CUDA facts exist.
    """
    info: dict[str, Any] = {"device": device_str or "cpu", "available": False}
    try:
        import torch

        if device_str and device_str.startswith("cuda") and torch.cuda.is_available():
            index = 0
            if ":" in device_str:
                index = int(device_str.split(":", 1)[1])
            free, total = torch.cuda.mem_get_info(index)
            info.update(
                {
                    "available": True,
                    "device_total_bytes": int(total),
                    "device_free_bytes": int(free),
                    "torch_allocated_bytes": int(torch.cuda.memory_allocated(index)),
                    "torch_reserved_bytes": int(torch.cuda.memory_reserved(index)),
                }
            )
    except Exception as exc:  # noqa: BLE001 - probing is observability, not authority
        info["error"] = repr(exc)
    return info


class RuntimeCapacityCoordinator:
    """Owns admission + residency lifecycle for one execution context."""

    def __init__(
        self,
        *,
        profile: ResourceProfile,
        envelope: CapacityEnvelope,
        estimator: DemandEstimator,
        oom_cooldown_seconds: float = 30.0,
        max_consecutive_ooms: int = 3,
        unknown_policy: str = "safe_profile",
        device_probe: Callable[[str | None], dict[str, Any]] = probe_device_capacity,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.profile = profile
        self.ledger = CapacityLedger(envelope)
        self.leases = ResidencyLeaseRegistry()
        self.estimator = estimator
        self.oom_cooldown_seconds = oom_cooldown_seconds
        self.max_consecutive_ooms = max_consecutive_ooms
        self.unknown_policy = unknown_policy
        self._device_probe = device_probe
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: dict[str, AdmissionTicket] = {}
        self._observations: list[ResidencyObservation] = []
        self._oom_events: list[dict[str, Any]] = []
        self._consecutive_ooms = 0
        self._protective_until: float = 0.0

    # -- admission lifecycle -------------------------------------------------

    def admit(
        self,
        job_id: str,
        filepath: str | Path,
        *,
        ocr_enabled: bool = True,
    ) -> AdmissionTicket:
        """Estimate demand and atomically reserve capacity + model lease.

        Raises :class:`AdmissionError` when the request must not enter the
        dangerous converter path — callers treat that as a truthful refusal,
        never as an excuse to run anyway.
        """
        estimate = self.estimator.estimate(
            filepath, profile_id=self.profile.fingerprint(), ocr_enabled=ocr_enabled
        )
        return self.admit_estimate(job_id, estimate)

    def admit_estimate(self, job_id: str, estimate: DemandEstimate) -> AdmissionTicket:
        with self._lock:
            if self._clock() < self._protective_until:
                raise AdmissionError(
                    "runtime is in a bounded protective cooldown after repeated "
                    "unexpected OOMs; admission paused"
                )
            if estimate.profile_id != self.profile.fingerprint():
                raise AdmissionError(
                    "demand estimate belongs to a different runtime profile; "
                    "envelopes are not reusable across profiles"
                )

        exclusive = estimate.demand_class is not DemandClass.NORMAL
        if exclusive and self.unknown_policy != "safe_profile":
            raise AdmissionError(
                f"unknown-class demand refused by policy {self.unknown_policy!r}"
            )

        reservation = self.ledger.admit(
            job_id, estimate.envelope_bytes, exclusive=exclusive
        )
        try:
            lease = self.leases.acquire(job_id)
        except AdmissionError:
            self.ledger.release(reservation.reservation_id)
            raise
        ticket = AdmissionTicket(
            job_id=job_id,
            reservation=reservation,
            lease=lease,
            estimate=estimate,
            admitted_at=self._clock(),
        )
        with self._lock:
            self._tickets[job_id] = ticket
        return ticket

    def finish(
        self,
        ticket: AdmissionTicket,
        *,
        outcome: str,
        detail: str = "",
    ) -> None:
        """Terminal release for every path: success, error, cancel, OOM.

        Idempotent: a stale double-finish cannot release another request's
        capacity.
        """
        with self._lock:
            already = self._tickets.pop(ticket.job_id, None)
            if already is None or already is not ticket or ticket.completed:
                return
            ticket.completed = True
            if outcome == "oom":
                self._record_oom_locked(ticket, detail)
        self.ledger.release(ticket.reservation.reservation_id)
        self.leases.release(ticket.lease.lease_id)

    # -- residency observations ----------------------------------------------

    def observe_cold_load(self, elapsed_seconds: float) -> None:
        self._observe("cold_load", elapsed_seconds)

    def observe_warm_reuse(self, elapsed_seconds: float = 0.0) -> None:
        self._observe("warm_reuse", elapsed_seconds)

    def observe_unload(self, elapsed_seconds: float) -> None:
        self._observe("unload", elapsed_seconds)

    def _observe(self, transition: str, elapsed_seconds: float) -> None:
        observation = ResidencyObservation(
            generation=self.leases.generation,
            transition=transition,
            elapsed_seconds=elapsed_seconds,
            observed_at=self._clock(),
            device_label=self.profile.device_label,
        )
        with self._lock:
            self._observations.append(observation)
            if transition != "unload" and len(self._observations) > 256:
                del self._observations[:-256]

    def note_residency_states(
        self, *, loading: bool = False, warm: bool = False
    ) -> None:
        if loading:
            self.leases.begin_load()
        elif warm:
            self.leases.mark_warm()

    def latest_residency_observation(self) -> Optional[ResidencyObservation]:
        with self._lock:
            return self._observations[-1] if self._observations else None

    # -- unload / drain protocol ---------------------------------------------

    def request_unload(self, timeout: float = 30.0) -> bool:
        """Safe unload transition: stop new leases, drain, publish state.

        Returns True when models may be released; False means active
        borrowers still hold the generation and the caller must NOT unload
        (choose an explicit cancellation policy instead).
        """
        drained = self.leases.request_drain(timeout=timeout)
        if drained:
            self.observe_unload(0.0)
        return drained

    # -- OOM feedback ----------------------------------------------------------

    def _record_oom_locked(self, ticket: AdmissionTicket, detail: str) -> None:
        self._consecutive_ooms += 1
        self._oom_events.append(
            {
                "job_id": ticket.job_id,
                "demand": ticket.estimate.summary(),
                "detail": detail,
                "at": self._clock(),
            }
        )
        if len(self._oom_events) > 64:
            del self._oom_events[:-64]
        if self._consecutive_ooms >= self.max_consecutive_ooms:
            self._protective_until = self._clock() + self.oom_cooldown_seconds
            self._consecutive_ooms = 0
            logger.error(
                "runtime profile %s entered protective cooldown for %.1fs after "
                "%d consecutive unexpected OOMs",
                self.profile.fingerprint(),
                self.oom_cooldown_seconds,
                self.max_consecutive_ooms,
            )

    def note_successful_execution(self) -> None:
        """A clean execution resets consecutive-OOM pressure."""
        with self._lock:
            self._consecutive_ooms = 0

    def note_profile_transition(self, new_profile: ResourceProfile) -> None:
        """An explicit runtime change (e.g. batch halving after OOM).

        The new identity deliberately invalidates old-envelope reuse; a
        lower-memory retry is a visible profile transition, never a hidden
        global mutation.
        """
        with self._lock:
            self.profile = new_profile
            self._consecutive_ooms = 0
            self._protective_until = 0.0

    # -- observability -----------------------------------------------------------

    def device_snapshot(self) -> dict[str, Any]:
        return self._device_probe(self.profile.device_label)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            profile_summary = self.profile.summary()
            oom_events = list(self._oom_events)
            observations = [
                {
                    "generation": o.generation,
                    "transition": o.transition,
                    "elapsed_seconds": o.elapsed_seconds,
                    "observed_at": o.observed_at,
                    "device": o.device_label,
                }
                for o in self._observations[-8:]
            ]
            protective_until = self._protective_until
            tickets = len(self._tickets)
        return {
            "profile": profile_summary,
            "capacity": self.ledger.snapshot(),
            "residency": self.leases.snapshot(),
            "active_tickets": tickets,
            "oom_events": oom_events[-8:],
            "protective_cooldown_until": protective_until,
            "observations": observations,
        }


def coordinator_for_device(
    device_str: str | None,
    *,
    config: Any = None,
    versions: dict[str, str] | None = None,
) -> RuntimeCapacityCoordinator:
    """Build the default coordinator for one pinned device from config.

    ``config`` is ``app.core.config`` (imported lazily so tests can patch
    values); every knob has a conservative default so the module never
    crashes a worker because admission is misconfigured.
    """
    if config is None:
        from app.core import config as config  # noqa: PLC0415

    facts = DEFAULT_PREPROCESSOR_FACTS
    batch_vector = (
        ("recognition", facts.recognition_batch),
        ("layout", facts.layout_batch),
        ("detection", facts.detection_batch),
    )
    probe = probe_device_capacity(device_str)
    device_total = probe.get("device_total_bytes")

    usable_override = getattr(config, "ADMISSION_USABLE_BYTES", 0)
    if usable_override > 0:
        usable = int(usable_override)
    elif device_total:
        reserve = int(device_total * float(getattr(config, "ADMISSION_RESERVE_FRACTION", 0.10)))
        usable = int(device_total) - reserve
    else:
        # CPU-only / unprobed runtime: a small declared envelope keeps the
        # lifecycle real without pretending to offer CUDA capacity.
        usable = int(getattr(config, "ADMISSION_CPU_USABLE_BYTES", 512 * 1024 * 1024))

    weights_bound = int(getattr(config, "ADMISSION_WEIGHTS_BOUND_BYTES", 3 << 30))
    envelope = CapacityEnvelope(
        usable_bytes=usable,
        safety_reserve_bytes=(
            int(device_total * float(getattr(config, "ADMISSION_RESERVE_FRACTION", 0.10)))
            if device_total
            else 0
        ),
        base_resident_bytes=weights_bound,
        device_total_bytes=device_total,
        coefficients={
            "weights_resident_bytes": weights_bound,
            "per_layout_slice_bytes": getattr(config, "ADMISSION_LAYOUT_BYTES_PER_SLICE", 100 << 20),
            "per_detection_chunk_mp_bytes": getattr(
                config, "ADMISSION_DETECTION_BYTES_PER_CHUNK_MP", 32 << 20
            ),
            "per_recognition_token_bytes": getattr(
                config, "ADMISSION_RECOGNITION_BYTES_PER_TOKEN", 24 << 10
            ),
        },
        measured=False,
    )
    estimator = DemandEstimator(
        crops_per_megapixel=float(
            getattr(config, "ADMISSION_CROPS_PER_MEGAPIXEL", 250.0)
        ),
        bytes_weights_resident=envelope.coefficients["weights_resident_bytes"],
        bytes_per_layout_slice=envelope.coefficients["per_layout_slice_bytes"],
        bytes_per_detection_chunk_mp=envelope.coefficients["per_detection_chunk_mp_bytes"],
        bytes_per_recognition_token=envelope.coefficients["per_recognition_token_bytes"],
        max_page_lowres_pixels=int(
            getattr(config, "ADMISSION_MAX_PAGE_LOWRES_PIXELS", 30_000_000)
        ),
    )
    profile = ResourceProfile(
        family="marker-gpu" if (device_str or "").startswith("cuda") else "marker-cpu",
        device_label=device_str or "cpu",
        dtype_label=getattr(config, "ADMISSION_DTYPE_LABEL", "auto"),
        batch_vector=batch_vector,
        facts=facts,
        versions=versions or runtime_versions(),
    )
    return RuntimeCapacityCoordinator(
        profile=profile,
        envelope=envelope,
        estimator=estimator,
        oom_cooldown_seconds=float(
            getattr(config, "ADMISSION_OOM_COOLDOWN_SECONDS", 30.0)
        ),
        max_consecutive_ooms=int(
            getattr(config, "ADMISSION_MAX_CONSECUTIVE_OOMS", 3)
        ),
        unknown_policy=str(getattr(config, "ADMISSION_UNKNOWN_POLICY", "safe_profile")),
    )
