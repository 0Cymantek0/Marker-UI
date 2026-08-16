"""PR74 durability & fault-injection evidence (V3.2 §9.6).

Crashes/exceptions around proof validation and commit acceptance can
never leave: an assessment without its proof, a proof edge without its
assessment, a decision visible before its proof inputs, or outbox side
effects of a rejected batch. Restart/replay preserves identical claim
and proof semantics; every rematerialized record re-derives its stored
identity.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import (
    FAULT_PHASES,
    KernelCommitBatch,
    KernelCommitService,
)
from app.kernel.errors import InjectedFaultError, ProofCycleError
from app.kernel.models import KernelOutbox, KernelRecord, KernelRecordEdge
from app.kernel.outbox import OutboxIntent
from app.kernel.proofs import PROOF_ROLE_DERIVED, PROOF_ROLE_WITNESS, ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import read_head, replay, verify_history
from app.utils.canonical import record_identity_hash, to_json_ready

pytestmark = pytest.mark.asyncio

WS = "ws-pr74-durability"


def make_assertion() -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        record_id="assertion-1",
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


def proof_batch() -> KernelCommitBatch:
    assessment = ClaimAssessmentRecord(
        record_id="assessment-1",
        assertion_ref="assertion-1",
        outcome="verified",
        policy_id="policy.default",
        policy_revision="rev-3",
        evidence_refs=("obs-1",),
        snapshot_commit_id=0,
        workflow_class="standard.v1",
    )
    return KernelCommitBatch(
        workspace_id=WS,
        records=(
            make_assertion(),
            make_observation("obs-1", "witness"),
            assessment,
            ProofSupportRecord(
                record_id="sup-1",
                holder_ref="assessment-1",
                evidence_ref="obs-1",
                role=PROOF_ROLE_WITNESS,
                authority_rule="policy.default/rev-3:witness-v1",
            ),
        ),
    )


async def count_rows(factory: async_sessionmaker, model) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(model).where(
                    model.workspace_id == WS
                )
            )
        ).scalar_one()


async def test_fault_at_every_post_proof_phase_rolls_back_whole_batch(kernel_env):
    """A crash anywhere after proof validation (records inserted,
    manifest, outbox, head advanced, pre-commit) leaves nothing: the
    assessment, its proof support, and every side effect land together
    or not at all."""
    for phase in (
        "proof-checked",
        "records-inserted",
        "manifest-inserted",
        "outbox-inserted",
        "head-advanced",
        "pre-commit",
    ):
        assert phase in FAULT_PHASES
        service = KernelCommitService(kernel_env)
        with pytest.raises(InjectedFaultError):
            await service.commit(proof_batch(), _inject_fault_at=phase)
        assert await count_rows(kernel_env, KernelRecord) == 0
        assert await count_rows(kernel_env, KernelRecordEdge) == 0
        assert await read_head(kernel_env, WS) == 0


async def test_rejected_proof_leaves_no_outbox_side_effects(kernel_env):
    """Successor work authorized by a batch whose proof is invalid must
    not enqueue — the outbox rows commit with their batch or not at
    all."""
    service = KernelCommitService(kernel_env)
    assessment = ClaimAssessmentRecord(
        record_id="assessment-1",
        assertion_ref="assertion-1",
        outcome="verified",
        policy_id="policy.default",
        policy_revision="rev-3",
        evidence_refs=("obs-1",),
    )
    with pytest.raises(ProofCycleError):
        await service.commit(
            KernelCommitBatch(
                workspace_id=WS,
                records=(
                    make_assertion(),
                    make_observation("obs-1", "witness"),
                    assessment,
                    ProofSupportRecord(
                        record_id="sup-1", holder_ref="assessment-1",
                        evidence_ref="obs-1", role=PROOF_ROLE_DERIVED,
                        authority_rule="policy.default/rev-3:witness-v1",
                    ),
                ),
                edges=(KernelEdge(edge_kind="derived_from",
                                  source_ref="obs-1",
                                  target_ref="assessment-1"),),
                outbox=(
                    OutboxIntent(
                        work_kind="index.claim",
                        payload={"claim": "invoice.total"},
                    ),
                ),
            )
        )
    assert await count_rows(kernel_env, KernelRecord) == 0
    async with kernel_env() as session:
        outbox = (
            await session.execute(
                select(func.count()).select_from(KernelOutbox)
            )
        ).scalar_one()
    assert outbox == 0


async def test_restart_replay_preserves_claim_proof_semantics(kernel_env):
    """After a fresh service instance (restart) over the same durable
    database: history verifies, replay digests are identical, and every
    claim/proof record rematerializes to its stored identity."""
    service = KernelCommitService(kernel_env)
    await service.commit(proof_batch())

    restarted = KernelCommitService(kernel_env)
    result = await verify_history(kernel_env, WS)
    assert result.ok
    first = await replay(kernel_env, WS)
    second = await replay(kernel_env, WS)
    assert first.replay_digest == second.replay_digest
    assert first.replay_digest  # a real digest, not empty

    # Rematerialize every PR74 record class from stored payloads.
    async with kernel_env() as session:
        rows = (
            await session.execute(
                select(
                    KernelRecord.id,
                    KernelRecord.record_class,
                    KernelRecord.payload_json,
                    KernelRecord.identity_hash,
                ).where(KernelRecord.workspace_id == WS)
            )
        ).all()
    constructors = {
        "claim_assertion": ClaimAssertionRecord,
        "claim_assessment": ClaimAssessmentRecord,
        "proof_support": ProofSupportRecord,
    }
    for record_id, record_class, payload_json, identity_hash in rows:
        constructor = constructors.get(record_class)
        if constructor is None:
            continue
        remat = constructor.from_payload(json.loads(payload_json), record_id=record_id)
        recomputed = record_identity_hash(
            record_type=remat.record_type,
            schema_version=remat.schema_version,
            payload=to_json_ready(remat.identity_payload()),
        )
        assert recomputed == identity_hash, record_id

    # The restarted service still rejects a laundering attempt against
    # the committed proof state.
    with pytest.raises(ProofCycleError):
        await restarted.commit(
            KernelCommitBatch(
                workspace_id=WS,
                edges=(KernelEdge(edge_kind="derived_from",
                                  source_ref="obs-1",
                                  target_ref="assessment-1"),),
            )
        )
    assert await read_head(kernel_env, WS) == 1
