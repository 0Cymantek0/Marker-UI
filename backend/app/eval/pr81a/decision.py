"""PR81A promotion rule — declared before interpretation, applied mechanically.

The thresholds below were fixed when the benchmark was designed, not
after seeing results. :func:`evaluate_decision` consumes only the
aggregate metrics the scoring layer produces and returns one of the
masterplan's outcomes:

* ``promote_narrow``     — a visual route beats the targeted-rendering
  baseline by the declared margin on the visual-hard slices, holds the
  text-easy control, keeps every security probe clean, and fits the
  declared cost envelope;
* ``narrow_rerank_only`` — dense visual indexing does not pay, but the
  hybrid VLM reranker over lexical candidates does;
* ``experimental``       — signal exists but below the promotion margin;
* ``do_not_promote``     — no material signal, or any security/cost
  blocker fires.
"""

from __future__ import annotations

from typing import Mapping

B2_SYSTEM = "lexical-render"
V2_SYSTEM = "visual-hybrid-rerank"
VISUAL_DENSE_PREFIX = "visual-dense:"

#: slices where page appearance plausibly carries retrieval signal
VISUAL_HARD_SLICES = (
    "chart.appearance",
    "chart.value_read",
    "table.cell_grid",
    "form.label_placement",
    "layout.column_bind",
)
TEXT_EASY_SLICE = "text.easy_control"

#: declared margins (absolute task_success_rate points)
PROMOTION_MARGIN = 0.10
CONTROL_TOLERANCE = 0.10  # visual routes may not lose more than this on text-easy
RERANK_MARGIN = 0.10
EXPERIMENTAL_FLOOR = 0.05

#: declared cost envelope for the local profile (per page averages)
MAX_RENDER_BYTES_PER_PAGE = 1_500_000
MAX_EMBEDDING_BYTES_PER_PAGE = 8_192
MAX_WARM_QUERY_MS = 250.0


def _slice_rate(metrics: Mapping, slice_tag: str, key: str = "task_success_rate") -> float | None:
    slices = metrics.get("slices") or {}
    bucket = slices.get(slice_tag)
    if not bucket or not bucket.get("queries"):
        return None
    return bucket.get(key)


def evaluate_decision(
    metrics: Mapping[str, Mapping],
    *,
    danger_totals: Mapping[str, int],
    economics: Mapping[str, float],
) -> dict:
    """Apply the declared rule. Pure function of measured aggregates."""
    base = metrics.get(B2_SYSTEM) or {}
    base_hard = [
        rate
        for tag in VISUAL_HARD_SLICES
        if (rate := _slice_rate(base, tag)) is not None
    ]
    base_hard_rate = sum(base_hard) / len(base_hard) if base_hard else None
    base_easy = _slice_rate(base, TEXT_EASY_SLICE)

    dense_results: dict[str, dict] = {}
    for system_id, system_metrics in metrics.items():
        if not system_id.startswith(VISUAL_DENSE_PREFIX):
            continue
        hard = [
            rate
            for tag in VISUAL_HARD_SLICES
            if (rate := _slice_rate(system_metrics, tag)) is not None
        ]
        easy = _slice_rate(system_metrics, TEXT_EASY_SLICE)
        dense_results[system_id] = {
            "visual_hard_rate": sum(hard) / len(hard) if hard else None,
            "text_easy_rate": easy,
            "gain_vs_baseline": (
                round(sum(hard) / len(hard) - base_hard_rate, 4)
                if hard and base_hard_rate is not None
                else None
            ),
        }
    v2 = metrics.get(V2_SYSTEM) or {}
    v2_hard = [
        rate
        for tag in VISUAL_HARD_SLICES
        if (rate := _slice_rate(v2, tag)) is not None
    ]
    v2_hard_rate = sum(v2_hard) / len(v2_hard) if v2_hard else None
    v2_easy = _slice_rate(v2, TEXT_EASY_SLICE)
    v2_gain = (
        round(v2_hard_rate - base_hard_rate, 4)
        if v2_hard_rate is not None and base_hard_rate is not None
        else None
    )

    security_blockers: list[str] = []
    if danger_totals.get("forbidden_delivered", 0) > 0:
        security_blockers.append("forbidden material was delivered")
    if danger_totals.get("stale_revision_delivered", 0) > 0:
        security_blockers.append("a stale revision was served as current")
    if danger_totals.get("unresolvable_source", 0) > 0:
        security_blockers.append("a hit could not be resolved to source identity")
    for system_id, system_metrics in metrics.items():
        total = system_metrics.get("no_delivery_required_total", 0)
        ok = system_metrics.get("no_delivery_required_ok", 0)
        if ok < total:
            security_blockers.append(
                f"{system_id} violated a no_delivery probe ({ok}/{total} clean)"
            )

    cost_blockers: list[str] = []
    if economics.get("avg_render_bytes_per_page", 0) > MAX_RENDER_BYTES_PER_PAGE:
        cost_blockers.append("render storage exceeds the declared envelope")
    if economics.get("avg_embedding_bytes_per_page", 0) > MAX_EMBEDDING_BYTES_PER_PAGE:
        cost_blockers.append("embedding storage exceeds the declared envelope")
    if economics.get("warm_query_ms_p50", float("inf")) > MAX_WARM_QUERY_MS:
        cost_blockers.append("warm query latency exceeds the declared envelope")

    blockers = security_blockers + cost_blockers

    best_dense_id, best_dense = None, None
    for system_id, result in dense_results.items():
        if result["gain_vs_baseline"] is None:
            continue
        if best_dense is None or result["gain_vs_baseline"] > best_dense["gain_vs_baseline"]:
            best_dense_id, best_dense = system_id, result

    def _control_ok(route_easy: float | None) -> bool:
        # the tolerance protects the route being considered: an unrelated
        # route's regression (e.g. admission-limited dense lanes that
        # cannot serve plain-text documents by design) must not block a
        # different route's promotion
        if base_easy is None or route_easy is None:
            return True
        return round(base_easy - route_easy, 4) <= CONTROL_TOLERANCE

    for system_id, result in dense_results.items():
        result["text_easy_control_ok"] = _control_ok(result["text_easy_rate"])
    v2_control_ok = _control_ok(v2_easy)

    rule_results = {
        "baseline_visual_hard_rate": base_hard_rate,
        "baseline_text_easy_rate": base_easy,
        "dense_routes": dense_results,
        "hybrid_gain_vs_baseline": v2_gain,
        "hybrid_text_easy_rate": v2_easy,
        "hybrid_text_easy_control_ok": v2_control_ok,
        "security_blockers": security_blockers,
        "cost_blockers": cost_blockers,
        "thresholds": {
            "promotion_margin": PROMOTION_MARGIN,
            "control_tolerance": CONTROL_TOLERANCE,
            "rerank_margin": RERANK_MARGIN,
            "experimental_floor": EXPERIMENTAL_FLOOR,
        },
    }

    if blockers:
        outcome = "do_not_promote"
        summary = (
            "Security or cost blockers fired: " + "; ".join(blockers)
            + ". No promotion regardless of measured gain."
        )
    elif (
        best_dense is not None
        and best_dense["gain_vs_baseline"] >= PROMOTION_MARGIN
        and best_dense.get("text_easy_control_ok", False)
    ):
        outcome = "promote_narrow"
        summary = (
            f"{best_dense_id} beats {B2_SYSTEM} by "
            f"{best_dense['gain_vs_baseline']:.3f} task-success points on the "
            f"visual-hard slices ({best_dense['visual_hard_rate']:.3f} vs "
            f"{base_hard_rate:.3f}) with the text-easy control held."
        )
    elif v2_gain is not None and v2_gain >= RERANK_MARGIN and v2_control_ok:
        outcome = "narrow_rerank_only"
        summary = (
            f"Dense visual routes do not pay, but {V2_SYSTEM} beats "
            f"{B2_SYSTEM} by {v2_gain:.3f} on the visual-hard slices "
            f"({v2_hard_rate:.3f} vs {base_hard_rate:.3f})."
        )
    else:
        candidates = [
            (r["gain_vs_baseline"], r.get("text_easy_control_ok", False))
            for r in dense_results.values()
            if r["gain_vs_baseline"] is not None
        ]
        if v2_gain is not None:
            candidates.append((v2_gain, v2_control_ok))
        best_gain, best_control = max(candidates, default=(None, False))
        if best_gain is not None and best_gain >= EXPERIMENTAL_FLOOR and best_control:
            outcome = "experimental"
            summary = (
                f"Best visual gain over the targeted-rendering baseline is "
                f"{best_gain:.3f}, below the {PROMOTION_MARGIN} promotion "
                f"margin but at or above the {EXPERIMENTAL_FLOOR} experimental "
                f"floor."
            )
        else:
            outcome = "do_not_promote"
            summary = (
                "No visual route produced a material downstream gain over "
                "text/structure plus targeted page rendering."
                + ("" if best_control else " The text-easy control also regressed.")
            )

    return {
        "outcome": outcome,
        "summary": summary,
        "rule_results": rule_results,
        "best_dense_system": best_dense_id,
        "declared_rule": (
            "promote_narrow iff the best dense visual route gains >= "
            f"{PROMOTION_MARGIN} absolute task-success over {B2_SYSTEM} on "
            f"{list(VISUAL_HARD_SLICES)} while holding {TEXT_EASY_SLICE} "
            f"within {CONTROL_TOLERANCE}, with zero forbidden/stale/"
            "unresolvable dangers, all no_delivery probes clean, and per-page "
            "storage plus warm query latency inside the declared envelope; "
            f"else narrow_rerank_only when {V2_SYSTEM} gains >= "
            f"{RERANK_MARGIN}; else experimental at >= {EXPERIMENTAL_FLOOR}; "
            "else do_not_promote."
        ),
    }
