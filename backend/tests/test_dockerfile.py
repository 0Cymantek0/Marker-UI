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
