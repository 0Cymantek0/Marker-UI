"""PR81A decision-rule tests. Matrix letter D.

The rule is declared ahead of results; these tests pin its mechanics so
the promotion outcome cannot be quietly re-tuned after interpretation.
"""

from __future__ import annotations

from app.eval.pr81a.decision import evaluate_decision


def _metrics(system: str, *, hard: float, easy: float, no_delivery_ok: int = 3, no_delivery_total: int = 3) -> dict:
    slices = {}
    for tag in (
        "chart.appearance",
        "chart.value_read",
        "table.cell_grid",
        "form.label_placement",
        "layout.column_bind",
    ):
        slices[tag] = {"queries": 2, "task_success": int(hard * 2), "task_success_rate": hard, "page_hits": 1, "page_hit_rate": hard, "dangers": 0}
    slices["text.easy_control"] = {"queries": 6, "task_success": int(easy * 6), "task_success_rate": easy, "page_hits": 5, "page_hit_rate": easy, "dangers": 0}
    return {
        system: {
            "task_success_rate": (hard * 10 + easy * 6) / 16,
            "slices": slices,
            "no_delivery_required_ok": no_delivery_ok,
            "no_delivery_required_total": no_delivery_total,
        }
    }


def _econ(**overrides) -> dict:
    base = {
        "avg_render_bytes_per_page": 200_000,
        "avg_embedding_bytes_per_page": 2_048,
        "warm_query_ms_p50": 12.0,
    }
    base.update(overrides)
    return base


def _merge(*dicts) -> dict:
    out: dict = {}
    for d in dicts:
        out.update(d)
    return out


class TestPromotionOutcomes:
    def test_promote_narrow_when_dense_gain_clear(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:openai/clip-vit-base-patch32", hard=0.7, easy=0.85),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] == "promote_narrow"
        assert decision["best_dense_system"].startswith("visual-dense:")

    def test_narrow_rerank_only_when_only_hybrid_pays(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.45, easy=0.9),
            _metrics("visual-hybrid-rerank", hard=0.75, easy=0.85),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] == "narrow_rerank_only"

    def test_experimental_band(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.47, easy=0.9),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] == "experimental"

    def test_do_not_promote_when_no_signal(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.6, easy=0.9),
            _metrics("visual-dense:x", hard=0.55, easy=0.9),
            _metrics("visual-hybrid-rerank", hard=0.62, easy=0.9),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] == "do_not_promote"


class TestBlockers:
    def test_forbidden_delivery_blocks_promotion(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.9, easy=0.9),
        )
        decision = evaluate_decision(
            metrics,
            danger_totals={"forbidden_delivered": 1},
            economics=_econ(),
        )
        assert decision["outcome"] == "do_not_promote"
        assert any("forbidden" in b for b in decision["rule_results"]["security_blockers"])

    def test_stale_revision_blocks_promotion(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.9, easy=0.9),
        )
        decision = evaluate_decision(
            metrics,
            danger_totals={"stale_revision_delivered": 1},
            economics=_econ(),
        )
        assert decision["outcome"] == "do_not_promote"

    def test_no_delivery_violation_blocks_promotion(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.9, easy=0.9, no_delivery_ok=2, no_delivery_total=3),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] == "do_not_promote"

    def test_cost_envelope_blocks_promotion(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.9, easy=0.9),
        )
        decision = evaluate_decision(
            metrics,
            danger_totals={},
            economics=_econ(avg_render_bytes_per_page=2_000_000),
        )
        assert decision["outcome"] == "do_not_promote"
        assert decision["rule_results"]["cost_blockers"]

    def test_text_easy_regression_blocks_promotion(self):
        metrics = _merge(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            _metrics("visual-dense:x", hard=0.9, easy=0.5),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] != "promote_narrow"
        assert decision["rule_results"]["text_easy_control_ok"] is False


class TestRuleShape:
    def test_declared_rule_recorded(self):
        decision = evaluate_decision(
            _metrics("lexical-render", hard=0.4, easy=0.9),
            danger_totals={},
            economics=_econ(),
        )
        assert "promote_narrow iff" in decision["declared_rule"]
        assert decision["rule_results"]["thresholds"]["promotion_margin"] == 0.10

    def test_margins_are_mechanical(self):
        # exactly at the promotion margin qualifies (>=)
        metrics = _merge(
            _metrics("lexical-render", hard=0.50, easy=0.9),
            _metrics("visual-dense:x", hard=0.60, easy=0.9),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] == "promote_narrow"
        # one point below does not
        metrics = _merge(
            _metrics("lexical-render", hard=0.51, easy=0.9),
            _metrics("visual-dense:x", hard=0.60, easy=0.9),
        )
        decision = evaluate_decision(
            metrics, danger_totals={}, economics=_econ()
        )
        assert decision["outcome"] != "promote_narrow"
