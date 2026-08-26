"""Focused tests for Invariant-62 displacement contracts, data models, and validation."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
from app.eval.accountability.displacement import (
    DIMENSION_DANGEROUS_FAILURES,
    DIMENSION_DOC_EXACT_RATE,
    DIMENSION_EVIDENCE_LINEAGE,
    DIMENSION_SCALAR_ACCURACY,
    INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
    INTEGRATION_STATUS_VERIFIED_ACTIVE,
    MEASUREMENT_STATUS_MEASURED,
    MEASUREMENT_STATUS_UNAVAILABLE,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_MARKER_RETAINED,
    PROTOCOL_PROSPECTIVE_PREREGISTRATION,
    PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    ComparatorDeclaredSpec,
    ComparatorMeasurements,
    CorpusPreregistration,
    DimensionMeasurement,
    DisplacementDecisionError,
    DisplacementMeasurementBundle,
    DisplacementPreregistration,
    ExecutedComparatorFacts,
    FairnessContract,
    FairnessVerification,
    FrozenDecisionThresholds,
    IntegrationVerification,
    derive_displacement_decision,
    validate_active_integration,
    validate_displacement_measurement_bundle,
    validate_displacement_preregistration,
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
# Unit & Contract Validation Tests
# -----------------------------------------------------------------------------


def test_preregistration_clean_validation():
    prereg = _build_test_preregistration()
    errors = validate_displacement_preregistration(
        prereg, as_of_date=INJECTED_AS_OF_DATE
    )
    assert errors == []


def test_preregistration_rejects_prospective_timing_lie():
    """Claiming prospective preregistration when evidence predates registration fails closed."""
    prereg = _build_test_preregistration(
        prereg_date="2026-08-25T00:00:00Z",
        protocol_timing=PROTOCOL_PROSPECTIVE_PREREGISTRATION,
    )
    bundle = _build_test_bundle(
        prereg,
        evidence_date="2026-08-20T00:00:00Z",
    )
    b_errs = validate_displacement_measurement_bundle(
        bundle, prereg=prereg, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("Prospective timing lie" in e for e in b_errs)

    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_INCONCLUSIVE
    assert any("Prospective timing violation" in b for b in decision.blockers)


def test_retrospective_timing_disclosure_accepted_cleanly():
    """Declaring retrospective_frozen_replay allows evidence pre-dating registration with clear disclosure."""
    prereg = _build_test_preregistration(
        prereg_date="2026-08-26T00:00:00Z",
        protocol_timing=PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    )
    bundle = _build_test_bundle(
        prereg,
        evidence_date="2026-08-20T00:00:00Z",
    )
    b_errs = validate_displacement_measurement_bundle(
        bundle, prereg=prereg, as_of_date=INJECTED_AS_OF_DATE
    )
    assert b_errs == []

    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE
    )
    assert decision.outcome == OUTCOME_MARKER_RETAINED
    assert any("Retrospective frozen replay" in lim for lim in decision.limitations)


def test_preregistration_fails_on_missing_or_duplicate_baseline():
    prereg = _build_test_preregistration()
    raw = prereg.to_dict()

    # Zero marker baselines
    raw_no_base = copy.deepcopy(raw)
    raw_no_base["comparators"][0]["is_marker_baseline"] = False
    errs = validate_displacement_preregistration(raw_no_base)
    assert any("exactly 1 is_marker_baseline" in e for e in errs)

    # Duplicate marker baselines
    raw_two_base = copy.deepcopy(raw)
    raw_two_base["comparators"][1]["is_marker_baseline"] = True
    errs2 = validate_displacement_preregistration(raw_two_base)
    assert any("exactly 1 is_marker_baseline" in e for e in errs2)


def test_measurement_tristate_preserves_unavailable_without_zero_conversion():
    meas_unavail = DimensionMeasurement(
        dimension="review_burden",
        status=MEASUREMENT_STATUS_UNAVAILABLE,
        value=None,
    )
    assert meas_unavail.is_unavailable
    assert meas_unavail.as_numeric() is None

    meas_measured = DimensionMeasurement(
        dimension="doc_exact_rate",
        status=MEASUREMENT_STATUS_MEASURED,
        value=0.95,
    )
    assert meas_measured.is_measured
    assert meas_measured.as_numeric() == 0.95

    with pytest.raises(DisplacementDecisionError):
        # Measured status requires concrete value
        DimensionMeasurement(
            dimension="cost",
            status=MEASUREMENT_STATUS_MEASURED,
            value=None,
        )


def test_integration_verification_validation_rules(tmp_path: Path):
    """Integration contract must validate against repo root, file existence, and exact SHA-256."""
    prereg = _build_test_preregistration()

    # Create dummy repo-relative file under docs/reference/measurements
    meas_dir = tmp_path / "docs" / "reference" / "measurements"
    meas_dir.mkdir(parents=True)
    art_file = meas_dir / "test_bridge.json"
    raw_content = b'{"bridge": "active_v1"}'
    art_file.write_bytes(raw_content)
    real_sha = hashlib.sha256(raw_content).hexdigest()

    valid_integration = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/test_bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs = validate_active_integration(
        valid_integration, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert errs == []

    # 1. Non repo-relative or path outside docs/reference/measurements
    bad_path_int = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="outside/dir/bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs_path = validate_active_integration(
        bad_path_int, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any(
        "must be repo-relative under docs/reference/measurements/" in e
        for e in errs_path
    )

    # 2. Path containing '..'
    bad_traversal = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/../bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs_trav = validate_active_integration(
        bad_traversal, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("cannot contain '..'" in e for e in errs_trav)

    # 3. Missing file
    missing_file_int = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/nonexistent_file.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs_missing = validate_active_integration(
        missing_file_int, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("does not exist" in e for e in errs_missing)

    # 4. Wrong SHA-256 digest on existing file
    wrong_sha_int = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/test_bridge.json",
        evidence_artifact_sha256="d" * 64,  # wrong digest!
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs_sha = validate_active_integration(
        wrong_sha_int, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("SHA mismatch" in e for e in errs_sha)

    # 5. Wrong workflow scope
    wrong_wf_int = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/test_bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope="other_workflow",
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs_wf = validate_active_integration(
        wrong_wf_int, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("workflow_scope" in e for e in errs_wf)

    # 6. Wrong corpus fingerprint scope
    wrong_fp_int = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/test_bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope="e" * 64,  # wrong fingerprint
        corroboration_contract="proof corroborated",
        verified_at="2026-08-22T00:00:00Z",
    )
    errs_fp = validate_active_integration(
        wrong_fp_int, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("corpus_fingerprint_scope" in e for e in errs_fp)

    # 7. Future verified_at date
    future_date_int = IntegrationVerification(
        system_id="specialist_a",
        status=INTEGRATION_STATUS_VERIFIED_ACTIVE,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="docs/reference/measurements/test_bridge.json",
        evidence_artifact_sha256=real_sha,
        workflow_scope=prereg.workflow,
        corpus_fingerprint_scope=prereg.corpus.fingerprint,
        corroboration_contract="proof corroborated",
        verified_at="2026-08-30T00:00:00Z",  # in future!
    )
    errs_dt = validate_active_integration(
        future_date_int, prereg, repo_root=tmp_path, as_of_date=INJECTED_AS_OF_DATE
    )
    assert any("in future" in e for e in errs_dt)
