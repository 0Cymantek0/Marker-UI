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

# Encryption
SECRET_KEY_PATH: Path = DATA_DIR / ".secret_key"
