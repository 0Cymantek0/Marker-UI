"""PR85 answer-evidence service semantics — the AE acceptance matrix.

Exercises the durable boundary through real migrated SQLite state and
real EvidencePackets produced by the continuation service: trace
fidelity, historical immutability, assessment separation and
non-mutation, fail-closed tenancy, idempotency/conflict truth, and
restart durability.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.answer_evidence import AnswerEvidenceService
from app.answer_evidence.domain import answer_content_digest
from app.answer_evidence.errors import (
    AnswerEvidenceContractError,
    AnswerTraceConflictError,
    AssessmentConflictError,
    DisclosureReferenceError,
)
from app.context_runtime import ContinuationService, CursorCodec, CursorKeyring
from app.context_runtime.packets import to_json as packet_to_json
from app.kernel.commit import KernelCommitBatch
from app.kernel.models import KernelAnswerTrace
from app.kernel.records import ACCESS_DENIAL_TARGET_RECORD
from tests.test_context_runtime_authorization import _deny, _epoch
from tests.test_context_runtime_service import _publish

pytestmark = pytest.mark.asyncio


def _codec() -> CursorCodec:
    return CursorCodec(
        CursorKeyring({"k1": b"pr85-test-key-" + b"x" * 32}, current_key_id="k1")
    )


def _request(workspace: str):
    from app.context_runtime import parse_query_request, QUERY_SCHEMA_VERSION

    return parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": workspace,
            "operations": [
                {"op": "lexical_search", "text": "needle", "limit": 25}
            ],
        }
    )


async def _collect_disclosures(
    factory, commit_service, workspace: str, evidence: AnswerEvidenceService,
    *, page_size: int = 4,
) -> tuple[list[dict], ContinuationService]:
    """Deliver every page of one query chain and disclose each packet."""

    await _publish(factory, commit_service, workspace)
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    outcome = await service.fresh_query(_request(workspace), page_size=page_size)
    disclosures: list[dict] = []
    cursor = outcome.next_cursor
    while True:
        if outcome.packet is not None:
            view = await evidence.record_disclosure(
                packet=packet_to_json(outcome.packet),
                workspace_id=workspace,
                delivery_status=outcome.status,
            )
            disclosures.append(view)
        if cursor is None:
            break
        outcome = await service.continue_query(
            cursor, workspace_id=workspace, page_size=page_size
        )
        cursor = outcome.next_cursor
    return disclosures, service


def _first_locator(packet_view: dict) -> dict:
    unit = packet_view["evidence"][0]
    return {
        "record_id": unit["record_id"],
        "view_id": unit["view_id"],
        "node_id": unit["node_id"],
}


# ---------------------------------------------------------------------------
# AE-01/02/03/15: trace fidelity and historical immutability
# ---------------------------------------------------------------------------


async def test_trace_preserves_disclosed_order_and_answer_time_truth(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _service = await _collect_disclosures(
        factory, commit_service, "ws-ae1", evidence, page_size=2
    )
    assert len(disclosures) >= 2, "expected a multi-page chain"

    trace = await evidence.commit_trace(
        workspace_id="ws-ae1",
        answer_ref="answer-1",
        answer_content="Revenue grew because the needle rows said so.",
        disclosure_ids=[d["disclosure_id"] for d in disclosures],
    )
    assert trace["answer_ref"] == "answer-1"
    assert [d["disclosure_id"] for d in trace["disclosures"]] == [
        d["disclosure_id"] for d in disclosures
    ]
    assert [d["packet_id"] for d in trace["disclosures"]] == [
        d["packet_id"] for d in disclosures
    ]
    # Each bound packet retains its full answer-time truth, including the
    # per-page evidence order and budget state at delivery.
    for stored, minted in zip(trace["disclosures"], disclosures):
        packet = stored["packet"]
        assert packet["schema_version"] == "marker.evidence_packet.v1"
        assert packet["identity_id"] == stored["packet_id"]
        assert "evidence" in packet and "budget" in packet
        order = [(u["record_id"], u["node_id"]) for u in packet["evidence"]]
        assert order == list(dict.fromkeys(order))


async def test_later_retrieval_change_never_rewrites_history(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _service = await _collect_disclosures(
        factory, commit_service, "ws-ae2", evidence, page_size=10
    )
    before = await evidence.commit_trace(
        workspace_id="ws-ae2",
        answer_ref="answer-hist",
        answer_content="Historical answer.",
        disclosure_ids=[disclosures[0]["disclosure_id"]],
    )
    # Move current authorization truth forward: deny the delivered view so
    # future retrieval shapes a different packet.
    await commit_service.commit(
        KernelCommitBatch(
            workspace_id="ws-ae2",
            records=(_deny(ACCESS_DENIAL_TARGET_RECORD, "view-1"),),
        )
    )
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    fresh = await service.fresh_query(_request("ws-ae2"), page_size=10)
    assert fresh.packet is not None
    new_view = await evidence.record_disclosure(
        packet=packet_to_json(fresh.packet),
        workspace_id="ws-ae2",
        delivery_status=fresh.status,
    )
    assert new_view["packet_id"] != disclosures[0]["packet_id"]

    after = await evidence.read_trace(
        workspace_id="ws-ae2", answer_ref="answer-hist"
    )
    assert after == before


async def test_policy_change_after_answer_keeps_answer_time_authorization(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _service = await _collect_disclosures(
        factory, commit_service, "ws-ae16", evidence, page_size=10
    )
    answer = "Answer under policy v1."
    trace = await evidence.commit_trace(
        workspace_id="ws-ae16",
        answer_ref="answer-policy",
        answer_content=answer,
        disclosure_ids=[disclosures[0]["disclosure_id"]],
    )
    auth_at_answer = trace["disclosures"][0]["packet"]["authorization"]

    # Advance the authorization epoch: future packets get a different
    # effective-authorization identity, the historical trace must not.
    await commit_service.commit(
        KernelCommitBatch(workspace_id="ws-ae16", records=(_epoch(9, 9),))
    )
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    fresh = await service.fresh_query(_request("ws-ae16"), page_size=10)
    assert fresh.packet is not None
    assert fresh.packet.authorization != auth_at_answer

    reread = await evidence.read_trace(
        workspace_id="ws-ae16", answer_ref="answer-policy"
    )
    assert (
        reread["disclosures"][0]["packet"]["authorization"] == auth_at_answer
    )


async def test_partial_page_truth_is_preserved_not_normalized(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    await _publish(factory, commit_service, "ws-ae15")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-ae15"), page_size=2)
    assert first.status == "partial"
    view = await evidence.record_disclosure(
        packet=packet_to_json(first.packet),
        workspace_id="ws-ae15",
        delivery_status=first.status,
    )
    trace = await evidence.commit_trace(
        workspace_id="ws-ae15",
        answer_ref="answer-partial",
        answer_content="Answer over a partial page.",
        disclosure_ids=[view["disclosure_id"]],
    )
    bound = trace["disclosures"][0]
    packet = bound["packet"]
    # Page-level partialness (continuation remains) is preserved as
    # delivery truth; packet-level completeness (nothing omitted) is
    # preserved separately and is not normalized away either.
    assert bound["delivery_status"] == "partial"
    assert packet["status"] == "complete"
    assert packet["budget"]["truncated"] is False
    assert packet["omitted"] == []


# ---------------------------------------------------------------------------
# AE-04..AE-08: assessment separation, states, non-mutation
# ---------------------------------------------------------------------------


async def _committed_trace_with_evidence(payload_env, workspace: str = "ws-aea"):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _service = await _collect_disclosures(
        factory, commit_service, workspace, evidence, page_size=10
    )
    trace = await evidence.commit_trace(
        workspace_id=workspace,
        answer_ref="answer-A",
        answer_content="Claim one is supported. Claim two is a guess.",
        disclosure_ids=[d["disclosure_id"] for d in disclosures],
    )
    return evidence, trace


async def test_trace_without_assessment_is_explicitly_unassessed(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    assert trace["assessment_state"] == "unassessed"
    assert trace["assessments"] == []
    assert trace["current_assessment"] is None


async def test_supported_assessment_is_durable_and_separate(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    locator = _first_locator(trace["disclosures"][0]["packet"])
    answer = trace["answer"]
    updated = await evidence.record_assessment(
        workspace_id=trace["workspace_id"],
        trace_id=trace["trace_id"],
        verdict="supported",
        claims=[
            {
                "claim_id": "c1",
                "span": {"start": 0, "end": 23},
                "verdict": "supported",
                "evidence": [
                    {
                        "disclosure_id": trace["disclosures"][0]["disclosure_id"],
                        **locator,
                    }
                ],
            }
        ],
        assessor={
            "kind": "human",
            "assessor_id": "reviewer-1",
            "procedure": "manual-review",
            "procedure_version": "1",
        },
        assessment_key="review-1",
        rationale="All material claims map to delivered rows.",
    )
    assert updated["assessment_state"] == "supported"
    assert len(updated["assessments"]) == 1
    stored = updated["current_assessment"]
    assert stored["seq"] == 1
    assert stored["verdict"] == "supported"
    assert stored["claims"][0]["claim_id"] == "c1"
    assert stored["assessor"]["kind"] == "human"
    assert answer  # sanity: answer still present alongside the judgment


async def test_unsupported_assessment_keeps_answer_byte_identical(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    body_before = trace["answer"]
    digest_before = trace["answer_digest"]
    updated = await evidence.record_assessment(
        workspace_id=trace["workspace_id"],
        trace_id=trace["trace_id"],
        verdict="unsupported",
        claims=[
            {
                "claim_id": "c2",
                "span": {
                    "start": 24,
                    "end": len(body_before),
                    "quote_digest": answer_content_digest(body_before[24:]),
                },
                "verdict": "unsupported",
                "evidence": [],
                "note": "no delivered row contains this",
            }
        ],
        assessor={
            "kind": "model",
            "assessor_id": "verifier-x",
            "procedure": "claim-align",
            "procedure_version": "0.3",
        },
        assessment_key="verify-1",
    )
    assert updated["assessment_state"] == "unsupported"
    claim = updated["current_assessment"]["claims"][0]
    assert claim["span"]["start"] == 24
    assert claim["verdict"] == "unsupported"
    assert updated["answer"] == body_before
    assert updated["answer_digest"] == digest_before
    # Durable row-level proof: the trace row itself never changed.
    factory = payload_env[0]
    async with factory() as session:
        row = await session.get(KernelAnswerTrace, trace["trace_id"])
        assert row.answer_content == body_before
        assert row.answer_digest == digest_before


async def test_uncertain_is_distinguishable_from_all_other_states(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    updated = await evidence.record_assessment(
        workspace_id=trace["workspace_id"],
        trace_id=trace["trace_id"],
        verdict="uncertain",
        claims=[],
        assessor={
            "kind": "tool",
            "assessor_id": "aligner",
            "procedure": "span-align",
            "procedure_version": "2",
        },
        assessment_key="align-1",
    )
    assert updated["assessment_state"] == "uncertain"
    assert updated["assessment_state"] != "supported"
    assert updated["assessment_state"] != "unsupported"
    fresh = await evidence.read_trace(
        workspace_id=trace["workspace_id"], trace_id=trace["trace_id"]
    )
    assert fresh["assessment_state"] == "uncertain"


# ---------------------------------------------------------------------------
# AE-09/AE-10/AE-14: fail-closed tenancy and references
# ---------------------------------------------------------------------------


async def test_cross_workspace_disclosure_reference_fails_closed(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _s = await _collect_disclosures(
        factory, commit_service, "ws-owner", evidence, page_size=10
    )
    foreign = disclosures[0]["disclosure_id"]
    with pytest.raises(DisclosureReferenceError):
        await evidence.commit_trace(
            workspace_id="ws-other",
            answer_ref="answer-steal",
            answer_content="Attempt to bind another tenant's context.",
            disclosure_ids=[foreign],
        )
    async with factory() as session:
        traces = (
            await session.execute(
                select(KernelAnswerTrace).where(
                    KernelAnswerTrace.workspace_id == "ws-other"
                )
            )
        ).scalars().all()
        assert traces == []


async def test_foreign_and_unknown_disclosure_ids_share_one_error(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _s = await _collect_disclosures(
        factory, commit_service, "ws-owner", evidence, page_size=10
    )
    foreign = disclosures[0]["disclosure_id"]
    unknown = "dsc_does_not_exist"
    errors = []
    for bad in (foreign, unknown):
        with pytest.raises(DisclosureReferenceError) as exc:
            await evidence.commit_trace(
                workspace_id="ws-tenant",
                answer_ref=f"answer-{bad[:8]}",
                answer_content="x",
                disclosure_ids=[bad],
            )
        errors.append(str(exc.value))
    assert errors[0] == errors[1]


async def test_cross_workspace_trace_and_assessment_access_fails_closed(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    _e, trace = await _committed_trace_with_evidence(payload_env, "ws-own")
    with pytest.raises(AnswerEvidenceContractError):
        await evidence.read_trace(
            workspace_id="ws-evil", trace_id=trace["trace_id"]
        )
    with pytest.raises(AnswerEvidenceContractError):
        await evidence.record_assessment(
            workspace_id="ws-evil",
            trace_id=trace["trace_id"],
            verdict="supported",
            claims=[],
            assessor={
                "kind": "rule",
                "assessor_id": "r",
                "procedure": "p",
                "procedure_version": "1",
            },
            assessment_key="k",
        )


async def test_missing_reference_is_explicit_failure_not_empty_trace(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    with pytest.raises(DisclosureReferenceError):
        await evidence.commit_trace(
            workspace_id="ws-x",
            answer_ref="answer-missing",
            answer_content="ghost context",
            disclosure_ids=["dsc_missing"],
        )


# ---------------------------------------------------------------------------
# AE-11/AE-12: idempotency and conflict truth
# ---------------------------------------------------------------------------


async def test_identical_commit_retry_is_idempotent(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _s = await _collect_disclosures(
        factory, commit_service, "ws-ae11", evidence, page_size=10
    )
    ids = [d["disclosure_id"] for d in disclosures]
    answer = "The one true answer."
    first = await evidence.commit_trace(
        workspace_id="ws-ae11", answer_ref="a", answer_content=answer,
        disclosure_ids=ids,
    )
    second = await evidence.commit_trace(
        workspace_id="ws-ae11", answer_ref="a", answer_content=answer,
        disclosure_ids=ids,
    )
    assert second["trace_id"] == first["trace_id"]
    assert second == first
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(KernelAnswerTrace)
                .where(KernelAnswerTrace.answer_ref == "a")
            )
        ).scalar_one()
        assert count == 1


async def test_concurrent_identical_commits_converge(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _s = await _collect_disclosures(
        factory, commit_service, "ws-race", evidence, page_size=10
    )
    ids = [d["disclosure_id"] for d in disclosures]
    results = await asyncio.gather(
        evidence.commit_trace(
            workspace_id="ws-race", answer_ref="a", answer_content="same",
            disclosure_ids=ids,
        ),
        evidence.commit_trace(
            workspace_id="ws-race", answer_ref="a", answer_content="same",
            disclosure_ids=ids,
        ),
    )
    assert results[0]["trace_id"] == results[1]["trace_id"]
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(KernelAnswerTrace)
                .where(KernelAnswerTrace.workspace_id == "ws-race")
            )
        ).scalar_one()
        assert count == 1


async def test_same_answer_ref_with_different_context_conflicts(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _s = await _collect_disclosures(
        factory, commit_service, "ws-ae12", evidence, page_size=2
    )
    assert len(disclosures) >= 2
    d1, d2 = disclosures[0]["disclosure_id"], disclosures[1]["disclosure_id"]
    await evidence.commit_trace(
        workspace_id="ws-ae12", answer_ref="a", answer_content="answer",
        disclosure_ids=[d1, d2],
    )
    # Different order.
    with pytest.raises(AnswerTraceConflictError):
        await evidence.commit_trace(
            workspace_id="ws-ae12", answer_ref="a", answer_content="answer",
            disclosure_ids=[d2, d1],
        )
    # Different set.
    with pytest.raises(AnswerTraceConflictError):
        await evidence.commit_trace(
            workspace_id="ws-ae12", answer_ref="a", answer_content="answer",
            disclosure_ids=[d1],
        )
    # Different body.
    with pytest.raises(AnswerTraceConflictError):
        await evidence.commit_trace(
            workspace_id="ws-ae12", answer_ref="a", answer_content="answer!",
            disclosure_ids=[d1, d2],
        )
    # History untouched by the failed replays.
    view = await evidence.read_trace(workspace_id="ws-ae12", answer_ref="a")
    assert view["answer"] == "answer"
    assert [d["disclosure_id"] for d in view["disclosures"]] == [d1, d2]


# ---------------------------------------------------------------------------
# AE-13: restart durability
# ---------------------------------------------------------------------------


async def test_trace_and_assessments_survive_reopen(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    disclosures, _s = await _collect_disclosures(
        factory, commit_service, "ws-ae13", evidence, page_size=10
    )
    trace = await evidence.commit_trace(
        workspace_id="ws-ae13",
        answer_ref="durable",
        answer_content="Still here after restart.",
        disclosure_ids=[d["disclosure_id"] for d in disclosures],
    )
    await evidence.record_assessment(
        workspace_id="ws-ae13",
        trace_id=trace["trace_id"],
        verdict="uncertain",
        claims=[],
        assessor={
            "kind": "human",
            "assessor_id": "r2",
            "procedure": "manual",
            "procedure_version": "1",
        },
        assessment_key="k1",
    )

    db_path = factory.kw["bind"].url.database
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    try:
        reopened = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        fresh_service = AnswerEvidenceService(reopened)
        view = await fresh_service.read_trace(
            workspace_id="ws-ae13", answer_ref="durable"
        )
        assert view["trace_id"] == trace["trace_id"]
        assert view["answer_digest"] == trace["answer_digest"]
        assert [d["disclosure_id"] for d in view["disclosures"]] == [
            d["disclosure_id"] for d in disclosures
        ]
        assert view["assessment_state"] == "uncertain"
        assert view["assessments"][0]["seq"] == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Assessment adversarial semantics
# ---------------------------------------------------------------------------


async def test_assessment_key_reuse_semantics(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    kwargs = dict(
        workspace_id=trace["workspace_id"],
        trace_id=trace["trace_id"],
        verdict="uncertain",
        claims=[],
        assessor={
            "kind": "human",
            "assessor_id": "r",
            "procedure": "p",
            "procedure_version": "1",
        },
        assessment_key="same-key",
    )
    first = await evidence.record_assessment(**kwargs)
    replay = await evidence.record_assessment(**kwargs)
    assert replay["assessment_id"] == first["assessments"][0]["assessment_id"]
    with pytest.raises(AssessmentConflictError):
        await evidence.record_assessment(
            **{**kwargs, "rationale": "a different judgment"}
        )


async def test_concurrent_assessors_append_deterministically(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    supported_claim = {
        "claim_id": "c1",
        "span": {"start": 0, "end": 23},
        "verdict": "supported",
        "evidence": [
            {
                "disclosure_id": trace["disclosures"][0]["disclosure_id"],
                **_first_locator(trace["disclosures"][0]["packet"]),
            }
        ],
    }
    base = dict(
        workspace_id=trace["workspace_id"],
        trace_id=trace["trace_id"],
    )
    await asyncio.gather(
        evidence.record_assessment(
            **base,
            verdict="supported",
            claims=[supported_claim],
            assessor={"kind": "human", "assessor_id": "a", "procedure": "p", "procedure_version": "1"},
            assessment_key="k-a",
        ),
        evidence.record_assessment(
            **base,
            verdict="uncertain",
            claims=[],
            assessor={"kind": "model", "assessor_id": "b", "procedure": "q", "procedure_version": "1"},
            assessment_key="k-b",
        ),
    )
    view = await evidence.read_trace(
        workspace_id=trace["workspace_id"], trace_id=trace["trace_id"]
    )
    seqs = [a["seq"] for a in view["assessments"]]
    assert sorted(seqs) == [1, 2]
    assert view["current_assessment"]["seq"] == max(seqs)
    assert len({a["assessment_id"] for a in view["assessments"]}) == 2


async def test_assessment_validation_rejects_fabrications(payload_env):
    evidence, trace = await _committed_trace_with_evidence(payload_env)
    ws = trace["workspace_id"]
    trace_id = trace["trace_id"]
    answer = trace["answer"]
    good_assessor = {
        "kind": "human",
        "assessor_id": "r",
        "procedure": "p",
        "procedure_version": "1",
    }

    async def expect_error(verdict, claims):
        with pytest.raises(AnswerEvidenceContractError):
            await evidence.record_assessment(
                workspace_id=ws,
                trace_id=trace_id,
                verdict=verdict,
                claims=claims,
                assessor=good_assessor,
                assessment_key=f"k-{verdict}-{len(claims)}",
            )

    # Bad verdict vocabulary.
    await expect_error("probably_fine", [])
    # Span beyond the committed answer.
    await expect_error(
        "unsupported",
        [{"claim_id": "c", "span": {"start": 0, "end": len(answer) + 5},
          "verdict": "unsupported", "evidence": []}],
    )
    # Quote digest that does not match the stored slice.
    await expect_error(
        "unsupported",
        [{"claim_id": "c", "span": {"start": 0, "end": 6,
          "quote_digest": answer_content_digest("never said this")},
          "verdict": "unsupported", "evidence": []}],
    )
    # Supported verdict with no claims.
    await expect_error("supported", [])
    # Supported claim citing no evidence.
    await expect_error(
        "supported",
        [{"claim_id": "c", "span": {"start": 0, "end": 6},
          "verdict": "supported", "evidence": []}],
    )
    # Duplicate claim ids.
    await expect_error(
        "unsupported",
        [
            {"claim_id": "c", "span": {"start": 0, "end": 6}, "verdict": "unsupported", "evidence": []},
            {"claim_id": "c", "span": {"start": 7, "end": 12}, "verdict": "unsupported", "evidence": []},
        ],
    )
    # Evidence citing a disclosure not bound to the trace.
    await expect_error(
        "unsupported",
        [{"claim_id": "c", "span": {"start": 0, "end": 6}, "verdict": "unsupported",
          "evidence": [{"disclosure_id": "dsc_not_bound", "record_id": "r",
                        "view_id": "v", "node_id": None}]}],
    )
    # Evidence locator that was never delivered in that disclosure.
    await expect_error(
        "unsupported",
        [{"claim_id": "c", "span": {"start": 0, "end": 6}, "verdict": "unsupported",
          "evidence": [{"disclosure_id": trace["disclosures"][0]["disclosure_id"],
                        "record_id": "fabricated", "view_id": "v", "node_id": None}]}],
    )


async def test_trace_input_validation(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    with pytest.raises(AnswerEvidenceContractError):
        await evidence.commit_trace(
            workspace_id="ws", answer_ref=" ", answer_content="x",
            disclosure_ids=[],
        )
    with pytest.raises(AnswerEvidenceContractError):
        await evidence.commit_trace(
            workspace_id="ws", answer_ref="ok", answer_content="   ",
            disclosure_ids=[],
        )
    with pytest.raises(AnswerEvidenceContractError):
        await evidence.commit_trace(
            workspace_id="ws", answer_ref="ok", answer_content="x" * 65_537,
            disclosure_ids=[],
        )


async def test_empty_disclosure_set_is_an_explicit_trace(payload_env):
    factory, _store, commit_service = payload_env
    evidence = AnswerEvidenceService(factory)
    trace = await evidence.commit_trace(
        workspace_id="ws-empty",
        answer_ref="no-context",
        answer_content="Answered with no delivered context.",
        disclosure_ids=[],
    )
    assert trace["disclosures"] == []
    assert trace["assessment_state"] == "unassessed"
