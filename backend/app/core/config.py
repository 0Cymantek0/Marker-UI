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
# message; larger fields travel through verified handles.
ARTIFACT_HANDLE_INLINE_LIMIT: int = int(
    os.getenv("MARKER_ARTIFACT_HANDLE_INLINE_LIMIT", str(256 * 1024))
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
