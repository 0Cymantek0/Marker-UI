"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
DB_PATH = DATA_DIR / "marker_ui.db"

# Server
HOST: str = os.getenv("MARKER_HOST", "127.0.0.1")
PORT: int = int(os.getenv("MARKER_PORT", "8000"))
DEBUG: bool = os.getenv("MARKER_DEBUG", "false").lower() in ("true", "1", "yes")

MAX_UPLOAD_SIZE: int = int(os.getenv("MARKER_MAX_UPLOAD_SIZE_MB", "100")) * 1024 * 1024
SOURCE_URL_ALLOWLIST: tuple[str, ...] = tuple(
    item.strip().lower()
    for item in os.getenv("MARKER_SOURCE_URL_ALLOWLIST", "").split(",")
    if item.strip()
)
SOURCE_URL_REQUIRE_ALLOWLIST: bool = os.getenv(
    "MARKER_SOURCE_URL_REQUIRE_ALLOWLIST",
    "false",
).lower() in ("true", "1", "yes")

# Model prewarming. Phase 1 default is lazy: marker models load on first marker
# job rather than at startup, so an office-only deployment never pays the
# multi-GB cold start. Set MARKER_PRELOAD_MODELS=true to restore eager loading.
PRELOAD_MARKER_MODELS: bool = os.getenv("MARKER_PRELOAD_MODELS", "false").lower() in ("true", "1", "yes")

# Database
DATABASE_URL: str = os.getenv("MARKER_DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")

# Truth Kernel durable payload store (PR64): content-addressed immutable
# objects backing committed kernel payload references.
KERNEL_PAYLOAD_ROOT: Path = Path(
    os.getenv("MARKER_KERNEL_PAYLOAD_ROOT", str(DATA_DIR / "kernel_payloads"))
)

# Source truth artifact store (PR70/71 local slice; PR83B3 industrial
# profile): content-addressed immutable copies of acquired
# local/uploaded/URL source documents. A committed ContentRevisionRecord
# references the blob key; probe/routing and conversion consume the
# artifact instead of re-trusting the external source path.
#
# ``MARKER_SOURCE_STORE_PROFILE`` selects the physical topology:
# * ``local`` — LocalSourceStore under ``MARKER_SOURCE_STORE_ROOT``
#   (the PR70/71 node-local profile, default for compatibility);
# * ``s3``    — S3SourceStore over an S3-compatible service (PR83B3
#   industrial profile). Requires the four ``MARKER_SOURCE_S3_*``
#   settings; selection fails closed rather than silently degrading
#   to the local profile.
SOURCE_STORE_ROOT: Path = Path(
    os.getenv("MARKER_SOURCE_STORE_ROOT", str(DATA_DIR / "source_store"))
)
SOURCE_STORE_PROFILE: str = os.getenv("MARKER_SOURCE_STORE_PROFILE", "local").strip().lower()

# Industrial source-artifact object store. The prefix deliberately
# differs from the kernel payload namespace (``kernel-payloads``) so
# source artifacts and kernel payloads can share a bucket while keeping
# ownership, listing, and deletion scopes disjoint.
SOURCE_S3_ENDPOINT: str = os.getenv("MARKER_SOURCE_S3_ENDPOINT", "").strip()
SOURCE_S3_BUCKET: str = os.getenv("MARKER_SOURCE_S3_BUCKET", "").strip()
SOURCE_S3_ACCESS_KEY: str = os.getenv("MARKER_SOURCE_S3_ACCESS_KEY", "").strip()
SOURCE_S3_SECRET_KEY: str = os.getenv("MARKER_SOURCE_S3_SECRET_KEY", "").strip()
SOURCE_S3_REGION: str = os.getenv("MARKER_SOURCE_S3_REGION", "us-east-1").strip()
SOURCE_S3_PREFIX: str = os.getenv("MARKER_SOURCE_S3_PREFIX", "kernel-sources").strip()

# Node-local verified materialization cache for non-local source
# profiles (PR83B3): converter-facing working copies rebuilt on demand
# from durable shared truth. A cache hit is only ever reused after full
# content verification; the cache is never a second source authority.
SOURCE_CACHE_ROOT: Path = Path(
    os.getenv("MARKER_SOURCE_CACHE_ROOT", str(DATA_DIR / "source_cache"))
)

# Kernel runtime authority (PR67B): conversions are authorized as kernel
# work, dispatched through the fair scheduler, kept alive by evidence-backed
# liveness, and completed only through fenced accepted publication. The
# kill switch restores the legacy direct-submission runtime.
KERNEL_RUNTIME_ENABLED: bool = os.getenv(
    "MARKER_KERNEL_RUNTIME", "true"
).lower() in ("true", "1", "yes")
KERNEL_RUNTIME_WORKSPACE: str = os.getenv("MARKER_KERNEL_RUNTIME_WORKSPACE", "local")
KERNEL_RUNTIME_OWNER: str = os.getenv("MARKER_KERNEL_RUNTIME_OWNER", "marker-runtime")
# Lease TTL for conversion work. Long enough that a healthy conversion in a
# silent phase (cold model load) is not wrongly superseded, short enough
# that a crashed worker becomes takeover-eligible in a reasonable window.
# Liveness renewal extends it continuously while real evidence flows.
KERNEL_LEASE_SECONDS: float = float(os.getenv("MARKER_KERNEL_LEASE_SECONDS", "900"))
# How often the renewal task checks for fresh control-loop evidence.
KERNEL_RENEW_INTERVAL_SECONDS: float = float(
    os.getenv("MARKER_KERNEL_RENEW_INTERVAL_SECONDS", "5")
)
# Dispatch loop idle poll interval.
KERNEL_DISPATCH_POLL_SECONDS: float = float(
    os.getenv("MARKER_KERNEL_DISPATCH_POLL_SECONDS", "0.25")
)
# Watchdog pass interval: lapsed-lease takeover prep and lost-ack repair.
KERNEL_WATCHDOG_INTERVAL_SECONDS: float = float(
    os.getenv("MARKER_KERNEL_WATCHDOG_INTERVAL_SECONDS", "15")
)
# Hard cap on concurrently leased conversion work (scheduling group policy).
KERNEL_MAX_IN_FLIGHT: int = int(os.getenv("MARKER_KERNEL_MAX_IN_FLIGHT", "4"))

# Local ArtifactHandle data plane (PR68A): verified ephemeral file handles
# that move large process-worker result fields out of the pickled control
# message. Kill switch restores pure queue-inline transport everywhere.
ARTIFACT_HANDLES_ENABLED: bool = os.getenv(
    "MARKER_ARTIFACT_HANDLES", "true"
).lower() in ("true", "1", "yes")
ARTIFACT_HANDLE_ROOT: Path = Path(
    os.getenv("MARKER_ARTIFACT_HANDLE_ROOT", str(DATA_DIR / "artifact_handles"))
)
# Fields whose encoded size is at or below this stay inline in the control
# message; larger fields travel through verified handles. 1 MiB per field is
# where the measured queue-inline advantage ends and control-channel byte
# relief starts to matter (see docs/reference/artifact-data-plane.md).
ARTIFACT_HANDLE_INLINE_LIMIT: int = int(
    os.getenv("MARKER_ARTIFACT_HANDLE_INLINE_LIMIT", str(1024 * 1024))
)
# Orphaned blobs (producer/consumer crash, cancelled jobs) are reclaimed
# once older than this; must comfortably exceed any stage->consume window.
ARTIFACT_HANDLE_SWEEP_SECONDS: float = float(
    os.getenv("MARKER_ARTIFACT_HANDLE_SWEEP_SECONDS", "3600")
)
# Hard bound on any single handle read; larger claims fail closed.
ARTIFACT_HANDLE_MAX_BYTES: int = int(
    os.getenv("MARKER_ARTIFACT_HANDLE_MAX_BYTES", str(1 << 30))
)

# Runtime admission + model leases (PR69). Kill switch restores the legacy
# enter-the-converter-directly behavior; every other knob is a conservative
# default until a per-profile characterization artifact tightens it.
ADMISSION_ENABLED: bool = os.getenv("MARKER_ADMISSION", "true").lower() in ("true", "1", "yes")
# Fraction of probed device capacity withheld as a safety reserve (allocator
# fragmentation, non-PyTorch-allocator allocations).
ADMISSION_RESERVE_FRACTION: float = float(os.getenv("MARKER_ADMISSION_RESERVE_FRACTION", "0.10"))
# 0 = derive usable capacity from the device probe; >0 pins it explicitly.
ADMISSION_USABLE_BYTES: int = int(os.getenv("MARKER_ADMISSION_USABLE_BYTES", "0"))
# Declared envelope when no CUDA device is probed (CPU worker / tests):
# large enough that the shared model-residency base does not zero the
# activation budget on ordinary CPU deployments.
ADMISSION_CPU_USABLE_BYTES: int = int(os.getenv("MARKER_ADMISSION_CPU_USABLE_BYTES", str(8 << 30)))
ADMISSION_DTYPE_LABEL: str = os.getenv("MARKER_ADMISSION_DTYPE_LABEL", "auto")
# Conservative envelope coefficients (unmeasured defaults; characterization
# replaces them with measured values per profile).
ADMISSION_WEIGHTS_BOUND_BYTES: int = int(os.getenv("MARKER_ADMISSION_WEIGHTS_BOUND_BYTES", str(3 << 30)))
ADMISSION_LAYOUT_BYTES_PER_SLICE: int = int(os.getenv("MARKER_ADMISSION_LAYOUT_BYTES_PER_SLICE", str(100 << 20)))
ADMISSION_DETECTION_BYTES_PER_CHUNK_MP: int = int(os.getenv("MARKER_ADMISSION_DETECTION_BYTES_PER_CHUNK_MP", str(32 << 20)))
ADMISSION_RECOGNITION_BYTES_PER_TOKEN: int = int(os.getenv("MARKER_ADMISSION_RECOGNITION_BYTES_PER_TOKEN", str(24 << 10)))
# Conservative line-crop bound per highres megapixel (true count is the
# detection model's output and cannot be known pre-execution).
ADMISSION_CROPS_PER_MEGAPIXEL: float = float(os.getenv("MARKER_ADMISSION_CROPS_PER_MEGAPIXEL", "250"))
# Pages whose lowres pixel area exceeds this are out-of-distribution: they
# take the declared safe path (exclusive serialized admission), never the
# normal high-throughput class.
ADMISSION_MAX_PAGE_LOWRES_PIXELS: int = int(os.getenv("MARKER_ADMISSION_MAX_PAGE_LOWRES_PIXELS", str(30_000_000)))
# Declared behavior for unknown/OOD demand: "safe_profile" (exclusive
# serialized admission) or "reject".
ADMISSION_UNKNOWN_POLICY: str = os.getenv("MARKER_ADMISSION_UNKNOWN_POLICY", "safe_profile")
# Bounded protective cooldown after repeated unexpected OOMs on one profile.
ADMISSION_OOM_COOLDOWN_SECONDS: float = float(os.getenv("MARKER_ADMISSION_OOM_COOLDOWN_SECONDS", "30"))
ADMISSION_MAX_CONSECUTIVE_OOMS: int = int(os.getenv("MARKER_ADMISSION_MAX_CONSECUTIVE_OOMS", "3"))
# Upper bound a safe unload waits for active execution leases before giving
# up (callers must not unload on timeout; see PR69 anti-eviction contract).
ADMISSION_DRAIN_TIMEOUT_SECONDS: float = float(os.getenv("MARKER_ADMISSION_DRAIN_TIMEOUT_SECONDS", "30"))

# Encryption
SECRET_KEY_PATH: Path = DATA_DIR / ".secret_key"
