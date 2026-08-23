"""PR81A OFF-vs-ON economics disposition tests.

The rule is declared ahead of results; these tests pin its mechanics so
the disposition cannot be quietly re-tuned after interpretation. Dense
and hybrid routes are judged by their own margins (PR81A established
they pay differently), and the realistic shape — dense below baseline,
hybrid rerank paying — must land on ``narrow_only``.
"""

from __future__ import annotations

import pytest

from app.eval.pr81a.decision import (
    CONTROL_TOLERANCE,
    EXPERIMENTAL_FLOOR,
    MAX_EMBEDDING_BYTES_PER_PAGE,
    MAX_RENDER_BYTES_PER_PAGE,
    MAX_WARM_QUERY_MS,
    PROMOTION_MARGIN,
    RERANK_MARGIN,
)
from app.eval.pr81a.economics_decision import (
    ALLOWED_DISPOSITIONS,
    REQUIRED_ACL_FIELDS,
    evaluate_economics_disposition,
)


def _off_quality() -> dict:
    return {
        "visual_hard_task_success_rate": 0.40,
        "text_easy_task_success_rate": 0.90,
    }


def _dense_quality(gain: float | None = -0.15) -> dict:
    """Dense route: the PR81A-committed shape is *below* baseline."""
    if gain is None:
        return {"visual_hard_task_success_rate": None,
                "text_easy_task_success_rate": 0.90}
    return {
        "visual_hard_task_success_rate": round(
            _off_quality()["visual_hard_task_success_rate"] + gain, 4
        ),
        "text_easy_task_success_rate": 0.90,
    }


def _hybrid_quality(gain: float | None = 0.30) -> dict:
    if gain is None:
        return {"visual_hard_task_success_rate": None,
                "text_easy_task_success_rate": 0.90}
    return {
        "visual_hard_task_success_rate": round(
            _off_quality()["visual_hard_task_success_rate"] + gain, 4
        ),
        "text_easy_task_success_rate": 0.90,
    }


def _off_economics() -> dict:
    return {
        "render_bytes": 1_000_000,
        "embedding_bytes": 0,
        "pages_rendered": 10,
        "avg_render_bytes_per_page": 100_000,
        "avg_embedding_bytes_per_page": 0,
        "warm_query_ms_p50": 12.0,
        "vlm_calls": 0,
        "index_build_ms": 0.0,
        "revision_rebuild_ms": 30.0,
    }


def _on_economics(**overrides) -> dict:
    base = {
        "render_bytes": 2_000_000,
        "embedding_bytes": 200_000,
        "pages_rendered": 10,
        "avg_render_bytes_per_page": 200_000,
        "avg_embedding_bytes_per_page": 2_000,
        "warm_query_ms_p50": 18.0,
        "vlm_calls": 40,
        "index_build_ms": 120.0,
        "revision_rebuild_ms": 80.0,
    }
    base.update(overrides)
    return base


def _acl(**overrides) -> dict:
    base = {
        "visual_partitions": 4,
        "partition_duplicate_bytes": 120_000,
        "partition_build_ms": 90.0,
        "deny_to_effective_ms": 5.0,
        "denied_rebuilds_required": 0,
        "authorized_universe_filter_calls": 12,
    }
    base.update(overrides)
    return base


_UNSET = object()


def _quality(arg, default_factory):
    if isinstance(arg, dict):
        return arg
    return default_factory() if arg is _UNSET else default_factory(arg)


def _evaluate(*, dense=_UNSET, hybrid=_UNSET, on_economics=None, acl=None, danger=None):
    return evaluate_economics_disposition(
        off_quality=_off_quality(),
        on_dense_quality=_quality(dense, _dense_quality),
        on_hybrid_quality=_quality(hybrid, _hybrid_quality),
        off_economics=_off_economics(),
        on_economics=_on_economics() if on_economics is None else on_economics,
        acl_cost=_acl() if acl is None else acl,
        danger_totals={} if danger is None else danger,
    )


class TestPromotionOutcomes:
    def test_committed_shape_dense_below_hybrid_pays_is_narrow_only(self):
        result = _evaluate(dense=-0.15, hybrid=0.30)
        assert result["disposition"] == "narrow_only"
        assert result["hybrid_gain"] == 0.30
        assert result["dense_gain"] == -0.15

    def test_dense_clearing_promotion_margin_promotes_under_profile(self):
        result = _evaluate(dense=0.12, hybrid=0.30)
        assert result["disposition"] == "promote_under_profile"
        assert result["dense_gain"] == 0.12

    def test_hybrid_gain_0_15_is_narrow_only(self):
        assert _evaluate(dense=-0.15, hybrid=0.15)["disposition"] == "narrow_only"

    def test_gain_0_05_is_experimental(self):
        result = _evaluate(dense=-0.15, hybrid=0.05)
        assert result["disposition"] == "experimental"

    def test_gain_0_02_is_keep_disabled(self):
        result = _evaluate(dense=-0.15, hybrid=0.02)
        assert result["disposition"] == "keep_disabled"

    def test_hybrid_gain_with_control_regression_is_keep_disabled(self):
        result = _evaluate(
            hybrid={"visual_hard_task_success_rate": 0.70,
                    "text_easy_task_success_rate": 0.75},
        )
        assert result["disposition"] == "keep_disabled"
        assert "control" in result["summary"].lower()

    def test_dense_promotion_with_control_regression_does_not_block_hybrid(self):
        # PR81A rule: an unrelated route's regression must not block a
        # different route — dense fails its own control, hybrid still pays
        result = _evaluate(
            dense={"visual_hard_task_success_rate": 0.55,
                   "text_easy_task_success_rate": 0.75},
            hybrid=0.30,
        )
        assert result["disposition"] == "narrow_only"


class TestBlockers:
    def test_forbidden_delivery_blocks(self):
        result = _evaluate(danger={"forbidden_delivered": 1})
        assert result["disposition"] == "keep_disabled"
        assert any("forbidden" in b for b in result["security_blockers"])
        assert any("forbidden" in b for b in result["acl_blockers"])

    def test_cost_envelope_violation_blocks(self):
        result = _evaluate(
            on_economics=_on_economics(
                avg_embedding_bytes_per_page=MAX_EMBEDDING_BYTES_PER_PAGE + 1
            )
        )
        assert result["disposition"] == "keep_disabled"
        assert result["cost_blockers"]

    def test_warm_query_violation_blocks(self):
        result = _evaluate(
            on_economics=_on_economics(warm_query_ms_p50=MAX_WARM_QUERY_MS + 1)
        )
        assert result["disposition"] == "keep_disabled"
        assert any("warm query" in b for b in result["cost_blockers"])

    def test_denied_rebuild_acl_blocker(self):
        result = _evaluate(acl=_acl(denied_rebuilds_required=1))
        assert result["disposition"] == "keep_disabled"
        assert any("rebuild" in b for b in result["acl_blockers"])

    def test_blockers_outrank_positive_gain(self):
        result = _evaluate(dense=0.20, hybrid=0.40, danger={"forbidden_delivered": 1})
        assert result["disposition"] == "keep_disabled"


class TestContractFailClosed:
    def test_missing_acl_field_raises(self):
        bad = dict(_acl())
        del bad["visual_partitions"]
        with pytest.raises(ValueError, match="visual_partitions"):
            _evaluate(acl=bad)

    def test_bool_acl_field_raises(self):
        bad = dict(_acl())
        bad["denied_rebuilds_required"] = True
        with pytest.raises(ValueError, match="denied_rebuilds_required"):
            _evaluate(acl=bad)

    def test_non_numeric_acl_field_raises(self):
        bad = dict(_acl())
        bad["partition_build_ms"] = "fast"
        with pytest.raises(ValueError, match="partition_build_ms"):
            _evaluate(acl=bad)

    def test_missing_on_envelope_field_raises(self):
        bad = dict(_on_economics())
        del bad["warm_query_ms_p50"]
        with pytest.raises(ValueError, match="warm_query_ms_p50"):
            _evaluate(on_economics=bad)

    def test_every_required_acl_field_is_covered_by_fixture(self):
        assert set(REQUIRED_ACL_FIELDS) <= set(_acl())


class TestRuleShape:
    def test_declared_rule_mentions_every_threshold(self):
        rule = _evaluate()["declared_rule"]
        for value in (
            str(PROMOTION_MARGIN),
            str(RERANK_MARGIN),
            str(EXPERIMENTAL_FLOOR),
            str(CONTROL_TOLERANCE),
            str(MAX_RENDER_BYTES_PER_PAGE),
            str(MAX_EMBEDDING_BYTES_PER_PAGE),
            str(MAX_WARM_QUERY_MS),
        ):
            assert value in rule, f"threshold {value} not in declared_rule"

    def test_rerank_margin_mechanical_at_boundary(self):
        assert _evaluate(hybrid=RERANK_MARGIN)["disposition"] == "narrow_only"
        below = _evaluate(hybrid=round(RERANK_MARGIN - 1e-4, 4))
        assert below["disposition"] != "narrow_only"

    def test_promotion_margin_mechanical_at_boundary(self):
        assert _evaluate(dense=PROMOTION_MARGIN)["disposition"] == "promote_under_profile"
        below = _evaluate(dense=round(PROMOTION_MARGIN - 1e-4, 4), hybrid=-0.10)
        assert below["disposition"] != "promote_under_profile"

    def test_disposition_values_are_allowed_strings(self):
        for dense_gain, hybrid_gain in ((0.12, 0.30), (-0.15, 0.30), (-0.15, 0.15),
                                        (-0.15, 0.05), (-0.15, 0.02)):
            assert _evaluate(dense=dense_gain, hybrid=hybrid_gain)[
                "disposition"
            ] in ALLOWED_DISPOSITIONS

    def test_indeterminate_when_no_gain_computable(self):
        result = _evaluate(dense=None, hybrid=None)
        assert result["disposition"] == "indeterminate"

    def test_cost_deltas_are_on_minus_off(self):
        result = _evaluate()
        assert result["cost_deltas"]["vlm_calls"] == 40
        assert result["cost_deltas"]["embedding_bytes"] == 200_000
        assert result["cost_deltas"]["revision_rebuild_ms"] == pytest.approx(50.0)
