"""PR85 boundary integration: disclosure minting inside run_agent_query.

Proves the disclosure is minted at the real delivery seam (not by
re-running retrieval later), that existing retrieval clients are
unaffected, and that the adapter surface (commit/read/assess) behaves
with the public error contract.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.agent_answer_evidence import (
    ANSWER_EVIDENCE_SCHEMA_VERSION,
    configure_answer_evidence_runtime,
    read_agent_answer_trace,
    record_agent_answer_assessment,
    record_agent_answer_trace,
    reset_answer_evidence_runtime,
)
from app.agent_query import (
    QUERY_RESULT_SCHEMA_VERSION,
    configure_query_runtime,
    reset_query_runtime,
    run_agent_query,
)
from app.context_runtime import QUERY_SCHEMA_VERSION
from app.errors import UsageError
from app.kernel.models import KernelAnswerTrace, KernelContextDisclosure
from tests.test_context_runtime_service import _publish

pytestmark = pytest.mark.asyncio


@pytest.fixture
def answer_env(payload_env, monkeypatch):
    factory, _store, commit_service = payload_env
    monkeypatch.setenv("MARKER_QUERY_CURSOR_KEY", "pr85-boundary-test-key")
    configure_query_runtime(factory)
    configure_answer_evidence_runtime(factory)
    yield factory, commit_service
    reset_query_runtime()
    reset_answer_evidence_runtime()


def _raw_request(workspace: str = "ws-boundary") -> dict:
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": [{"op": "lexical_search", "text": "needle", "limit": 25}],
    }


async def _collect_via_adapter(workspace: str, page_size: int = 2):
    """Drive one full disclosure chain through the public query adapter."""

    first = await run_agent_query(
        query=_raw_request(workspace), page_size=page_size, disclose=True
    )
    disclosures = []
    if first["disclosure_id"]:
        disclosures.append(first)
    cursor = first["next_cursor"]
    while cursor is not None:
        page = await run_agent_query(
            continuation=cursor,
            workspace_id=workspace,
            page_size=page_size,
            disclose=True,
        )
        if page["disclosure_id"]:
            disclosures.append(page)
        cursor = page["next_cursor"]
        if page["status"] == "complete":
            break
    return disclosures


async def test_disclose_mints_receipt_at_delivery_boundary(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-boundary")
    envelope = await run_agent_query(
        query=_raw_request(), page_size=10, disclose=True
    )
    assert envelope["schema_version"] == QUERY_RESULT_SCHEMA_VERSION
    assert envelope["status"] == "complete"
    disclosure_id = envelope["disclosure_id"]
    assert disclosure_id and disclosure_id.startswith("dsc_")
    json.dumps(envelope)  # transport-serializable

    # The durable row carries the exact delivered packet, not a recomputed
    # one: identity and evidence must match the envelope byte-for-byte.
    async with factory() as session:
        row = await session.get(KernelContextDisclosure, disclosure_id)
        assert row is not None
        assert row.workspace_id == "ws-boundary"
        assert row.delivery_status == "complete"
        assert row.packet_id == envelope["result"]["packet"]["identity_id"]
        stored = json.loads(row.packet_json)
    assert stored == envelope["result"]["packet"]


async def test_full_chain_discloses_every_page_in_order(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-chain")
    pages = await _collect_via_adapter("ws-chain", page_size=2)
    assert len(pages) >= 2
    assert pages[0]["status"] == "partial"
    ids = [p["disclosure_id"] for p in pages]
    assert len(set(ids)) == len(ids)
    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelContextDisclosure).where(
                    KernelContextDisclosure.workspace_id == "ws-chain"
                )
            )
        ).scalars().all()
    by_id = {row.disclosure_id: row for row in rows}
    assert {p["disclosure_id"] for p in pages} == set(by_id)
    partial_seen = any(by_id[i].delivery_status == "partial" for i in ids)
    assert partial_seen, "chain pages must record their partial delivery truth"


async def test_trace_binds_adapter_disclosures_idempotently(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-bind")
    pages = await _collect_via_adapter("ws-bind", page_size=10)
    ids = [p["disclosure_id"] for p in pages]
    first = await record_agent_answer_trace(
        workspace_id="ws-bind",
        answer_ref="turn-1",
        answer="Bound answer.",
        disclosure_ids=ids,
    )
    assert first["schema_version"] == ANSWER_EVIDENCE_SCHEMA_VERSION
    assert first["assessment_state"] == "unassessed"
    replay = await record_agent_answer_trace(
        workspace_id="ws-bind",
        answer_ref="turn-1",
        answer="Bound answer.",
        disclosure_ids=ids,
    )
    assert replay["trace_id"] == first["trace_id"]
    with pytest.raises(UsageError):
        await record_agent_answer_trace(
            workspace_id="ws-bind",
            answer_ref="turn-1",
            answer="Different body.",
            disclosure_ids=ids,
        )


async def test_assessment_via_adapter_judges_without_mutation(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-judge")
    pages = await _collect_via_adapter("ws-judge", page_size=10)
    trace = await record_agent_answer_trace(
        workspace_id="ws-judge",
        answer_ref="turn-1",
        answer="Claim one holds. Claim two does not.",
        disclosure_ids=[p["disclosure_id"] for p in pages],
    )
    packet = trace["disclosures"][0]["packet"]
    unit = packet["evidence"][0]
    judged = await record_agent_answer_assessment(
        workspace_id="ws-judge",
        trace_id=trace["trace_id"],
        verdict="unsupported",
        claims=[
            {
                "claim_id": "c2",
                "span": {"start": 17, "end": 36},
                "verdict": "unsupported",
                "evidence": [],
                "note": "no delivered support",
            },
            {
                "claim_id": "c1",
                "span": {"start": 0, "end": 16},
                "verdict": "supported",
                "evidence": [
                    {
                        "disclosure_id": trace["disclosures"][0]["disclosure_id"],
                        "record_id": unit["record_id"],
                        "view_id": unit["view_id"],
                        "node_id": unit["node_id"],
                    }
                ],
            },
        ],
        assessor={
            "kind": "model",
            "assessor_id": "verifier-1",
            "procedure": "claim-align",
            "procedure_version": "0.3",
        },
        assessment_key="verify-1",
    )
    assert judged["assessment_state"] == "unsupported"
    assert judged["answer"] == trace["answer"]
    assert judged["answer_digest"] == trace["answer_digest"]
    reread = await read_agent_answer_trace(
        workspace_id="ws-judge", trace_id=trace["trace_id"]
    )
    assert reread["current_assessment"]["verdict"] == "unsupported"


async def test_foreign_disclosure_via_adapter_fails_closed(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-a")
    await _publish(factory, commit_service, "ws-b", suffix="-b")
    owned = await _collect_via_adapter("ws-a", page_size=10)
    with pytest.raises(UsageError):
        await record_agent_answer_trace(
            workspace_id="ws-b",
            answer_ref="steal",
            answer="x",
            disclosure_ids=[owned[0]["disclosure_id"]],
        )
    async with factory() as session:
        stolen = (
            await session.execute(
                select(func.count())
                .select_from(KernelAnswerTrace)
                .where(KernelAnswerTrace.workspace_id == "ws-b")
            )
        ).scalar_one()
        assert stolen == 0


async def test_existing_clients_see_no_disclosure_by_default(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-legacy")
    envelope = await run_agent_query(query=_raw_request("ws-legacy"), page_size=10)
    assert "disclosure_id" not in envelope
    assert envelope["status"] == "complete"
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(KernelContextDisclosure)
            )
        ).scalar_one()
        assert count == 0


async def test_failed_outcomes_disclose_nothing(answer_env):
    factory, commit_service = answer_env
    await _publish(factory, commit_service, "ws-fail")
    bad_cursor = await run_agent_query(
        continuation="not-a-real-cursor", workspace_id="ws-fail", disclose=True
    )
    assert bad_cursor["status"] == "invalidated"
    assert bad_cursor["disclosure_id"] is None
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(KernelContextDisclosure)
            )
        ).scalar_one()
        assert count == 0
