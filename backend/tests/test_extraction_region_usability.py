"""Extraction-seam two-region differential usability (PR88, invariant 22).

The invoice workload runs against a publication where one region
(invoice_number) is grounded and another (total_due) is a live conflict.
The executable properties:

* the accepted region is committed as a kernel claim and stays usable;
* the review-required region never reaches the claim layer without
  review (the authoritative acceptance path cannot be bypassed);
* adjudicating the review-required region does not disturb the accepted
  region's committed identity;
* run-level escalation to ``review_required`` is honest reporting, not
  a poisoning of the usable region.

Everything runs through the real publication/query/extraction/review
seams — no mocked packets, no synthetic review state.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    RUN_REVIEW_REQUIRED,
    USABLE_FIELD_OUTCOMES,
)
from app.extraction.review import ReviewDecision
from app.extraction.service import (
    _assertion_record_id,
    result_record_id,
)
from app.kernel.models import KernelRecord as KernelRecordRow

from tests.test_extraction_service import (
    _invoice_header_doc,
    _items_doc,
    _publish,
    _run,
    _service,
)
from tests.test_extraction_service import GOOD_ROWS

pytestmark = pytest.mark.asyncio

WS = "ws-extract"


async def _publish_conflict(payload_env) -> None:
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


async def _claim_key_exists(factory, claim_key: str) -> bool:
    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecordRow.payload_json).where(
                    KernelRecordRow.workspace_id == WS,
                    KernelRecordRow.record_class == "claim_assertion",
                )
            )
        ).all()
    return any(json.loads(payload)["claim_key"] == claim_key for payload, in rows)


async def test_accepted_region_usable_while_conflicted_region_requires_review(
    payload_env,
):
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    # Region A (grounded): accepted and usable.
    invoice_number = result.fields["invoice_number"]
    assert invoice_number.status == FIELD_OUTCOME_ACCEPTED
    assert invoice_number.value == "INV-2026-042"
    assert invoice_number.status in USABLE_FIELD_OUTCOMES

    # Region B (conflict): explicitly not usable — review required.
    total_due = result.fields["total_due"]
    assert total_due.status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert total_due.status not in USABLE_FIELD_OUTCOMES

    # Run-level escalation is honest reporting about B, not a collapse
    # of A: A's per-field state is unchanged by B's conflict.
    assert result.run_status == RUN_REVIEW_REQUIRED
    assert result.fields["invoice_number"].status == FIELD_OUTCOME_ACCEPTED


async def test_review_required_region_cannot_reach_claim_layer_without_review(
    payload_env,
):
    """The bypass probe: no acceptance path exists around review."""
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    # The grounded region IS committed as a kernel claim.
    assert await _claim_key_exists(payload_env[0], "demo.invoice@invoice_number")

    # The conflicted region is NOT committed: the only authoritative
    # acceptance path (_persist_result) admits accepted fields alone, so
    # a review-required value cannot silently become a claim.
    assert not await _claim_key_exists(payload_env[0], "demo.invoice@total_due")

    # A reviewer cannot mint evidence by fiat either: accepting an
    # ungrounded field fails closed at the review seam.
    from app.extraction.results import FieldOutcome
    from app.extraction.review import ReviewError, apply_review

    decision = ReviewDecision(
        result_identity=result.identity,
        schema_identity=result.schema_identity,
        publication_set_id=result.context.publication_set_id,
        field_path="ghost",
        action="accept",
        reviewer="reviewer@example.test",
        rationale="bypass attempt without evidence",
    )
    with pytest.raises(ReviewError, match="grounded"):
        apply_review(
            FieldOutcome(status=FIELD_OUTCOME_REVIEW_REQUIRED, candidates=()),
            decision,
            result_identity=decision.result_identity,
            schema_identity=decision.schema_identity,
            publication_set_id=decision.publication_set_id,
        )


async def test_adjudicating_conflicted_region_leaves_accepted_region_stable(
    payload_env,
):
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    accepted_assertion_id = _assertion_record_id(
        "demo.invoice@invoice_number", "INV-2026-042"
    )
    async with payload_env[0]() as session:
        before = await session.get(KernelRecordRow, accepted_assertion_id)
    assert before is not None
    before_payload = before.payload_json

    updated = await service.apply_review(
        ReviewDecision(
            result_identity=result.identity,
            schema_identity=result.schema_identity,
            publication_set_id=result.context.publication_set_id,
            field_path="total_due",
            action="accept",
            reviewer="reviewer@example.test",
            rationale="adjudicated in review",
        )
    )

    # B became usable through the review authority path…
    assert updated.fields["total_due"].status == FIELD_OUTCOME_ACCEPTED
    assert updated.fields["total_due"].review["action"] == "accept"
    # …while A's committed claim is untouched (append-only history).
    async with payload_env[0]() as session:
        after = await session.get(KernelRecordRow, accepted_assertion_id)
    assert after.payload_json == before_payload

    # The original result (B unresolved) remains historically readable.
    original = await service.load_result(result.identity)
    assert original.fields["total_due"].status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert result_record_id(original.identity) != result_record_id(updated.identity)


async def test_bounded_consumer_view_separates_region_states(payload_env):
    """A bounded consumer sees per-region status, never a doc Boolean."""
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    view = {
        name: {
            "status": outcome.status,
            "usable": outcome.status in USABLE_FIELD_OUTCOMES,
            "value": outcome.value if outcome.status in USABLE_FIELD_OUTCOMES else None,
        }
        for name, outcome in result.fields.items()
    }
    assert view["invoice_number"] == {
        "status": "accepted",
        "usable": True,
        "value": "INV-2026-042",
    }
    assert view["total_due"] == {
        "status": "review_required",
        "usable": False,
        "value": None,
    }
    # Asking only for the unresolved region yields its own state; the
    # accepted neighbor is not smuggled in as evidence for it.
    only_b = {name: state for name, state in view.items() if not state["usable"]}
    assert set(only_b) == {"total_due"}
