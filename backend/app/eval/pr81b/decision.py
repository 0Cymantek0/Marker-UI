"""PR81B confirmation rule — declared before any matrix numbers were read.

PR81A promoted ``narrow_rerank_only`` from one VLM identity. This module
fixes, in advance, what the multi-model matrix must show for that
promotion to survive, when it must be re-scoped, and when it must be
downgraded. It consumes only aggregate artifacts the PR81A runner
already produces (one per model) plus each model's capability-probe
result, and it reuses the committed PR81A decision rule per model so
"the hybrid held" means exactly what PR81A declared.

Declared outcome space:

* ``confirmed``               — the narrow_rerank_only promotion holds
  across model quality tiers: at least MIN_HOLDERS capable models
  (including at least MIN_FRONTIER_HOLDERS frontier-tier models) hold
  the hybrid route with zero security dangers and clean no-delivery
  probes, under the per-model PR81A rule.
* ``model_gated_experimental`` — the gain holds for at least one model
  but the holder count or frontier requirement fails; the route stays
  experimental behind an explicit model-quality gate.
* ``do_not_promote``          — no capable model holds the hybrid gain;
  the PR81A result was a single-model artifact, or security failed
  systemically.

Separately and independently, the ablation lanes attribute *where* the
gain lives. ``attribution`` is one of:

* ``answer_vision``  — for at least one holder, removing pixels from the
  answer prompt (``visual-hybrid-rerank-text``) costs ≥ MATERIAL_MARGIN
  on the visual-hard slices: the answer step itself needs vision.
* ``rerank_vision``  — pixels never matter in the answer step across
  holders, but removing the visual rerank (``visual-hybrid-union-only``)
  costs ≥ MATERIAL_MARGIN for at least one holder: vision contributes
  through selection.
* ``retrieval_only`` — neither ablation ever crosses the margin across
  holders: the win is candidate-union recall plus rerank-as-text
  selection, and the report must re-scope the claim accordingly.

Ceiling clause (pre-declared): a frontier model may lift the B2
baseline so high that a ≥ 0.10 absolute gain becomes arithmetically
impossible. A model still *holds* if its hybrid visual-hard rate is ≥
CEILING_FLOOR and does not regress versus its own baseline, with the
control and security clauses intact.
"""

from __future__ import annotations

from typing import Mapping

from app.eval.pr81a.decision import (
    TEXT_EASY_SLICE,
    VISUAL_HARD_SLICES,
    V2_SYSTEM as HYBRID_SYSTEM,
    B2_SYSTEM as BASELINE_SYSTEM,
    evaluate_decision,
)
from app.eval.pr81a.lanes import (
    V2_JOINT_SYSTEM,
    V2_TEXT_SYSTEM,
    V2_UNION_SYSTEM,
)

#: quality tiers, declared up front; confirmation requires frontier depth
FRONTIER_TIER_MODELS: frozenset[str] = frozenset(
    {
        "kr/claude-sonnet-4.5",
        "kr/claude-haiku-4.5",
        "cx/gpt-5.6-luna",
    }
)
ECONOMY_TIER_MODELS: frozenset[str] = frozenset(
    {
        "free/bbl/gemini-3.0-flash",
        "oc/mimo-v2.5-free",
        "google/gemma-4-26b-a4b-it:free",  # PR81A identity, for reference rows
    }
)

MATERIAL_MARGIN = 0.10  # ablation delta that counts as material (visual-hard ts)
CEILING_FLOOR = 0.90  # hybrid visual-hard rate that satisfies the ceiling clause
MIN_HOLDERS = 3
MIN_FRONTIER_HOLDERS = 2

#: dangers that disqualify a model outright, whatever its gains
SECURITY_DANGERS = ("forbidden_delivered", "stale_revision_delivered", "unresolvable_source")

HOLDING_OUTCOMES = frozenset({"narrow_rerank_only", "promote_narrow"})


def _visual_hard_rate(system_metrics: Mapping) -> float | None:
    slices = system_metrics.get("slices") or {}
    rates = [
        bucket.get("task_success_rate")
        for tag in VISUAL_HARD_SLICES
        if (bucket := slices.get(tag)) and bucket.get("queries")
    ]
    return sum(rates) / len(rates) if rates else None


def _text_easy_rate(system_metrics: Mapping) -> float | None:
    bucket = (system_metrics.get("slices") or {}).get(TEXT_EASY_SLICE)
    if not bucket or not bucket.get("queries"):
        return None
    return bucket.get("task_success_rate")


def evaluate_model(model: str, artifact: Mapping) -> dict:
    """Run the committed PR81A rule on one model's artifact, PR81B-style.

    ``artifact`` is one per-model benchmark artifact: ``metrics``,
    ``danger_totals``, ``economics``, and optionally ``capability_probe``.
    """
    metrics = artifact.get("metrics") or {}
    danger_totals = artifact.get("danger_totals") or {}
    economics = artifact.get("economics") or {}
    probe = artifact.get("capability_probe") or {}

    decision = evaluate_decision(metrics, danger_totals=danger_totals, economics=economics)
    rule_results = decision["rule_results"]

    base_hard = rule_results.get("baseline_visual_hard_rate")
    hybrid_hard = _visual_hard_rate(metrics.get(HYBRID_SYSTEM) or {})
    hybrid_easy = _text_easy_rate(metrics.get(HYBRID_SYSTEM) or {})
    text_hard = _visual_hard_rate(metrics.get(V2_TEXT_SYSTEM) or {})
    joint_hard = _visual_hard_rate(metrics.get(V2_JOINT_SYSTEM) or {})
    union_hard = _visual_hard_rate(metrics.get(V2_UNION_SYSTEM) or {})

    security_dangers = {d: danger_totals[d] for d in SECURITY_DANGERS if danger_totals.get(d)}
    no_delivery_clean = all(
        (m.get("no_delivery_required_ok", 0) >= m.get("no_delivery_required_total", 0))
        for m in metrics.values()
        if isinstance(m, Mapping) and m.get("no_delivery_required_total")
    )
    probe_passed = bool(probe.get("passed"))

    gain = rule_results.get("hybrid_gain_vs_baseline")
    control_ok = bool(rule_results.get("hybrid_text_easy_control_ok"))
    outcome_holds = decision["outcome"] in HOLDING_OUTCOMES
    ceiling_holds = (
        hybrid_hard is not None
        and base_hard is not None
        and hybrid_hard >= CEILING_FLOOR
        and hybrid_hard >= base_hard
        and control_ok
    )
    holds = (
        probe_passed
        and outcome_holds
        and not security_dangers
        and no_delivery_clean
    ) or (
        probe_passed
        and ceiling_holds
        and not security_dangers
        and no_delivery_clean
    )

    return {
        "model": model,
        "tier": "frontier" if model in FRONTIER_TIER_MODELS else (
            "economy" if model in ECONOMY_TIER_MODELS else "undeclared"
        ),
        "capability_probe_passed": probe_passed,
        "capability_probe_correct": probe.get("correct"),
        "per_model_outcome": decision["outcome"],
        "baseline_visual_hard_rate": base_hard,
        "hybrid_visual_hard_rate": hybrid_hard,
        "hybrid_gain_vs_baseline": gain,
        "hybrid_text_easy_rate": hybrid_easy,
        "hybrid_text_easy_control_ok": control_ok,
        "ceiling_clause_applied": bool(ceiling_holds and not outcome_holds),
        "security_dangers": security_dangers,
        "no_delivery_clean": no_delivery_clean,
        "holds": holds,
        "ablation_visual_hard_rates": {
            "hybrid_image_answer": hybrid_hard,
            "hybrid_text_answer": text_hard,
            "hybrid_joint_answer": joint_hard,
            "union_only_no_rerank": union_hard,
        },
        "ablation_deltas": {
            # positive = removing pixels (or the rerank) cost this much
            "answer_vision_delta": (
                round(hybrid_hard - text_hard, 4)
                if hybrid_hard is not None and text_hard is not None
                else None
            ),
            "rerank_delta": (
                round(hybrid_hard - union_hard, 4)
                if hybrid_hard is not None and union_hard is not None
                else None
            ),
            "joint_over_image_delta": (
                round(joint_hard - hybrid_hard, 4)
                if hybrid_hard is not None and joint_hard is not None
                else None
            ),
        },
    }


def evaluate_confirmation(per_model: Mapping[str, Mapping]) -> dict:
    """Apply the declared PR81B rule to the full model-sensitivity matrix."""
    models = {model: evaluate_model(model, artifact) for model, artifact in per_model.items()}

    holders = {m: r for m, r in models.items() if r["holds"]}
    frontier_holders = {m: r for m, r in holders.items() if r["tier"] == "frontier"}
    capable = {m: r for m, r in models.items() if r["capability_probe_passed"]}
    any_gain = any(r["hybrid_gain_vs_baseline"] is not None for r in models.values())
    systemic_security_failure = sum(1 for r in models.values() if r["security_dangers"]) >= 3

    answer_vision_models = [
        m
        for m, r in holders.items()
        if (r["ablation_deltas"]["answer_vision_delta"] or 0) >= MATERIAL_MARGIN
    ]
    rerank_vision_models = [
        m
        for m, r in holders.items()
        if (r["ablation_deltas"]["rerank_delta"] or 0) >= MATERIAL_MARGIN
    ]
    if answer_vision_models:
        attribution = "answer_vision"
    elif rerank_vision_models:
        attribution = "rerank_vision"
    elif holders:
        attribution = "retrieval_only"
    else:
        attribution = "unattributed"

    if systemic_security_failure:
        outcome = "do_not_promote"
        summary = (
            "Security dangers fired across multiple models; the hybrid route "
            "cannot be confirmed at any quality tier."
        )
    elif len(holders) >= MIN_HOLDERS and len(frontier_holders) >= MIN_FRONTIER_HOLDERS:
        outcome = "confirmed"
        summary = (
            f"{len(holders)} capable models hold the hybrid route "
            f"({len(frontier_holders)} frontier-tier) under the per-model PR81A "
            f"rule with zero security dangers; attribution: {attribution}."
        )
    elif holders:
        outcome = "model_gated_experimental"
        summary = (
            f"The hybrid gain holds for {len(holders)} model(s) "
            f"({len(frontier_holders)} frontier-tier), below the declared "
            f"{MIN_HOLDERS}-holder / {MIN_FRONTIER_HOLDERS}-frontier confirmation "
            "bar; the route stays experimental behind an explicit model-quality gate."
        )
    else:
        outcome = "do_not_promote"
        summary = (
            "No capable model holds the declared hybrid gain"
            + (" under the per-model rule" if any_gain else " (no comparable gains measured)")
            + "; the PR81A single-model result does not generalize."
        )

    return {
        "outcome": outcome,
        "attribution": attribution,
        "summary": summary,
        "holders": sorted(holders),
        "frontier_holders": sorted(frontier_holders),
        "capable_models": sorted(capable),
        "answer_vision_models": sorted(answer_vision_models),
        "rerank_vision_models": sorted(rerank_vision_models),
        "models": models,
        "thresholds": {
            "material_margin": MATERIAL_MARGIN,
            "ceiling_floor": CEILING_FLOOR,
            "min_holders": MIN_HOLDERS,
            "min_frontier_holders": MIN_FRONTIER_HOLDERS,
            "security_dangers": list(SECURITY_DANGERS),
        },
        "declared_rule": (
            "Per model: the committed PR81A decision rule is re-applied to that "
            "model's artifact (B2 baseline vs hybrid on the visual-hard slices, "
            "text-easy control, zero forbidden/stale/unresolvable dangers, clean "
            "no-delivery probes, cost envelope). A model holds iff it passed the "
            "capability probe, its per-model outcome is narrow_rerank_only or "
            "promote_narrow, and it has no security dangers — or the pre-declared "
            "ceiling clause applies (hybrid visual-hard >= "
            f"{CEILING_FLOOR} without regressing its own baseline, control and "
            "security intact). Confirmation requires >= "
            f"{MIN_HOLDERS} holders including >= {MIN_FRONTIER_HOLDERS} "
            "frontier-tier models; 1..N-1 holders means model_gated_experimental "
            "with a model-quality gate; zero holders means do_not_promote. "
            "Independently, attribution is answer_vision if the text-answer "
            "ablation costs >= "
            f"{MATERIAL_MARGIN} for any holder, else rerank_vision if the "
            "union-only ablation costs >= "
            f"{MATERIAL_MARGIN} for any holder, else retrieval_only and the "
            "promotion claim is re-scoped from visual answering to candidate-union "
            "recall plus rerank selection."
        ),
    }
