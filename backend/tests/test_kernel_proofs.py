"""PR74 proof-support graph & cycle rejection tests (V3.2 §9.2).

Commit-boundary behavior: valid proofs land atomically; circular or
self-supporting proofs are rejected with typed errors BEFORE any row is
inserted — no records, no edges, no manifest, no head movement. Cyclic
non-authoritative navigation stays legal.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    InjectedFaultError,
    KernelError,
    ProofCycleError,
    ProofInputIntegrityError,
)
from app.kernel.models import (
    KernelCommitManifest,
    KernelRecord,
    KernelRecordEdge,
    KernelViewHead,
)
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord
from app.kernel.proofs import (
    PROOF_ROLE_DERIVED,
    PROOF_ROLE_INPUT,
    PROOF_ROLE_WITNESS,
    ProofSupportRecord,
)
from app.kernel.reading_order import NODE_KIND_CONTENT, OrderNode, ReadingOrderGraph
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import read_head

pytestmark = pytest.mark.asyncio

WS = "ws-proof"


def make_assertion(record_id: str, claim_key: str = "invoice.total") -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        record_id=record_id,
        claim_key=claim_key,
        subject="doc:invoice-42",
        predicate="total_amount",
        value="1250.00",
        qualifiers={"currency": "USD"},
    )


def make_observation(record_id: str, summary: str = "witness") -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        observer="marker-test",
        derivation={"stage": "test"},
        summary=summary,
    )


def make_assessment(
    record_id: str,
    assertion_ref: str,
    *,
    outcome: str = "verified",
    evidence_refs: tuple[str, ...] = ("obs-1",),
    snapshot_commit_id: int = 0,
) -> ClaimAssessmentRecord:
    return ClaimAssessmentRecord(
        record_id=record_id,
        assertion_ref=assertion_ref,
        outcome=outcome,
        policy_id="policy.default",
        policy_revision="rev-3",
        evidence_refs=evidence_refs,
        snapshot_commit_id=snapshot_commit_id,
        workflow_class="standard.v1",
    )


def support(
    record_id: str,
    holder: str,
    evidence: str,
    *,
    role: str = PROOF_ROLE_WITNESS,
    rule: str = "policy.default/rev-3:witness-v1",
) -> ProofSupportRecord:
    return ProofSupportRecord(
        record_id=record_id,
        holder_ref=holder,
        evidence_ref=evidence,
        role=role,
        authority_rule=rule,
    )


def derived(source: str, target: str) -> KernelEdge:
    return KernelEdge(
        edge_kind="derived_from", source_ref=source, target_ref=target
    )


def make_view(record_id: str = "view-1") -> ViewDocumentRecord:
    graph = ReadingOrderGraph.build(
        [OrderNode(node_id="run-a", kind=NODE_KIND_CONTENT, anchor_ref="anchor-a")],
        [],
    )
    return ViewDocumentRecord(
        record_id=record_id,
        content_revision_ref="rev-s1",
        graph=graph,
        texts={"run-a": "Alpha"},
    )


async def fetch_count(factory: async_sessionmaker, model, workspace_id: str) -> int:
    from sqlalchemy import func

    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(model).where(
                    model.workspace_id == workspace_id
                )
            )
        ).scalar_one()


async def assert_clean_state(factory: async_sessionmaker, expected_head: int) -> None:
    """A rejected commit leaves nothing behind."""
    assert await fetch_count(factory, KernelRecord, WS) == 0
    assert await fetch_count(factory, KernelRecordEdge, WS) == 0
    assert await fetch_count(factory, KernelCommitManifest, WS) == 0
    assert await read_head(factory, WS) == expected_head


# ---------------------------------------------------------------------------
# proof-support record contract
# ---------------------------------------------------------------------------


def test_proof_support_construction_rejects_ambiguous_shapes():
    with pytest.raises(KernelError, match="own holder"):
        support("s-1", "a-1", "a-1")
    with pytest.raises(KernelError, match="authority_rule"):
        support("s-1", "a-1", "obs-1", rule="")
    with pytest.raises(KernelError, match="role"):
        support("s-1", "a-1", "obs-1", role="maybe")
    remat = ProofSupportRecord.from_payload(
        support("s-1", "a-1", "obs-1").identity_payload(), record_id="s-9"
    )
    assert remat.role == PROOF_ROLE_WITNESS
    with pytest.raises(KernelError, match="unknown proof support payload fields"):
        ProofSupportRecord.from_payload(
            {"holder_ref": "a-1", "evidence_ref": "o", "role": "witness",
             "authority_rule": "r", "extra": 1},
            record_id="s-9",
        )


# ---------------------------------------------------------------------------
# valid proofs
# ---------------------------------------------------------------------------


async def test_valid_witness_proof_commits_atomically(kernel_env):
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    observation = make_observation("obs-1")
    assessment = make_assessment("assessment-1", "assertion-1")
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(assertion, observation, assessment,
                     support("sup-1", "assessment-1", "obs-1")),
            edges=(KernelEdge(edge_kind="assesses",
                              source_ref="assessment-1",
                              target_ref="assertion-1"),),
        )
    )
    assert receipt.record_count == 4
    assert await read_head(kernel_env, WS) == 1


async def test_derived_and_input_roles_accepted_with_exposed_lineage(kernel_env):
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    page = make_observation("obs-page", "source page")
    crop = make_observation("obs-crop", "cropped region")
    topology = make_observation("obs-topology", "table topology")
    assessment = make_assessment(
        "assessment-1", "assertion-1", evidence_refs=("obs-crop", "obs-topology")
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                assertion, page, crop, topology, assessment,
                support("sup-1", "assessment-1", "obs-crop",
                        role=PROOF_ROLE_DERIVED),
                support("sup-2", "assessment-1", "obs-topology",
                        role=PROOF_ROLE_INPUT),
            ),
            edges=(derived("obs-crop", "obs-page"),),
        )
    )
    assert receipt.record_count == 7
    assert receipt.edge_count == 1


# ---------------------------------------------------------------------------
# cycle rejection
# ---------------------------------------------------------------------------


async def test_same_batch_three_node_cycle_rejected_atomically(kernel_env):
    """assessment -> obs-1 (proof, derived); obs-1 -> obs-2 (derived);
    obs-2 -> assessment (derived). Authority flows back into its own
    consumer."""
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    o1 = make_observation("obs-1")
    o2 = make_observation("obs-2", "reconciliation of assessment-1")
    assessment = make_assessment("assessment-1", "assertion-1")
    with pytest.raises(ProofCycleError) as excinfo:
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(assertion, o1, o2, assessment,
                         support("sup-1", "assessment-1", "obs-1",
                                 role=PROOF_ROLE_DERIVED)),
                edges=(derived("obs-1", "obs-2"), derived("obs-2", "assessment-1")),
            )
        )
    assert "assessment-1" in " -> ".join(excinfo.value.cycle_path)
    await assert_clean_state(kernel_env, 0)


async def test_two_node_proof_cycle_via_derivation(kernel_env):
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    o1 = make_observation("obs-1")
    assessment = make_assessment("assessment-1", "assertion-1")
    with pytest.raises(ProofCycleError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(assertion, o1, assessment,
                         support("sup-1", "assessment-1", "obs-1",
                                 role=PROOF_ROLE_DERIVED)),
                edges=(derived("obs-1", "assessment-1"),),
            )
        )
    await assert_clean_state(kernel_env, 0)


async def test_cycle_closed_against_committed_history(kernel_env):
    """A new edge closing a loop through earlier commits is rejected,
    and the previously committed state stays intact."""
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    o1 = make_observation("obs-1")
    o2 = make_observation("obs-2", "second witness")
    assessment = make_assessment("assessment-1", "assertion-1")
    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(assertion, o1, o2, assessment,
                     support("sup-1", "assessment-1", "obs-1",
                             role=PROOF_ROLE_DERIVED)),
            edges=(derived("obs-1", "obs-2"),),
        )
    )
    with pytest.raises(ProofCycleError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                edges=(derived("obs-2", "assessment-1"),),
            )
        )
    # Committed history untouched; the rejected edge landed nowhere.
    assert await fetch_count(kernel_env, KernelRecord, WS) == 5
    assert await fetch_count(kernel_env, KernelRecordEdge, WS) == 1
    assert await read_head(kernel_env, WS) == 1


async def test_long_cycle_through_mixed_edge_kinds(kernel_env):
    """5-node loop alternating proof and derivation relations."""
    service = KernelCommitService(kernel_env)
    records = [make_assertion("assertion-1")] + [
        make_observation(f"obs-{i}", f"step {i}") for i in range(1, 5)
    ]
    records.append(make_assessment("assessment-1", "assertion-1"))
    with pytest.raises(ProofCycleError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=tuple(records)
                + (support("sup-1", "assessment-1", "obs-1",
                           role=PROOF_ROLE_DERIVED),),
                edges=(
                    derived("obs-1", "obs-2"),
                    derived("obs-2", "obs-3"),
                    derived("obs-3", "obs-4"),
                    derived("obs-4", "assessment-1"),
                ),
            )
        )
    await assert_clean_state(kernel_env, 0)


async def test_cyclic_non_authoritative_navigation_stays_legal(kernel_env):
    """Navigation cycles (assesses/observes/evidence_for) are NOT proof
    support; over-rejecting them would ban legitimate provenance webs."""
    service = KernelCommitService(kernel_env)
    records = tuple(
        make_observation(f"obs-{i}", f"navigation {i}") for i in range(1, 4)
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=records,
            edges=(
                KernelEdge(edge_kind="observes", source_ref="obs-1", target_ref="obs-2"),
                KernelEdge(edge_kind="observes", source_ref="obs-2", target_ref="obs-3"),
                KernelEdge(edge_kind="evidence_for", source_ref="obs-3", target_ref="obs-1"),
            ),
        )
    )
    assert receipt.edge_count == 3


async def test_self_supporting_derived_evidence_rejected(kernel_env):
    """assessment -> summary (proof, role=derived); summary derived from
    the very assertion being assessed: authority launders itself."""
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    summary = make_observation("obs-summary", "reconciled from claim")
    assessment = make_assessment(
        "assessment-1", "assertion-1", evidence_refs=("obs-summary",)
    )
    with pytest.raises(ProofInputIntegrityError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(assertion, summary, assessment,
                         support("sup-1", "assessment-1", "obs-summary",
                                 role=PROOF_ROLE_DERIVED)),
                edges=(derived("obs-summary", "assertion-1"),),
            )
        )
    await assert_clean_state(kernel_env, 0)


async def test_witness_reaching_another_claim_rejected(kernel_env):
    """Support derived from ANY claim (not just the assessed one) is
    laundering through unresolved authority."""
    service = KernelCommitService(kernel_env)
    other = make_assertion("assertion-other", claim_key="invoice.tax")
    chain = make_observation("obs-chain", "summary of other claim")
    page = make_observation("obs-page")
    assertion = make_assertion("assertion-1")
    assessment = make_assessment(
        "assessment-1", "assertion-1", evidence_refs=("obs-page",)
    )
    with pytest.raises(ProofInputIntegrityError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(assertion, other, chain, page, assessment,
                         support("sup-1", "assessment-1", "obs-page",
                                 role=PROOF_ROLE_DERIVED)),
                edges=(derived("obs-page", "obs-chain"),
                       derived("obs-chain", "assertion-other")),
            )
        )
    await assert_clean_state(kernel_env, 0)


# ---------------------------------------------------------------------------
# transactional non-effects under fault injection
# ---------------------------------------------------------------------------


async def test_fault_at_proof_phase_rolls_back_everything(kernel_env):
    service = KernelCommitService(kernel_env)
    assertion = make_assertion("assertion-1")
    observation = make_observation("obs-1")
    assessment = make_assessment("assessment-1", "assertion-1")
    with pytest.raises(InjectedFaultError) as excinfo:
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(assertion, observation, assessment,
                         support("sup-1", "assessment-1", "obs-1")),
            ),
            _inject_fault_at="proof-checked",
        )
    assert excinfo.value.phase == "proof-checked"
    await assert_clean_state(kernel_env, 0)


async def test_invalid_proof_with_view_advancement_moves_no_head(kernel_env):
    """A batch that would also move a view head fails on proof integrity
    BEFORE the head flip — no view-head row ever appears."""
    service = KernelCommitService(kernel_env)
    view = make_view()
    assertion = make_assertion("assertion-1")
    o1 = make_observation("obs-1")
    assessment = make_assessment("assessment-1", "assertion-1")
    with pytest.raises(ProofCycleError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(view, assertion, o1, assessment,
                         support("sup-1", "assessment-1", "obs-1",
                                 role=PROOF_ROLE_DERIVED)),
                edges=(derived("obs-1", "assessment-1"),),
                view_advancement=ViewAdvancement(
                    new_revision_id=view.view_revision_id(),
                    view_id="document",
                ),
            )
        )
    async with kernel_env() as session:
        heads = (
            await session.execute(select(KernelViewHead).where(
                KernelViewHead.workspace_id == WS))
        ).all()
    assert heads == []
    await assert_clean_state(kernel_env, 0)
