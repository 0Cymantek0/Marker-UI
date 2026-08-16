"""PR74 proof-input integrity tests (V3.2 §9.3).

Intentionally deceptive proof shapes: hidden derivation, missing
validator inputs, evidence/graph disagreement, authority consumers as
evidence, future snapshots. None may ever yield an authority-bearing
accepted assessment; rejection is atomic.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    InvalidClaimAssessmentError,
    ProofInputIntegrityError,
    UnknownRecordReferenceError,
)
from app.kernel.models import KernelRecord
from app.kernel.proofs import (
    PROOF_ROLE_DERIVED,
    PROOF_ROLE_INPUT,
    PROOF_ROLE_WITNESS,
    ProofSupportRecord,
)
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    DecisionRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import read_head, verify_history

pytestmark = pytest.mark.asyncio

WS = "ws-proof-inputs"


def make_assertion(record_id: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        record_id=record_id,
        claim_key="invoice.total",
        subject="doc:invoice-42",
        predicate="total_amount",
        value="1250.00",
    )


def make_observation(record_id: str, summary: str) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        observer="marker-test",
        derivation={"stage": "test"},
        summary=summary,
    )


def make_assessment(
    record_id: str,
    *,
    outcome: str = "verified",
    evidence_refs: tuple[str, ...] = ("obs-1",),
    snapshot_commit_id: int = 0,
) -> ClaimAssessmentRecord:
    return ClaimAssessmentRecord(
        record_id=record_id,
        assertion_ref="assertion-1",
        outcome=outcome,
        policy_id="policy.default",
        policy_revision="rev-3",
        evidence_refs=evidence_refs,
        snapshot_commit_id=snapshot_commit_id,
        workflow_class="standard.v1",
    )


def support(record_id: str, holder: str, evidence: str, *, role: str) -> ProofSupportRecord:
    return ProofSupportRecord(
        record_id=record_id,
        holder_ref=holder,
        evidence_ref=evidence,
        role=role,
        authority_rule="policy.default/rev-3:witness-v1",
    )


def derived(source: str, target: str) -> KernelEdge:
    return KernelEdge(edge_kind="derived_from", source_ref=source, target_ref=target)


async def assert_nothing_committed(factory: async_sessionmaker) -> None:
    async with factory() as session:
        rows = (
            await session.execute(
                KernelRecord.__table__.select().where(
                    KernelRecord.workspace_id == WS
                )
            )
        ).all()
    assert rows == []
    assert await read_head(factory, WS) == 0


# ---------------------------------------------------------------------------
# hidden / incomplete proof inputs
# ---------------------------------------------------------------------------


async def test_derived_evidence_without_derivation_path_rejected(kernel_env):
    """A crop presented with role=derived but no derivation edge: its
    inputs are hidden, so it cannot raise authority."""
    service = KernelCommitService(kernel_env)
    batch = KernelCommitBatch(
        workspace_id=WS,
        records=(
            make_assertion("assertion-1"),
            make_observation("obs-1", "crop without lineage"),
            make_observation("obs-page", "page"),
            make_assessment("assessment-1"),
            support("sup-1", "assessment-1", "obs-1", role=PROOF_ROLE_DERIVED),
        ),
    )
    with pytest.raises(ProofInputIntegrityError, match="exposes no derivation path"):
        await service.commit(batch)
    await assert_nothing_committed(kernel_env)


async def test_derived_evidence_presented_as_independent_witness_rejected(kernel_env):
    """role=witness while carrying derivation lineage: independence is
    structural, not asserted."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="witness"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-1", "normalized copy"),
                    make_observation("obs-page", "page"),
                    make_assessment("assessment-1"),
                    support("sup-1", "assessment-1", "obs-1", role=PROOF_ROLE_WITNESS),
                ),
                edges=(derived("obs-1", "obs-page"),),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_validator_input_absent_from_evidence_set_rejected(kernel_env):
    """A validator declared a table-region input in its support graph
    but the assessment's evidence set does not carry it: the declared
    evidence and the actual proof disagree."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="agree exactly"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-total", "table total"),
                    make_observation("obs-region", "table region"),
                    make_assessment("assessment-1", evidence_refs=("obs-total",)),
                    support("sup-1", "assessment-1", "obs-total",
                            role=PROOF_ROLE_WITNESS),
                    support("sup-2", "assessment-1", "obs-region",
                            role=PROOF_ROLE_INPUT),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_declared_evidence_without_support_rejected(kernel_env):
    """The inverse direction: evidence listed but never covered by the
    support graph."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="agree exactly"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-total", "table total"),
                    make_observation("obs-region", "table region"),
                    make_assessment(
                        "assessment-1", evidence_refs=("obs-total", "obs-region")
                    ),
                    support("sup-1", "assessment-1", "obs-total",
                            role=PROOF_ROLE_WITNESS),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_valid_bytes_but_incomplete_derivation_metadata_rejected(kernel_env):
    """An evidence object may be well-formed while its derivation story
    is not: derived role demands an exposed path, witness role demands
    none — one honest role must fit."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-render", "rendered page"),
                    make_observation("obs-src", "source page"),
                    make_assessment(
                        "assessment-1", evidence_refs=("obs-render",)
                    ),
                    support("sup-1", "assessment-1", "obs-render",
                            role=PROOF_ROLE_DERIVED),
                ),
                edges=(derived("obs-render", "obs-src"),
                       derived("obs-src", "assertion-1")),
            )
        )
    await assert_nothing_committed(kernel_env)


# ---------------------------------------------------------------------------
# authority-consumer evidence & holders
# ---------------------------------------------------------------------------


async def test_claim_as_evidence_rejected(kernel_env):
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="authority consumers"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    ClaimAssertionRecord(
                        record_id="assertion-2",
                        claim_key="invoice.tax",
                        subject="doc:invoice-42",
                        predicate="tax_amount",
                        value="250.00",
                    ),
                    make_assessment(
                        "assessment-1", evidence_refs=("assertion-2",)
                    ),
                    support("sup-1", "assessment-1", "assertion-2",
                            role=PROOF_ROLE_WITNESS),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_assessment_as_evidence_rejected(kernel_env):
    """Self-support through a peer assessment is still laundering."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="authority consumers"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-1", "witness"),
                    # A legacy-outcome assessment is committable but is
                    # still an authority consumer, never evidence.
                    make_assessment("assessment-2", outcome="uncertain",
                                    evidence_refs=()),
                    make_assessment(
                        "assessment-1", evidence_refs=("assessment-2",)
                    ),
                    support("sup-1", "assessment-1", "assessment-2",
                            role=PROOF_ROLE_WITNESS),
                    support("sup-2", "assessment-2", "obs-1",
                            role=PROOF_ROLE_WITNESS),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_non_assessment_holder_rejected(kernel_env):
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="holder"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-1", "witness"),
                    make_observation("obs-2", "pretend holder"),
                    support("sup-1", "obs-2", "obs-1", role=PROOF_ROLE_WITNESS),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_duplicate_support_pair_rejected(kernel_env):
    service = KernelCommitService(kernel_env)
    with pytest.raises(ProofInputIntegrityError, match="duplicate proof support"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-1", "witness"),
                    make_assessment("assessment-1"),
                    support("sup-1", "assessment-1", "obs-1",
                            role=PROOF_ROLE_WITNESS),
                    support("sup-2", "assessment-1", "obs-1",
                            role=PROOF_ROLE_INPUT),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


# ---------------------------------------------------------------------------
# authority-bearing outcomes & snapshots
# ---------------------------------------------------------------------------


async def test_authority_bearing_outcome_without_support_rejected(kernel_env):
    service = KernelCommitService(kernel_env)
    for outcome in ("verified", "source_exact"):
        with pytest.raises(InvalidClaimAssessmentError, match="without any proof"):
            await service.commit(
                KernelCommitBatch(
                    workspace_id=WS,
                    records=(
                        make_assertion("assertion-1"),
                        make_observation("obs-1", "witness"),
                        make_assessment("assessment-1", outcome=outcome),
                    ),
                )
            )
    await assert_nothing_committed(kernel_env)


async def test_future_snapshot_rejected(kernel_env):
    """An assessment can never claim a cut beyond the committed head."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(InvalidClaimAssessmentError, match="future cut"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-1", "witness"),
                    make_assessment("assessment-1", snapshot_commit_id=1),
                    support("sup-1", "assessment-1", "obs-1",
                            role=PROOF_ROLE_WITNESS),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


async def test_unresolved_references_rejected(kernel_env):
    """assertion/evidence refs that point at nothing visible fail
    closed exactly like edge references."""
    service = KernelCommitService(kernel_env)
    with pytest.raises(UnknownRecordReferenceError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_observation("obs-1", "witness"),
                    make_assessment("assessment-1"),
                ),
            )
        )
    with pytest.raises(UnknownRecordReferenceError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion("assertion-1"),
                    make_observation("obs-1", "witness"),
                    make_assessment("assessment-1"),
                    support("sup-1", "assessment-1", "obs-9",
                            role=PROOF_ROLE_WITNESS),
                ),
            )
        )
    await assert_nothing_committed(kernel_env)


# ---------------------------------------------------------------------------
# honest non-authority states stay representable
# ---------------------------------------------------------------------------


async def test_non_authority_outcomes_commit_without_proof(kernel_env):
    """failed/uncertain/abstained assessments are honest history without
    proof — explicitly non-authority-bearing, never a silent verified."""
    service = KernelCommitService(kernel_env)
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(make_assertion("assertion-1"),
                     make_observation("obs-1", "witness")),
        )
    )
    for outcome in ("failed", "uncertain", "abstained", "supported"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assessment(f"assessment-{outcome}", outcome=outcome,
                                    evidence_refs=()),
                ),
            )
        )
    result = await verify_history(kernel_env, WS)
    assert result.ok
    assert await read_head(kernel_env, WS) == 5


async def test_authority_rule_decision_requires_support(kernel_env):
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    observation = make_observation("obs-1", "witness")
    decision = DecisionRecord(
        record_id="decision-1",
        decision_key="publish-invoice",
        outcome="accepted",
        rationale="witness verified",
        input_refs=("assertion-1",),
        authority_rule="policy.default/rev-3:publish-v1",
    )
    with pytest.raises(InvalidClaimAssessmentError, match="authority rule"):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(assertion, observation, decision),
            )
        )
    await assert_nothing_committed(kernel_env)
    # With its proof in the same commit the decision lands.
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(assertion, observation, decision,
                     support("sup-1", "decision-1", "obs-1",
                             role=PROOF_ROLE_WITNESS)),
        )
    )
    assert await read_head(kernel_env, WS) == 1
