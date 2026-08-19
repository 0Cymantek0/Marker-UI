"""PR80A review/adjudication seam tests (matrix I).

Review decisions resolve/correct/reject unresolved fields without
erasing candidate history, are bound to the exact result/schema/
publication context they reviewed, and are rejected when replayed
against stale context. Reviewers cannot mint evidence: accepting a
field with no grounded candidate fails closed.
"""

from __future__ import annotations

import json

import pytest

from app.extraction.contract import INVOICE_SCHEMA
from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_CORRECTED,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    RUN_REVIEW_REQUIRED,
)
from app.extraction.review import (
    ReviewDecision,
    ReviewError,
    StaleReviewError,
    apply_review,
)
from app.extraction.service import ExtractionService
from app.kernel.commit import KernelCommitBatch
from app.kernel.generations import GenerationService
from app.kernel.publications import PublicationService
from app.kernel.snapshots import resolve_snapshot

from tests.test_extraction_service import (
    GOOD_ROWS,
    _doc,
    _invoice_header_doc,
    _items_doc,
    _publish,
    _run,
    _service,
)

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


async def _review(
    service: ExtractionService, result, field: str, action: str, **overrides
):
    return await service.apply_review(
        ReviewDecision(
            result_identity=result.identity,
            schema_identity=result.schema_identity,
            publication_set_id=result.context.publication_set_id,
            field_path=field,
            action=action,
            reviewer="reviewer@example.test",
            rationale="adjudicated in review",
            **overrides,
        )
    )


async def test_review_accepts_grounded_unresolved_value_and_persist_audit(
    payload_env,
):
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)
    assert result.run_status == RUN_REVIEW_REQUIRED
    assert result.fields["total_due"].status == FIELD_OUTCOME_REVIEW_REQUIRED

    updated = await _review(service, result, "total_due", "accept")

    total = updated.fields["total_due"]
    assert total.status == FIELD_OUTCOME_ACCEPTED
    assert total.value in {"154.97", "777.77"}
    # Candidates and evidence trail survive the decision.
    assert len(total.candidates) == 2
    assert total.review["action"] == "accept"
    assert total.review["bound_result_identity"] == result.identity
    # The original stored result is untouched (append-only history).
    original = await service.load_result(result.identity)
    assert original.fields["total_due"].status == FIELD_OUTCOME_REVIEW_REQUIRED
    # The decision itself is committed as a kernel decision record.
    from app.kernel.models import KernelRecord

    factory = payload_env[0]
    async with factory() as session:
        decision_rows = (
            await session.execute(
                KernelRecord.__table__.select().where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == "decision",
                )
            )
        ).all()
    assert len(decision_rows) == 1
    payload = json.loads(decision_rows[0].payload_json)
    assert payload["outcome"] == "accept"
    assert payload["decision_key"].startswith("extraction-review:")


async def test_review_correction_records_human_sourced_value(payload_env):
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    updated = await _review(
        service, result, "total_due", "correct", value="154.97"
    )
    total = updated.fields["total_due"]
    assert total.status == FIELD_OUTCOME_CORRECTED
    assert total.value == "154.97"
    assert total.candidates  # history preserved
    assert total.review["action"] == "correct"

    # The corrected value is committed as its own reviewed claim with a
    # non-authority-bearing assessment (human source, no proof support).
    from sqlalchemy import select

    from app.kernel.models import KernelRecord

    factory = payload_env[0]
    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecord).where(
                    KernelRecord.workspace_id == WS,
                    KernelRecord.record_class == "claim_assessment",
                )
            )
        ).scalars().all()
    outcomes = {json.loads(row.payload_json).get("outcome") for row in rows}
    assert "accepted_with_warning" in outcomes  # the reviewed correction


async def test_review_rejection_is_auditable_and_value_stops_flowing(payload_env):
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    updated = await _review(service, result, "total_due", "reject")
    total = updated.fields["total_due"]
    assert total.status == "rejected"
    assert total.value is None
    assert total.candidates  # evidence trail intact


async def test_stale_review_after_publication_change_is_rejected(payload_env):
    factory, _store, commit_service = payload_env
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    # Advance authoritative context: new doc + new publication set.
    await commit_service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(
                _doc(
                    "invoice-header-v2",
                    _invoice_header_doc(total="200.00"),
                    "rev-h2",
                ),
            ),
        )
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, WS)
    )
    await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )

    with pytest.raises(StaleReviewError, match="no longer active"):
        await _review(service, result, "total_due", "accept")


async def test_review_decision_bound_to_other_result_is_rejected(payload_env):
    await _publish_conflict(payload_env)
    service = _service(payload_env)
    result = await _run(service)

    decision = ReviewDecision(
        result_identity="sha256:" + "0" * 64,
        schema_identity=result.schema_identity,
        publication_set_id=result.context.publication_set_id,
        field_path="total_due",
        action="accept",
        reviewer="reviewer@example.test",
        rationale="recorded against a different result",
    )
    with pytest.raises(KeyError, match="no stored extraction result"):
        # An unknown result identity cannot be reviewed at all.
        await service.apply_review(decision)


async def test_cannot_accept_a_field_with_no_grounded_candidate(payload_env):
    """A reviewer cannot mint evidence by fiat (grounding rule)."""
    from app.extraction.results import FieldOutcome

    decision = ReviewDecision(
        result_identity="sha256:" + "1" * 64,
        schema_identity=INVOICE_SCHEMA.identity,
        publication_set_id="pub-1",
        field_path="ghost",
        action="accept",
        reviewer="reviewer@example.test",
        rationale="attempt to verify without evidence",
    )
    with pytest.raises(ReviewError, match="grounded"):
        apply_review(
            FieldOutcome(status=FIELD_OUTCOME_REVIEW_REQUIRED, candidates=()),
            decision,
            result_identity=decision.result_identity,
            schema_identity=decision.schema_identity,
            publication_set_id=decision.publication_set_id,
        )


async def test_review_actions_are_versioned_and_shape_checked():
    with pytest.raises(ReviewError, match="invalid review action"):
        ReviewDecision(
            result_identity="sha256:" + "2" * 64,
            schema_identity=INVOICE_SCHEMA.identity,
            publication_set_id="pub-1",
            field_path="total_due",
            action="approve",
            reviewer="r",
            rationale="bogus action",
        )
    with pytest.raises(ReviewError, match="correction must carry"):
        ReviewDecision(
            result_identity="sha256:" + "2" * 64,
            schema_identity=INVOICE_SCHEMA.identity,
            publication_set_id="pub-1",
            field_path="total_due",
            action="correct",
            reviewer="r",
            rationale="no value",
        )
