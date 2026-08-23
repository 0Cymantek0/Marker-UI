"""Two-region differential claim usability (PR88, invariant 22).

One document context (same workspace, same subject document, same
commit) carries independently assessed claim regions. The executable
property under test is the full negative statement:

> one unresolved region can neither silently poison nor silently
> promote its neighboring regions.

All scenarios run through the real commit boundary — the PR74 proof
graph and the PR75/PR88 high-risk risk gate included — and status is
read back through :mod:`app.kernel.assessment_view`, never by
inspecting in-memory objects only.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.kernel.assessment_view import (
    DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS,
)
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.models import KernelRecord as KernelRecordRow
from app.kernel.proofs import PROOF_ROLE_INPUT, PROOF_ROLE_WITNESS, ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    NativeFactRecord,
)
from app.kernel.replay import read_head
from app.kernel.verification_risk import (
    AUTHORITY_SOURCE_NATIVE,
    EVIDENCE_SOURCE_NATIVE,
    HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
    HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
    HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
    SHIFT_MATCHED,
    VerificationRiskEvidenceRecord,
)

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-pr88-regions"
SLICE = "invoice-total/en/matched/v1"


def make_risk(record_id: str = "risk-1") -> VerificationRiskEvidenceRecord:
    return VerificationRiskEvidenceRecord(
        record_id=record_id,
        policy_id=HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
        policy_revision=HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
        workflow_class=HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
        claim_authority_class=AUTHORITY_SOURCE_NATIVE,
        evaluation_slice_id=SLICE,
        sample_count=50,
        risk_upper_bound="0.04",
        risk_estimate="0.02",
        evaluated_at="2026-08-01T00:00:00Z",
        expires_at="2026-09-01T00:00:00Z",
        shift_status=SHIFT_MATCHED,
        evidence_kind=EVIDENCE_SOURCE_NATIVE,
        model_only=False,
        consensus=False,
        method_id="wilson-upper-bound",
        method_version="1.0.0",
        metadata={"calibration_population": "invoice-total/en/matched/v1"},
    )


def make_region(
    *,
    suffix: str,
    predicate: str,
    value: str,
    outcome: str,
    assessment_id: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    with_native_chain: bool = False,
    risk_ref: str = "risk-1",
    snapshot: int = 0,
    include_assertion: bool = True,
) -> list:
    """One claim region: assertion + assessment (+ witness chain).

    ``include_assertion=False`` reuses an already-committed assertion
    (same semantic content deduplicates to one kernel record).
    """
    assertion_id = f"assertion-{suffix}"
    assessment_id = assessment_id or f"assessment-{suffix}"
    records: list = []
    if include_assertion:
        records.append(
            ClaimAssertionRecord(
                record_id=assertion_id,
                claim_key=f"invoice.{predicate}",
                subject="doc:invoice-42",
                predicate=predicate,
                value=value,
            )
        )
    records.append(
        ClaimAssessmentRecord(
            record_id=assessment_id,
            assertion_ref=assertion_id,
            outcome=outcome,
            policy_id=HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
            policy_revision=HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
            evidence_refs=evidence_refs,
            snapshot_commit_id=snapshot,
            workflow_class=HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
            declared_context={
                "verification_risk": {
                    "evidence_ref": risk_ref,
                    "evaluation_slice_id": SLICE,
                    "as_of": "2026-08-15T00:00:00Z",
                }
            }
            if outcome == "verified"
            else {"region": suffix, "reason": "insufficient native evidence"},
        )
    )
    if with_native_chain:
        fact_id = f"native-fact-{suffix}"
        records.append(
            NativeFactRecord(
                record_id=fact_id,
                native_object_ref="doc:invoice-42",
                property_name=predicate,
                raw_representation=value,
                typed_interpretation=value,
                extractor_name="marker-native",
                extractor_version="1.0.0",
            )
        )
        records.append(
            ProofSupportRecord(
                record_id=f"support-risk-{suffix}",
                holder_ref=assessment_id,
                evidence_ref=risk_ref,
                role=PROOF_ROLE_INPUT,
                authority_rule="marker.high_risk.source_native/1:risk-v1",
            )
        )
        records.append(
            ProofSupportRecord(
                record_id=f"support-fact-{suffix}",
                holder_ref=assessment_id,
                evidence_ref=fact_id,
                role=PROOF_ROLE_WITNESS,
                authority_rule="marker.high_risk.source_native/1:witness-v1",
            )
        )
    return records


def first_commit() -> KernelCommitBatch:
    """Document with region A verified and region B unresolved."""
    return KernelCommitBatch(
        workspace_id=WORKSPACE,
        records=(
            make_risk(),
            *make_region(
                suffix="a",
                predicate="total_amount",
                value="1250.00",
                outcome="verified",
                evidence_refs=("risk-1", "native-fact-a"),
                with_native_chain=True,
            ),
            *make_region(
                suffix="b",
                predicate="tax_amount",
                value="100.00",
                outcome="uncertain",
            ),
        ),
    )


async def load_assessments(factory) -> list[tuple[int, ClaimAssessmentRecord]]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    KernelRecordRow.kernel_commit_id,
                    KernelRecordRow.id,
                    KernelRecordRow.payload_json,
                ).where(
                    KernelRecordRow.workspace_id == WORKSPACE,
                    KernelRecordRow.record_class == "claim_assessment",
                )
            )
        ).all()
    return [
        (
            commit_id,
            ClaimAssessmentRecord.from_payload(
                json.loads(payload), record_id=record_id
            ),
        )
        for commit_id, record_id, payload in rows
    ]


def resolve(carried_assessments, **overrides):
    from app.kernel.assessment_view import resolve_effective_assessments

    context = dict(
        policy_id=HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
        policy_revision=HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
        workflow_class=HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
        as_of_commit=1,
    )
    context.update(overrides)
    return resolve_effective_assessments(carried_assessments, **context)


# ---------------------------------------------------------------------------
# differential coexistence
# ---------------------------------------------------------------------------


async def test_verified_and_unresolved_regions_coexist_without_poisoning(
    kernel_env,
):
    receipt = await KernelCommitService(kernel_env).commit(first_commit())
    assert receipt.kernel_commit_id == 1
    assessments = await load_assessments(kernel_env)

    view = resolve(assessments)
    assert set(view) == {"assertion-a", "assertion-b"}

    # A stays usable: the unresolved neighbor did not poison it.
    region_a = view["assertion-a"]
    assert region_a.usability_class == "usable_authority"
    assert region_a.outcome == "verified"
    assert region_a.usable is True
    assert region_a.assessment_id == "assessment-a"
    assert region_a.snapshot_commit_id == 0
    assert region_a.evidence_refs == ("native-fact-a", "risk-1")  # stored order is canonical/sorted

    # B stays unresolved: the verified neighbor did not promote it.
    region_b = view["assertion-b"]
    assert region_b.usability_class == "unresolved_uncertain"
    assert region_b.outcome == "uncertain"
    assert region_b.usable is False
    assert region_b.evidence_refs == ()

    # The view states the as-of policy/snapshot context it resolved under.
    assert region_a.as_dict()["resolved_under"] == {
        "policy_id": HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
        "policy_revision": HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
        "workflow_class": HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
        "as_of_commit": 1,
    }


async def test_summary_preserves_region_states_without_boolean_collapse(
    kernel_env,
):
    from app.kernel.assessment_view import summarize_regions

    await KernelCommitService(kernel_env).commit(first_commit())
    view = resolve(await load_assessments(kernel_env))
    summary = summarize_regions(view)

    assert summary["document_state"] == DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS
    assert summary["region_count"] == 2
    assert summary["usable_regions"] == 1
    assert summary["unresolved_regions"] == 1
    assert summary["usability_counts"]["usable_authority"] == 1
    assert summary["usability_counts"]["unresolved_uncertain"] == 1
    # No document-wide verification boolean exists to misread.
    assert not any(
        key in summary for key in ("verified", "is_verified", "document_verified")
    )


async def test_bounded_read_for_one_region_does_not_leak_the_neighbor(kernel_env):
    await KernelCommitService(kernel_env).commit(first_commit())
    assessments = await load_assessments(kernel_env)

    # A bounded caller asking only for the unresolved region receives
    # exactly that region's uncertainty — not the neighbor's acceptance.
    only_b = resolve(assessments, assertion_refs=["assertion-b"])
    assert set(only_b) == {"assertion-b"}
    assert only_b["assertion-b"].usable is False
    assert only_b["assertion-b"].assessment_id == "assessment-b"
    assert "assertion-a" not in only_b

    only_a = resolve(assessments, assertion_refs=["assertion-a"])
    assert set(only_a) == {"assertion-a"}
    assert only_a["assertion-a"].usable is True

    # An unknown region resolves to the honest no-status state.
    ghost = resolve(assessments, assertion_refs=["assertion-ghost"])
    assert ghost["assertion-ghost"].usability_class == "unresolved_unavailable"
    assert ghost["assertion-ghost"].outcome is None
    assert ghost["assertion-ghost"].usable is False


# ---------------------------------------------------------------------------
# region resolution after new evidence (append-only history)
# ---------------------------------------------------------------------------


async def test_region_b_becomes_verified_while_region_a_history_is_stable(
    kernel_env,
):
    service = KernelCommitService(kernel_env)
    await service.commit(first_commit())

    # New evidence resolves B: a competent native fact for B plus the
    # already-committed risk evidence authorize a verified assessment.
    await service.commit(
        KernelCommitBatch(
            workspace_id=WORKSPACE,
            records=tuple(
                make_region(
                    suffix="b",
                    predicate="tax_amount",
                    value="100.00",
                    outcome="verified",
                    assessment_id="assessment-b-verified",
                    evidence_refs=("risk-1", "native-fact-b"),
                    with_native_chain=True,
                    snapshot=1,
                    include_assertion=False,
                )
            ),
        )
    )
    assert await read_head(kernel_env, WORKSPACE) == 2

    view = resolve(await load_assessments(kernel_env), as_of_commit=2)
    assert view["assertion-b"].usable is True
    assert view["assertion-b"].outcome == "verified"
    assert view["assertion-b"].assessment_id == "assessment-b-verified"

    # A's historical assessment is unchanged: same record id, same
    # evidence, same identity — new commits append, never rewrite.
    assessments = await load_assessments(kernel_env)
    region_a_records = [
        record for _commit, record in assessments if record.assertion_ref == "assertion-a"
    ]
    assert len(region_a_records) == 1
    assert region_a_records[0].record_id == "assessment-a"
    assert region_a_records[0].outcome == "verified"
    assert region_a_records[0].evidence_refs == ("native-fact-a", "risk-1")
    # B's earlier uncertain assessment remains historically readable.
    b_outcomes = sorted(
        record.outcome
        for _commit, record in assessments
        if record.assertion_ref == "assertion-b"
    )
    assert b_outcomes == ["uncertain", "verified"]


# ---------------------------------------------------------------------------
# policy / snapshot relativity
# ---------------------------------------------------------------------------


async def test_policy_revision_change_does_not_carry_old_assessments(kernel_env):
    await KernelCommitService(kernel_env).commit(first_commit())
    assessments = await load_assessments(kernel_env)

    stale = resolve(assessments, policy_revision="2")
    assert {e.usability_class for e in stale.values()} == {
        "unresolved_unavailable"
    }
    assert all(e.outcome is None for e in stale.values())

    other_workflow = resolve(assessments, workflow_class="standard.v1")
    assert all(
        e.usability_class == "unresolved_unavailable"
        for e in other_workflow.values()
    )


async def test_snapshot_cut_selects_historical_state_without_rewriting(kernel_env):
    service = KernelCommitService(kernel_env)
    await service.commit(first_commit())
    await service.commit(
        KernelCommitBatch(
            workspace_id=WORKSPACE,
            records=tuple(
                make_region(
                    suffix="b",
                    predicate="tax_amount",
                    value="100.00",
                    outcome="verified",
                    assessment_id="assessment-b-verified",
                    evidence_refs=("risk-1", "native-fact-b"),
                    with_native_chain=True,
                    snapshot=1,
                    include_assertion=False,
                )
            ),
        )
    )
    assessments = await load_assessments(kernel_env)

    # As-of the first cut, B is still uncertain — the later commit's
    # assessment (computed against cut 1 but carried by commit 2) does
    # not retroactively change what cut 1 knew.
    before = resolve(assessments, as_of_commit=1)
    assert before["assertion-b"].outcome == "uncertain"
    assert before["assertion-b"].usable is False

    after = resolve(assessments, as_of_commit=2)
    assert after["assertion-b"].outcome == "verified"


async def test_failed_region_does_not_remove_verified_neighbor(kernel_env):
    from app.kernel.assessment_view import summarize_regions

    batch = first_commit()
    records = list(batch.records) + list(
        make_region(
            suffix="c",
            predicate="currency",
            value="EUR",
            outcome="failed",
        )
    )
    await KernelCommitService(kernel_env).commit(
        KernelCommitBatch(workspace_id=WORKSPACE, records=tuple(records))
    )
    view = resolve(await load_assessments(kernel_env))
    assert view["assertion-a"].usable is True
    assert view["assertion-b"].usability_class == "unresolved_uncertain"
    assert view["assertion-c"].usability_class == "unresolved_failed"
    summary = summarize_regions(view)
    assert summary["usable_regions"] == 1
    assert summary["usability_counts"]["unresolved_failed"] == 1
    assert summary["document_state"] == DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS


async def test_warning_cleared_region_is_usable_and_distinct(kernel_env):
    from app.kernel.assessment_view import summarize_regions

    batch = first_commit()
    records = list(batch.records) + list(
        make_region(
            suffix="d",
            predicate="po_number",
            value="PO-77",
            outcome="accepted_with_warning",
        )
    )
    await KernelCommitService(kernel_env).commit(
        KernelCommitBatch(workspace_id=WORKSPACE, records=tuple(records))
    )
    view = resolve(await load_assessments(kernel_env))
    region_d = view["assertion-d"]
    assert region_d.usability_class == "usable_with_warning"
    assert region_d.usable is True
    # Distinct from authority-bearing usability.
    assert region_d.usability_class != view["assertion-a"].usability_class
    summary = summarize_regions(view)
    assert summary["usability_counts"]["usable_with_warning"] == 1
