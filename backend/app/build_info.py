"""Build provenance, identity, and dependency lock verification.

Provides inspectable runtime metadata about build commit, package versions,
hardware variant (CPU vs GPU), and lockfile integrity.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]

LOCK_CPU = BACKEND_DIR / "requirements-cpu.lock"
LOCK_GPU = BACKEND_DIR / "requirements-gpu.lock"

KEY_PACKAGE_NAMES = (
    "fastapi",
    "mcp",
    "marker-pdf",
    "surya-ocr",
    "torch",
    "torchvision",
    "pydantic",
    "sqlalchemy",
    "alembic",
    "uvicorn",
    "faster-whisper",
    "pytesseract",
    "supervisor",
)


def get_git_commit_sha() -> str:
    """Retrieve git commit SHA from env or git command."""
    env_sha = os.getenv("MARKER_COMMIT_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def get_marker_version() -> str:
    """Retrieve package version."""
    return os.getenv("MARKER_VERSION", "0.1.0").strip()


def detect_variant() -> str:
    """Detect whether running in GPU or CPU mode."""
    env_variant = os.getenv("MARKER_VARIANT", "").strip().lower()
    if env_variant in ("cpu", "gpu"):
        return env_variant
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "gpu"
        if "+cpu" in getattr(torch, "__version__", ""):
            return "cpu"
    except Exception:
        pass
    return "cpu"


def calculate_file_sha256(path: Path) -> str | None:
    """Calculate SHA-256 of a file."""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def parse_lockfile_pins(lock_path: Path) -> dict[str, str]:
    """Parse exact package==version pins from a lockfile."""
    pins: dict[str, str] = {}
    if not lock_path.is_file():
        return pins
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            parts = line.split("==")
            pkg = parts[0].strip().lower()
            ver = parts[1].split(";")[0].split()[0].strip()
            pins[pkg] = ver
    return pins


def get_installed_package_version(name: str) -> str | None:
    """Get currently installed version for a package name."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def get_key_package_versions() -> dict[str, str]:
    """Return installed versions of key Marker UI dependencies."""
    versions: dict[str, str] = {}
    for pkg in KEY_PACKAGE_NAMES:
        ver = get_installed_package_version(pkg)
        if ver is not None:
            versions[pkg] = ver
    return versions


def get_active_lock_path(variant: str | None = None) -> Path:
    """Determine the active lockfile path for the given or detected variant."""
    var = variant or detect_variant()
    return LOCK_GPU if var == "gpu" else LOCK_CPU


def get_build_provenance(variant: str | None = None) -> dict[str, Any]:
    """Return complete structured build and runtime provenance information."""
    active_variant = variant or detect_variant()
    lock_path = get_active_lock_path(active_variant)
    lock_sha = calculate_file_sha256(lock_path)
    lock_pins = parse_lockfile_pins(lock_path)

    return {
        "service": "marker",
        "version": get_marker_version(),
        "commit_sha": get_git_commit_sha(),
        "variant": active_variant,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "lockfile": {
            "path": str(lock_path.relative_to(REPO_ROOT)) if lock_path.is_relative_to(REPO_ROOT) else str(lock_path),
            "sha256": lock_sha,
            "pinned_package_count": len(lock_pins),
        },
        "key_packages": get_key_package_versions(),
    }


def verify_dependency_lock(
    variant: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Verify runtime environment against the declared lockfile pins.

    In standard mode (default), checks that all locked packages are installed at
    the expected version.
    In strict mode, also reports installed top-level packages that are not present
    in the declared lockfile (excluding base system/build packages).
    """
    active_variant = variant or detect_variant()
    lock_path = get_active_lock_path(active_variant)

    if not lock_path.is_file():
        return {
            "ok": False,
            "error": f"Lockfile {lock_path.name} not found.",
            "mismatches": {},
            "missing": [],
            "unexpected": [],
        }

    pins = parse_lockfile_pins(lock_path)
    mismatches: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    unexpected: list[str] = []

    for pkg, expected_ver in pins.items():
        installed = get_installed_package_version(pkg)
        if installed is None:
            missing.append(pkg)
        elif installed.lower() != expected_ver.lower():
            mismatches[pkg] = {
                "expected": expected_ver,
                "installed": installed,
            }

    if strict:
        # Ignore base tooling distributions
        ignored = {"pip", "setuptools", "wheel", "uv", "pytest"}
        try:
            for dist in importlib.metadata.distributions():
                name = dist.metadata["Name"].lower()
                if name not in pins and name not in ignored and not name.startswith("pytest-"):
                    unexpected.append(name)
        except Exception:
            pass

    ok = len(mismatches) == 0 and len(missing) == 0 and (not strict or len(unexpected) == 0)

    result: dict[str, Any] = {
        "ok": ok,
        "lockfile": lock_path.name,
        "mode": "strict" if strict else "standard",
        "total_pins": len(pins),
        "missing_count": len(missing),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "missing": missing,
    }
    if strict:
        result["unexpected_count"] = len(unexpected)
        result["unexpected"] = sorted(unexpected)

    return result
