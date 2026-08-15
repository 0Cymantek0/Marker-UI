"""Verify the Dockerfile and supervisord.conf fix the build/runtime blockers.

Each test maps to a specific defect documented in the issue #8 investigation:

- D1: ffmpeg installed via apt
- D3: supervisord sets HOME/HF_HOME/TRANSFORMERS_CACHE for appuser
- D4: Dockerfile creates huggingface cache dir under volume-backed path
- D8: no pnpm approve-builds call (pnpm 9.x lacks the subcommand)
- D9: pnpm-workspace.yaml is gone (vestigial, malformed for pnpm 9.x)
- D10: Dockerfile creates /var/log/supervisor + /run/supervisor before chown
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_SUPERVISORD = _REPO_ROOT / "supervisord.conf"
_WORKSPACE_YAML = _REPO_ROOT / "frontend" / "pnpm-workspace.yaml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- D1: ffmpeg in apt install ------------------------------------------------
def test_runtime_image_installs_ffmpeg_for_video_converter() -> None:
    text = _read(_DOCKERFILE)
    assert "ffmpeg" in text
    assert "apt-get install" in text


# --- D8: pnpm approve-builds removed ------------------------------------------
def test_dockerfile_does_not_call_pnpm_approve_builds() -> None:
    text = _read(_DOCKERFILE)
    assert "approve-builds" not in text, (
        "pnpm approve-builds requires pnpm v10+; Dockerfile pins pnpm 9.x"
    )


# --- D9: vestigial pnpm-workspace.yaml deleted --------------------------------
def test_pnpm_workspace_yaml_does_not_exist() -> None:
    assert not _WORKSPACE_YAML.exists(), (
        "pnpm-workspace.yaml is vestigial — not a monorepo, and the file "
        "lacked the packages: field required by pnpm 9.x"
    )


def test_dockerfile_does_not_copy_pnpm_workspace_yaml() -> None:
    text = _read(_DOCKERFILE)
    assert "pnpm-workspace.yaml" not in text, (
        "Dockerfile should not copy the deleted pnpm-workspace.yaml"
    )


# --- D10: supervisor runtime dirs created before chown ------------------------
def test_dockerfile_creates_supervisor_dirs_before_chown() -> None:
    text = _read(_DOCKERFILE)
    assert "/var/log/supervisor" in text and "/run/supervisor" in text, (
        "pip install supervisor does not create /var/log/supervisor or "
        "/run/supervisor; the Dockerfile must mkdir them before chown"
    )
    mkdir_line = next(
        (line for line in text.splitlines() if "/var/log/supervisor" in line and "mkdir" in line),
        "",
    )
    assert mkdir_line, "mkdir must precede chown for supervisor runtime dirs"


# --- D4: HF cache dir created under volume-backed path ------------------------
def test_dockerfile_creates_huggingface_cache_dir() -> None:
    text = _read(_DOCKERFILE)
    assert "/app/backend/data/huggingface" in text, (
        "HF model cache must live under /app/backend/data (the marker-data "
        "volume mount) so weights persist across container restarts"
    )


# --- D3: supervisord sets HOME + HF_HOME for appuser --------------------------
def test_supervisord_sets_home_for_appuser() -> None:
    text = _read(_SUPERVISORD)
    assert 'HOME="/app"' in text, (
        "Without HOME set, appuser inherits HOME=/root from root supervisord "
        "and gets PermissionError on the HF cache"
    )


def test_supervisord_sets_hf_home() -> None:
    text = _read(_SUPERVISORD)
    assert "HF_HOME=" in text, (
        "HF_HOME must point under the volume-backed data dir so model "
        "downloads persist and appuser can write to them"
    )


def test_supervisord_sets_transformers_cache() -> None:
    text = _read(_SUPERVISORD)
    assert "TRANSFORMERS_CACHE=" in text, (
        "TRANSFORMERS_CACHE covers older HF library code paths that read "
        "it instead of HF_HOME"
    )


# ---------------------------------------------------------------------------
# CPU/GPU torch split — prevents ~5 GB of nvidia-*-cu12 CUDA packages from
# being pulled into the default (CPU-only) Docker image.
#
# marker-pdf depends on torch>=2.7.0.  On Linux x86_64 the PyPI torch wheel
# depends on 14 nvidia-*-cu12 packages (~5 GB).  Pre-installing CPU torch
# from the dedicated CPU index satisfies the constraint so pip skips every
# nvidia-* dependency.
# ---------------------------------------------------------------------------

_REQUIREMENTS_CPU = _REPO_ROOT / "backend" / "requirements-cpu.txt"
_REQUIREMENTS_GPU = _REPO_ROOT / "backend" / "requirements-gpu.txt"


def test_dockerfile_has_variant_build_arg() -> None:
    """Dockerfile must accept VARIANT=cpu|gpu to select torch flavour."""
    text = _read(_DOCKERFILE)
    assert "ARG VARIANT" in text, (
        "Dockerfile must declare ARG VARIANT so the build can select "
        "CPU or GPU torch"
    )


def test_dockerfile_installs_cpu_torch_by_default() -> None:
    """Default VARIANT=cpu must install from requirements-cpu.lock."""
    text = _read(_DOCKERFILE)
    assert "requirements-cpu.lock" in text, (
        "Dockerfile must install requirements-cpu.lock (CPU torch + locked deps) to avoid "
        "pulling 14 nvidia-*-cu12 CUDA packages from PyPI"
    )


def test_dockerfile_supports_gpu_variant() -> None:
    """VARIANT=gpu must install from requirements-gpu.lock."""
    text = _read(_DOCKERFILE)
    assert "requirements-gpu.lock" in text, (
        "Dockerfile must support VARIANT=gpu to install CUDA torch + locked deps"
    )


def test_cpu_requirements_pin_cpu_torch_index() -> None:
    """requirements-cpu.txt must point at the CPU-only pytorch index."""
    text = _read(_REQUIREMENTS_CPU)
    assert "download.pytorch.org/whl/cpu" in text, (
        "CPU requirements must use the dedicated CPU wheel index so pip "
        "never resolves to the CUDA-bundled PyPI torch"
    )
    assert "torch" in text and "+cpu" in text, (
        "CPU requirements must pin a +cpu torch wheel"
    )


def test_gpu_requirements_pin_cuda_torch_index() -> None:
    """requirements-gpu.txt must point at the CUDA pytorch index."""
    text = _read(_REQUIREMENTS_GPU)
    assert "download.pytorch.org/whl/cu126" in text, (
        "GPU requirements must use the cu126 wheel index for CUDA torch"
    )
    assert "torch" in text, (
        "GPU requirements must include torch"
    )


def test_compose_gpu_override_exists() -> None:
    """docker-compose.gpu.yml must pass GPU devices and set VARIANT=gpu."""
    compose_gpu = _REPO_ROOT / "docker-compose.gpu.yml"
    assert compose_gpu.exists(), (
        "docker-compose.gpu.yml override must exist for GPU deployments"
    )
    text = _read(compose_gpu)
    assert "VARIANT: gpu" in text, (
        "GPU compose override must set the VARIANT build arg to gpu"
    )
    assert "nvidia" in text, (
        "GPU compose override must pass NVIDIA devices to the container"
    )


def test_dockerfile_declares_commit_sha_provenance() -> None:
    """Dockerfile must accept ARG COMMIT_SHA and set MARKER_COMMIT_SHA."""
    text = _read(_DOCKERFILE)
    assert "ARG COMMIT_SHA" in text, "Dockerfile must declare ARG COMMIT_SHA for build provenance"
    assert "ENV MARKER_COMMIT_SHA=${COMMIT_SHA}" in text, "Dockerfile must set MARKER_COMMIT_SHA environment variable"


def test_dockerfile_avoids_unpinned_pip_install() -> None:
    """Dockerfile must not run unpinned pip install commands."""
    text = _read(_DOCKERFILE)
    assert "pip install --no-cache-dir supervisor" not in text, (
        "supervisor must be locked in dependency lockfile, not installed unpinned"
    )

