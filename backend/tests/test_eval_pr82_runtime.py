"""Runtime/data-plane fault matrix tests (PR82A Q7/Q8)."""

from __future__ import annotations

import pytest

from app.eval.pr82.runtime import evaluate_runtime


@pytest.mark.asyncio
async def test_fault_matrix_all_invariants_hold(kernel_env, tmp_path):
    result = await evaluate_runtime(kernel_env, tmp_path / "artifacts")
    held = {fault.fault_id: fault.held for fault in result.faults}
    assert result.violation_count == 0, held


@pytest.mark.asyncio
async def test_fault_matrix_covers_the_declared_fault_classes(kernel_env, tmp_path):
    result = await evaluate_runtime(kernel_env, tmp_path / "artifacts")
    fault_ids = {fault.fault_id for fault in result.faults}
    assert fault_ids == {
        "crash_before_linearization",
        "crash_after_linearization",
        "stale_worker_publication",
        "divergent_result",
        "duplicate_execution_single_truth",
        "cancelled_owner_cannot_complete",
        "slow_consumer_never_blocks_truth",
        "restart_recovery_explicit",
        "artifact_tamper_fails_closed",
    }
    summary = result.summary()
    assert summary["held"] == len(result.faults)
    assert summary["violations"] == 0


@pytest.mark.asyncio
async def test_fault_result_reports_detail_for_every_fault(kernel_env, tmp_path):
    result = await evaluate_runtime(kernel_env, tmp_path / "artifacts")
    for fault in result.faults:
        assert fault.invariant
        assert fault.detail  # evidence, not just a boolean badge
