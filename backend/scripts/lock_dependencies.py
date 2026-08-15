#!/usr/bin/env python3
"""Deterministic backend dependency locking tool using uv.

Compiles top-level dependency requirements into exact, reproducible lockfiles
for both CPU and GPU variants.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
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
    extra_index_url: str,
    upgrade: bool = False,
    uv_cmd: str | None = None,
) -> None:
    """Compile requirement files into a pinned lockfile using uv pip compile."""
    uv = uv_cmd or find_uv_executable()
    cmd = [
        uv,
        "pip",
        "compile",
        *[str(p) for p in requirements_files],
        "--extra-index-url",
        extra_index_url,
        "--index-strategy",
        "unsafe-best-match",
        "--output-file",
        str(output_lock),
    ]
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
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )


def extract_pinned_packages(content: str) -> dict[str, str]:
    """Parse exact package==version mappings from a lockfile."""
    pins: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            parts = line.split("==")
            pkg = parts[0].strip().lower()
            ver = parts[1].split(";")[0].split()[0].strip()
            pins[pkg] = ver
    return pins


def check_lockfile(
    *,
    requirements_files: list[Path],
    expected_lock: Path,
    extra_index_url: str,
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
            extra_index_url=extra_index_url,
            upgrade=False,
            uv_cmd=uv_cmd,
        )
        current_content = expected_lock.read_text(encoding="utf-8").strip()
        compiled_content = tmp_lock.read_text(encoding="utf-8").strip()

        current_pins = extract_pinned_packages(current_content)
        compiled_pins = extract_pinned_packages(compiled_content)

        if current_pins != compiled_pins:
            diff_added = set(compiled_pins.items()) - set(current_pins.items())
            diff_removed = set(current_pins.items()) - set(compiled_pins.items())
            print(f"Drift detected in {expected_lock.name}:", file=sys.stderr)
            if diff_added:
                print(f"  New/Changed in compilation: {diff_added}", file=sys.stderr)
            if diff_removed:
                print(f"  Missing from compilation: {diff_removed}", file=sys.stderr)
            return False
        return True


def lock_cpu(*, check: bool = False, upgrade: bool = False, uv_cmd: str | None = None) -> bool:
    """Lock or check CPU variant dependencies."""
    reqs = [REQ_CORE, REQ_CPU]
    if check:
        return check_lockfile(
            requirements_files=reqs,
            expected_lock=LOCK_CPU,
            extra_index_url=CPU_EXTRA_INDEX,
            uv_cmd=uv_cmd,
        )
    compile_lockfile(
        requirements_files=reqs,
        output_lock=LOCK_CPU,
        extra_index_url=CPU_EXTRA_INDEX,
        upgrade=upgrade,
        uv_cmd=uv_cmd,
    )
    print(f"Successfully generated {LOCK_CPU.relative_to(REPO_ROOT)}")
    return True


def lock_gpu(*, check: bool = False, upgrade: bool = False, uv_cmd: str | None = None) -> bool:
    """Lock or check GPU variant dependencies."""
    reqs = [REQ_CORE, REQ_GPU]
    if check:
        return check_lockfile(
            requirements_files=reqs,
            expected_lock=LOCK_GPU,
            extra_index_url=GPU_EXTRA_INDEX,
            uv_cmd=uv_cmd,
        )
    compile_lockfile(
        requirements_files=reqs,
        output_lock=LOCK_GPU,
        extra_index_url=GPU_EXTRA_INDEX,
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
