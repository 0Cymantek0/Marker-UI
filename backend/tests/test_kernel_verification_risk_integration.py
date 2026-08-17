"""PR75 commit-boundary integration with PR74 claim/proof authority."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.kernel.commit import (
    FAULT_PHASES,
    KernelCommitBatch,
    KernelCommitService,
    PHASE_RISK_CHECKED,
)
from app.kernel.errors import (
    InjectedFaultError,
    ProofInputIntegrityError,
    VerificationRiskGateError,
)
from app.kernel.models import KernelCommitManifest, KernelRecord, KernelRecordEdge
from app.kernel.proofs import PROOF_ROLE_INPUT, PROOF_ROLE_WITNESS, ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    NativeFactRecord,
)
from app.kernel.replay import read_head, replay, verify_history
from app.kernel.verification_risk import (
    AUTHORITY_SOURCE_NATIVE,
    EVIDENCE_MODEL,
    EVIDENCE_SOURCE_NATIVE,
    HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
    HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
    HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
    SHIFT_MATCHED,
    SHIFT_SHIFTED,
    VerificationRiskEvidenceRecord,
)
from app.utils.canonical import record_identity_hash, to_json_ready

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-pr75-risk"
SLICE = "invoice-total/en/matched/v1"


def make_risk(**changes) -> VerificationRiskEvidenceRecord:
    values = {
        "record_id": "risk-1",
        "policy_id": HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
        "policy_revision": HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
        "workflow_class": HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
        "claim_authority_class": AUTHORITY_SOURCE_NATIVE,
        "evaluation_slice_id": SLICE,
        "sample_count": 50,
        "risk_upper_bound": "0.04",
        "risk_estimate": "0.02",
        "evaluated_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-09-01T00:00:00Z",
        "shift_status": SHIFT_MATCHED,
        "evidence_kind": EVIDENCE_SOURCE_NATIVE,
        "model_only": False,
        "consensus": False,
        "method_id": "wilson-upper-bound",
        "method_version": "1.0.0",
    }
    values.update(changes)
    return VerificationRiskEvidenceRecord(**values)


def make_fact(**changes) -> NativeFactRecord:
    values = {
        "record_id": "native-fact-1",
        "native_object_ref": "doc:invoice-42",
        "property_name": "total_amount",
        "raw_representation": "1250.00",
        "typed_interpretation": "1250.00",
        "extractor_name": "marker-native",
        "extractor_version": "1.0.0",
    }
    values.update(changes)
    return NativeFactRecord(**values)


def make_assessment(
    *,
    risk_ref: str = "risk-1",
    evidence_refs: tuple[str, ...] = ("risk-1", "native-fact-1"),
    declared_context: dict | None = None,
    **changes,
) -> ClaimAssessmentRecord:
    values = {
        "record_id": "assessment-1",
        "assertion_ref": "assertion-1",
        "outcome": "verified",
        "policy_id": HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
        "policy_revision": HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
        "evidence_refs": evidence_refs,
        "snapshot_commit_id": 0,
        "workflow_class": HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
        "declared_context": declared_context
        if declared_context is not None
        else {
            "verification_risk": {
                "evidence_ref": risk_ref,
                "evaluation_slice_id": SLICE,
                "as_of": "2026-08-15T00:00:00Z",
            }
        },
    }
    values.update(changes)
    return ClaimAssessmentRecord(**values)


def make_support(
    record_id: str, evidence_ref: str, *, role: str = PROOF_ROLE_WITNESS
) -> ProofSupportRecord:
    return ProofSupportRecord(
        record_id=record_id,
        holder_ref="assessment-1",
        evidence_ref=evidence_ref,
        role=role,
        authority_rule=f"{HIGH_RISK_SOURCE_NATIVE_POLICY_ID}/1:witness-v1",
    )


def valid_batch(
    *,
    risk: VerificationRiskEvidenceRecord | None = None,
    fact: NativeFactRecord | None = None,
    assertion: ClaimAssertionRecord | None = None,
    **changes,
):
    risk = risk or make_risk()
    assessment = make_assessment(**changes)
    return KernelCommitBatch(
        workspace_id=WORKSPACE,
        records=(
            assertion
            or ClaimAssertionRecord(
                record_id="assertion-1",
                claim_key="invoice.total",
                subject="doc:invoice-42",
                predicate="total_amount",
                value="1250.00",
            ),
            risk,
            fact or make_fact(),
            assessment,
            make_support("support-risk", risk.record_id, role=PROOF_ROLE_INPUT),
            make_support("support-fact", "native-fact-1"),
        ),
    )


async def row_count(factory, model) -> int:
    async with factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


async def assert_rejected_atomically(factory, batch, *, match: str | None = None):
    service = KernelCommitService(factory)
    with pytest.raises(VerificationRiskGateError, match=match):
        await service.commit(batch)
    assert await row_count(factory, KernelRecord) == 0
    assert await row_count(factory, KernelRecordEdge) == 0
    assert await row_count(factory, KernelCommitManifest) == 0
    assert await read_head(factory, WORKSPACE) == 0


async def test_valid_high_risk_source_native_commit(kernel_env):
    receipt = await KernelCommitService(kernel_env).commit(valid_batch())
    assert receipt.kernel_commit_id == 1
    assert receipt.record_count == 6
    assert await read_head(kernel_env, WORKSPACE) == 1


@pytest.mark.parametrize(
    "fact_changes",
    [
        {"native_object_ref": "doc:other-42"},
        {"property_name": "tax_amount"},
        {"typed_interpretation": "12.50"},
    ],
)
async def test_claim_relative_native_fact_mismatch_is_rejected_atomically(
    kernel_env, fact_changes
):
    await assert_rejected_atomically(
        kernel_env,
        valid_batch(fact=make_fact(**fact_changes)),
        match="not competent for claim",
    )


async def test_unrelated_native_fact_cannot_authorize_high_risk_claim_atomically(
    kernel_env,
):
    batch = valid_batch(fact=make_fact(native_object_ref="doc:other-42"))
    await assert_rejected_atomically(
        kernel_env, batch, match="native_fact.*not competent for claim"
    )


async def test_qualified_assertion_cannot_use_native_authority_atomically(
    kernel_env,
):
    qualified = ClaimAssertionRecord(
        record_id="assertion-1",
        claim_key="invoice.total",
        subject="doc:invoice-42",
        predicate="total_amount",
        value="1250.00",
        qualifiers={"page": 2},
    )
    await assert_rejected_atomically(
        kernel_env,
        valid_batch(assertion=qualified),
        match="no anchor-to-qualifier binding",
    )


async def test_native_binding_stays_outside_standard_workflow(kernel_env):
    receipt = await KernelCommitService(kernel_env).commit(
        valid_batch(
            fact=make_fact(native_object_ref="doc:other-42"),
            workflow_class="standard.v1",
        )
    )
    assert receipt.kernel_commit_id == 1
    assert await read_head(kernel_env, WORKSPACE) == 1


async def test_model_only_consensus_rejected_atomically(kernel_env):
    risk = make_risk(
        evidence_kind=EVIDENCE_MODEL,
        model_only=True,
        consensus=True,
        witness_refs=("model-a", "model-b"),
        disclosure_refs=("disclosure-a", "disclosure-b"),
    )
    await assert_rejected_atomically(kernel_env, valid_batch(risk=risk))


@pytest.mark.parametrize(
    "changes",
    [
        {
            "declared_context": {
                "verification_risk": {
                    "evidence_ref": "risk-1",
                    "evaluation_slice_id": SLICE,
                }
            }
        },
        {"risk": make_risk(expires_at="2026-08-10T00:00:00Z")},
        {"risk": make_risk(shift_status=SHIFT_SHIFTED)},
        {"risk": make_risk(sample_count=49)},
        {"risk": make_risk(risk_upper_bound="0.06")},
        {"risk": make_risk(risk_upper_bound=None)},
        {"policy_id": "marker.other.policy"},
    ],
)
async def test_invalid_risk_context_or_evidence_is_rejected_atomically(
    kernel_env, changes
):
    risk = changes.pop("risk", None)
    await assert_rejected_atomically(
        kernel_env,
        valid_batch(risk=risk, **changes),
    )


async def test_structural_pr74_error_wins_before_risk_gate(kernel_env):
    # Derived support without a derivation edge violates PR74.  Risk gate
    # would also reject the batch if reached, but must never mask PR74.
    batch = valid_batch()
    records = list(batch.records)
    records[-1] = ProofSupportRecord(
        record_id="support-fact",
        holder_ref="assessment-1",
        evidence_ref="native-fact-1",
        role="derived",
        authority_rule="marker.high_risk.source_native/1:witness-v1",
    )
    batch = KernelCommitBatch(workspace_id=WORKSPACE, records=tuple(records))
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError):
        await service.commit(batch)
    assert await row_count(kernel_env, KernelRecord) == 0
    assert await read_head(kernel_env, WORKSPACE) == 0


async def test_fault_at_risk_checked_rolls_back_everything(kernel_env):
    assert PHASE_RISK_CHECKED in FAULT_PHASES
    with pytest.raises(InjectedFaultError):
        await KernelCommitService(kernel_env).commit(
            valid_batch(), _inject_fault_at=PHASE_RISK_CHECKED
        )
    assert await row_count(kernel_env, KernelRecord) == 0
    assert await row_count(kernel_env, KernelRecordEdge) == 0
    assert await row_count(kernel_env, KernelCommitManifest) == 0
    assert await read_head(kernel_env, WORKSPACE) == 0


async def test_replay_and_risk_rematerialization_preserve_identity(kernel_env):
    await KernelCommitService(kernel_env).commit(valid_batch())
    history = await verify_history(kernel_env, WORKSPACE)
    assert history.ok
    first = await replay(kernel_env, WORKSPACE)
    second = await replay(kernel_env, WORKSPACE)
    assert first.replay_digest == second.replay_digest

    async with kernel_env() as session:
        rows = (
            await session.execute(
                select(
                    KernelRecord.id,
                    KernelRecord.record_class,
                    KernelRecord.payload_json,
                    KernelRecord.identity_hash,
                ).where(KernelRecord.workspace_id == WORKSPACE)
            )
        ).all()
    risk_row = next(row for row in rows if row.record_class == "verification_risk_evidence")
    risk = VerificationRiskEvidenceRecord.from_payload(
        json.loads(risk_row.payload_json), record_id=risk_row.id
    )
    assert risk.identity_hash() == risk_row.identity_hash
    assessment_row = next(row for row in rows if row.record_class == "claim_assessment")
    assessment = ClaimAssessmentRecord.from_payload(
        json.loads(assessment_row.payload_json), record_id=assessment_row.id
    )
    assert (
        record_identity_hash(
            record_type=assessment.record_type,
            schema_version=assessment.schema_version,
            payload=to_json_ready(assessment.identity_payload()),
        )
        == assessment_row.identity_hash
    )
