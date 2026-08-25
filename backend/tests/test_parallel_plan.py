"""Default parallelism resolution must scale yet never compromise safety."""

from __future__ import annotations

from tests.parallel_plan import (
    MEMORY_BUDGET_BYTES_PER_WORKER,
    MAX_WORKERS,
    detect_cgroup_memory_limit_bytes,
    detect_cpu_capacity,
    resolve_worker_count,
    should_auto_parallelize,
)

AMPLE_MEMORY = 64 * 1024 * 1024 * 1024


def test_default_scales_to_cpu_capacity():
    assert resolve_worker_count({}, cpu_capacity=12, total_memory_bytes=AMPLE_MEMORY) == 12


def test_single_cpu_machine_stays_serial():
    assert resolve_worker_count({}, cpu_capacity=1, total_memory_bytes=AMPLE_MEMORY) is None


def test_low_memory_machine_is_capped_below_cpu_count():
    memory = 3 * MEMORY_BUDGET_BYTES_PER_WORKER
    assert resolve_worker_count({}, cpu_capacity=32, total_memory_bytes=memory) == 3


def test_unknown_memory_falls_back_to_cpu_capacity():
    assert resolve_worker_count({}, cpu_capacity=8, total_memory_bytes=None) == 8


def test_env_override_pins_worker_count():
    assert resolve_worker_count(
        {"MARKER_TEST_WORKERS": "4"}, cpu_capacity=12, total_memory_bytes=AMPLE_MEMORY
    ) == 4


def test_env_override_cannot_bypass_resource_safety_caps():
    assert resolve_worker_count(
        {"MARKER_TEST_WORKERS": "1000000"},
        cpu_capacity=1,
        total_memory_bytes=AMPLE_MEMORY,
    ) == MAX_WORKERS
    assert resolve_worker_count(
        {"MARKER_TEST_WORKERS": "8"},
        cpu_capacity=12,
        total_memory_bytes=2 * MEMORY_BUDGET_BYTES_PER_WORKER,
    ) == 2


def test_env_override_can_force_serial():
    for value in ("0", "1", "off", "disable", "serial", "none"):
        assert resolve_worker_count(
            {"MARKER_TEST_WORKERS": value}, cpu_capacity=12, total_memory_bytes=AMPLE_MEMORY
        ) is None


def test_env_override_auto_uses_capacity():
    assert resolve_worker_count(
        {"MARKER_TEST_WORKERS": "auto"}, cpu_capacity=6, total_memory_bytes=AMPLE_MEMORY
    ) == 6


def test_invalid_env_override_falls_back_to_capacity():
    assert resolve_worker_count(
        {"MARKER_TEST_WORKERS": "banana"}, cpu_capacity=6, total_memory_bytes=AMPLE_MEMORY
    ) == 6


def test_nested_worker_never_reresolves():
    assert resolve_worker_count(
        {"PYTEST_XDIST_WORKER": "gw0"}, cpu_capacity=12, total_memory_bytes=AMPLE_MEMORY
    ) is None


def test_detected_cpu_capacity_is_positive():
    assert detect_cpu_capacity() >= 1


def test_cgroup_memory_detection_uses_smallest_finite_limit(tmp_path):
    v2 = tmp_path / "memory.max"
    v1 = tmp_path / "memory.limit_in_bytes"
    unlimited = tmp_path / "unlimited"
    v2.write_text(str(4 * MEMORY_BUDGET_BYTES_PER_WORKER), encoding="ascii")
    v1.write_text(str(3 * MEMORY_BUDGET_BYTES_PER_WORKER), encoding="ascii")
    unlimited.write_text("max", encoding="ascii")

    assert detect_cgroup_memory_limit_bytes((v2, v1, unlimited)) == (
        3 * MEMORY_BUDGET_BYTES_PER_WORKER
    )


def test_default_parallelism_is_limited_to_broad_runs():
    assert should_auto_parallelize(("-q",), {}) is True
    assert should_auto_parallelize(("backend/tests", "-q"), {}) is True
    assert (
        should_auto_parallelize(("backend/tests/test_parallel_plan.py", "-q"), {})
        is False
    )
    assert (
        should_auto_parallelize(
            (
                "backend/tests/test_parallel_plan.py::"
                "test_default_scales_to_cpu_capacity",
            ),
            {},
        )
        is False
    )
    assert should_auto_parallelize(("-k", "parallel_plan"), {}) is False
    assert should_auto_parallelize(("--last-failed",), {}) is False


def test_worker_override_can_parallelize_a_focused_run():
    assert should_auto_parallelize(
        ("backend/tests/test_parallel_plan.py",), {"MARKER_TEST_WORKERS": "4"}
    ) is True
