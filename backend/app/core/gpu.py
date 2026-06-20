"""GPU detection and worker-count resolution for multi-GPU scaling.

The parent orchestrator calls :func:`detect_gpus` to learn how many CUDA
devices exist, and :func:`resolve_worker_count` to fold the user's settings
(auto vs manual override) into the actual number of conversion workers to
spawn. Both are pure, side-effect-free, and degrade safely on a CPU-only box
(detected == 0 -> exactly one CPU worker).

Detection is deliberately import-light: ``torch.cuda.device_count()`` first
(fast, no subprocess), then ``nvidia-smi -L`` as a fallback for environments
where torch is CPU-only but GPUs are physically present. Never raises — a
detection failure resolves to a single CPU worker.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def detect_gpus() -> int:
    """Return the number of usable CUDA GPUs, or 0 when none / undetectable.

    Tries torch first (authoritative for what PyTorch can actually use), then
    falls back to parsing ``nvidia-smi -L``. Any error -> 0.
    """
    count = _detect_via_torch()
    if count > 0:
        return count
    return _detect_via_nvidia_smi()


def _detect_via_torch() -> int:
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.device_count())
    except Exception as exc:  # noqa: BLE001 - torch may be CPU-only or absent
        logger.debug("torch GPU detection failed: %s", exc)
    return 0


def _detect_via_nvidia_smi() -> int:
    """Count GPUs by parsing ``nvidia-smi -L`` output lines.

    Each GPU is one ``GPU N: ...`` line. Returns 0 if nvidia-smi is missing or
    errors (the common CPU-only / no-driver case).
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("nvidia-smi GPU detection failed: %s", exc)
        return 0

    return sum(
        1 for line in result.stdout.splitlines() if line.strip().startswith("GPU ")
    )


def resolve_worker_count(
    mode: str,
    manual_count: int | None,
    detected: int,
) -> int:
    """Fold settings into the number of conversion workers to spawn.

    * CPU-only (``detected <= 0``) always resolves to exactly 1 worker,
      regardless of mode — there is no GPU to pin a second worker to.
    * ``mode == "manual"`` clamps the user's ``manual_count`` to
      ``[1, detected]`` so a stale/oversized value can never spawn more
      workers than GPUs.
    * ``mode == "auto"`` (or any unknown mode) uses one worker per detected
      GPU.
    """
    if detected <= 0:
        return 1

    if mode == "manual" and manual_count is not None:
        return max(1, min(manual_count, detected))

    return detected


def device_for_worker(worker_index: int, detected: int) -> str:
    """Map a worker index to its pinned device string.

    Worker ``i`` pins to ``cuda:i`` when GPUs exist; otherwise ``cpu``.
    """
    if detected <= 0:
        return "cpu"
    return f"cuda:{worker_index}"
