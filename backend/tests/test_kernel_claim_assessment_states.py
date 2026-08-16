"""PR74 policy/snapshot-relative assessment states (V3.2 §9.4).

One claim can carry different historical assessments under different
policies and snapshots; history is append-only and auditable; there is
no policy-free global verified state anywhere in the schema; an
assessment cannot silently borrow evidence from beyond its declared
snapshot cut.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InvalidClaimAssessmentError
from app.kernel.models import KernelRecord
from app.kernel.proofs import PROOF_ROLE_WITNESS, ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    ObservationRecord,
)
from app.kernel.replay import read_head, verify_history

pytestmark = pytest.mark.asyncio

WS = "ws-claim-states"


def make_assertion(record_id: str, claim_key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        record_id=record_id,
        claim_key=claim_key,
        subject="doc:invoice-42",
        predicate=claim_key,
        value="1250.00",
    )


def make_observation(record_id: str, summary: str) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        observer="marker-test",
        derivation={"stage": "test", "pass": len(record_id) % 4},
        summary=summary,
    )


def make_assessment(
    record_id: str,
    assertion_ref: str,
    *,
    outcome: str,
    policy_revision: str,
    evidence_refs: tuple[str, ...],
    snapshot_commit_id: int,
) -> ClaimAssessmentRecord:
    return ClaimAssessmentRecord(
        record_id=record_id,
        assertion_ref=assertion_ref,
        outcome=outcome,
        policy_id="policy.claims",
        policy_revision=policy_revision,
        evidence_refs=evidence_refs,
        snapshot_commit_id=snapshot_commit_id,
        workflow_class="standard.v1",
    )


async def committed_payloads(
    factory: async_sessionmaker, record_class: str
) -> dict[str, str]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord.id, KernelRecord.payload_json).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == record_class,
                )
            )
        ).all()
    return {record_id: payload for record_id, payload in rows}


# ---------------------------------------------------------------------------
# policy-relative history
# ---------------------------------------------------------------------------


async def test_one_claim_two_policies_two_historical_assessments(kernel_env):
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(make_assertion("assertion-1", "invoice.total"),
                     make_observation("obs-1", "witness")),
        )
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                make_assessment("assessment-v1", "assertion-1",
                                outcome="verified", policy_revision="rev-1",
                                evidence_refs=("obs-1",), snapshot_commit_id=1),
                ProofSupportRecord(record_id="sup-v1", holder_ref="assessment-v1",
                                   evidence_ref="obs-1", role=PROOF_ROLE_WITNESS,
                                   authority_rule="policy.claims/rev-1"),
            ),
        )
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                make_assessment("assessment-v2", "assertion-1",
                                outcome="accepted_with_warning",
                                policy_revision="rev-2",
                                evidence_refs=("obs-1",), snapshot_commit_id=2),
                ProofSupportRecord(record_id="sup-v2", holder_ref="assessment-v2",
                                   evidence_ref="obs-1", role=PROOF_ROLE_WITNESS,
                                   authority_rule="policy.claims/rev-2"),
            ),
        )
    )
    payloads = await committed_payloads(kernel_env, "claim_assessment")
    assert set(payloads) == {"assessment-v1", "assessment-v2"}
    v1 = json.loads(payloads["assessment-v1"])
    v2 = json.loads(payloads["assessment-v2"])
    assert v1["outcome"] == "verified" and v1["policy"]["revision"] == "rev-1"
    assert v2["outcome"] == "accepted_with_warning"
    assert v2["policy"]["revision"] == "rev-2"
    # The newer policy did not rewrite the older assessment's payload.
    assert v1["snapshot_commit_id"] == 1
    result = await verify_history(kernel_env, WS)
    assert result.ok
    assert await read_head(kernel_env, WS) == 3


async def test_two_claims_independent_states_no_global_boolean(kernel_env):
    """Two claims in one document carry independent assessment state;
    nothing in the durable schema stores a document-level verified
    flag."""
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                make_assertion("assertion-total", "invoice.total"),
                make_assertion("assertion-tax", "invoice.tax"),
                make_observation("obs-total", "total witness"),
                make_observation("obs-tax", "tax witness"),
            ),
        )
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                make_assessment("assessment-total", "assertion-total",
                                outcome="verified", policy_revision="rev-1",
                                evidence_refs=("obs-total",), snapshot_commit_id=1),
                ProofSupportRecord(record_id="sup-total",
                                   holder_ref="assessment-total",
                                   evidence_ref="obs-total",
                                   role=PROOF_ROLE_WITNESS,
                                   authority_rule="policy.claims/rev-1"),
                make_assessment("assessment-tax", "assertion-tax",
                                outcome="uncertain", policy_revision="rev-1",
                                evidence_refs=(), snapshot_commit_id=1),
            ),
        )
    )
    payloads = await committed_payloads(kernel_env, "claim_assessment")
    outcomes = {
        rid: json.loads(payload)["outcome"] for rid, payload in payloads.items()
    }
    assert outcomes == {"assessment-total": "verified", "assessment-tax": "uncertain"}

    # Schema-level honesty: the durable record model has no verified
    # column — status lives only in per-assessment payloads.
    columns = {c.name for c in KernelRecord.__table__.columns}
    assert "verified" not in columns
    assert "status" not in columns


async def test_assessment_cannot_borrow_beyond_its_snapshot(kernel_env):
    """Evidence that only became visible AFTER the declared snapshot
    cut cannot back an assessment bound to that older cut."""
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(make_assertion("assertion-1", "invoice.total"),
                     make_observation("obs-1", "first witness")),
        )
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(make_observation("obs-2", "late witness"),),
        )
    )
    # obs-2 landed at commit 2; the assessment claims snapshot 1.
    with pytest.raises(InvalidClaimAssessmentError, match="declares snapshot"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assessment("assessment-1", "assertion-1",
                                    outcome="verified", policy_revision="rev-1",
                                    evidence_refs=("obs-2",), snapshot_commit_id=1),
                    ProofSupportRecord(record_id="sup-1", holder_ref="assessment-1",
                                       evidence_ref="obs-2", role=PROOF_ROLE_WITNESS,
                                       authority_rule="policy.claims/rev-1"),
                ),
            )
        )
    assert await read_head(kernel_env, WS) == 2
    # Declaring the honest later cut admits the same evidence.
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                make_assessment("assessment-1", "assertion-1",
                                outcome="verified", policy_revision="rev-1",
                                evidence_refs=("obs-2",), snapshot_commit_id=2),
                ProofSupportRecord(record_id="sup-1", holder_ref="assessment-1",
                                   evidence_ref="obs-2", role=PROOF_ROLE_WITNESS,
                                   authority_rule="policy.claims/rev-1"),
            ),
        )
    )
    assert await read_head(kernel_env, WS) == 3
