"""Tests for capability matrix and accountability contracts (Invariant 59)."""

from __future__ import annotations

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
        support_owner="domain:kernel_transaction_authority",
        rollback=RollbackPath(
            mechanism="transaction_retry",
            procedure="Whole-operation retry on classified contention with typed exhaustion handling and zero partial state acceptance.",
            verified=True,
            verification_node="backend/tests/test_kernel_dialects.py::test_contention_budget_retries_whole_operation_then_converges",
            verification_evidence="docs/reference/measurements/pr83a-kernel-parity.json",
            verification_sha256=SAMPLE_SHA,
        ),
        expiry=ExpiryBoundary(
            evaluated_at="2026-08-20T00:00:00Z",
            retest_deadline="2027-08-20T00:00:00Z",
            triggers=("runtime_or_dependency_change", "drift_or_distribution_shift"),
        ),
        kill_condition=KillCondition(
            trigger_expression="unresolved_corruption_rate > 0.0001",
            evaluation_metric="data_corruption_rate",
            threshold=0.0001,
            action="fail_closed_and_disable",
            triggered=False,
        ),
        utility_basis=CapabilityUtilityBasis(
            evidence_artifact="docs/reference/measurements/pr83a-kernel-parity.json",
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


def test_promoted_rollback_requires_verification_binding():
    rec_dict = _valid_promoted_record().to_dict()
    del rec_dict["rollback"]["verification_node"]
    del rec_dict["rollback"]["verification_evidence"]
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("requires verification_node or verification_evidence" in err for err in errors)


def test_fictional_environment_flags_rejected_in_rollback():
    fictional_flags = (
        "MARKER_KERNEL_MODE=0",
        "ANCHOR_MAPPING_CASCADE_MODE=off",
        "REBUILD_FORCE_CLEAN=true",
        "RETRIEVAL_FALLBACK_TO_UNPAGED=1",
        "OPERATIONAL_AS_OF_SURFACE=disabled",
        "VISUAL_RERANK_ENABLED=0",
        "SPECIALIST_BRIDGE_ENABLED=false",
    )
    for flag_expr in fictional_flags:
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["rollback"]["procedure"] = f"Set {flag_expr} and restart service"
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("references fictional/nonexistent environment variable or flag" in err for err in errors)


def test_unsafe_fallback_language_rejected_in_rollback():
    unsafe_phrases = (
        "Fallback to unbounded context query",
        "Revert to snapshot-unpinned review stream",
        "Temporarily disable_security checks",
        "Set policy to ignore_acl for performance",
        "Permit permissive_disclosure on error",
    )
    for unsafe_phrase in unsafe_phrases:
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["rollback"]["procedure"] = unsafe_phrase
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("cannot use unsafe fallback language" in err for err in errors)


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
    # boolean rejected
    rec_dict1 = _valid_promoted_record().to_dict()
    rec_dict1["kill_condition"]["threshold"] = True
    errs1 = validate_capability_record(rec_dict1, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill_condition.threshold cannot be a boolean" in e for e in errs1)

    # nan / inf rejected
    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["kill_condition"]["threshold"] = float("nan")
    errs2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill_condition.threshold must be a finite number" in e for e in errs2)

    # empty string rejected
    rec_dict3 = _valid_promoted_record().to_dict()
    rec_dict3["kill_condition"]["threshold"] = "   "
    errs3 = validate_capability_record(rec_dict3, as_of_date="2026-08-26T00:00:00Z")
    assert any("kill_condition.threshold string cannot be empty" in e for e in errs3)


def test_evaluated_at_after_retest_deadline_fails():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["expiry"]["evaluated_at"] = "2027-09-01T00:00:00Z"
    rec_dict["expiry"]["retest_deadline"] = "2027-08-20T00:00:00Z"
    errors = validate_capability_record(rec_dict, as_of_date="2028-01-01T00:00:00Z")
    assert any("must be <= retest_deadline" in err for err in errors)


def test_future_evaluated_at_fails_relative_to_as_of():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["expiry"]["evaluated_at"] = "2026-09-01T00:00:00Z"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("expiry.evaluated_at is in the future relative to as_of_date" in err for err in errors)


def test_reject_unknown_fields_in_record_and_nested():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["unknown_top_field"] = "bad"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("unknown field 'unknown_top_field' in capability record" in err for err in errors)

    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["rollback"]["unknown_rb_field"] = "bad"
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("unknown field 'unknown_rb_field' in rollback object" in err for err in errors2)


def test_promoted_requires_complexity_adjusted_conclusion_and_operational_burden():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["utility_basis"]["complexity_adjusted_conclusion"] = "invalid_conclusion"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("utility_basis.complexity_adjusted_conclusion must be one of" in err for err in errors)

    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["utility_basis"]["operational_burden_status"] = "unavailable"
    rec_dict2["utility_basis"].pop("operational_burden_reason", None)
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("operational_burden_status 'unavailable' requires non-empty reason" in err for err in errors2)


def test_quality_gain_finite_and_cost_delta_nonempty():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["utility_basis"]["quality_gain"] = float("inf")
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("quality_gain must be a finite float" in err for err in errors)

    rec_dict2 = _valid_promoted_record().to_dict()
    rec_dict2["utility_basis"]["operational_cost_delta"] = {}
    errors2 = validate_capability_record(rec_dict2, as_of_date="2026-08-26T00:00:00Z")
    assert any("operational_cost_delta must be a non-empty mapping" in err for err in errors2)


def test_unresolved_limits_typed_and_nonempty_for_active_records():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["unresolved_limits"] = []
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("promoted capability cannot declare empty unresolved_limits" in err for err in errors)


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
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["expiry"]["retest_deadline"] = "2026-01-01T00:00:00Z"
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("is expired (retest deadline" in err for err in errors)


def test_generic_rollback_placeholder_rejected():
    for placeholder in ("revert commit", "git revert", "rollback", "tbd", "none", "n/a"):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["rollback"]["procedure"] = placeholder
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("rollback.procedure cannot be generic placeholder" in err for err in errors)


def test_generic_kill_condition_placeholder_rejected():
    for placeholder in ("disable if bad", "if errors occur", "remove later", "kill if needed", "tbd", "none", "n/a"):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["kill_condition"]["trigger_expression"] = placeholder
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("kill_condition.trigger_expression cannot be placeholder" in err for err in errors)


def test_non_promoted_and_experimental_dispositions_require_evidence():
    for disp in (DISPOSITION_NON_PROMOTED, DISPOSITION_EXPERIMENTAL_SHADOW):
        rec_dict = _valid_promoted_record().to_dict()
        rec_dict["disposition"] = disp
        del rec_dict["utility_basis"]
        errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any(f"{disp} capability 'kernel.commit_authority' requires utility_basis" in err for err in errors)


def test_disabled_disposition_requires_full_utility_or_explicit_rationale():
    rec_dict = _valid_promoted_record().to_dict()
    rec_dict["disposition"] = DISPOSITION_DISABLED
    del rec_dict["utility_basis"]
    errors = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert any("disabled capability without utility_basis requires non-empty disabled_rationale" in err for err in errors)

    rec_dict["disabled_rationale"] = "Decommissioned permanently per PR87C storage amplification evidence"
    errors2 = validate_capability_record(rec_dict, as_of_date="2026-08-26T00:00:00Z")
    assert errors2 == []
