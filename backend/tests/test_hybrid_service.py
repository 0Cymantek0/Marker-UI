"""Hybrid extraction service end-to-end tests (bridge workstreams D + E).

Real kernel, real publication, real query path — the only scripted
piece is the specialist provider transport, which is exactly what the
replay provider makes honest in production benchmarks. These tests pin
the integration contract:

* the lane is opt-in: services without one keep byte-identical PR80A
  results;
* corroborated values commit a full hybrid-attributed claim/assessment/
  proof graph over REAL source records;
* proposal-only values commit no authority at all and survive result
  persistence/reload for review;
* lane failures fall back to the deterministic result honestly;
* replayed responses are deterministic in identity; materially
  different responses are distinguishable;
* a human can never accept a proposal into source authority.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from app.extraction.contract import INVOICE_SCHEMA
from app.extraction.provider import OpenAICompatProvider
from app.extraction.results import PROPOSAL_UNPROVED_REVIEW
from app.extraction.service import (
    CORROBORATION_AUTHORITY_RULE,
    ExtractionService,
    result_record_id,
)
from app.extraction.specialist import (
    LANE_CONTEXT_REFUSED,
    LANE_OK,
    LANE_OUTPUT_CONTRACT_FAILURE,
    LANE_PROVIDER_FAILURE,
    SPECIALIST_ROUTE,
    SpecialistLane,
    SpecialistLaneResult,
)
from app.kernel.models import KernelRecord

from tests.test_extraction_service import WS, _publish, _run, _service

pytestmark = pytest.mark.asyncio

EU_ROWS = [
    "LINEITEM | SKU-1 | Widget | 2 | 9,99 | 19,98",
    "LINEITEM | SKU-2 | Gadget | 1 | 15,00 | 15,00",
]


def _eu_header_doc(*, po: str | None = "PO-77") -> dict[str, str]:
    texts = {
        "h1": "Invoice Number: INV-2026-042",
        "h2": "Invoice Date: March 15, 2026",
        "h3": "Currency: US Dollars",
        "h4": "Total Due: 34,98",
    }
    if po is not None:
        texts["h5"] = f"PO Number: {po}"
    return texts


def _items_doc(rows: list[str]) -> dict[str, str]:
    return {f"r{index}": row for index, row in enumerate(rows, start=1)}


def _specialist_content(
    *,
    total: str = "34.98",
    po: str | None = "PO-77",
    date: str = "2026-03-15",
    currency: str = "USD",
    invoice_number: str = "INV-2026-042",
    extra_items: list[dict[str, Any]] | None = None,
) -> str:
    items = [
        {
            "identity": {"sku": "SKU-1"},
            "fields": {
                "description": "Widget",
                "quantity": "2",
                "unit_price": "9.99",
                "amount": "19.98",
            },
        },
        {
            "identity": {"sku": "SKU-2"},
            "fields": {
                "description": "Gadget",
                "quantity": "1",
                "unit_price": "15.00",
                "amount": "15.00",
            },
        },
    ]
    if extra_items:
        items.extend(extra_items)
    return json.dumps(
        {
            "contract_version": "marker.specialist.output.v1",
            "fields": {
                "invoice_number": invoice_number,
                "invoice_date": date,
                "currency": currency,
                "po_number": po,
                "total_due": total,
            },
            "items": items,
            "flags": [],
        }
    )


def _scripted_provider(content: str) -> OpenAICompatProvider:
    def transport(payload: dict[str, Any]) -> tuple[int, str]:
        return (
            200,
            json.dumps(
                {
                    "model": "m1",
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 321, "completion_tokens": 65},
                }
            ),
        )

    return OpenAICompatProvider(
        model="m1", transport=transport, api_key="sk-test", sleep=lambda _s: None
    )


def _hybrid_service(payload_env, content: str) -> ExtractionService:
    factory, _store, commit_service = payload_env
    lane = SpecialistLane(_scripted_provider(content))
    return ExtractionService(
        factory, commit_service, workspace_id=WS, specialist=lane
    )


async def _publish_eu(payload_env, *, po: str | None = "PO-77"):
    factory, _store, commit_service = payload_env
    return await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _eu_header_doc(po=po), "rev-h1"),
            ("invoice-items", _items_doc(EU_ROWS), "rev-i1"),
        ],
    )


async def _committed_payloads(factory, record_class: str) -> list[tuple[str, dict]]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == record_class,
                )
            )
        ).scalars().all()
    return [(row.id, json.loads(row.payload_json)) for row in rows]


async def test_hybrid_disabled_by_default_keeps_pr80a_result(payload_env):
    await _publish_eu(payload_env)
    service = _service(payload_env)
    result = await _run(service)
    # Normalization-blind document: strict parser refuses EU decimals,
    # the deterministic route alone cannot accept them.
    assert result.specialist is None
    assert result.fields["total_due"].status != "accepted"
    assert result.fields["total_due"].proposals == ()


async def test_corroborated_values_commit_hybrid_proof_graph(payload_env):
    factory = payload_env[0]
    await _publish_eu(payload_env)
    service = _hybrid_service(payload_env, _specialist_content())
    result = await _run(service)

    assert result.specialist is not None
    assert result.specialist.status == LANE_OK
    assert result.specialist.runtime is not None
    assert result.specialist.runtime.prompt_tokens == 321

    total = result.fields["total_due"]
    assert total.status == "accepted"
    assert total.value == "34.98"
    assert total.rule == "hybrid.corroboration.deterministic_normalization.v1"
    assert total.proposals[0].disposition == "corroborated"
    assert result.fields["invoice_date"].value == "2026-03-15"
    assert result.fields["currency"].value == "USD"
    row = result.line_items["items"][0]
    assert row.status == "accepted"
    assert row.fields["unit_price"].value == "9.99"
    assert result.invariants[0].finding == "satisfied"
    assert result.run_status == "accepted"

    assertions = await _committed_payloads(factory, "claim_assertion")
    assessments = await _committed_payloads(factory, "claim_assessment")
    supports = await _committed_payloads(factory, "proof_support")

    total_assertions = [
        (rid, a) for rid, a in assertions if a["predicate"] == "total_due"
    ]
    assert total_assertions and total_assertions[0][1]["value"] == "34.98"
    assertion_id = total_assertions[0][0]
    assessment_id, total_assessment = next(
        (rid, a) for rid, a in assessments if a["assertion_ref"] == assertion_id
    )
    assert total_assessment["outcome"] == "source_exact"
    assert total_assessment["policy"]["revision"] == "v1+v1"
    assert total_assessment["policy"]["policy_id"] == "marker.extraction.reconcile"
    assert total_assessment["workflow_class"] == "marker.extraction.hybrid.v1"
    assert total_assessment["declared_context"]["hybrid_rule"] == (
        "hybrid.corroboration.deterministic_normalization.v1"
    )
    total_supports = [
        s for rid, s in supports if rid == assessment_id or s.get("holder_ref") == assessment_id
    ]
    assert total_supports
    assert {s["authority_rule"] for s in total_supports} == {
        CORROBORATION_AUTHORITY_RULE
    }
    # the proof ends at real source records, not at the model
    evidence_ids = set(total_assessment["evidence_refs"])
    assert evidence_ids == {s["evidence_ref"] for s in total_supports}
    assert all(ref.startswith("invoice-") for ref in evidence_ids)


async def test_proposal_only_value_commits_no_authority_and_survives_reload(
    payload_env,
):
    factory = payload_env[0]
    await _publish_eu(payload_env, po=None)  # no PO line in the source
    service = _hybrid_service(payload_env, _specialist_content())
    result = await _run(service)

    po = result.fields["po_number"]
    assert po.status == "review_required"
    assert po.value is None
    assert po.proposals[0].typed_value == "PO-77"
    assert po.proposals[0].disposition == PROPOSAL_UNPROVED_REVIEW
    assert po.proposals[0].producer_id == "openai-compatible:m1"

    assertions = await _committed_payloads(factory, "claim_assertion")
    assert all(a["predicate"] != "po_number" for _rid, a in assertions)
    assessments = await _committed_payloads(factory, "claim_assessment")
    assert assessments  # other accepted fields did commit claims


    reloaded = await service.load_result(result.identity)
    assert reloaded.fields["po_number"].proposals == po.proposals
    assert reloaded.specialist is not None
    assert reloaded.specialist.producer_id == "openai-compatible:m1"
    assert reloaded.specialist.provenance.route == SPECIALIST_ROUTE
    assert reloaded.identity == result.identity


async def test_provider_failure_falls_back_to_deterministic_result(payload_env):
    await _publish_eu(payload_env)

    def transport(payload):
        return 503, "provider down"

    provider = OpenAICompatProvider(
        model="m1", transport=transport, api_key="sk-test", sleep=lambda _s: None
    )
    factory, _store, commit_service = payload_env
    service = ExtractionService(
        factory,
        commit_service,
        workspace_id=WS,
        specialist=SpecialistLane(provider),
    )
    result = await _run(service)
    assert result.specialist.status == LANE_PROVIDER_FAILURE
    assert result.specialist.proposal_count == 0
    # deterministic evidence that already succeeded is not erased
    assert result.fields["invoice_number"].status == "accepted"
    assert result.fields["total_due"].status != "accepted"
    assert all(not out.proposals for out in result.fields.values())


async def test_malformed_output_is_contract_failure_with_evidence_intact(
    payload_env,
):
    await _publish_eu(payload_env)
    service = _hybrid_service(payload_env, "this is not json")
    result = await _run(service)
    assert result.specialist.status == LANE_OUTPUT_CONTRACT_FAILURE
    assert result.specialist.proposal_count == 0
    assert result.fields["invoice_number"].status == "accepted"
    assert all(not out.proposals for out in result.fields.values())


async def test_replayed_response_is_identity_stable_and_change_is_distinguishable(
    payload_env,
):
    factory = payload_env[0]
    await _publish_eu(payload_env)
    service = _hybrid_service(payload_env, _specialist_content())
    first = await _run(service)
    second = await _run(service)
    assert first.identity == second.identity

    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == "native_object",
                    KernelRecord.id.startswith("extraction.result."),
                )
            )
        ).scalars().all()
    assert len(rows) == 1  # idempotent replay did not duplicate

    changed = _hybrid_service(payload_env, _specialist_content(total="99.99"))
    third = await _run(changed)
    assert third.identity != first.identity
    reloaded_first = await service.load_result(first.identity)
    assert reloaded_first.fields["po_number"].proposals == (
        first.fields["po_number"].proposals
    )


async def test_stale_lane_context_is_refused(payload_env):
    await _publish_eu(payload_env)
    service = _hybrid_service(payload_env, _specialist_content())
    lane = service._specialist
    original_generate = lane.generate

    def stale_generate(packet, schema, *, workspace_id):
        result = original_generate(packet, schema, workspace_id=workspace_id)
        stale_provenance = type(result.provenance)(
            **{
                **result.provenance.to_dict(),
                "publication_set_id": "pub-STALE",
            }
        )
        return SpecialistLaneResult(
            status=result.status,
            producer_id=result.producer_id,
            producer_family=result.producer_family,
            config_identity=result.config_identity,
            provenance=stale_provenance,
            proposals=result.proposals,
            runtime=result.runtime,
        )

    service_hybrid = type(service)(
        service._session_factory,
        service._commit_service,
        workspace_id=WS,
        specialist=_StaleLane(lane, stale_generate),
    )
    result = await _run(service_hybrid)
    assert result.specialist.status == LANE_CONTEXT_REFUSED
    assert result.specialist.proposal_count == 0
    # no proposal attached anywhere: pure PR80A semantics under refusal
    assert all(not out.proposals for out in result.fields.values())


class _StaleLane:
    """Lane wrapper that reports a stale context binding."""

    def __init__(self, inner, generate_fn) -> None:
        self._inner = inner
        self._generate_fn = generate_fn

    def generate(self, packet, schema, *, workspace_id):
        return self._generate_fn(packet, schema, workspace_id=workspace_id)


async def test_review_cannot_accept_a_proposal_into_source_authority(payload_env):
    await _publish_eu(payload_env, po=None)
    service = _hybrid_service(payload_env, _specialist_content())
    result = await _run(service)
    assert result.fields["po_number"].status == "review_required"

    from app.extraction.review import ReviewDecision, ReviewError

    accept = ReviewDecision(
        result_identity=result.identity,
        schema_identity=result.schema_identity,
        publication_set_id=result.context.publication_set_id,
        field_path="po_number",
        action="accept",
        reviewer="human-1",
        rationale="looks right",
    )
    with pytest.raises(ReviewError, match="grounded"):
        await service.apply_review(accept)

    correct = ReviewDecision(
        result_identity=result.identity,
        schema_identity=result.schema_identity,
        publication_set_id=result.context.publication_set_id,
        field_path="po_number",
        action="correct",
        reviewer="human-1",
        rationale="typed from the scanned cover page",
        value="PO-77",
    )
    corrected = await service.apply_review(correct)
    assert corrected.fields["po_number"].status == "corrected"
    factory = payload_env[0]
    assessments = await _committed_payloads(factory, "claim_assessment")
    warning = [
        a
        for _rid, a in assessments
        if a["outcome"] == "accepted_with_warning"
        and a["declared_context"].get("result_identity") == result.identity
    ]
    assert warning  # human-sourced correction, never source authority


async def test_result_record_round_trips_lane_report_runtime(payload_env):
    await _publish_eu(payload_env)
    service = _hybrid_service(payload_env, _specialist_content())
    result = await _run(service)
    async with factory_session(service) as session:
        row = await session.get(KernelRecord, result_record_id(result.identity))
    stored = json.loads(row.payload_json)["properties"]["result"]
    assert stored["specialist"]["runtime"]["prompt_tokens"] == 321
    assert stored["specialist"]["status"] == LANE_OK


class _SessionCtx:
    def __init__(self, service) -> None:
        self._service = service

    async def __aenter__(self):
        self._cm = self._service._session_factory()
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


def factory_session(service):
    return _SessionCtx(service)
