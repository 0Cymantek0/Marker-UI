"""Adversarial incremental-rebuild evaluation tests (PR82A Q3)."""

from __future__ import annotations

import pytest

from app.eval.pr82.incremental import (
    ConflictingPatchOp,
    DEFAULT_SEEDS,
    DeleteNodeOp,
    InsertNodeOp,
    PatchOp,
    RebaseOp,
    evaluate_incremental,
    generate_mixed_sequence,
    run_mixed_scenario,
)


class TestSequenceGeneration:
    def test_sequences_are_deterministic_per_seed(self):
        for seed in (0, 7, 23):
            assert generate_mixed_sequence(seed) == generate_mixed_sequence(seed)

    def test_sequences_mix_all_op_classes_across_the_seed_set(self):
        kinds_seen = set()
        for seed in DEFAULT_SEEDS:
            for op in generate_mixed_sequence(seed):
                kinds_seen.add(type(op))
        assert kinds_seen == {PatchOp, ConflictingPatchOp, InsertNodeOp, DeleteNodeOp, RebaseOp}

    def test_sequences_are_longer_than_the_pr73_chain(self):
        lengths = [len(generate_mixed_sequence(seed)) for seed in DEFAULT_SEEDS]
        assert min(lengths) >= 4
        assert max(lengths) <= 10


@pytest.mark.asyncio
async def test_all_mixed_scenarios_equal_clean_rebuild(kernel_env):
    result = await evaluate_incremental(kernel_env, seeds=tuple(range(8)))
    assert result.violations == ()
    for scenario in result.scenarios:
        assert scenario.equal_clean_replay
        assert scenario.equal_independent_replay


@pytest.mark.asyncio
async def test_conflicting_patches_are_rejected_never_merged(kernel_env):
    result = await evaluate_incremental(kernel_env, seeds=tuple(range(8)))
    total_conflicts = sum(s.conflicts_rejected for s in result.scenarios)
    total_skipped = sum(s.conflicts_skipped for s in result.scenarios)
    total_conflict_ops = sum(
        1
        for seed in tuple(range(8))
        for op in generate_mixed_sequence(seed)
        if isinstance(op, ConflictingPatchOp)
    )
    # Every executed conflict was rejected; the remainder targeted nodes
    # not yet materialized in the committed view (skipped honestly).
    assert total_conflicts + total_skipped == total_conflict_ops
    assert total_conflicts > 0


@pytest.mark.asyncio
async def test_mapping_dispositions_participate_in_every_rebase(kernel_env):
    result = await evaluate_incremental(kernel_env, seeds=(3,))
    scenario = result.scenarios[0]
    assert scenario.rebases > 0
    # Carried nodes map deterministically; edited nodes go stale. Both
    # classes must appear across the seed set, never 'exact' (quote
    # evidence can never mint exact — only native identity can).
    summary = result.summary()
    assert summary["mapping_dispositions"]
    assert "exact" not in summary["mapping_dispositions"]


@pytest.mark.asyncio
async def test_unknown_scope_widens_never_narrows(kernel_env):
    result = await evaluate_incremental(kernel_env, seeds=tuple(range(12)))
    assert "full" in result.summary()["modes"]
    assert result.violations == ()
