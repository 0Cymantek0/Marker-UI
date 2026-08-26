"""Invariant-62 rational-user displacement decision engine."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .contracts import (
    DIMENSION_DOC_EXACT_RATE,
    DIMENSION_EVIDENCE_LINEAGE,
    DIMENSION_LANE_ERRORS,
    DIMENSION_REVIEW_BURDEN,
    DIMENSION_SCALAR_ACCURACY,
    DISPLACEMENT_DECISION_SCHEMA_VERSION,
    INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
    INTEGRATION_STATUS_VERIFIED_ACTIVE,
    MEASUREMENT_STATUS_NOT_APPLICABLE,
    MEASUREMENT_STATUS_UNAVAILABLE,
    OUTCOME_EXPLICIT_CONCESSION,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_INTEGRATE_OR_ROUTE,
    OUTCOME_MARKER_RETAINED,
    PROTOCOL_PROSPECTIVE_PREREGISTRATION,
    PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    REASON_STATUS_CONCEDED,
    REASON_STATUS_INTEGRATED,
    REASON_STATUS_MEASURED,
    REASON_STATUS_UNKNOWN,
    ComparatorEvaluation,
    DimensionMeasurement,
    DisplacementDecision,
    DisplacementMeasurementBundle,
    DisplacementPreregistration,
    ReasonToLeaveItem,
    _compute_sha256,
    _parse_iso_dt,
)
from .validation import (
    validate_active_integration,
    validate_displacement_measurement_bundle,
    validate_displacement_preregistration,
)


def derive_displacement_decision(
    prereg: DisplacementPreregistration,
    bundle: DisplacementMeasurementBundle,
    as_of_date: str,
    repo_root: Path | str | None = None,
) -> DisplacementDecision:
    """Deterministically derive Invariant-62 rational-user displacement decision.

    Pure function of (preregistration, measurements, as_of_date, repo_root).
    """
    blockers: list[str] = []
    reason_ledger: list[ReasonToLeaveItem] = []
    comparator_evals: dict[str, ComparatorEvaluation] = {}
    limitations: list[str] = []

    # Dynamic limitations based on preregistration parameters
    if prereg.protocol_timing == PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY:
        limitations.append(
            f"Retrospective frozen replay of declared evidence for workflow {prereg.workflow!r}; "
            f"decision thresholds frozen post-experiment, subject to retrospective replay scope."
        )
    limitations.append(
        f"Dangerous failure threshold ({prereg.frozen_thresholds.max_acceptable_dangerous_failures}) "
        f"is an observed-count gate on declared {prereg.corpus.document_count}-document corpus slice; "
        f"no population-level statistical leadership claim is made."
    )

    # 1. Structural validation
    p_errors = validate_displacement_preregistration(prereg, as_of_date=as_of_date)
    blockers.extend(p_errors)

    b_errors = validate_displacement_measurement_bundle(
        bundle, prereg=prereg, as_of_date=as_of_date
    )
    blockers.extend(b_errors)

    # 2. Date checks
    dt_p = _parse_iso_dt(prereg.preregistration_date, "preregistration_date", [])
    dt_e = _parse_iso_dt(bundle.evidence_date, "evidence_date", [])
    if (
        dt_p
        and dt_e
        and dt_e < dt_p
        and prereg.protocol_timing == PROTOCOL_PROSPECTIVE_PREREGISTRATION
    ):
        blockers.append(
            f"Prospective timing violation: evidence ({bundle.evidence_date}) was recorded before prospective preregistration ({prereg.preregistration_date})"
        )

    # 3. Fairness verification gate: check executed facts against declared spec
    fairness = bundle.fairness
    fairness_discrepancies: list[str] = list(fairness.discrepancies)

    if not fairness.input_parity_verified:
        fairness_discrepancies.append("Input parity not verified across comparators")
    if not fairness.adaptation_parity_verified:
        fairness_discrepancies.append(
            "Adaptation parity not verified across comparators"
        )
    if not fairness.full_corpus_evaluated:
        fairness_discrepancies.append("Not all systems evaluated on full corpus")
    if not fairness.is_fair:
        fairness_discrepancies.append("Fairness verification flag is false")

    # Compare executed facts vs declared comparator specifications
    executed_facts_map = {f.system_id: f for f in fairness.executed_facts}
    for comp in prereg.comparators:
        if comp.system_id not in executed_facts_map:
            fairness_discrepancies.append(
                f"No executed system facts provided for declared comparator {comp.system_id!r}"
            )
            continue
        fact = executed_facts_map[comp.system_id]
        if fact.system_identity != comp.system_identity:
            fairness_discrepancies.append(
                f"Identity mismatch for {comp.system_id!r}: declared {comp.system_identity!r} != executed {fact.system_identity!r}"
            )
        if fact.input_path != comp.input_path_declared:
            fairness_discrepancies.append(
                f"Input path mismatch for {comp.system_id!r}: declared {comp.input_path_declared!r} != executed {fact.input_path!r}"
            )
        if fact.adaptation_rules != comp.adaptation_rules_declared:
            fairness_discrepancies.append(
                f"Adaptation rules mismatch for {comp.system_id!r}: declared {comp.adaptation_rules_declared!r} != executed {fact.adaptation_rules!r}"
            )

    fairness_passed = len(fairness_discrepancies) == 0

    if not fairness_passed:
        disc_text = "; ".join(sorted(set(fairness_discrepancies)))
        blockers.append(f"Fairness mismatch: {disc_text}")

    # 4. Check material dimensions availability across all comparators
    # Missing dimensions CANNOT become zero or measured.
    for dim in prereg.material_dimensions:
        for comp_spec in prereg.comparators:
            comp_m = bundle.comparators.get(comp_spec.system_id)
            if comp_m is None:
                blockers.append(
                    f"Missing measurements object for comparator {comp_spec.system_id!r}"
                )
                continue
            meas = comp_m.get_dimension(dim)
            if meas is None or meas.status == MEASUREMENT_STATUS_UNAVAILABLE:
                blockers.append(
                    f"Material dimension {dim!r} is unavailable for comparator {comp_spec.system_id!r}"
                )
            elif (
                meas.status == MEASUREMENT_STATUS_NOT_APPLICABLE
                and not meas.not_applicable_justification.strip()
            ):
                blockers.append(
                    f"Material dimension {dim!r} for comparator {comp_spec.system_id!r} is not_applicable without justification"
                )

    # 5. Marker baseline evaluation
    marker_spec = prereg.get_marker_baseline()
    marker_meas = bundle.comparators.get(marker_spec.system_id) if marker_spec else None

    marker_exact: float | None = None
    marker_acc: float | None = None
    marker_cov: float | None = None
    marker_danger_count = 0
    marker_breached_danger = False
    marker_lane_errors = 0

    if marker_spec and marker_meas:
        marker_exact = marker_meas.get_numeric(DIMENSION_DOC_EXACT_RATE)
        marker_acc = marker_meas.get_numeric(DIMENSION_SCALAR_ACCURACY)
        marker_cov = marker_meas.get_numeric(DIMENSION_EVIDENCE_LINEAGE)
        marker_danger_count = marker_meas.total_dangerous_failures()
        marker_lane_errors = int(marker_meas.get_numeric(DIMENSION_LANE_ERRORS) or 0)

        if (
            marker_danger_count
            > prereg.frozen_thresholds.max_acceptable_dangerous_failures
        ):
            marker_breached_danger = True
            blockers.append(
                f"Marker baseline breached dangerous failure budget ({marker_danger_count} > {prereg.frozen_thresholds.max_acceptable_dangerous_failures})"
            )

        if (
            marker_cov is not None
            and marker_cov < prereg.frozen_thresholds.min_evidence_coverage_for_retained
        ):
            blockers.append(
                f"Marker baseline evidence coverage {marker_cov:.2f} below required {prereg.frozen_thresholds.min_evidence_coverage_for_retained:.2f}"
            )

        comparator_evals[marker_spec.system_id] = ComparatorEvaluation(
            system_id=marker_spec.system_id,
            is_marker_baseline=True,
            doc_exact_rate=marker_exact,
            scalar_accuracy=marker_acc,
            evidence_coverage=marker_cov,
            dangerous_failure_count=marker_danger_count,
            dangerous_budget_breached=marker_breached_danger,
            lane_errors=marker_lane_errors,
            review_burden_status=marker_meas.get_dimension_status(
                DIMENSION_REVIEW_BURDEN
            ),
            review_burden_summary=str(
                marker_meas.dimensions.get(
                    DIMENSION_REVIEW_BURDEN,
                    DimensionMeasurement(
                        DIMENSION_REVIEW_BURDEN, MEASUREMENT_STATUS_UNAVAILABLE
                    ),
                ).value
                or ""
            ),
            advantages_over_marker=(),
            disadvantages_vs_marker=(),
            integrable_as_candidate=False,
            active_integration_verified=False,
        )

    # 6. Specialist evaluations & Reason-to-leave analysis
    specialist_specs = prereg.get_specialists()
    active_integrated_specialists: list[str] = []
    conceded_specialists: list[str] = []

    # Map available integration verifications by system_id
    integrations_map = {i.system_id: i for i in bundle.integrations}

    for s_spec in specialist_specs:
        s_meas = bundle.comparators.get(s_spec.system_id)
        if not s_meas:
            continue

        s_exact = s_meas.get_numeric(DIMENSION_DOC_EXACT_RATE)
        s_acc = s_meas.get_numeric(DIMENSION_SCALAR_ACCURACY)
        s_cov = s_meas.get_numeric(DIMENSION_EVIDENCE_LINEAGE)
        s_danger = s_meas.total_dangerous_failures()
        s_breached_danger = (
            s_danger > prereg.frozen_thresholds.max_acceptable_dangerous_failures
        )
        s_lane_errors = int(s_meas.get_numeric(DIMENSION_LANE_ERRORS) or 0)

        advantages: list[str] = []
        disadvantages: list[str] = []

        if s_breached_danger:
            disadvantages.append(
                f"Breached dangerous failure budget with {s_danger} dangerous failures ({dict(s_meas.danger_counts)})"
            )

        if s_cov is not None and (marker_cov is not None and s_cov < marker_cov):
            disadvantages.append(
                f"Lacks verifiable evidence lineage ({s_cov:.2f} vs {marker_cov:.2f})"
            )

        if s_lane_errors > 0:
            disadvantages.append(f"Experienced {s_lane_errors} lane/provider errors")

        # Quality comparisons: only when both are measured numeric values
        exact_gain = (
            (s_exact - marker_exact)
            if (s_exact is not None and marker_exact is not None)
            else 0.0
        )
        acc_gain = (
            (s_acc - marker_acc)
            if (s_acc is not None and marker_acc is not None)
            else 0.0
        )

        if (
            exact_gain >= prereg.frozen_thresholds.quality_margin_for_displacement
            or acc_gain >= prereg.frozen_thresholds.quality_margin_for_displacement
        ):
            advantages.append(
                f"Superior raw accuracy/field coverage (exact delta: +{exact_gain:.3f}, acc delta: +{acc_gain:.3f})"
            )

        # Validate integration verification
        int_ver = integrations_map.get(s_spec.system_id)
        has_active_verified_integration = False
        integration_errors: list[str] = []

        if int_ver is not None and int_ver.status == INTEGRATION_STATUS_VERIFIED_ACTIVE:
            integration_errors = validate_active_integration(
                int_ver, prereg, repo_root, as_of_date
            )
            if not integration_errors:
                has_active_verified_integration = True
            else:
                limitations.append(
                    f"Integration candidate {s_spec.system_id!r} failed active verification: {'; '.join(integration_errors)}"
                )

        integrable_as_candidate = bool(
            prereg.frozen_thresholds.allow_candidate_integration
            and (s_breached_danger or (s_cov is not None and s_cov < 0.5))
        )

        # Reason to leave formulation
        if advantages:
            for adv in advantages:
                if s_breached_danger or (s_cov is not None and s_cov < 0.5):
                    # Specialist cannot be authority due to dangerous failure breach or zero lineage.
                    # Integration requires active verified integration binding; otherwise it is measured.
                    if (
                        has_active_verified_integration
                        and integrable_as_candidate
                        and int_ver is not None
                    ):
                        reason_status = REASON_STATUS_INTEGRATED
                        active_integrated_specialists.append(s_spec.system_id)
                        res_details = (
                            f"Integrated as non-authoritative candidate generator feeding Marker proof machinery; "
                            f"verified by bridge artifact {int_ver.evidence_artifact_path!r} ({int_ver.evidence_artifact_sha256[:12]})."
                        )
                    else:
                        reason_status = REASON_STATUS_MEASURED
                        if (
                            int_ver
                            and int_ver.status
                            == INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED
                        ):
                            res_details = (
                                f"Advantage measured and bounded; specialist rejected as authority due to {s_danger} dangerous failures. "
                                f"Candidate integration is prospective/unimplemented in current release."
                            )
                        elif integration_errors:
                            res_details = (
                                f"Advantage measured and bounded; specialist rejected as authority due to {s_danger} dangerous failures "
                                f"and invalid integration binding ({'; '.join(integration_errors)})."
                            )
                        else:
                            res_details = (
                                f"Advantage measured and bounded; specialist rejected as authority due to {s_danger} dangerous failures "
                                f"and lack of active verified integration bridge."
                            )
                else:
                    # Specialist is safe and superior without dangerous failures:
                    # Marker explicitly concedes the workflow/slice (Invariant 61/62).
                    reason_status = REASON_STATUS_CONCEDED
                    conceded_specialists.append(s_spec.system_id)
                    res_details = "Specialist achieves strictly superior verified outcome; Marker concedes slice."

                reason_ledger.append(
                    ReasonToLeaveItem(
                        reason_id=f"reason_{s_spec.system_id}_{len(reason_ledger) + 1}",
                        specialist_system_id=s_spec.system_id,
                        dimension=(
                            DIMENSION_SCALAR_ACCURACY
                            if "accuracy" in adv.lower()
                            else DIMENSION_DOC_EXACT_RATE
                        ),
                        description=adv,
                        status=reason_status,
                        is_material=True,
                        resolution_details=res_details,
                    )
                )

        comparator_evals[s_spec.system_id] = ComparatorEvaluation(
            system_id=s_spec.system_id,
            is_marker_baseline=False,
            doc_exact_rate=s_exact,
            scalar_accuracy=s_acc,
            evidence_coverage=s_cov,
            dangerous_failure_count=s_danger,
            dangerous_budget_breached=s_breached_danger,
            lane_errors=s_lane_errors,
            review_burden_status=s_meas.get_dimension_status(DIMENSION_REVIEW_BURDEN),
            review_burden_summary=str(
                s_meas.dimensions.get(
                    DIMENSION_REVIEW_BURDEN,
                    DimensionMeasurement(
                        DIMENSION_REVIEW_BURDEN, MEASUREMENT_STATUS_UNAVAILABLE
                    ),
                ).value
                or ""
            ),
            advantages_over_marker=tuple(advantages),
            disadvantages_vs_marker=tuple(disadvantages),
            integrable_as_candidate=integrable_as_candidate,
            active_integration_verified=has_active_verified_integration,
        )

    # 7. Check for unknown/unresolved material reasons in the ledger
    has_unknown_material_reason = any(
        r.status == REASON_STATUS_UNKNOWN and r.is_material for r in reason_ledger
    )
    if has_unknown_material_reason:
        blockers.append(
            "Reason-to-leave ledger contains unknown/unresolved material reasons."
        )

    # 8. Terminal Outcome Determination
    if blockers:
        outcome = OUTCOME_INCONCLUSIVE
        summary = (
            f"Evaluation inconclusive due to {len(blockers)} blocker(s): "
            + "; ".join(blockers)
        )
    elif conceded_specialists:
        # A safe specialist beat Marker on material dimensions and Marker explicitly concedes.
        # Accepts negative simplification without requiring Marker win.
        outcome = OUTCOME_EXPLICIT_CONCESSION
        summary = (
            f"Explicit concession: Marker UI concedes slice to specialist(s) {sorted(conceded_specialists)} "
            f"who achieve superior, safe, and verified performance. Rational user is directed to specialist or simplified route."
        )
    elif active_integrated_specialists:
        # Specialist raw generative advantage is real AND verified active candidate bridge exists.
        outcome = OUTCOME_INTEGRATE_OR_ROUTE
        summary = (
            f"Integrate or route: Marker retains authority while candidate-generating advantages of "
            f"{sorted(active_integrated_specialists)} are actively integrated into verification machinery."
        )
    else:
        # Marker UI satisfies evidence lineage, safety budget, and no specialist displaced it or is active-integrated.
        outcome = OUTCOME_MARKER_RETAINED
        summary = (
            "Marker retained: Marker UI is the only route satisfying full evidence lineage and safety budgets "
            "without unmitigated specialist failure modes (observed dangerous failure gate: 0 on declared corpus)."
        )

    decision_id = f"disp_dec_{prereg.preregistration_id}_{hashlib.sha256((prereg.preregistration_id + as_of_date).encode('utf-8')).hexdigest()[:12]}"

    # 9. Compute deterministic rederivation digest
    digest_body = {
        "schema_version": DISPLACEMENT_DECISION_SCHEMA_VERSION,
        "decision_id": decision_id,
        "preregistration_id": prereg.preregistration_id,
        "workflow": prereg.workflow,
        "as_of_date": as_of_date,
        "outcome": outcome,
        "fairness_passed": fairness_passed,
        "protocol_timing": prereg.protocol_timing,
        "blockers": sorted(blockers),
        "reason_ledger": [r.to_dict() for r in reason_ledger],
        "supporting_artifact_sha256": bundle.supporting_artifact_sha256,
    }
    rederivation_digest = _compute_sha256(digest_body)

    return DisplacementDecision(
        schema_version=DISPLACEMENT_DECISION_SCHEMA_VERSION,
        decision_id=decision_id,
        preregistration_id=prereg.preregistration_id,
        workflow=prereg.workflow,
        as_of_date=as_of_date,
        outcome=outcome,
        summary=summary,
        protocol_timing=prereg.protocol_timing,
        limitations=tuple(limitations),
        reason_ledger=tuple(reason_ledger),
        fairness_passed=fairness_passed,
        blockers=tuple(blockers),
        comparator_evaluations=comparator_evals,
        supporting_artifact_sha256=bundle.supporting_artifact_sha256,
        rederivation_digest=rederivation_digest,
    )


__all__ = [
    "derive_displacement_decision",
]
