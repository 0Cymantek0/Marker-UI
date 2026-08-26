"""Tests for capability matrix and accountability contracts (Invariant 59)."""

from __future__ import annotations

import math
from app.eval.accountability.capability_matrix import (
    CAPABILITY_MATRIX_SCHEMA_VERSION,
    DISPOSITION_DISABLED,
    DISPOSITION_EXPERIMENTAL_SHADOW,
    DISPOSITION_NON_PROMOTED,
    DISPOSITION_PROMOTED,
    EVIDENCE_CURRENT,
    EVIDENCE_STALE,
    EVIDENCE_SUPERSEDED,
    CapabilityRecord,
    CapabilityUtilityBasis,
    ExpiryBoundary,
    KillCondition,
    RollbackPath,
    validate_capability_record,
)

SAMPLE_SHA = "a" * 64


def _valid_promoted_record() -> CapabilityRecord:
    return CapabilityRecord(
        id="kernel.commit_authority",
        name="Transactional Commit Authority",
        category="core_kernel",
        disposition=DISPOSITION_PROMOTED,
        support_owner="core_kernel_team",
        rollback=RollbackPath(
            mechanism="feature_flag",
            procedure="Set KERNEL_ENABLE_TRANSACTIONAL_AUTHORITY=0 and restart worker",
            verified=True,
        ),
        expiry=ExpiryBoundary(
            evaluated_at="2026-08-20T00:00:00Z",
            retest_deadline="2027-08-20T00:00:00Z",
            triggers=("time_expiry", "policy_revision_change"),
        ),
        kill_condition=KillCondition(
            trigger_expression="unresolved_corruption_rate > 0.0001",
            evaluation_metric="data_corruption_rate",
            threshold=0.0001,
            action="fail_closed_and_disable",
            triggered=False,
        ),
        utility_basis=CapabilityUtilityBasis(
            evidence_artifact="docs/reference/measurements/pr84a-evidence-run.json",
            evidence_sha256=SAMPLE_SHA,
            lifecycle=EVIDENCE_CURRENT,
            complexity_adjusted_conclusion="promoted_complexity_justified",
            operational_burden_status="measured",
            quality_gain=1.0,
            operational_cost_delta={"latency_ms": 1.2, "storage_bytes": 0},
            justification_summary="Guarantees atomic commit and zero partial state across crashes",
        ),
        unresolved_limits=("SQLite dev topology only; PostgreSQL failover tested separately",),
    )


def test_valid_promoted_record_passes():
    record = _valid_promoted_record()
    errors = validate_capability_record(record, as_of_date="2026-08-26T00:00:00Z")
    assert errors == []


def test_mapping_input_requires_exact_schema_version():
    rec_dict = _valid_promoted_record().to_dict()
    del rec_dict["schema_version"]
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("schema_version must be" in err for err in errors)

    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["schema_version"] = "wrong.version.v99"
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any(f"schema_version must be {CAPABILITY_MATRIX_SCHEMA_VERSION!r}" in err for err in errors2)


def test_promoted_rollback_must_be_verified():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["rollback"]["verified"] = False
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("requires verified rollback path" in err for err in errors)


def test_promoted_kill_condition_triggered_fails():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["kill_condition"]["triggered"] = True
    rec_dict["kill_condition"]["trigger_reason"] = "Corruption threshold breached in telemetry"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill condition is triggered" in err for err in errors)


def test_kill_condition_invalid_action_rejected():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["kill_condition"]["action"] = "invalid_custom_action"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill_condition.action must be one of" in err for err in errors)


def test_kill_threshold_finite_numeric_or_nonempty_string():
    # Valid explicit string threshold
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["kill_condition"]["threshold"] = "loss_of_quorum"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert errors == []

    # Invalid: boolean
    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["kill_condition"]["threshold"] = True
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill_condition.threshold cannot be a boolean" in err for err in errors2)

    # Invalid: NaN or inf
    for bad_num in (math.nan, math.inf, -math.inf):
        rec_dict3 = _valid_promoted_record().to_dict()
        rec_dict3["kill_condition"]["threshold"] = bad_num
        errors3 = validate_capability_record(rec_dict3, as_of_date="2026-08-26T00:00:00Z")
        assert any("kill_condition.threshold must be a finite number" in err for err in errors3)

    # Invalid: container/empty string
    rec_dict4 = _valid_promoted_record().to_dict()
    rec_dict4["kill_condition"]["threshold"] = ""
    errors4 = validate_capability_record(rec_dict4, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill_condition.threshold string cannot be empty" in err for err in errors4)


def test_evaluated_at_after_retest_deadline_fails():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["expiry"]["evaluated_at"] = "2027-01-01T00:00:00Z"
    rec_dict["expiry"]["retest_deadline"] = "2026-01-01T00:00:00Z"
    errors = validate_capability_record(rec_dict, as_of_date="2027-01-01T00:00:00Z")
    assert any("must be <= retest_deadline" in err for err in errors)


def test_future_evaluated_at_fails_relative_to_as_of():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["expiry"]["evaluated_at"] = "2028-01-01T00:00:00Z"
    rec_dict["expiry"]["retest_deadline"] = "2029-01-01T00:00:00Z"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("evaluated_at is in the future relative to as_of_date" in err for err in errors)


def test_reject_unknown_fields_in_record_and_nested():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["unknown_top_level"] = "bad"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("unknown field 'unknown_top_level'" in err for err in errors)


def test_promoted_requires_complexity_adjusted_conclusion_and_operational_burden():
    rec_dict = _valid_promoted_record().to_dict()
    del rec_dict["utility_basis"]["complexity_adjusted_conclusion"]
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("complexity_adjusted_conclusion must be one of" in err for err in errors)

    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["utility_basis"]["operational_burden_status"] = "unavailable"
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("operational_burden_status 'unavailable' requires non-empty reason" in err for err in errors2)


def test_quality_gain_finite_and_cost_delta_nonempty():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["utility_basis"]["quality_gain"] = math.nan
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("quality_gain must be a finite float" in err for err in errors)

    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["utility_basis"]["operational_cost_delta"] = {}
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("operational_cost_delta must be a non-empty mapping" in err for err in errors2)


def test_unresolved_limits_typed_and_nonempty_for_active_records():
    for disp in (DISPOSITION_PROMOTED, DISPOSITION_EXPERIMENTAL_SHADOW, DISPOSITION_NON_PROMOTED):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["disposition"] = disp
        rec_dict["unresolved_limits"] = []
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any(f"{disp} capability cannot declare empty unresolved_limits" in err for err in errors)

        rec_dict2 = _valid_promoted_record().to_dict()
        rec_dict2["disposition"] = disp
        rec_dict2["unresolved_limits"] = [""]
        errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
        assert any("unresolved limit entry must be a non-empty string" in err for err in errors2)


def test_promoted_record_requires_utility_basis():
    rec_dict = _valid_promoted_record().to_dict()
    del rec_dict["utility_basis"]
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("requires utility_basis" in err for err in errors)


def test_promoted_record_cannot_rest_on_stale_or_superseded_evidence():
    for lc in (EVIDENCE_STALE, EVIDENCE_SUPERSEDED):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["utility_basis"]["lifecycle"] = lc
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any(f"cannot rest on {lc} evidence" in err for err in errors)


def test_promoted_record_expired_deadline_fails():
    rec = _valid_promoted_record()
    errors = validate_capability_record(rec, as_of_date="2028-01-01T00:00:00Z")
    assert any("is expired" in err for err in errors)


def test_generic_rollback_placeholder_rejected():
    for generic in ("revert commit", "git revert", "tbd", "none"):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["rollback"]["procedure"] = generic
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("cannot be generic placeholder" in err for err in errors)


def test_generic_kill_condition_placeholder_rejected():
    for generic in ("disable if bad", "if errors occur", "tbd", "none"):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["kill_condition"]["trigger_expression"] = generic
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("cannot be placeholder" in err for err in errors)


def test_non_promoted_and_experimental_dispositions_require_evidence():
    for disp in (DISPOSITION_NON_PROMOTED, DISPOSITION_EXPERIMENTAL_SHADOW):
        rec = CapabilityRecord(
            id=f"test.{disp}",
            name=f"Test {disp}",
            category="experimental",
            disposition=disp,
            support_owner="eval_domain",
            rollback=RollbackPath(
                mechanism="config",
                procedure="Disable experimental config flag EVAL_EXP_01",
                verified=False,
            ),
            expiry=ExpiryBoundary(
                evaluated_at="2026-08-01T00:00:00Z",
                retest_deadline="2027-08-01T00:00:00Z",
                triggers=("model_or_operator_change",),
            ),
            kill_condition=KillCondition(
                trigger_expression="experimental_error_rate > 0.05",
                evaluation_metric="error_rate",
                threshold=0.05,
                action="disable_route",
            ),
            utility_basis=CapabilityUtilityBasis(
                evidence_artifact="docs/reference/measurements/pr82-quality-lab.json",
                evidence_sha256=SAMPLE_SHA,
                lifecycle=EVIDENCE_CURRENT,
                complexity_adjusted_conclusion="non_promoted_research_accepted",
                operational_burden_status="not_applicable",
                operational_burden_reason="Shadow experimental execution only",
                justification_summary=f"Evidence justifying {disp} disposition",
            ),
            unresolved_limits=("Non-promoted research slice",),
        )
        errors = validate_capability_record(rec, as_of_date="2026-08-26T00:00:00Z")
        assert errors == []


def test_disabled_disposition_requires_full_utility_or_explicit_rationale():
    # 1. Disabled with full utility_basis
    rec1 = CapabilityRecord(
        id="test.disabled_feature",
        name="Disabled Dense Rerank",
        category="retrieval",
        disposition=DISPOSITION_DISABLED,
        support_owner="retrieval_domain",
        rollback=RollbackPath(
            mechanism="feature_flag",
            procedure="Flag already disabled",
            verified=True,
        ),
        expiry=ExpiryBoundary(
            evaluated_at="2026-08-01T00:00:00Z",
            retest_deadline="2027-08-01T00:00:00Z",
            triggers=("time_expiry",),
        ),
        kill_condition=KillCondition(
            trigger_expression="disabled_permanently",
            evaluation_metric="operational_status",
            threshold="disabled",
            action="fail_closed_and_disable",
        ),
        utility_basis=CapabilityUtilityBasis(
            evidence_artifact="docs/reference/measurements/pr81a-visual-retrieval.json",
            evidence_sha256=SAMPLE_SHA,
            lifecycle=EVIDENCE_CURRENT,
            complexity_adjusted_conclusion="decommissioned_or_disabled",
            operational_burden_status="not_applicable",
            operational_burden_reason="Feature completely disabled in configuration",
            justification_summary="No active utility claim; feature disabled per PR81A economics",
        ),
        unresolved_limits=("Decommissioned route",),
    )
    errors1 = validate_capability_record(rec1, as_of_date="2026-08-26T00:00:00Z")
    assert errors1 == []

    # 2. Disabled without utility_basis but with explicit disabled_rationale
    rec2 = CapabilityRecord(
        id="test.disabled_no_utility",
        name="Disabled Deprecated Route",
        category="retrieval",
        disposition=DISPOSITION_DISABLED,
        support_owner="retrieval_domain",
        rollback=RollbackPath(
            mechanism="feature_flag",
            procedure="Flag disabled",
            verified=True,
        ),
        expiry=ExpiryBoundary(
            evaluated_at="2026-08-01T00:00:00Z",
            retest_deadline="2027-08-01T00:00:00Z",
            triggers=("time_expiry",),
        ),
        kill_condition=KillCondition(
            trigger_expression="disabled_permanently",
            evaluation_metric="operational_status",
            threshold="disabled",
            action="fail_closed_and_disable",
        ),
        utility_basis=None,
        disabled_rationale="Explicitly decommissioned in favor of sparse retrieval route",
        unresolved_limits=(),
    )
    errors2 = validate_capability_record(rec2, as_of_date="2026-08-26T00:00:00Z")
    assert errors2 == []

    # 3. Disabled without utility_basis and without disabled_rationale -> Fails
    rec_dict3 = rec2.to_dict()
    del rec_dict3["disabled_rationale"]
    errors3 = validate_capability_record(rec_dict3, as_of_date="2026-08-26T00:00:00Z")
    assert any("disabled capability without utility_basis requires non-empty disabled_rationale" in err for err in errors3)
