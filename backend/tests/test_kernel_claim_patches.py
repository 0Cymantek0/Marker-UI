"""PR74 claim-dependent patch preconditions (V3.2 §9.5).

The PR73 fail-closed ``required_claim_refs`` placeholder is replaced by
typed ClaimRequirement entries evaluated authoritatively inside the
commit transaction: satisfied preconditions admit the patch (all-or-
conflict preserved); missing, stale, wrong-assertion, policy-mismatched,
or proof-invalid assessments reject the whole commit.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import ClaimPreconditionUnmetError, KernelError
from app.kernel.models import KernelRecord, KernelViewHead
from app.kernel.patches import (
    PatchOperation,
    PatchPreconditions,
    PatchProposalRecord,
    TargetCheck,
    view_text_hash,
)
from app.kernel.patching import (
    clean_rebuild_view,
    initialize_view,
    read_current_view,
    submit_patch,
)
from app.kernel.proofs import PROOF_ROLE_WITNESS, ProofSupportRecord
from app.kernel.reading_order import NODE_KIND_CONTENT, OrderNode, ReadingOrderGraph
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import read_head, verify_history

pytestmark = pytest.mark.asyncio

WS = "ws-claim-patch"


def base_graph() -> ReadingOrderGraph:
    return ReadingOrderGraph.build(
        [
            OrderNode(node_id="run-a", kind=NODE_KIND_CONTENT, anchor_ref="anchor-a"),
            OrderNode(node_id="run-b", kind=NODE_KIND_CONTENT, anchor_ref="anchor-b"),
        ],
        [],
    )


async def setup_view(factory: async_sessionmaker, service: KernelCommitService):
    return await initialize_view(
        factory,
        service,
        workspace_id=WS,
        content_revision_ref="rev-s1",
        graph=base_graph(),
        texts={"run-a": "Alpha", "run-b": "Beta"},
    )


async def commit_verified_assessment(
    service: KernelCommitService,
    *,
    outcome: str = "verified",
    policy_id: str = "policy.default",
    policy_revision: str = "rev-3",
    snapshot_commit_id: int = 1,
    assertion_id: str = "assertion-1",
    assessment_id: str = "assessment-1",
    include_base: bool = True,
) -> None:
    records: list = []
    if include_base:
        records.append(
            ClaimAssertionRecord(
                record_id=assertion_id,
                claim_key="invoice.total",
                subject="doc:invoice-42",
                predicate="total_amount",
                value="1250.00",
            )
        )
        records.append(
            ObservationRecord(
                record_id="obs-1",
                observer="marker-test",
                derivation={"stage": "test"},
                summary="witness",
            )
        )
    assessment = ClaimAssessmentRecord(
        record_id=assessment_id,
        assertion_ref=assertion_id,
        outcome=outcome,
        policy_id=policy_id,
        policy_revision=policy_revision,
        evidence_refs=("obs-1",),
        snapshot_commit_id=snapshot_commit_id,
        workflow_class="standard.v1",
    )
    records.append(assessment)
    records.append(
        ProofSupportRecord(
            record_id=f"sup-{assessment_id}",
            holder_ref=assessment_id,
            evidence_ref="obs-1",
            role=PROOF_ROLE_WITNESS,
            authority_rule="policy.default/rev-3:witness-v1",
        )
    )
    await service.commit(
        KernelCommitBatch(workspace_id=WS, records=tuple(records))
    )


def claim_gated_proposal(
    current_revision: str,
    *,
    assertion_ref: str = "assertion-1",
    policy_id: str = "policy.default",
    policy_revision: str = "rev-3",
    accepted_outcomes: tuple[str, ...] = (),
    assessment_ref: str | None = None,
    min_snapshot_commit_id: int = 0,
) -> PatchProposalRecord:
    from app.kernel.proofs import ClaimRequirement

    return PatchProposalRecord(
        record_id="proposal-claim-1",
        preconditions=PatchPreconditions(
            base_revision_id=current_revision,
            target_checks=(TargetCheck(node_id="run-a",
                                       before_hash=view_text_hash("Alpha")),),
            required_claims=(
                ClaimRequirement(
                    assertion_ref=assertion_ref,
                    policy_id=policy_id,
                    policy_revision=policy_revision,
                    accepted_outcomes=accepted_outcomes,
                    assessment_ref=assessment_ref,
                    min_snapshot_commit_id=min_snapshot_commit_id,
                ),
            ),
        ),
        operations=(PatchOperation.replace_text(node_id="run-a",
                                                after_text="Alpha-edited"),),
    )


async def assert_untouched(factory: async_sessionmaker, head: int, revision: str):
    assert await read_head(factory, WS) == head
    current = await read_current_view(factory, WS)
    assert current is not None and current.revision_id == revision


# ---------------------------------------------------------------------------
# satisfied preconditions
# ---------------------------------------------------------------------------


async def test_satisfied_claim_precondition_permits_patch(kernel_env):
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service)
    acceptance = await submit_patch(
        kernel_env, service,
        workspace_id=WS,
        proposal=claim_gated_proposal(view.revision_id),
    )
    assert acceptance.result.view.text_of("run-a") == "Alpha-edited"
    current = await read_current_view(kernel_env, WS)
    assert current.revision_id == acceptance.result.revision_id
    result = await verify_history(kernel_env, WS)
    assert result.ok


async def test_accepted_outcomes_can_admit_non_authority_states(kernel_env):
    """A patch may explicitly gate on an uncertain state — the vocabulary
    it accepts is the patch's declared choice, not a global boolean."""
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(
        service, outcome="uncertain", assessment_id="assessment-u"
    )
    with pytest.raises(ClaimPreconditionUnmetError):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id),
        )
    await assert_untouched(kernel_env, 2, view.revision_id)
    acceptance = await submit_patch(
        kernel_env, service,
        workspace_id=WS,
        proposal=claim_gated_proposal(
            view.revision_id, accepted_outcomes=("uncertain",)
        ),
    )
    assert acceptance.result.view.text_of("run-a") == "Alpha-edited"


# ---------------------------------------------------------------------------
# failing preconditions (all-or-conflict)
# ---------------------------------------------------------------------------


async def test_missing_assessment_rejects_whole_patch(kernel_env):
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    with pytest.raises(ClaimPreconditionUnmetError, match="no committed assessment"):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id),
        )
    await assert_untouched(kernel_env, 1, view.revision_id)
    async with kernel_env() as session:
        count = len((
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == "patch_proposal",
                )
            )
        ).all())
    assert count == 0  # the rejected patch left no proposal behind


async def test_wrong_assertion_rejects(kernel_env):
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service)
    with pytest.raises(ClaimPreconditionUnmetError):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id,
                                          assertion_ref="assertion-other"),
        )
    await assert_untouched(kernel_env, 2, view.revision_id)


async def test_policy_mismatch_rejects(kernel_env):
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service)
    with pytest.raises(ClaimPreconditionUnmetError, match="no committed assessment"):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id, policy_revision="rev-9"),
        )
    with pytest.raises(ClaimPreconditionUnmetError, match="mismatch"):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(
                view.revision_id, assessment_ref="assessment-1",
                policy_revision="rev-9",
            ),
        )
    await assert_untouched(kernel_env, 2, view.revision_id)


async def test_stale_snapshot_rejects(kernel_env):
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service, snapshot_commit_id=1)
    with pytest.raises(ClaimPreconditionUnmetError, match="predates"):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id,
                                          min_snapshot_commit_id=2),
        )
    await assert_untouched(kernel_env, 2, view.revision_id)


async def test_proof_invalid_assessment_cannot_satisfy_precondition(kernel_env):
    """A later commit taints the assessment's proof closure (its witness
    now derives from the claim itself); the precondition revalidation
    fails closed at the current cut."""
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service)
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            edges=(KernelEdge(edge_kind="derived_from",
                              source_ref="obs-1",
                              target_ref="assertion-1"),),
        )
    )
    with pytest.raises(ClaimPreconditionUnmetError):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id),
        )
    await assert_untouched(kernel_env, 3, view.revision_id)


async def test_pinned_missing_assessment_rejects(kernel_env):
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service)
    with pytest.raises(ClaimPreconditionUnmetError, match="not committed"):
        await submit_patch(
            kernel_env, service,
            workspace_id=WS,
            proposal=claim_gated_proposal(view.revision_id,
                                          assessment_ref="assessment-none"),
        )
    await assert_untouched(kernel_env, 2, view.revision_id)


async def test_latest_assessment_resolution_is_deterministic(kernel_env):
    """Unpinned resolution picks the latest committed assessment under
    the exact policy ask (causal order, not wall time); pinning the
    older snapshot keeps the explicit history auditable."""
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service, policy_revision="rev-3",
                                     assessment_id="assessment-old",
                                     snapshot_commit_id=1)
    await commit_verified_assessment(service, policy_revision="rev-3",
                                     assessment_id="assessment-new",
                                     snapshot_commit_id=2,
                                     include_base=False)
    # Unpinned + freshness floor 2: resolves assessment-new, accepted.
    acceptance = await submit_patch(
        kernel_env, service,
        workspace_id=WS,
        proposal=claim_gated_proposal(view.revision_id,
                                      min_snapshot_commit_id=2),
    )
    assert acceptance.result.view.text_of("run-a") == "Alpha-edited"
    # The same freshness floor against the pinned older assessment
    # rejects — explicit history stays inspectable and honest.
    await assert_untouched(kernel_env, 4, acceptance.result.revision_id)
    async with kernel_env() as session:
        assessments = (
            await session.execute(
                select(KernelRecord.id).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == "claim_assessment",
                )
            )
        ).scalars().all()
    assert sorted(assessments) == ["assessment-new", "assessment-old"]


# ---------------------------------------------------------------------------
# restart / rematerialization
# ---------------------------------------------------------------------------


async def test_claim_gate_survives_service_restart_and_replay(kernel_env):
    """New service instance over the same durable database: identical
    precondition behavior, clean rebuild reproduces the gated revision,
    and the stored proposal rematerializes with its claim requirement."""
    service = KernelCommitService(kernel_env)
    view = await setup_view(kernel_env, service)
    await commit_verified_assessment(service)
    acceptance = await submit_patch(
        kernel_env, service,
        workspace_id=WS,
        proposal=claim_gated_proposal(view.revision_id),
    )

    # Restart: a fresh service/process over the same file-backed DB.
    restarted = KernelCommitService(kernel_env)
    current = await read_current_view(kernel_env, WS)
    assert current is not None
    assert current.revision_id == acceptance.result.revision_id

    # The same claim gate still holds for a follow-up patch.
    followup = PatchProposalRecord(
        record_id="proposal-claim-2",
        preconditions=PatchPreconditions(
            base_revision_id=current.revision_id,
            target_checks=(TargetCheck(
                node_id="run-a", before_hash=view_text_hash("Alpha-edited")
            ),),
            required_claims=claim_gated_proposal(
                current.revision_id
            ).preconditions.required_claims,
        ),
        operations=(PatchOperation.replace_text(node_id="run-a",
                                                after_text="Alpha-final"),),
    )
    acceptance2 = await submit_patch(
        kernel_env, restarted, workspace_id=WS, proposal=followup
    )
    assert acceptance2.result.view.text_of("run-a") == "Alpha-final"

    # Clean rebuild reproduces the gated history exactly.
    rebuilt = await clean_rebuild_view(kernel_env, WS)
    assert rebuilt.view_revision_id() == acceptance2.result.revision_id

    # Stored proposal rematerializes with its claim requirement intact.
    async with kernel_env() as session:
        payload_json = (
            await session.execute(
                select(KernelRecord.payload_json).where(
                    KernelRecord.id == "proposal-claim-1",
                    KernelRecord.workspace_id == WS,
                )
            )
        ).scalar_one()
    remat = PatchProposalRecord.from_payload(
        json.loads(payload_json), record_id="proposal-claim-1"
    )
    (requirement,) = remat.preconditions.required_claims
    assert requirement.assertion_ref == "assertion-1"
    assert requirement.policy_revision == "rev-3"
    assert remat.proposal_id() == acceptance.proposal_id
