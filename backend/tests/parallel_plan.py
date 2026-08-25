"""Default test parallelism resolution.

The suite scales to available capacity for a plain `python -m pytest`
run. Resolution happens in the repository-root conftest rather than
`addopts` so a bare environment without pytest-xdist (the
cross-platform conformance matrix installs pytest only) still runs
serially instead of failing on an unknown option.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

WORKERS_ENV = "MARKER_TEST_WORKERS"
SERIAL_VALUES = frozenset({"0", "1", "off", "disable", "disabled", "serial", "none"})
DEFAULT_DISTRIBUTION = "loadscope"

# Each worker is a full interpreter importing the app package. This budget
# keeps many-core/low-memory machines from swapping themselves to death.
MEMORY_BUDGET_BYTES_PER_WORKER = 768 * 1024 * 1024
MAX_WORKERS = 32
CGROUP_MEMORY_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)

_FOCUSED_OPTIONS = frozenset(
    {
        "--collect-only",
        "--failed-first",
        "--ff",
        "--last-failed",
        "--lf",
        "--stepwise",
        "--stepwise-skip",
        "--sw",
        "-k",
        "-m",
    }
)
_MEMORY_AUTO = object()


def should_auto_parallelize(
    invocation_args: Iterable[str], env: Mapping[str, str]
) -> bool:
    """Use implicit xdist only for broad runs or an explicit worker request."""
    if (env.get(WORKERS_ENV) or "").strip():
        return True

    args = tuple(str(arg) for arg in invocation_args)
    for arg in args:
        target = arg.split("::", 1)[0].lower()
        if target.endswith(".py"):
            return False
        if arg in _FOCUSED_OPTIONS:
            return False
        if arg.startswith(("-k=", "-m=")):
            return False
    return True


def detect_cpu_capacity() -> int:
    """Logical CPUs usable by this process, honoring affinity and cgroups."""
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        detected = process_cpu_count()
        if detected:
            return detected
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        try:
            detected = len(sched_getaffinity(0))
            if detected:
                return detected
        except OSError:
            pass
    return os.cpu_count() or 1


def detect_cgroup_memory_limit_bytes(
    paths: Iterable[Path] = CGROUP_MEMORY_LIMIT_PATHS,
) -> int | None:
    """Return smallest finite cgroup memory limit exposed by Linux."""
    limits: list[int] = []
    for path in paths:
        try:
            raw = path.read_text(encoding="ascii").strip()
            value = int(raw)
        except (OSError, UnicodeError, ValueError):
            continue
        # cgroup v1 uses enormous near-int64 values to mean unlimited.
        if 0 < value < 2**60:
            limits.append(value)
    return min(limits) if limits else None


def detect_total_memory_bytes() -> int | None:
    """Effective physical/cgroup memory, or None when undetectable."""
    physical: int | None = None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        page_size = page_count = None
    if page_size and page_count:
        physical = int(page_size) * int(page_count)

    if physical is None and os.name == "nt":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                physical = int(status.ullTotalPhys)
        except Exception:  # noqa: BLE001 - capacity probe must never break startup
            pass

    cgroup = detect_cgroup_memory_limit_bytes() if os.name == "posix" else None
    detected = [value for value in (physical, cgroup) if value is not None]
    return min(detected) if detected else None


def resolve_worker_count(
    env: Mapping[str, str],
    cpu_capacity: int | None = None,
    total_memory_bytes: int | None | object = _MEMORY_AUTO,
) -> int | None:
    """Return the worker count for this run, or None to stay serial."""
    # A worker process re-runs collection; re-resolving would nest xdist.
    if env.get("PYTEST_XDIST_WORKER"):
        return None

    memory = (
        detect_total_memory_bytes()
        if total_memory_bytes is _MEMORY_AUTO
        else total_memory_bytes
    )
    memory_limit = MAX_WORKERS
    if memory:
        memory_limit = max(1, int(memory // MEMORY_BUDGET_BYTES_PER_WORKER))

    raw = (env.get(WORKERS_ENV) or "").strip().lower()
    if raw in SERIAL_VALUES:
        return None
    if raw and raw not in ("auto", ""):
        try:
            requested = int(raw)
        except ValueError:
            requested = None
        if requested is not None:
            workers = min(requested, MAX_WORKERS, memory_limit)
            return workers if workers > 1 else None

    cpus = cpu_capacity if cpu_capacity is not None else detect_cpu_capacity()
    workers = max(1, min(cpus, MAX_WORKERS))

    workers = min(workers, memory_limit)

    return workers if workers > 1 else None
