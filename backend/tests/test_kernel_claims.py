"""PR74 claim identity contract tests (V3.2 §4.1, §9.1).

Pure contract level — deterministic semantic identity for
ClaimAssertion/ClaimAssessment, append-only assessment history, typed
context binding, and fail-closed rematerialization of stored payloads
(including the PR63 legacy shape).
"""

from __future__ import annotations

import pytest

from app.kernel.errors import KernelError
from app.kernel.records import (
    AUTHORITY_BEARING_OUTCOMES,
    CLAIM_ASSESSMENT_OUTCOMES,
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
)
from app.utils.canonical import record_identity_hash, to_json_ready


def identity_of(record: ClaimAssertionRecord | ClaimAssessmentRecord) -> str:
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


def make_assertion(**overrides) -> ClaimAssertionRecord:
    params = dict(
        record_id="assertion-1",
        claim_key="invoice-42.total",
        subject="doc:invoice-42",
        predicate="total_amount",
        value="1250.00",
        qualifiers={"currency": "USD"},
    )
    params.update(overrides)
    return ClaimAssertionRecord(**params)


def make_assessment(**overrides) -> ClaimAssessmentRecord:
    params = dict(
        record_id="assessment-1",
        assertion_ref="assertion-1",
        outcome="verified",
        policy_id="policy.default",
        policy_revision="rev-3",
        evidence_refs=("observation-1",),
        snapshot_commit_id=2,
        workflow_class="standard.v1",
    )
    params.update(overrides)
    return ClaimAssessmentRecord(**params)


# ---------------------------------------------------------------------------
# assertion identity
# ---------------------------------------------------------------------------


def test_assertion_identity_ignores_record_id_and_qualifier_order():
    a = make_assertion()
    b = make_assertion(
        record_id="assertion-2",
        qualifiers={"currency": "USD"},
    )
    assert identity_of(a) == identity_of(b)


def test_assertion_identity_includes_claim_key_by_design():
    """Documented PR74 decision: claim_key scopes the claim's referent.

    A different key is a different claim even when the subject/
    predicate/value triple coincides — renaming mints a new claim."""
    a = make_assertion()
    b = make_assertion(claim_key="invoice-42.grand-total")
    assert identity_of(a) != identity_of(b)


def test_assertion_identity_separates_meaning_changes():
    base = make_assertion()
    assert identity_of(base) != identity_of(make_assertion(value="1251.00"))
    assert identity_of(base) != identity_of(make_assertion(subject="doc:invoice-43"))
    assert identity_of(base) != identity_of(make_assertion(predicate="subtotal"))
    assert identity_of(base) != identity_of(
        make_assertion(qualifiers={"currency": "EUR"})
    )


def test_assertion_identity_preserves_raw_unicode():
    """Raw Unicode is never normalized (PR61 contract): a combining-
    sequence value and its precomposed twin stay distinct claims."""
    decomposed = make_assertion(value="cafe\u0301")
    precomposed = make_assertion(value="caf\u00e9")
    assert decomposed.value != precomposed.value
    assert identity_of(decomposed) != identity_of(precomposed)


def test_assertion_construction_rejects_blank_semantic_fields():
    with pytest.raises(KernelError, match="claim_key"):
        make_assertion(claim_key="")
    with pytest.raises(KernelError, match="subject"):
        make_assertion(subject="")
    with pytest.raises(KernelError, match="predicate"):
        make_assertion(predicate="")


def test_assertion_from_payload_round_trip_and_fail_closed():
    assertion = make_assertion()
    payload = assertion.identity_payload()
    remat = ClaimAssertionRecord.from_payload(payload, record_id="assertion-9")
    assert identity_of(remat) == identity_of(assertion)
    with pytest.raises(KernelError, match="unknown assertion payload fields"):
        ClaimAssertionRecord.from_payload(
            {**payload, "extra": 1}, record_id="assertion-9"
        )
    with pytest.raises(KernelError, match="missing"):
        ClaimAssertionRecord.from_payload(
            {k: v for k, v in payload.items() if k != "subject"},
            record_id="assertion-9",
        )


# ---------------------------------------------------------------------------
# assessment identity & context binding
# ---------------------------------------------------------------------------


def test_assessment_identity_ignores_record_id_and_evidence_order():
    a = make_assessment(evidence_refs=("observation-1", "observation-2"))
    b = make_assessment(
        record_id="assessment-2",
        evidence_refs=("observation-2", "observation-1"),
    )
    assert identity_of(a) == identity_of(b)


def test_evidence_change_does_not_mutate_assertion():
    """Different evidence produces a different assessment of the SAME
    assertion — the assertion's identity is untouched."""
    assertion = make_assertion()
    a = make_assessment()
    b = make_assessment(evidence_refs=("observation-1", "observation-2"))
    assert identity_of(a) != identity_of(b)
    assert identity_of(assertion) == identity_of(make_assertion())


def test_policy_revision_change_yields_distinct_assessment_identity():
    a = make_assessment()
    b = make_assessment(policy_revision="rev-4")
    assert identity_of(a) != identity_of(b)


def test_snapshot_and_workflow_changes_yield_distinct_assessment_identity():
    base = make_assessment(snapshot_commit_id=2, workflow_class="standard.v1")
    assert identity_of(base) != identity_of(make_assessment(snapshot_commit_id=3))
    assert identity_of(base) != identity_of(make_assessment(workflow_class="fast.v1"))


def test_assessment_identity_ignores_declared_context_key_order():
    a = make_assessment(declared_context={"x": 1, "y": 2})
    b = make_assessment(declared_context={"y": 2, "x": 1})
    assert identity_of(a) == identity_of(b)


def test_outcome_vocabulary_is_the_versioned_contract():
    assert CLAIM_ASSESSMENT_OUTCOMES == {
        "source_exact",
        "verified",
        "accepted_with_warning",
        "uncertain",
        "unavailable",
        "abstained",
        "failed",
    }
    assert AUTHORITY_BEARING_OUTCOMES == {"source_exact", "verified"}


def test_assessment_construction_validates_typed_context():
    with pytest.raises(KernelError, match="outcome"):
        make_assessment(outcome="")
    with pytest.raises(KernelError, match="policy_id"):
        make_assessment(policy_id="")
    with pytest.raises(KernelError, match="snapshot_commit_id"):
        make_assessment(snapshot_commit_id=-1)
    with pytest.raises(KernelError, match="snapshot_commit_id"):
        make_assessment(snapshot_commit_id=True)
    with pytest.raises(KernelError, match="workflow_class"):
        make_assessment(workflow_class=7)
    with pytest.raises(KernelError, match="evidence_ref"):
        make_assessment(evidence_refs=("bad ref!",))
    with pytest.raises(KernelError, match="assertion_ref"):
        make_assessment(assertion_ref="also bad")


def test_assessment_from_payload_remateralizes_legacy_pr63_shape():
    """Stored PR63 payloads predate snapshot/workflow fields: they
    rematerialize with honest defaults, unknown fields fail closed."""
    legacy_payload = {
        "assertion_ref": "assertion-1",
        "outcome": "supported",
        "policy": {"policy_id": "policy.default", "revision": "rev-1"},
        "evidence_refs": ["observation-1"],
        "declared_context": {"as_of": "commit-local"},
    }
    remat = ClaimAssessmentRecord.from_payload(
        legacy_payload, record_id="assessment-legacy"
    )
    assert remat.snapshot_commit_id == 0
    assert remat.workflow_class == ""
    assert remat.outcome == "supported"  # historical outcomes stay readable
    assert remat.evidence_refs == ("observation-1",)


def test_assessment_from_payload_round_trip_current_shape():
    assessment = make_assessment()
    payload = assessment.identity_payload()
    remat = ClaimAssessmentRecord.from_payload(payload, record_id="assessment-9")
    assert identity_of(remat) == identity_of(assessment)
    with pytest.raises(KernelError, match="unknown assessment payload fields"):
        ClaimAssessmentRecord.from_payload(
            {**payload, "extra": 1}, record_id="assessment-9"
        )
    broken = {k: v for k, v in payload.items() if k != "policy"}
    with pytest.raises(KernelError, match="missing"):
        ClaimAssessmentRecord.from_payload(broken, record_id="assessment-9")
