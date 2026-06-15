"""Real per-stage progress by tapping marker/surya's tqdm bars.

marker and surya report fine-grained progress through ``tqdm`` bars written to
stderr (e.g. ``Recognizing Text: 45/101``). The logging-based stage hints in
``TaskManager`` cannot see those counts, so the bar used to stall in a single
band for the entire (dominant) OCR pass.

This module monkeypatches ``tqdm`` once so every live bar updates a per-job
progress value. Each known stage description maps to a band of the overall
0-100 scale; within a band we interpolate by the bar's ``n / total``. The OCR
("Recognizing Text") band is the widest because it dominates wall-clock time.

The active job for a bar is resolved from the running thread via
``active_conversion_threads`` (populated by ``TaskManager._run_conversion``),
so multiple concurrent conversions stay isolated.
"""

from __future__ import annotations

import threading
from typing import Callable

# (substring matched against tqdm desc, label, band_start, band_end)
# Ordered by how marker runs them. Bands are contiguous and weighted by the
# real time each stage costs; OCR is the heavyweight.
_STAGES: list[tuple[str, str, float, float]] = [
    ("detecting bboxes", "Detecting text regions", 12.0, 22.0),
    ("recognizing layout", "Analyzing document layout", 22.0, 30.0),
    ("running ocr error detection", "Checking OCR quality", 30.0, 33.0),
    ("recognizing text", "Recognizing text (OCR)", 33.0, 80.0),
    ("recognizing tables", "Recognizing tables", 80.0, 86.0),
    ("running page extraction", "Extracting page content", 86.0, 90.0),
    ("llm processors running", "Refining with LLM", 90.0, 96.0),
    ("running", "Refining with LLM", 90.0, 96.0),  # generic LLM processor bars
]


def _match_stage(desc: str) -> tuple[str, float, float] | None:
    d = (desc or "").lower()
    for needle, label, start, end in _STAGES:
        if needle in d:
            return label, start, end
    return None


# Callback set by TaskManager: (job_id, percent, status_label) -> None
_reporter: Callable[[str, int, str], None] | None = None
_installed = False
_lock = threading.Lock()


def set_reporter(reporter: Callable[[str, int, str], None]) -> None:
    """Register the sink that receives (job_id, percent, label) updates."""
    global _reporter
    _reporter = reporter


def _resolve_job_id() -> str | None:
    # Imported lazily to avoid a circular import at module load.
    from app.services.task_manager import active_conversion_threads

    return active_conversion_threads.get(threading.get_ident())


def _emit(desc: str, n: float, total: float) -> None:
    if _reporter is None or not total or total <= 0:
        return
    job_id = _resolve_job_id()
    if not job_id:
        return
    stage = _match_stage(desc)
    if stage is None:
        return
    label, start, end = stage
    frac = max(0.0, min(1.0, n / total))
    percent = int(round(start + (end - start) * frac))
    detail = f"{label} ({int(n)}/{int(total)})" if total > 1 else label
    _reporter(job_id, percent, detail)


def install() -> None:
    """Monkeypatch ``tqdm.tqdm`` to mirror live bar progress. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        try:
            import tqdm as tqdm_mod
        except Exception:  # noqa: BLE001 - tqdm always present with marker, but be safe
            return

        tqdm_cls = tqdm_mod.tqdm
        orig_update = tqdm_cls.update
        orig_init = tqdm_cls.__init__
        orig_close = tqdm_cls.close

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            orig_init(self, *args, **kwargs)
            try:
                _emit(getattr(self, "desc", "") or "", 0, getattr(self, "total", 0) or 0)
            except Exception:  # noqa: BLE001 - progress is best effort
                pass

        def patched_update(self, n=1):  # type: ignore[no-untyped-def]
            result = orig_update(self, n)
            try:
                _emit(
                    getattr(self, "desc", "") or "",
                    getattr(self, "n", 0) or 0,
                    getattr(self, "total", 0) or 0,
                )
            except Exception:  # noqa: BLE001 - progress is best effort
                pass
            return result

        def patched_close(self):  # type: ignore[no-untyped-def]
            try:
                _emit(
                    getattr(self, "desc", "") or "",
                    getattr(self, "total", 0) or 0,
                    getattr(self, "total", 0) or 0,
                )
            except Exception:  # noqa: BLE001 - progress is best effort
                pass
            return orig_close(self)

        tqdm_cls.update = patched_update
        tqdm_cls.__init__ = patched_init
        tqdm_cls.close = patched_close
        _installed = True
