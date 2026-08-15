#!/usr/bin/env python3
"""Deterministic backend dependency locking tool using uv.

Lock contract
-------------
CPU lock (`requirements-cpu.lock`):
    Universal resolution (`--universal --python-version 3.11`). Environment
    markers are preserved so platform-only dependencies (e.g. pywin32 via
    mcp) are gated to their platform. The same artifact installs truthfully
    on Linux (CI + Docker) and on Windows/macOS developer hosts.

GPU lock (`requirements-gpu.lock`):
    Explicit target resolution for Linux x86_64 / CPython 3.11
    (`--python-platform x86_64-unknown-linux-gnu --python-version 3.11`).
    CUDA wheels only exist for that platform, so the GPU lock is consumed
    exclusively by the GPU Docker image and Linux GPU deployments.

Both locks are compiled deterministically regardless of the contributor's
host OS: uv resolves against the declared target(s), never the host.
Compilation always targets a fresh temporary output with --refresh, so
neither a pre-existing lockfile (uv prefers previously pinned versions)
nor a stale local uv index cache can influence resolution. Generation and
drift checking therefore share an identical resolution basis.
Drift checking compares marker-aware pins (name, version, marker), so
removing or widening a marker is detected as drift.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"

REQ_CORE = BACKEND_DIR / "requirements.txt"
REQ_CPU = BACKEND_DIR / "requirements-cpu.txt"
REQ_GPU = BACKEND_DIR / "requirements-gpu.txt"

LOCK_CPU = BACKEND_DIR / "requirements-cpu.lock"
LOCK_GPU = BACKEND_DIR / "requirements-gpu.lock"

CPU_EXTRA_INDEX = "https://download.pytorch.org/whl/cpu"
GPU_EXTRA_INDEX = "https://download.pytorch.org/whl/cu126"

PYTHON_VERSION = "3.11"
GPU_TARGET_PLATFORM = "x86_64-unknown-linux-gnu"


@dataclass(frozen=True)
class Resolution:
    """Declared resolution semantics for a lock target."""

    extra_index_url: str
    universal: bool
    python_platform: str | None = None


RESOLUTION_CPU = Resolution(extra_index_url=CPU_EXTRA_INDEX, universal=True)
RESOLUTION_GPU = Resolution(
    extra_index_url=GPU_EXTRA_INDEX,
    universal=False,
    python_platform=GPU_TARGET_PLATFORM,
)


def find_uv_executable() -> str:
    """Locate uv executable in PATH or virtualenv."""
    uv_path = shutil.which("uv")
    if uv_path:
        return uv_path
    py_dir = Path(sys.executable).parent
    candidate = py_dir / ("uv.exe" if os.name == "nt" else "uv")
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError(
        "uv executable not found in PATH or Python environment. "
        "Install uv via 'pip install uv' or https://docs.astral.sh/uv/"
    )


def compile_lockfile(
    *,
    requirements_files: list[Path],
    output_lock: Path,
    resolution: Resolution,
    upgrade: bool = False,
    uv_cmd: str | None = None,
) -> None:
    """Compile requirement files into a pinned lockfile using uv pip compile.

    Resolution always runs against a fresh temporary output so uv cannot
    prefer pins from a pre-existing lockfile, and with --refresh so a stale
    local index cache cannot hide newer distributions. The result is copied
    to output_lock only after a successful compile.
    """
    uv = uv_cmd or find_uv_executable()
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_out = Path(tmp_dir) / output_lock.name
        cmd = [
            uv,
            "pip",
            "compile",
            # Repo-relative inputs keep the lock header host-independent.
            *[str(p.relative_to(REPO_ROOT)) for p in requirements_files],
            "--python-version",
            PYTHON_VERSION,
            "--extra-index-url",
            resolution.extra_index_url,
            "--index-strategy",
            "unsafe-best-match",
            "--no-header",
            "--refresh",
            "--output-file",
            str(tmp_out),
        ]
        if resolution.universal:
            cmd.append("--universal")
        elif resolution.python_platform:
            cmd.extend(["--python-platform", resolution.python_platform])
        if upgrade:
            cmd.append("--upgrade")

        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"uv pip compile failed for {output_lock.name}:\n"
                f"command: {shlex.join(cmd)}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        shutil.copyfile(tmp_out, output_lock)


def extract_pinned_packages(content: str) -> dict[str, str]:
    """Parse exact package==version mappings from a lockfile (markers stripped)."""
    pins: dict[str, str] = {}
    for record in extract_semantic_pins(content):
        pins[record.name] = record.version
    return pins


@dataclass(frozen=True)
class SemanticPin:
    """A single lock pin including the environment marker that scopes it."""

    name: str
    version: str
    marker: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name}=={self.version}" + (f" ; {self.marker}" if self.marker else "")


def extract_semantic_pins(content: str) -> list[SemanticPin]:
    """Parse package==version[; marker] records from a lockfile.

    Markers are part of dependency truth: a pin whose marker was removed or
    widened must be detectable as drift, so they are preserved verbatim
    (normalized for whitespace) rather than discarded.
    """
    pins: list[SemanticPin] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        pin_part = line.split("#", 1)[0].strip()
        marker = ""
        if ";" in pin_part:
            pin_part, marker_part = pin_part.split(";", 1)
            marker = " ".join(marker_part.split())
        parts = pin_part.split("==")
        pkg = parts[0].strip().lower()
        ver = parts[1].strip().split()[0] if parts[1].strip() else ""
        if pkg and ver:
            pins.append(SemanticPin(name=pkg, version=ver, marker=marker))
    return pins


def check_lockfile(
    *,
    requirements_files: list[Path],
    expected_lock: Path,
    resolution: Resolution,
    uv_cmd: str | None = None,
) -> bool:
    """Check if existing lockfile is in sync with requirements without modifying it."""
    if not expected_lock.is_file():
        print(f"ERROR: Expected lockfile {expected_lock.name} does not exist.", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_lock = Path(tmp_dir) / expected_lock.name
        compile_lockfile(
            requirements_files=requirements_files,
            output_lock=tmp_lock,
            resolution=resolution,
            upgrade=False,
            uv_cmd=uv_cmd,
        )
        current_pins = extract_semantic_pins(expected_lock.read_text(encoding="utf-8"))
        compiled_pins = extract_semantic_pins(tmp_lock.read_text(encoding="utf-8"))

        if current_pins != compiled_pins:
            current_set = set(current_pins)
            compiled_set = set(compiled_pins)
            print(f"Drift detected in {expected_lock.name}:", file=sys.stderr)
            for pin in sorted(compiled_set - current_set, key=str):
                print(f"  New/changed in compilation: {pin}", file=sys.stderr)
            for pin in sorted(current_set - compiled_set, key=str):
                print(f"  Missing/widened in compilation: {pin}", file=sys.stderr)
            return False
        return True


def lock_cpu(*, check: bool = False, upgrade: bool = False, uv_cmd: str | None = None) -> bool:
    """Lock or check CPU variant dependencies (universal, marker-preserving)."""
    reqs = [REQ_CORE, REQ_CPU]
    if check:
        return check_lockfile(
            requirements_files=reqs,
            expected_lock=LOCK_CPU,
            resolution=RESOLUTION_CPU,
            uv_cmd=uv_cmd,
        )
    compile_lockfile(
        requirements_files=reqs,
        output_lock=LOCK_CPU,
        resolution=RESOLUTION_CPU,
        upgrade=upgrade,
        uv_cmd=uv_cmd,
    )
    print(f"Successfully generated {LOCK_CPU.relative_to(REPO_ROOT)}")
    return True


def lock_gpu(*, check: bool = False, upgrade: bool = False, uv_cmd: str | None = None) -> bool:
    """Lock or check GPU variant dependencies (explicit Linux x86_64 target)."""
    reqs = [REQ_CORE, REQ_GPU]
    if check:
        return check_lockfile(
            requirements_files=reqs,
            expected_lock=LOCK_GPU,
            resolution=RESOLUTION_GPU,
            uv_cmd=uv_cmd,
        )
    compile_lockfile(
        requirements_files=reqs,
        output_lock=LOCK_GPU,
        resolution=RESOLUTION_GPU,
        upgrade=upgrade,
        uv_cmd=uv_cmd,
    )
    print(f"Successfully generated {LOCK_GPU.relative_to(REPO_ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Lock and verify Marker UI Python dependencies.")
    parser.add_argument(
        "--variant",
        choices=["cpu", "gpu", "all"],
        default="all",
        help="Target dependency profile variant (default: all)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify lockfiles match current requirements without rewriting",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Upgrade dependencies to latest permitted versions during compile",
    )

    args = parser.parse_args()
    uv_cmd = find_uv_executable()

    success = True
    if args.variant in ("cpu", "all"):
        ok = lock_cpu(check=args.check, upgrade=args.upgrade, uv_cmd=uv_cmd)
        success = success and ok

    if args.variant in ("gpu", "all"):
        ok = lock_gpu(check=args.check, upgrade=args.upgrade, uv_cmd=uv_cmd)
        success = success and ok

    if args.check:
        if success:
            print("OK: Lockfiles are in sync with requirements.")
            return 0
        else:
            print("FAIL: Lockfiles are out of sync with requirements. Run 'python backend/scripts/lock_dependencies.py' to update.", file=sys.stderr)
            return 1

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
