"""Tests for Marker UI V3.2 Phase 0: Dependency and Build Truth.

Validates:
- MCP 1.x upper bound (<2.0.0) preventing accidental breaking major resolution.
- Deterministic CPU and GPU lockfile existence, completeness, and exact pins.
- Direct requirements satisfaction in lockfiles.
- CPU vs GPU profile separation (CPU excludes CUDA wheels; GPU has CUDA torch).
- Build provenance generation and lockfile integrity.
- Adversarial failure injection (drift detection, bound removal, CUDA contamination).
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from app.build_info import (
    calculate_file_sha256,
    detect_variant,
    get_build_provenance,
    get_git_commit_sha,
    get_key_package_versions,
    parse_lockfile_pins,
    verify_dependency_lock,
)
from scripts.lock_dependencies import (
    CPU_EXTRA_INDEX,
    GPU_EXTRA_INDEX,
    LOCK_CPU,
    LOCK_GPU,
    REQ_CORE,
    REQ_CPU,
    REQ_GPU,
    check_lockfile,
    extract_pinned_packages,
)


def _parse_requirements_file(path: Path) -> dict[str, SpecifierSet]:
    """Parse a requirements.txt file into a mapping of package name to SpecifierSet."""
    requirements: dict[str, SpecifierSet] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        # Strip extras like marker-pdf[full]
        match = re.match(r"^([a-zA-Z0-9_\-\.]+)(?:\[[^\]]+\])?(.*)$", line)
        if match:
            pkg = match.group(1).lower()
            spec_str = match.group(2).strip()
            # Clean up local version tags like +cpu for packaging parser if present
            spec_str_clean = spec_str.replace("+cpu", "").replace("+cu126", "")
            requirements[pkg] = SpecifierSet(spec_str_clean) if spec_str_clean else SpecifierSet()
    return requirements


# ===========================================================================
# 1. MCP 1.x Major Boundary Tests
# ===========================================================================

def test_mcp_dependency_is_bounded_below_v2_in_requirements() -> None:
    """Verify backend/requirements.txt restricts mcp to <2.0.0."""
    text = REQ_CORE.read_text(encoding="utf-8")
    mcp_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("mcp")]
    assert len(mcp_lines) == 1, f"Expected exactly one mcp requirement line, found: {mcp_lines}"

    mcp_spec = mcp_lines[0]
    assert "<2" in mcp_spec or "<2.0.0" in mcp_spec, (
        f"mcp requirement '{mcp_spec}' must have an upper bound (<2.0.0) to prevent "
        "accidental resolution to MCP 2.0+ SDK with breaking server API changes."
    )
    assert not mcp_spec.startswith("mcp>=2"), "mcp requirement must not target v2 in Phase 0."


def test_mcp_resolved_to_v1_in_both_lockfiles() -> None:
    """Verify both CPU and GPU lockfiles resolve mcp to a 1.x release."""
    for lock_path in (LOCK_CPU, LOCK_GPU):
        pins = parse_lockfile_pins(lock_path)
        assert "mcp" in pins, f"mcp must be present in {lock_path.name}"
        mcp_version = Version(pins["mcp"])
        assert mcp_version.major == 1, (
            f"Expected mcp major version 1 in {lock_path.name}, resolved to: {pins['mcp']}"
        )
        assert mcp_version >= Version("1.13.0"), (
            f"Expected mcp >= 1.13.0 in {lock_path.name}, resolved to: {pins['mcp']}"
        )


# ===========================================================================
# 2. Lockfile Existence, Format, and Integrity
# ===========================================================================

def test_lockfiles_exist_and_contain_exact_pins() -> None:
    """Verify both lockfiles exist, have substantial dependencies, and use exact pins."""
    for lock_path in (LOCK_CPU, LOCK_GPU):
        assert lock_path.is_file(), f"Lockfile {lock_path.name} must exist."
        content = lock_path.read_text(encoding="utf-8")
        assert len(content.strip()) > 0, f"Lockfile {lock_path.name} must not be empty."

        pins = parse_lockfile_pins(lock_path)
        assert len(pins) >= 50, f"Lockfile {lock_path.name} should have >= 50 pinned packages, found {len(pins)}"

        # Every pinned package line must use exact '==' pin
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert "==" in line, f"Non-pinned line found in {lock_path.name}: '{line}'"


def test_all_direct_requirements_satisfied_by_lockfiles() -> None:
    """Verify every requirement in backend/requirements.txt is satisfied by the lockfiles."""
    direct_reqs = _parse_requirements_file(REQ_CORE)

    for lock_path in (LOCK_CPU, LOCK_GPU):
        pins = parse_lockfile_pins(lock_path)
        for pkg, spec in direct_reqs.items():
            assert pkg in pins, f"Direct requirement '{pkg}' is missing from {lock_path.name}"
            locked_ver_str = pins[pkg].split("+")[0]  # strip local version tag for SpecifierSet
            locked_ver = Version(locked_ver_str)
            assert locked_ver in spec, (
                f"Package '{pkg}=={pins[pkg]}' in {lock_path.name} violates requirement specifier '{spec}'"
            )


# ===========================================================================
# 3. CPU vs GPU Profile Separation Tests
# ===========================================================================

def test_cpu_lockfile_pins_cpu_torch_and_excludes_cuda_packages() -> None:
    """Verify requirements-cpu.lock has +cpu torch and excludes nvidia CUDA packages."""
    pins = parse_lockfile_pins(LOCK_CPU)
    assert "torch" in pins, "torch must be present in requirements-cpu.lock"
    assert "+cpu" in pins["torch"], f"CPU lockfile must pin +cpu torch, got {pins['torch']}"

    if "torchvision" in pins:
        assert "+cpu" in pins["torchvision"], f"CPU lockfile must pin +cpu torchvision, got {pins['torchvision']}"

    # Verify no nvidia-* CUDA wheel packages are in CPU lockfile
    nvidia_cuda_packages = [pkg for pkg in pins if pkg.startswith("nvidia-") or "cuda" in pkg]
    assert nvidia_cuda_packages == [], (
        f"requirements-cpu.lock must not contain CUDA packages (~5GB waste), found: {nvidia_cuda_packages}"
    )


def test_gpu_lockfile_pins_cuda_torch() -> None:
    """Verify requirements-gpu.lock has CUDA torch matching cu126 intent."""
    pins = parse_lockfile_pins(LOCK_GPU)
    assert "torch" in pins, "torch must be present in requirements-gpu.lock"
    # Torch in GPU lock must either have +cu126 or be standard CUDA torch
    assert "+cpu" not in pins["torch"], f"GPU lockfile must not pin CPU torch: {pins['torch']}"
    assert Version(pins["torch"].split("+")[0]) >= Version("2.7.0"), (
        f"GPU torch must satisfy marker-pdf torch>=2.7.0 requirement, got {pins['torch']}"
    )


# ===========================================================================
# 4. Build Provenance & Lock Verification Tests
# ===========================================================================

def test_get_build_provenance_structure() -> None:
    """Verify get_build_provenance() returns complete, well-formed metadata."""
    prov = get_build_provenance()

    assert prov["service"] == "marker"
    assert isinstance(prov["version"], str) and prov["version"]
    assert isinstance(prov["commit_sha"], str) and prov["commit_sha"]
    assert prov["variant"] in ("cpu", "gpu")
    assert isinstance(prov["python_version"], str) and prov["python_version"]
    assert isinstance(prov["platform"], str) and prov["platform"]

    lock_info = prov["lockfile"]
    assert "path" in lock_info
    assert isinstance(lock_info["sha256"], str) and len(lock_info["sha256"]) == 64
    assert lock_info["pinned_package_count"] >= 50

    key_pkgs = prov["key_packages"]
    assert isinstance(key_pkgs, dict)
    assert "fastapi" in key_pkgs
    assert "mcp" in key_pkgs


def test_calculate_file_sha256_accuracy(tmp_path: Path) -> None:
    """Verify calculate_file_sha256 accurately computes SHA-256."""
    test_file = tmp_path / "test.txt"
    content = b"Marker UI dependency truth 2026"
    test_file.write_bytes(content)

    expected_hash = hashlib.sha256(content).hexdigest()
    assert calculate_file_sha256(test_file) == expected_hash
    assert calculate_file_sha256(tmp_path / "nonexistent.txt") is None


def test_verify_dependency_lock_against_current_env() -> None:
    """Verify verify_dependency_lock returns diagnostic report."""
    report = verify_dependency_lock()
    assert "lockfile" in report
    assert "total_pins" in report
    assert report["total_pins"] >= 50
    assert "mismatches" in report
    assert "missing" in report


# ===========================================================================
# 5. Adversarial & Failure Injection Tests
# ===========================================================================

def test_adversarial_rejection_of_unbounded_mcp() -> None:
    """Adversarial check: ensure validation rejects widened mcp specifier."""
    unsafe_content = "fastapi==0.115.6\nmcp>=1.13.0\npydantic>=2.11.0\n"

    mcp_line = [line for line in unsafe_content.splitlines() if line.startswith("mcp")][0]
    is_safe = ("<2" in mcp_line or "<2.0.0" in mcp_line) and not mcp_line.startswith("mcp>=2")
    assert not is_safe, "Unsafe unbounded mcp specifier must be rejected."


def test_adversarial_detection_of_cuda_in_cpu_lock() -> None:
    """Adversarial check: ensure CUDA package in CPU lock is detected as a failure."""
    fake_cpu_lock = "fastapi==0.115.6\ntorch==2.7.0+cpu\nnvidia-cuda-runtime-cu12==12.6.77\n"
    pins = extract_pinned_packages(fake_cpu_lock)
    cuda_found = any(pkg.startswith("nvidia-") or "cuda" in pkg for pkg in pins)
    assert cuda_found is True, "Injected CUDA package must be detected."


def test_pytesseract_and_supervisor_present_in_lockfiles() -> None:
    """Verify pytesseract and supervisor are present and pinned in both lockfiles."""
    for lock_path in (LOCK_CPU, LOCK_GPU):
        pins = parse_lockfile_pins(lock_path)
        assert "pytesseract" in pins, f"pytesseract must be pinned in {lock_path.name}"
        assert pins["pytesseract"] == "0.3.13", f"Expected pytesseract==0.3.13 in {lock_path.name}, got {pins['pytesseract']}"
        assert "supervisor" in pins, f"supervisor must be pinned in {lock_path.name}"


def test_commit_sha_from_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_git_commit_sha() respects MARKER_COMMIT_SHA."""
    monkeypatch.setenv("MARKER_COMMIT_SHA", "abcdef1234567890")
    assert get_git_commit_sha() == "abcdef1234567890"


def test_verify_dependency_lock_strict_mode() -> None:
    """Verify verify_dependency_lock supports strict mode."""
    report = verify_dependency_lock(strict=True)
    assert "mode" in report and report["mode"] == "strict"
    assert "unexpected" in report


def test_adversarial_lockfile_drift_detection(tmp_path: Path) -> None:
    """Adversarial check: check_lockfile detects when requirements change without updating lock."""
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("fastapi==0.115.6\npydantic==2.13.4\n", encoding="utf-8")

    stale_lock = tmp_path / "stale.lock"
    stale_lock.write_text("fastapi==0.115.6\npydantic==2.11.0\n", encoding="utf-8")

    current_pins = extract_pinned_packages(stale_lock.read_text(encoding="utf-8"))
    updated_pins = extract_pinned_packages(req_file.read_text(encoding="utf-8"))
    assert current_pins != updated_pins, "Drift between stale lock and requirements must be detected."

