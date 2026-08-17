"""PR75 immutable dependency disclosure and scoped risk-policy tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.kernel.errors import KernelError
from app.kernel.verification_risk import (
    AUTHORITY_EMPIRICALLY_VALIDATED_MODEL,
    AUTHORITY_SOURCE_NATIVE,
    DEPENDENCY_CORRELATED,
    DEPENDENCY_INDEPENDENT,
    DEPENDENCY_UNKNOWN,
    DISCLOSURE_COMPLETE,
    DISCLOSURE_PARTIAL,
    EVIDENCE_MODEL,
    EVIDENCE_SOURCE_NATIVE,
    OUTCOME_ABSTAINED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_UNCERTAIN,
    OUTCOME_VERIFIED,
    REASON_EXPIRED,
    REASON_INSUFFICIENT,
    REASON_MODEL_ONLY_HIGH_RISK,
    REASON_RISK_BOUND,
    REASON_SCOPE,
    REASON_SHIFT,
    REASON_UNKNOWN_OR_CORRELATED,
    SHIFT_MATCHED,
    SHIFT_SHIFTED,
    DependencyDisclosureRecord,
    VerificationRiskEvidenceRecord,
    VerificationRiskPolicy,
    classify_dependency_status,
    evaluate_verification_risk_policy,
)


def disclosure(record_id: str, witness_ref: str, **changes) -> DependencyDisclosureRecord:
    values = {
        "record_id": record_id,
        "witness_ref": witness_ref,
        "disclosure_quality": DISCLOSURE_COMPLETE,
        "architecture_family": "ocr-transformer",
        "base_model_family": f"base-{witness_ref}",
        "training_sources": ("dataset-b", "dataset-a"),
        "teacher_lineage": (f"teacher-{witness_ref}",),
        "shared_dependency_refs": (),
        "renderer_profile": f"renderer-{witness_ref}",
        "preprocessor_profile": f"pre-{witness_ref}",
        "runtime_profile": "onnxruntime-1.22/cpu",
        "quantization_profile": "fp32",
    }
    values.update(changes)
    return DependencyDisclosureRecord(**values)


def risk_evidence(**changes) -> VerificationRiskEvidenceRecord:
    values = {
        "record_id": "risk-1",
        "policy_id": "policy.invoice-total",
        "policy_revision": "rev-1",
        "workflow_class": "high_risk.invoice.v1",
        "claim_authority_class": AUTHORITY_SOURCE_NATIVE,
        "evaluation_slice_id": "invoice-total/en/matched/v1",
        "sample_count": 100,
        "risk_upper_bound": "0.04",
        "risk_estimate": "0.01",
        "joint_error_rate": "0.01",
        "disagreement_rate": "0.20",
        "evaluated_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-09-01T00:00:00Z",
        "shift_status": SHIFT_MATCHED,
        "dependency_status": DEPENDENCY_INDEPENDENT,
        "evidence_kind": EVIDENCE_SOURCE_NATIVE,
        "model_only": False,
        "consensus": False,
        "method_id": "wilson-upper-bound",
        "method_version": "1.0.0",
    }
    values.update(changes)
    return VerificationRiskEvidenceRecord(**values)


def policy(**changes) -> VerificationRiskPolicy:
    values = {
        "policy_id": "policy.invoice-total",
        "policy_revision": "rev-1",
        "workflow_class": "high_risk.invoice.v1",
        "evaluation_slice_id": "invoice-total/en/matched/v1",
        "claim_authority_class": AUTHORITY_SOURCE_NATIVE,
        "risk_bound": "0.05",
        "min_sample_count": 50,
        "high_risk": True,
        "require_independent_witnesses": True,
    }
    values.update(changes)
    return VerificationRiskPolicy(**values)


def test_disclosure_identity_normalizes_unordered_sets_and_rematerializes():
    first = disclosure("disc-1", "model-a")
    reordered = disclosure(
        "disc-2",
        "model-a",
        training_sources=("dataset-a", "dataset-b"),
    )
    assert first.identity_hash() == reordered.identity_hash()
    rematerialized = DependencyDisclosureRecord.from_payload(
        first.identity_payload(), record_id="disc-remat"
    )
    assert rematerialized.identity_hash() == first.identity_hash()
    with pytest.raises(TypeError, match="immutable"):
        rematerialized.runtime_profile = "changed"  # type: ignore[misc]


def test_material_profile_change_mints_identity_and_unknown_fields_fail_closed():
    first = disclosure("disc-1", "model-a")
    changed = disclosure(
        "disc-2", "model-a", runtime_profile="onnxruntime-1.22/cuda"
    )
    assert changed.identity_hash() != first.identity_hash()
    with pytest.raises(KernelError, match="unknown disclosure payload fields"):
        DependencyDisclosureRecord.from_payload(
            {**first.identity_payload(), "future_field": True}, record_id="bad"
        )
    with pytest.raises(KernelError, match="contains float"):
        disclosure("disc-float", "model-a", metadata={"score": 0.5})


def test_dependency_classification_is_conservative_and_detects_shared_causes():
    left = disclosure("disc-a", "model-a", renderer_profile="shared-renderer")
    right = disclosure("disc-b", "model-b", renderer_profile="shared-renderer")
    assert (
        classify_dependency_status((left, right), ("model-a", "model-b"))
        == DEPENDENCY_CORRELATED
    )
    independent = disclosure(
        "disc-c",
        "model-b",
        architecture_family="independent-architecture",
        training_sources=("dataset-independent",),
    )
    assert (
        classify_dependency_status((left, independent), ("model-a", "model-b"))
        == DEPENDENCY_INDEPENDENT
    )
    partial = disclosure(
        "disc-p", "model-b", disclosure_quality=DISCLOSURE_PARTIAL
    )
    assert (
        classify_dependency_status((left, partial), ("model-a", "model-b"))
        == DEPENDENCY_UNKNOWN
    )
    assert classify_dependency_status((left,), ("model-a", "alias-a")) == DEPENDENCY_UNKNOWN


def test_complete_disclosure_with_empty_lineage_is_unknown():
    empty = disclosure(
        "disc-empty",
        "model-empty",
        architecture_family=None,
        base_model_family=None,
        training_sources=(),
        teacher_lineage=(),
        shared_dependency_refs=(),
    )
    known = disclosure(
        "disc-known",
        "model-known",
        architecture_family="independent-architecture",
        training_sources=("dataset-independent",),
    )
    assert (
        classify_dependency_status((empty, known), ("model-empty", "model-known"))
        == DEPENDENCY_UNKNOWN
    )


def test_shared_architecture_family_is_correlated():
    left = disclosure(
        "disc-arch-a",
        "model-arch-a",
        architecture_family="shared-architecture",
        training_sources=("dataset-a",),
    )
    right = disclosure(
        "disc-arch-b",
        "model-arch-b",
        architecture_family="shared-architecture",
        training_sources=("dataset-b",),
    )
    assert (
        classify_dependency_status((left, right), ("model-arch-a", "model-arch-b"))
        == DEPENDENCY_CORRELATED
    )


def test_overlapping_training_sources_are_correlated():
    left = disclosure(
        "disc-training-a",
        "model-training-a",
        architecture_family="architecture-a",
        training_sources=("dataset-shared", "dataset-a"),
    )
    right = disclosure(
        "disc-training-b",
        "model-training-b",
        architecture_family="architecture-b",
        training_sources=("dataset-shared", "dataset-b"),
    )
    assert (
        classify_dependency_status(
            (left, right), ("model-training-a", "model-training-b")
        )
        == DEPENDENCY_CORRELATED
    )


def test_complete_disclosure_with_incomplete_lineage_is_unknown():
    incomplete = disclosure(
        "disc-incomplete",
        "model-incomplete",
        architecture_family="architecture-incomplete",
        base_model_family=None,
        training_sources=("dataset-incomplete",),
        teacher_lineage=(),
    )
    known = disclosure(
        "disc-known",
        "model-known",
        architecture_family="independent-architecture",
        training_sources=("dataset-independent",),
    )
    assert (
        classify_dependency_status(
            (incomplete, known), ("model-incomplete", "model-known")
        )
        == DEPENDENCY_UNKNOWN
    )


def test_risk_evidence_identity_binds_policy_and_reloads_without_float_metrics():
    first = risk_evidence()
    rematerialized = VerificationRiskEvidenceRecord.from_payload(
        first.identity_payload(), record_id="risk-remat"
    )
    assert rematerialized.identity_hash() == first.identity_hash()
    changed = risk_evidence(record_id="risk-2", policy_revision="rev-2")
    assert changed.identity_hash() != first.identity_hash()
    with pytest.raises(KernelError, match="unknown risk evidence payload fields"):
        VerificationRiskEvidenceRecord.from_payload(
            {**first.identity_payload(), "silent_default": "bad"}, record_id="bad"
        )
    with pytest.raises(KernelError, match="contains float"):
        risk_evidence(record_id="risk-float", metrics={"ece": 0.1})


def test_valid_source_native_risk_evidence_can_verify_narrow_high_risk_claim():
    decision = evaluate_verification_risk_policy(
        risk_evidence(), policy(), as_of="2026-08-15T00:00:00+00:00"
    )
    assert decision.outcome == OUTCOME_VERIFIED
    assert decision.authority_granted


def test_model_consensus_alone_never_verifies_high_risk_claim():
    evidence = risk_evidence(
        claim_authority_class=AUTHORITY_EMPIRICALLY_VALIDATED_MODEL,
        evidence_kind=EVIDENCE_MODEL,
        model_only=True,
        consensus=True,
        witness_refs=("model-a", "model-b"),
        disclosure_refs=("disc-a", "disc-b"),
    )
    decision = evaluate_verification_risk_policy(
        evidence,
        policy(claim_authority_class=AUTHORITY_EMPIRICALLY_VALIDATED_MODEL),
        disclosures=(disclosure("disc-a", "model-a"), disclosure("disc-b", "model-b")),
        as_of="2026-08-15",
    )
    assert decision.outcome == OUTCOME_ABSTAINED
    assert decision.reason_code == REASON_MODEL_ONLY_HIGH_RISK
    assert not decision.authority_granted


def test_require_independent_witnesses_fails_closed_without_disclosures():
    evidence = risk_evidence(
        evidence_kind=EVIDENCE_MODEL,
        model_only=False,
        consensus=False,
        witness_refs=("model-a", "model-b"),
        disclosure_refs=("disc-a", "disc-b"),
        dependency_status=DEPENDENCY_INDEPENDENT,
    )
    decision = evaluate_verification_risk_policy(
        evidence, policy(), as_of="2026-08-15"
    )
    assert decision.outcome == OUTCOME_ABSTAINED
    assert decision.reason_code == REASON_UNKNOWN_OR_CORRELATED
    assert decision.dependency_status == DEPENDENCY_UNKNOWN
    assert not decision.authority_granted


@pytest.mark.parametrize(
    ("evidence_changes", "policy_changes", "as_of", "outcome", "reason"),
    [
        ({"sample_count": 4}, {}, "2026-08-15", OUTCOME_UNCERTAIN, REASON_INSUFFICIENT),
        ({}, {}, "2026-10-01", OUTCOME_UNAVAILABLE, REASON_EXPIRED),
        ({"shift_status": SHIFT_SHIFTED}, {}, "2026-08-15", OUTCOME_ABSTAINED, REASON_SHIFT),
        ({"risk_upper_bound": "0.20"}, {}, "2026-08-15", OUTCOME_ABSTAINED, REASON_RISK_BOUND),
        ({"policy_revision": "rev-0"}, {}, "2026-08-15", OUTCOME_UNAVAILABLE, REASON_SCOPE),
    ],
)
def test_conservative_policy_outcomes(
    evidence_changes, policy_changes, as_of, outcome, reason
):
    decision = evaluate_verification_risk_policy(
        risk_evidence(**evidence_changes), policy(**policy_changes), as_of=as_of
    )
    assert decision.outcome == outcome
    assert decision.reason_code == reason
    assert not decision.authority_granted


def test_timestamp_validation_and_policy_history_are_versioned():
    with pytest.raises(KernelError, match="expires_at cannot precede"):
        risk_evidence(expires_at="2026-07-01")
    historical = risk_evidence()
    current_policy = replace(policy(), policy_revision="rev-2")
    decision = evaluate_verification_risk_policy(
        historical, current_policy, as_of="2026-08-15"
    )
    assert decision.reason_code == REASON_SCOPE
    assert historical.policy_revision == "rev-1"
