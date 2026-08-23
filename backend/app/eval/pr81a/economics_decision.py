"""PR81A OFF-vs-ON economics disposition — declared before interpretation.

This rule consumes the measured OFF baseline and the selective-visual ON arm
from the same declared workload, plus the measured ACL cost vector, and returns
a disposition for the visual-retrieval route (masterplan invariant 58: "its
downstream gain pays measured storage/update/ACL complexity").

The numeric thresholds below are imported from ``decision.py``; they were fixed
when the benchmark was designed, not after seeing results. ACL acceptance is
STRUCTURAL, not a tuned number: it requires a complete measured ACL vector,
zero forbidden delivery, and deny propagation that needs zero visual rebuilds.

A negative result (``keep_disabled``) is first-class successful research per
masterplan invariant 61 — failing to promote is a valid, recorded outcome, not
an error. The rule fails closed on any evidence-contract violation (missing or
non-numeric ACL field, missing ON-arm envelope field).
"""

from __future__ import annotations

from typing import Mapping

from app.eval.pr81a.decision import (
    CONTROL_TOLERANCE,
    EXPERIMENTAL_FLOOR,
    MAX_EMBEDDING_BYTES_PER_PAGE,
    MAX_RENDER_BYTES_PER_PAGE,
    MAX_WARM_QUERY_MS,
    PROMOTION_MARGIN,
    RERANK_MARGIN,
)

#: the ACL complexity vector raw counters (must all be present and numeric)
REQUIRED_ACL_FIELDS = (
    "visual_partitions",
    "partition_duplicate_bytes",
    "partition_build_ms",
    "deny_to_effective_ms",
    "denied_rebuilds_required",
    "authorized_universe_filter_calls",
)

#: the five dispositions this rule may return
ALLOWED_DISPOSITIONS = (
    "keep_disabled",
    "indeterminate",
    "promote_under_profile",
    "narrow_only",
    "experimental",
)

#: numeric cost fields subject to delta accounting (on minus off)
_COST_FIELDS = (
    "render_bytes",
    "embedding_bytes",
    "pages_rendered",
    "avg_render_bytes_per_page",
    "avg_embedding_bytes_per_page",
    "warm_query_ms_p50",
    "vlm_calls",
    "index_build_ms",
    "revision_rebuild_ms",
)

#: ON arm must report these envelope fields or the contract fails closed
_REQUIRED_ON_ENVELOPE = (
    "avg_render_bytes_per_page",
    "avg_embedding_bytes_per_page",
    "warm_query_ms_p50",
)


def _is_number(value) -> bool:
    # bool is a subclass of int and is not a valid cost/ACL counter here
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_acl(acl_cost: Mapping) -> None:
    for field in REQUIRED_ACL_FIELDS:
        if field not in acl_cost:
            raise ValueError(f"acl_cost missing required field: {field}")
        if not _is_number(acl_cost[field]):
            raise ValueError(
                f"acl_cost[{field}] must be numeric (use int, not bool/str)"
            )


def _control_held(off_quality: Mapping, on_quality: Mapping) -> bool:
    off_easy = off_quality.get("text_easy_task_success_rate")
    on_easy = on_quality.get("text_easy_task_success_rate")
    if off_easy is None or on_easy is None:
        return True
    return round(off_easy - on_easy, 4) <= CONTROL_TOLERANCE


def evaluate_economics_disposition(
    *,
    off_quality: Mapping,
    on_dense_quality: Mapping,
    on_hybrid_quality: Mapping,
    off_economics: Mapping,
    on_economics: Mapping,
    acl_cost: Mapping,
    danger_totals: Mapping,
) -> dict:
    """Apply the declared OFF-vs-ON economics rule. Pure function of inputs.

    Quality inputs carry ``visual_hard_task_success_rate`` /
    ``text_easy_task_success_rate`` per arm; the dense and hybrid ON
    routes are judged separately because PR81A already established they
    pay differently (dense indexing failed, hybrid rerank paid).
    """
    _validate_acl(acl_cost)
    for field in _REQUIRED_ON_ENVELOPE:
        if field not in on_economics:
            raise ValueError(
                f"on_economics missing required envelope field: {field}"
            )

    # 1. cost deltas: on minus off for every numeric cost field present in both
    cost_deltas: dict = {}
    for field in _COST_FIELDS:
        if field in off_economics and field in on_economics:
            o = off_economics[field]
            n = on_economics[field]
            if _is_number(o) and _is_number(n):
                delta = n - o
                cost_deltas[field] = (
                    round(delta, 4) if isinstance(delta, float) else delta
                )

    # 2. security blockers scoped to the ON arm's danger totals
    security_blockers: list[str] = []
    if danger_totals.get("forbidden_delivered", 0) > 0:
        security_blockers.append("forbidden material was delivered")
    if danger_totals.get("stale_revision_delivered", 0) > 0:
        security_blockers.append("a stale revision was served as current")
    if danger_totals.get("unresolvable_source", 0) > 0:
        security_blockers.append("a hit could not be resolved to source identity")

    # 3. cost blockers: ON arm must stay inside the imported envelope
    cost_blockers: list[str] = []
    if on_economics.get("avg_render_bytes_per_page", 0) > MAX_RENDER_BYTES_PER_PAGE:
        cost_blockers.append("render storage exceeds the declared envelope")
    if (
        on_economics.get("avg_embedding_bytes_per_page", 0)
        > MAX_EMBEDDING_BYTES_PER_PAGE
    ):
        cost_blockers.append("embedding storage exceeds the declared envelope")
    warm = on_economics.get("warm_query_ms_p50", None)
    if warm is not None and warm > MAX_WARM_QUERY_MS:
        cost_blockers.append("warm query latency exceeds the declared envelope")

    # 4. ACL structural blockers (architecture semantics, not a tuned number)
    acl_blockers: list[str] = []
    if acl_cost["denied_rebuilds_required"] != 0:
        acl_blockers.append("deny propagation required visual rebuild(s)")
    if danger_totals.get("forbidden_delivered", 0) > 0:
        acl_blockers.append("forbidden material was delivered (ACL/security)")

    blockers = security_blockers + cost_blockers + acl_blockers

    # 5. quality gains per ON route (None-safe)
    def _hard(quality: Mapping):
        value = quality.get("visual_hard_task_success_rate")
        return value if _is_number(value) else None

    off_hard = _hard(off_quality)
    dense_gain = (
        round(_hard(on_dense_quality) - off_hard, 4)
        if _hard(on_dense_quality) is not None and off_hard is not None
        else None
    )
    hybrid_gain = (
        round(_hard(on_hybrid_quality) - off_hard, 4)
        if _hard(on_hybrid_quality) is not None and off_hard is not None
        else None
    )
    dense_control_ok = _control_held(off_quality, on_dense_quality)
    hybrid_control_ok = _control_held(off_quality, on_hybrid_quality)

    # 6. disposition — dense and hybrid pay differently, so they are judged
    # by their own margins in the PR81A order (dense first, then rerank)
    if blockers:
        disposition = "keep_disabled"
        summary = (
            "Blockers fired (" + "; ".join(blockers) + "). Route stays disabled."
        )
    elif dense_gain is None and hybrid_gain is None:
        disposition = "indeterminate"
        summary = "Quality gain not computable (a required success-rate was None)."
    elif (
        dense_gain is not None
        and dense_gain >= PROMOTION_MARGIN
        and dense_control_ok
    ):
        disposition = "promote_under_profile"
        summary = (
            f"Dense visual route gains {dense_gain:.3f} over OFF on the "
            f"visual-hard tasks with the text-easy control held: promote under "
            f"the measured profile."
        )
    elif (
        hybrid_gain is not None
        and hybrid_gain >= RERANK_MARGIN
        and hybrid_control_ok
    ):
        disposition = "narrow_only"
        summary = (
            f"Hybrid rerank gains {hybrid_gain:.3f} over OFF on the visual-hard "
            f"tasks (dense did not clear its margin) with the text-easy control "
            f"held: enable the narrow visual route under the measured profile."
        )
    else:
        best_gain = max(
            gain for gain in (dense_gain, hybrid_gain) if gain is not None
        )
        best_control = dense_control_ok if dense_gain == best_gain else hybrid_control_ok
        if best_gain >= EXPERIMENTAL_FLOOR and best_control:
            disposition = "experimental"
            summary = (
                f"Best ON-route gain over OFF is {best_gain:.3f}, below the "
                f"promotion/rerank margins but at or above the experimental floor."
            )
        else:
            disposition = "keep_disabled"
            summary = (
                "Text-easy control regressed beyond tolerance; keep disabled."
                if not best_control
                else "Gain below the experimental floor; keep disabled."
            )

    return {
        "disposition": disposition,
        "dense_gain": dense_gain,
        "hybrid_gain": hybrid_gain,
        "cost_deltas": cost_deltas,
        "acl_cost": dict(acl_cost),
        "security_blockers": security_blockers,
        "cost_blockers": cost_blockers,
        "acl_blockers": acl_blockers,
        "thresholds": {
            "promotion_margin": PROMOTION_MARGIN,
            "rerank_margin": RERANK_MARGIN,
            "experimental_floor": EXPERIMENTAL_FLOOR,
            "control_tolerance": CONTROL_TOLERANCE,
            "max_render_bytes_per_page": MAX_RENDER_BYTES_PER_PAGE,
            "max_embedding_bytes_per_page": MAX_EMBEDDING_BYTES_PER_PAGE,
            "max_warm_query_ms": MAX_WARM_QUERY_MS,
        },
        "declared_rule": (
            "keep_disabled when any security/cost/ACL blocker fires, when the "
            "text-easy control regresses beyond the 0.10 tolerance, or when both "
            "route gains are below the 0.05 experimental floor; "
            "promote_under_profile when the dense visual route gains >= 0.10 "
            "over OFF on the visual-hard tasks while holding text-easy within "
            "0.10; narrow_only when the dense route missed its margin but the "
            "hybrid rerank gains >= 0.10 over OFF with control held; "
            "experimental when the best route gains >= 0.05 over OFF with "
            "control held; indeterminate when no gain is computable. The ON arm "
            "must keep per-page storage inside the declared envelope "
            "(avg_render <= 1500000 bytes, avg_embedding <= 8192 bytes) and "
            "warm query latency <= 250.0 ms; ACL acceptance is structural: a "
            "complete measured ACL vector, zero forbidden delivery, and deny "
            "propagation requiring zero visual rebuilds."
        ),
        "summary": summary,
    }
