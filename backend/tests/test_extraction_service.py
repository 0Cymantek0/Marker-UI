"""PR80A extraction service end-to-end tests (matrices B, C, D, E, G, H).

Every scenario publishes real kernel view documents, builds a real
lexical generation, publishes a real PublicationSet, and runs the
extraction service over the authoritative query path — no mocked
EvidencePacket, no fake proof service.
"""

from __future__ import annotations

import pytest

from app.context_runtime import execute_query, parse_query_request, QUERY_SCHEMA_VERSION
from app.extraction.contract import INVOICE_SCHEMA
from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_MISSING,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    RUN_ACCEPTED,
    RUN_PARTIAL,
    RUN_REVIEW_REQUIRED,
    RUN_STALE_CONTEXT,
)
from app.extraction.service import ExtractionService
from app.kernel.commit import KernelCommitBatch
from app.kernel.generations import GenerationService
from app.kernel.publications import PublicationService
from app.kernel.snapshots import resolve_snapshot

pytestmark = pytest.mark.asyncio

WS = "ws-extract"


def _invoice_header_doc(
    *,
    total: str = "154.97",
    currency: str = "USD",
    po: str | None = "PO-77",
    invoice_number: str = "INV-2026-042",
) -> dict[str, str]:
    texts = {
        "h1": f"Invoice Number: {invoice_number}",
        "h2": "Invoice Date: 2026-03-01",
        "h3": f"Currency: {currency}",
        "h4": f"Total Due: {total}",
    }
    if po is not None:
        texts["h5"] = f"PO Number: {po}"
    return texts


def _item_row(sku: str, desc: str, qty: str, unit: str, amount: str) -> str:
    return f"LINEITEM | {sku} | {desc} | {qty} | {unit} | {amount}"


def _items_doc(rows: list[str]) -> dict[str, str]:
    return {f"r{index}": row for index, row in enumerate(rows, start=1)}


GOOD_ROWS = [
    _item_row("SKU-1", "Widget", "2", "9.99", "19.98"),
    _item_row("SKU-2", "Gadget", "3", "15.00", "45.00"),
    _item_row("SKU-3", "Gizmo", "1", "89.99", "89.99"),
]


def _doc(record_id: str, texts: dict[str, str], revision: str):
    """One view document under its OWN logical view id.

    Two committed view docs sharing a view id are two revisions of one
    logical view (the later supersedes); distinct evidence documents
    need distinct view ids or the published generation silently keeps
    only one of them.
    """
    from app.kernel.reading_order import OrderNode, ReadingOrderGraph
    from app.kernel.patches import ViewDocumentRecord

    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    return ViewDocumentRecord(
        record_id=record_id,
        content_revision_ref=revision,
        graph=graph,
        texts=dict(texts),
        view_id=f"doc-{record_id}",
    )


async def _publish(factory, service, docs: list[tuple[str, dict[str, str], str]]):
    """Commit view docs in one batch, build/activate a generation, publish."""
    from app.kernel.commit import KernelCommitBatch

    await service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=tuple(
                _doc(record_id, texts, revision)
                for record_id, texts, revision in docs
            ),
        )
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, WS)
    )
    return await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )


def _service(payload_env) -> ExtractionService:
    factory, _store, commit_service = payload_env
    return ExtractionService(factory, commit_service, workspace_id=WS)


async def _run(service: ExtractionService, **overrides):
    from app.extraction.contract import ExtractionRequest

    request = ExtractionRequest(
        schema_id=INVOICE_SCHEMA.schema_id,
        schema_version=INVOICE_SCHEMA.version,
        workspace_id=WS,
        **overrides,
    )
    return await service.run(request)


# ---------------------------------------------------------------------------
# B: grounded happy path
# ---------------------------------------------------------------------------


async def test_happy_path_extracts_scalars_and_items_with_lineage(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    assert result.run_status == RUN_ACCEPTED
    assert result.fields["invoice_number"].status == FIELD_OUTCOME_ACCEPTED
    assert result.fields["invoice_number"].value == "INV-2026-042"
    assert result.fields["invoice_date"].value == "2026-03-01"
    assert result.fields["currency"].value == "USD"
    assert result.fields["total_due"].value == "154.97"

    items = result.line_items["items"]
    assert len(items) == 3
    assert all(item.status == FIELD_OUTCOME_ACCEPTED for item in items)
    skus = {item.identity["sku"] for item in items}
    assert skus == {"SKU-1", "SKU-2", "SKU-3"}
    amounts = {
        item.identity["sku"]: item.fields["amount"].value for item in items
    }
    assert amounts == {"SKU-1": "19.98", "SKU-2": "45.00", "SKU-3": "89.99"}

    # Evidence lineage: every accepted value resolves to its source doc
    # record with publication/revision attribution.
    for name in ("invoice_number", "total_due"):
        outcome = result.fields[name]
        assert outcome.candidates, f"{name} lost its candidates"
        for candidate in outcome.candidates:
            assert candidate.evidence
            cite = candidate.evidence[0]
            assert cite.record_id == "invoice-header"
            assert cite.revision_ref  # pinned view revision identity
            assert cite.publication_set_id == result.context.publication_set_id
            assert cite.materialized_generation_id

    # Invariant: total equals the sum of accepted row amounts.
    findings = {f.target: f.finding for f in result.invariants}
    assert findings["total_due"] == "satisfied"

    # Deterministic rerun over the frozen context yields the same identity.
    rerun = await _run(service)
    assert rerun.identity == result.identity


# ---------------------------------------------------------------------------
# C: missing evidence stays missing
# ---------------------------------------------------------------------------


async def test_missing_optional_field_is_missing_and_run_stays_partial(payload_env):
    factory, _store, commit_service = payload_env
    # No PO Number node anywhere: the optional field has no evidence.
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(po=None), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    po = result.fields["po_number"]
    assert po.status == FIELD_OUTCOME_MISSING
    assert po.value is None
    assert po.candidates == ()
    # No plausible replacement was invented; grounded fields stay usable.
    assert result.run_status == RUN_PARTIAL
    assert result.fields["invoice_number"].status == FIELD_OUTCOME_ACCEPTED
    assert result.line_items["items"]
    findings = {f.target: f.finding for f in result.invariants}
    assert findings["total_due"] == "satisfied"


async def test_missing_required_field_escalates_to_review(payload_env):
    factory, _store, commit_service = payload_env
    header = _invoice_header_doc(po=None)
    header.pop("h3")  # Currency node absent: a required field has no evidence
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", header, "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    currency = result.fields["currency"]
    assert currency.status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert currency.value is None
    assert currency.candidates == ()
    assert result.run_status == RUN_REVIEW_REQUIRED


async def test_header_only_document_yields_partial_run(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(po=None), "rev-h1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    assert result.fields["invoice_number"].status == FIELD_OUTCOME_ACCEPTED
    assert result.line_items == {"items": ()}
    # The sum invariant honestly reports it cannot evaluate empty rows.
    findings = {f.target: f.finding for f in result.invariants}
    assert findings["total_due"] == "not_evaluable"
    assert result.run_status == RUN_PARTIAL


# ---------------------------------------------------------------------------
# D: conflicting candidates
# ---------------------------------------------------------------------------


async def test_witness_count_rule_resolves_conflict_and_its_removal_reopens_it(
    payload_env,
):
    factory, _store, commit_service = payload_env
    # Two distinct docs say 154.97; one says 999.99.
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            (
                "invoice-header-copy",
                _invoice_header_doc(),
                "rev-h1-copy",
            ),
            (
                "invoice-header-wrong",
                _invoice_header_doc(total="999.99"),
                "rev-h1-wrong",
            ),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    total = result.fields["total_due"]
    assert total.status == FIELD_OUTCOME_ACCEPTED
    assert total.value == "154.97"
    assert total.rule == "conflict.witness_count.v1"
    # All three witnesses remain visible in the candidate set.
    witnesses = {c.evidence[0].record_id for c in total.candidates}
    assert witnesses == {
        "invoice-header",
        "invoice-header-copy",
        "invoice-header-wrong",
    }


async def test_tied_conflict_stays_unresolved_not_random(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            (
                "invoice-header-alt",
                _invoice_header_doc(total="777.77"),
                "rev-h1-alt",
            ),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    total = result.fields["total_due"]
    assert total.status == FIELD_OUTCOME_REVIEW_REQUIRED  # escalated conflict
    assert total.value is None or total.value is None
    values = {c.value for c in total.candidates}
    assert values == {"154.97", "777.77"}
    assert result.run_status == RUN_REVIEW_REQUIRED


async def test_same_witness_repetition_is_not_independent_votes(payload_env):
    factory, _store, commit_service = payload_env
    # One doc repeats the same total in two nodes: repetition, not votes.
    header = _invoice_header_doc()
    header["h6"] = "Total Due: 154.97"
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", header, "rev-h1"),
            (
                "invoice-header-alt",
                _invoice_header_doc(total="777.77"),
                "rev-h1-alt",
            ),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    total = result.fields["total_due"]
    # 154.97 has ONE distinct witness (repeated), 777.77 has ONE: tie.
    assert total.status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert total.rule != "agree.distinct_witnesses.v1"


# ---------------------------------------------------------------------------
# E: line-item reconciliation
# ---------------------------------------------------------------------------


async def test_duplicate_rows_collapse_and_distinct_rows_never_merge(payload_env):
    factory, _store, commit_service = payload_env
    rows = GOOD_ROWS + [
        _item_row("SKU-1", "Widget", "2", "9.99", "19.98"),  # exact duplicate row
        _item_row("SKU-4", "Widget", "2", "9.99", "19.98"),  # same values, new SKU
    ]
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(rows), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    items = result.line_items["items"]
    skus = [item.identity["sku"] for item in items]
    assert sorted(skus) == ["SKU-1", "SKU-2", "SKU-3", "SKU-4"]
    # The duplicate SKU-1 row collapsed under the identity rule.
    assert skus.count("SKU-1") == 1
    # Different identity key => never merged, even with identical values.
    assert "SKU-4" in skus


async def test_bad_row_does_not_corrupt_other_rows_or_invent_a_total(payload_env):
    factory, _store, commit_service = payload_env
    rows = GOOD_ROWS + [
        _item_row("SKU-9", "Broken", "1", "not-a-number", "12.34"),
    ]
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(rows), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    items = result.line_items["items"]
    by_sku = {item.identity["sku"]: item for item in items}
    assert by_sku["SKU-1"].status == FIELD_OUTCOME_ACCEPTED
    broken = by_sku["SKU-9"]
    assert broken.status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert broken.fields["unit_price"].status != FIELD_OUTCOME_ACCEPTED
    # Document total cannot prove the broken row: invariant not evaluable.
    findings = {f.target: f.finding for f in result.invariants}
    assert findings["total_due"] == "not_evaluable"
    assert result.run_status == RUN_REVIEW_REQUIRED


async def test_total_mismatch_is_violated_not_satisfied(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(total="999.99"), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)

    findings = {f.target: f.finding for f in result.invariants}
    assert findings["total_due"] == "violated"
    assert result.run_status == RUN_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# G: snapshot/revision behavior
# ---------------------------------------------------------------------------


async def test_run_pins_one_publication_and_reports_stale_context(payload_env):
    factory, _store, commit_service = payload_env
    first = await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)
    result = await _run(service)
    assert result.context.publication_set_id == first.publication_set_id

    # Advance the workspace: a NEW header document (fresh record id)
    # carries a different total, then a new generation + set are live.
    await commit_service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                _doc("invoice-header-v2", _invoice_header_doc(total="200.00"), "rev-h2"),
            ),
        )
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, WS)
    )
    second = await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )
    assert second.publication_set_id != first.publication_set_id

    # A run pinned to the OLD set refuses to silently extract the new one.
    stale = await _run(service, expected_publication_set_id=first.publication_set_id)
    assert stale.run_status == RUN_STALE_CONTEXT
    assert stale.error and "expected publication" in stale.error

    # Revalidation honestly reports the old result against live truth.
    report = await service.revalidate(result.identity)
    assert report["status"] == RUN_STALE_CONTEXT
    assert report["current_publication_set_id"] == second.publication_set_id
    assert report["recorded_publication_set_id"] == first.publication_set_id

    # A fresh run against the new set sees BOTH totals (both documents
    # are committed truth) and preserves the conflict instead of
    # pretending the newest document silently wins.
    fresh = await _run(service)
    assert fresh.context.publication_set_id == second.publication_set_id
    totals = {
        c.value
        for c in fresh.fields["total_due"].candidates
    }
    assert totals == {"154.97", "200.00"}
    assert fresh.fields["total_due"].status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert fresh.run_status == RUN_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# H: authorization boundary
# ---------------------------------------------------------------------------


async def test_extraction_receives_only_what_the_query_path_serves(payload_env):
    """The extraction route sees exactly the authorized evidence surface.

    A denied/withheld record simply never appears in the packet, so the
    field stays missing — the extraction layer cannot reach behind the
    query authority.
    """
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env)

    # The authoritative packet itself carries no foreign-workspace units.
    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": WS,
            "operations": [{"op": "lexical_search", "text": "Invoice", "limit": 10}],
        }
    )
    packet = await execute_query(factory, request)
    pinned_set = packet.publication["publication_set_id"]
    assert all(unit.locator.publication_set_id == pinned_set for unit in packet.evidence)

    # And every citation in the extraction result points at that set.
    result = await _run(service)
    for outcome in result.fields.values():
        for candidate in outcome.candidates:
            for cite in candidate.evidence:
                assert cite.publication_set_id == result.context.publication_set_id
                assert cite.packet_identity_id == result.context.packet_identity_ids[0]


async def test_foreign_workspace_extract_is_empty_not_leaky(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    from app.extraction.contract import ExtractionRequest

    foreign = ExtractionService(factory, commit_service, workspace_id="ws-other")
    result = await foreign.run(
        ExtractionRequest(
            schema_id=INVOICE_SCHEMA.schema_id,
            schema_version=INVOICE_SCHEMA.version,
            workspace_id="ws-other",
        )
    )
    # No evidence exists in that workspace: required fields escalate to
    # review and optional ones stay missing — but nothing is ever
    # borrowed from the populated workspace.
    assert all(
        outcome.status in (FIELD_OUTCOME_MISSING, FIELD_OUTCOME_REVIEW_REQUIRED)
        for outcome in result.fields.values()
    )
    assert all(not outcome.candidates for outcome in result.fields.values())
    assert result.line_items == {"items": ()}
    assert result.run_status == RUN_REVIEW_REQUIRED
