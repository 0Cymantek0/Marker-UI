"""Hybrid OCR setup and model-cache helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MODEL_IDS = {
    "glm_ocr": "zai-org/GLM-OCR",
    "paddleocr_vl": "PaddlePaddle/PaddleOCR-VL",
}

ENV_MODEL_DIRS = {
    "glm_ocr": "MARKER_GLM_OCR_MODEL_DIR",
    "paddleocr_vl": "MARKER_PADDLE_OCR_VL_MODEL_DIR",
}


def default_model_root() -> Path:
    return Path(os.environ.get("MARKER_HYBRID_OCR_MODEL_ROOT", Path.home() / ".cache" / "marker-ui" / "hybrid-ocr"))


def expected_model_dir(engine: str) -> Path:
    configured = os.environ.get(ENV_MODEL_DIRS[engine])
    if configured:
        return Path(configured).expanduser()
    return default_model_root() / engine


def model_snapshot_present(engine: str) -> bool:
    model_dir = expected_model_dir(engine)
    if not model_dir.exists():
        return False
    return any(model_dir.iterdir())


def download_model_snapshot(engine: str, *, force: bool = False) -> dict[str, Any]:
    if engine not in MODEL_IDS:
        raise ValueError(f"Unsupported Hybrid OCR engine: {engine}")
    target = expected_model_dir(engine)
    if target.exists() and any(target.iterdir()) and not force:
        return {"engine": engine, "status": "present", "path": str(target)}
    target.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - dependency normally present through marker stack
        raise RuntimeError("huggingface_hub is required for Hybrid OCR model setup.") from exc
    snapshot_download(repo_id=MODEL_IDS[engine], local_dir=str(target), local_dir_use_symlinks=False)
    return {"engine": engine, "status": "downloaded", "path": str(target)}


def hybrid_setup_status() -> dict[str, Any]:
    return {
        "model_root": str(default_model_root()),
        "engines": {
            engine: {
                "model_id": MODEL_IDS[engine],
                "model_dir": str(expected_model_dir(engine)),
                "model_present": model_snapshot_present(engine),
            }
            for engine in MODEL_IDS
        },
    }
