"""Focused tests for Invariant-62 rational-user displacement decision engine."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.eval.accountability.displacement import (
    DIMENSION_DANGEROUS_FAILURES,
    DIMENSION_DOC_EXACT_RATE,
    DIMENSION_EVIDENCE_LINEAGE,
    DIMENSION_SCALAR_ACCURACY,
    INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
    INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
    INTEGRATION_STATUS_VERIFIED_ACTIVE,
    MEASUREMENT_STATUS_MEASURED,
    MEASUREMENT_STATUS_NOT_APPLICABLE,
    MEASUREMENT_STATUS_UNAVAILABLE,
    OUTCOME_EXPLICIT_CONCESSION,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_INTEGRATE_OR_ROUTE,
    OUTCOME_MARKER_RETAINED,
    PROTOCOL_PROSPECTIVE_PREREGISTRATION,
    REASON_STATUS_CONCEDED,
    REASON_STATUS_INTEGRATED,
    REASON_STATUS_MEASURED,
    ComparatorDeclaredSpec,
    ComparatorMeasurements,
    CorpusPreregistration,
    DimensionMeasurement,
    DisplacementMeasurementBundle,
    DisplacementPreregistration,
    ExecutedComparatorFacts,
    FairnessContract,
    FairnessVerification,
    FrozenDecisionThresholds,
    IntegrationVerification,
    derive_displacement_decision,
    validate_persisted_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INJECTED_AS_OF_DATE = "2026-08-26T00:00:00Z"


def _build_test_preregistration(
    *,
    prereg_id: str = "test_prereg_001",
    workflow: str = "document_extraction",
    allow_candidate_integration: bool = True,
    max_dangerous_failures: int = 0,
    quality_margin: float = 0.05,
    prereg_date: str = "2026-08-20T00:00:00Z",
    protocol_timing: str = PROTOCOL_PROSPECTIVE_PREREGISTRATION,
) -> DisplacementPreregistration:
    corpus = CorpusPreregistration(
        manifest_version="marker.test_corpus.v1",
        fingerprint="a" * 64,
        document_count=20,
        slices=("slice.a", "slice.b"),
        task_description="Test extraction task",
        normalization_rules={"field": "exact"},
        declared_invariants=("inv_1",),
    )
    comparators = (
        ComparatorDeclaredSpec(
            system_id="marker_route",
            is_marker_baseline=True,
            system_kind="evidence_backed",
            system_identity="marker.v1",
            input_path_declared="plain text",
            adaptation_rules_declared="verifiable evidence only",
        ),
        ComparatorDeclaredSpec(
            system_id="specialist_a",
            is_marker_baseline=False,
            system_kind="external_model",
            system_identity="specialist.v1",
            input_path_declared="plain text",
            adaptation_rules_declared="standard prompt",
        ),
    )
    fairness = FairnessContract(
        same_user_level_input_required=True,
        declared_adaptation_rules_required=True,
        disallow_privileged_features=True,
    )
    thresholds = FrozenDecisionThresholds(
        max_acceptable_dangerous_failures=max_dangerous_failures,
        threshold_scope="declared_corpus_observed_count",
        min_evidence_coverage_for_retained=1.0,
        quality_margin_for_displacement=quality_margin,
        allow_candidate_integration=allow_candidate_integration,
    )
    material_dims = (
        DIMENSION_DOC_EXACT_RATE,
        DIMENSION_SCALAR_ACCURACY,
        DIMENSION_EVIDENCE_LINEAGE,
        DIMENSION_DANGEROUS_FAILURES,
    )
    return DisplacementPreregistration(
        preregistration_id=prereg_id,
        workflow=workflow,
        corpus=corpus,
        comparators=comparators,
        fairness_contract=fairness,
        material_dimensions=material_dims,
        frozen_thresholds=thresholds,
        preregistration_date=prereg_date,
        protocol_timing=protocol_timing,
    )


def _build_test_bundle(
    prereg: DisplacementPreregistration,
    *,
    marker_exact: float = 0.90,
    marker_acc: float = 0.95,
    marker_cov: float = 1.0,
    marker_danger: int = 0,
    specialist_exact: float = 0.85,
    specialist_acc: float = 0.90,
    specialist_cov: float = 0.0,
    specialist_danger: int = 0,
    is_fair: bool = True,
    input_parity: bool = True,
    adaptation_parity: bool = True,
    full_corpus: bool = True,
    discrepancies: tuple[str, ...] = (),
    evidence_date: str = "2026-08-22T00:00:00Z",
    supporting_sha: str = "b" * 64,
    integrations: tuple[IntegrationVerification, ...] = (),
) -> DisplacementMeasurementBundle:
    marker_dims = {
        DIMENSION_DOC_EXACT_RATE: DimensionMeasurement(
            DIMENSION_DOC_EXACT_RATE, MEASUREMENT_STATUS_MEASURED, marker_exact
        ),
        DIMENSION_SCALAR_ACCURACY: DimensionMeasurement(
            DIMENSION_SCALAR_ACCURACY, MEASUREMENT_STATUS_MEASURED, marker_acc
        ),
        DIMENSION_EVIDENCE_LINEAGE: DimensionMeasurement(
            DIMENSION_EVIDENCE_LINEAGE, MEASUREMENT_STATUS_MEASURED, marker_cov
        ),
        DIMENSION_DANGEROUS_FAILURES: DimensionMeasurement(
            DIMENSION_DANGEROUS_FAILURES, MEASUREMENT_STATUS_MEASURED, marker_danger
        ),
    }
    spec_dims = {
        DIMENSION_DOC_EXACT_RATE: DimensionMeasurement(
            DIMENSION_DOC_EXACT_RATE, MEASUREMENT_STATUS_MEASURED, specialist_exact
        ),
        DIMENSION_SCALAR_ACCURACY: DimensionMeasurement(
            DIMENSION_SCALAR_ACCURACY, MEASUREMENT_STATUS_MEASURED, specialist_acc
        ),
        DIMENSION_EVIDENCE_LINEAGE: DimensionMeasurement(
            DIMENSION_EVIDENCE_LINEAGE, MEASUREMENT_STATUS_MEASURED, specialist_cov
        ),
        DIMENSION_DANGEROUS_FAILURES: DimensionMeasurement(
            DIMENSION_DANGEROUS_FAILURES, MEASUREMENT_STATUS_MEASURED, specialist_danger
        ),
    }

    comps = {
        "marker_route": ComparatorMeasurements(
            system_id="marker_route",
            dimensions=marker_dims,
            danger_counts={"fabrications": marker_danger} if marker_danger else {},
        ),
        "specialist_a": ComparatorMeasurements(
            system_id="specialist_a",
            dimensions=spec_dims,
            danger_counts={"fabrications": specialist_danger}
            if specialist_danger
            else {},
        ),
    }
    executed_facts = (
        ExecutedComparatorFacts(
            system_id="marker_route",
            system_kind="evidence_backed",
            system_identity="marker.v1",
            input_path="plain text",
            adaptation_rules="verifiable evidence only",
        ),
        ExecutedComparatorFacts(
            system_id="specialist_a",
            system_kind="external_model",
            system_identity="specialist.v1",
            input_path="plain text",
            adaptation_rules="standard prompt",
        ),
    )
    fairness = FairnessVerification(
        input_parity_verified=input_parity,
        adaptation_parity_verified=adaptation_parity,
        full_corpus_evaluated=full_corpus,
        is_fair=is_fair,
        executed_facts=executed_facts,
        discrepancies=discrepancies,
    )
    return DisplacementMeasurementBundle(
        measurement_id="test_bundle_001",
        preregistration_id=prereg.preregistration_id,
        corpus_fingerprint=prereg.corpus.fingerprint,
        comparators=comps,
        fairness=fairness,
        evidence_date=evidence_date,
        supporting_artifact_sha256=supporting_sha,
        integrations=integrations,
    )


# -----------------------------------------------------------------------------
# Decision Outcome & Rederivation Tests
# -----------------------------------------------------------------------------


def test_outcome_integrate_or_route_with_verified_temp_artifact(tmp_path: Path):
    """Counterfactual: active verified integration bridge with real file yields integrate_or_route."""
    prereg = _build_test_preregistration(allow_candidate_integration=True)

    meas_dir = tmp_path / "docs" / "reference" / "measurements"
    meas_dir.mkdir(parents=True)
    art_file = meas_dir / "candidate_bridge.json"
    raw_content = b'{"candidate_bridge": "verified"}'
    art_file.write_bytes(raw_content)
    real_sha = hashlib.sha256(raw_content).hexdigest()

    active_integration = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/candidate_bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="independent verification with proof fallback",
        verified_at="2026-08-22T00:00:00Z",
    )
    bundle = _build_test_bundle(
        prereg,
        marker_exact=0.80,
        marker_acc=0.90,
        marker_cov=1.0,
        marker_danger=0,
        specialist_exact=0.95,  # +15% gain
        specialist_acc=0.99,
        specialist_cov=0.0,  # no lineage
        specialist_danger=2,  # dangerous breach
        integrations=(active_integration,),
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE, repo_root=tmp_path
    )
    assert decision.outcome == OUTCOME_INTEGRATE_OR_ROUTE
    assert len(decision.reason_ledger) == 1
    assert decision.reason_ledger[0].status == REASON_STATUS_INTEGRATED
    assert "bridge artifact" in decision.reason_ledger[0].resolution_details

    # Rederivation validation passes cleanly with repo_root
    val_errs = validate_persisted_decision(
        decision, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE, repo_root=tmp_path
    )
    assert val_errs == []


def test_specialist_better_unintegrated_yields_measured_and_marker_retained():
    """Specialist has raw coverage gain but integration is future/unimplemented -> reason is measured, outcome is marker_retained."""
    prereg = _build_test_preregistration(allow_candidate_integration=True)
    future_integration = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="",
        evidence_artifact_sha256="",
        workflow_scope="document_extraction",
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="future proof machinery",
    )
    bundle = _build_test_bundle(
        prereg,
        marker_exact=0.80,
        marker_acc=0.90,
        marker_cov=1.0,
        marker_danger=0,
        specialist_exact=0.95,
        specialist_acc=0.99,
        specialist_cov=0.0,
        specialist_danger=2,
        integrations=(future_integration,),
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_MARKER_RETAINED
    assert len(decision.reason_ledger) == 1
    assert decision.reason_ledger[0].status == REASON_STATUS_MEASURED
    assert "unimplemented" in decision.reason_ledger[0].resolution_details.lower()


def test_fake_or_missing_integration_digest_rejects_integration(tmp_path: Path):
    """Invalid/fake SHA-256 or missing path rejects active integration and falls back to measured."""
    prereg = _build_test_preregistration(allow_candidate_integration=True)
    fake_integration = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/nonexistent.json",
        evidence_artifact_sha256="c" * 64,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="fake",
        verified_at="2026-08-22T00:00:00Z",
    )

    bundle = _build_test_bundle(
        prereg,
        marker_exact=0.80,
        specialist_exact=0.95,
        specialist_danger=2,
        integrations=(fake_integration,),
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE, repo_root=tmp_path
    )
    assert decision.outcome == OUTCOME_MARKER_RETAINED
    assert decision.reason_ledger[0].status == REASON_STATUS_MEASURED
    assert any("failed active verification" in lim for lim in decision.limitations)


def test_outcome_explicit_concession_valid_negative_simplification():
    """Invariant 61/62: Specialist is safe, superior, verified; Marker explicitly concedes slice."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(
        prereg,
        marker_exact=0.75,
        marker_acc=0.80,
        marker_cov=1.0,
        marker_danger=0,
        specialist_exact=0.95,  # +20% gain
        specialist_acc=0.98,
        specialist_cov=1.0,  # verified lineage
        specialist_danger=0,  # 0 dangerous failures
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_EXPLICIT_CONCESSION
    assert len(decision.reason_ledger) == 1
    assert decision.reason_ledger[0].status == REASON_STATUS_CONCEDED
    assert (
        "concession" in decision.summary.lower()
        or "concedes" in decision.summary.lower()
    )


def test_outcome_inconclusive_fairness_mismatch_inputs():
    """Fairness mismatch (unequal inputs) forces inconclusive."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(
        prereg,
        input_parity=False,
        is_fair=False,
        discrepancies=(
            "Specialist received pre-normalized structured text instead of raw text",
        ),
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert decision.fairness_passed is False
    assert any("Fairness mismatch" in b for b in decision.blockers)


def test_outcome_inconclusive_fairness_mismatch_identity():
    """Executed system identity differing from declared spec fails fairness and forces inconclusive."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg)

    # Tamper executed facts identity
    tampered_facts = (
        ExecutedComparatorFacts(
            system_id="marker_route",
            system_kind="evidence_backed",
            system_identity="marker.v1",
            input_path="plain text",
            adaptation_rules="verifiable evidence only",
        ),
        ExecutedComparatorFacts(
            system_id="specialist_a",
            system_kind="external_model",
            system_identity="tampered_model_v2_not_declared",  # mismatch!
            input_path="plain text",
            adaptation_rules="standard prompt",
        ),
    )
    fairness_tampered = FairnessVerification(
        input_parity_verified=True,
        adaptation_parity_verified=True,
        full_corpus_evaluated=True,
        is_fair=True,
        executed_facts=tampered_facts,
    )
    bundle_tampered = DisplacementMeasurementBundle(
        measurement_id=bundle.measurement_id,
        preregistration_id=bundle.preregistration_id,
        corpus_fingerprint=bundle.corpus_fingerprint,
        comparators=bundle.comparators,
        fairness=fairness_tampered,
        evidence_date=bundle.evidence_date,
        supporting_artifact_sha256=bundle.supporting_artifact_sha256,
    )
    decision = derive_displacement_decision(
        prereg, bundle_tampered, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert decision.fairness_passed is False
    assert any("Identity mismatch" in b for b in decision.blockers)


def test_outcome_inconclusive_fairness_false_acceptance():
    """full_corpus_evaluated = False fails fairness and forces inconclusive."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg, full_corpus=False)
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert any(
        "Not all systems evaluated on full corpus" in b for b in decision.blockers
    )


def test_outcome_inconclusive_missing_material_dimension():
    """Missing material dimension cannot become 0.0 or measured; forces inconclusive."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg)
    new_dims = dict(bundle.comparators["specialist_a"].dimensions)
    new_dims[DIMENSION_DANGEROUS_FAILURES] = DimensionMeasurement(
        DIMENSION_DANGEROUS_FAILURES, MEASUREMENT_STATUS_UNAVAILABLE, None
    )
    new_spec = ComparatorMeasurements(
        system_id="specialist_a",
        dimensions=new_dims,
    )
    new_comps = dict(bundle.comparators)
    new_comps["specialist_a"] = new_spec
    bundle_missing = DisplacementMeasurementBundle(
        measurement_id=bundle.measurement_id,
        preregistration_id=bundle.preregistration_id,
        corpus_fingerprint=bundle.corpus_fingerprint,
        comparators=new_comps,
        fairness=bundle.fairness,
        evidence_date=bundle.evidence_date,
        supporting_artifact_sha256=bundle.supporting_artifact_sha256,
    )

    decision = derive_displacement_decision(
        prereg, bundle_missing, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert any(
        "Material dimension 'dangerous_failures' is unavailable" in b
        for b in decision.blockers
    )


def test_outcome_inconclusive_unjustified_not_applicable_dimension():
    """Unjustified not_applicable material dimension forces inconclusive."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg)
    new_dims = dict(bundle.comparators["specialist_a"].dimensions)
    new_dims[DIMENSION_DANGEROUS_FAILURES] = DimensionMeasurement(
        DIMENSION_DANGEROUS_FAILURES,
        MEASUREMENT_STATUS_NOT_APPLICABLE,
        value=None,
        not_applicable_justification="",  # unjustified!
    )
    new_spec = ComparatorMeasurements(
        system_id="specialist_a",
        dimensions=new_dims,
    )
    new_comps = dict(bundle.comparators)
    new_comps["specialist_a"] = new_spec
    bundle_na = DisplacementMeasurementBundle(
        measurement_id=bundle.measurement_id,
        preregistration_id=bundle.preregistration_id,
        corpus_fingerprint=bundle.corpus_fingerprint,
        comparators=new_comps,
        fairness=bundle.fairness,
        evidence_date=bundle.evidence_date,
        supporting_artifact_sha256=bundle.supporting_artifact_sha256,
    )

    decision = derive_displacement_decision(
        prereg, bundle_na, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert any("not_applicable without justification" in b for b in decision.blockers)


def test_outcome_inconclusive_marker_dangerous_breach():
    """If Marker itself breaches the dangerous failure budget, retention is blocked."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(
        prereg,
        marker_exact=0.90,
        marker_danger=3,  # Marker breaches budget
        specialist_exact=0.80,
        specialist_danger=0,
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert any(
        "Marker baseline breached dangerous failure budget" in b
        for b in decision.blockers
    )


def test_specialist_better_nonintegrable_tradeoff_measured():
    """When specialist has dangerous failures and candidate integration is disallowed, tradeoff is measured."""
    prereg = _build_test_preregistration(allow_candidate_integration=False)
    bundle = _build_test_bundle(
        prereg,
        marker_exact=0.85,
        specialist_exact=0.95,
        specialist_danger=1,
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_MARKER_RETAINED
    assert len(decision.reason_ledger) == 1
    assert decision.reason_ledger[0].status == REASON_STATUS_MEASURED


def test_persisted_decision_detects_manual_flip():
    """Manual flipping of outcome fails closed during validation."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg, marker_exact=0.95, specialist_exact=0.80)
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )

    # Valid persisted decision passes cleanly
    errs = validate_persisted_decision(
        decision, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert errs == []

    # Manually flip outcome
    tampered_dict = decision.to_dict()
    tampered_dict["outcome"] = OUTCOME_EXPLICIT_CONCESSION
    errs_tamper = validate_persisted_decision(
        tampered_dict, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("Outcome mismatch" in e for e in errs_tamper)


def test_persisted_decision_detects_digest_tamper():
    """Tampering with rederivation digest fails validation."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg, marker_exact=0.95, specialist_exact=0.80)
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )

    tampered_digest = decision.to_dict()
    tampered_digest["rederivation_digest"] = "f" * 64
    errs_digest = validate_persisted_decision(
        tampered_digest, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("Rederivation digest mismatch" in e for e in errs_digest)


def test_persisted_decision_detects_supporting_artifact_change():
    """Changing supporting artifact SHA-256 fails validation."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(prereg)
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )

    tampered_dict = decision.to_dict()
    tampered_dict["supporting_artifact_sha256"] = "c" * 64
    errs = validate_persisted_decision(
        tampered_dict, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("Supporting artifact SHA-256 mismatch" in e for e in errs)


def test_persisted_decision_detects_reason_ledger_tamper():
    """Tampering with reason ledger item status fails validation."""
    prereg = _build_test_preregistration()
    bundle = _build_test_bundle(
        prereg, marker_exact=0.80, specialist_exact=0.95, specialist_danger=1
    )
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )

    tampered_dict = decision.to_dict()
    tampered_dict["reason_ledger"][0]["status"] = REASON_STATUS_INTEGRATED
    errs = validate_persisted_decision(
        tampered_dict, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("Reason ledger mismatch" in e for e in errs)
