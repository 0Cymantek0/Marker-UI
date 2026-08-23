"""Operational review-policy accounting tests (PR88, invariant 26).

Coverage, dwell, outcomes, backlog, stale rejection, bypass refusal,
and replay idempotency are derived from durable transitions committed
by the REAL review seam — nothing here mocks the service, the kernel,
or the publication lifecycle. Clocks are injected, so dwell is exact
and deterministic; no wall-clock sleeps anywhere.
"""

from __future__ import annotations

import copy

import pytest

from app.extraction.reconciliation import (
    RECONCILE_POLICY_ID,
    RECONCILE_POLICY_VERSION,
)
from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_REVIEW_REQUIRED,
)
from app.extraction.review import ReviewDecision, ReviewError, StaleReviewError
from app.extraction.review_ops import (
    REVIEW_OPS_SCHEMA_VERSION,
    REVIEW_TRANSITION_ACCEPTED,
    REVIEW_TRANSITION_REJECTED,
    REVIEW_TRANSITION_REQUIRED,
    ReviewOpsError,
    ReviewTransition,
    derive_review_metrics,
    load_review_transitions,
    review_transition_record,
    validate_review_ops_report,
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
)

WS = "ws-extract"

T0 = "2026-08-20T10:00:00Z"
T1 = "2026-08-20T10:05:00Z"
T2 = "2026-08-20T10:12:00Z"
T3 = "2026-08-20T10:20:00Z"
T4 = "2026-08-20T10:25:00Z"


async def _publish_two_conflicts(payload_env) -> None:
    """Header + alt header conflicting on total_due AND currency."""
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            (
                "invoice-header-alt",
                _invoice_header_doc(total="777.77", currency="EUR"),
                "rev-h1-alt",
            ),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )


def _clock(*timestamps: str):
    """Deterministic clock yielding the given ISO timestamps in order."""
    queue = list(timestamps)
    return lambda: queue.pop(0) if queue else timestamps[-1]


def _service(payload_env, clock) -> ExtractionService:
    factory, _store, commit_service = payload_env
    return ExtractionService(
        factory, commit_service, workspace_id=WS, review_clock=clock
    )


def _decision(result, field: str, action: str, **overrides) -> ReviewDecision:
    return ReviewDecision(
        result_identity=result.identity,
        schema_identity=result.schema_identity,
        publication_set_id=result.context.publication_set_id,
        field_path=field,
        action=action,
        reviewer="reviewer@example.test",
        rationale="adjudicated in review",
        **overrides,
    )


async def _metrics(payload_env):
    transitions = await load_review_transitions(payload_env[0], WS)
    return derive_review_metrics(
        transitions,
        workspace_id=WS,
        schema_id="demo.invoice",
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )


# ---------------------------------------------------------------------------
# lifecycle: required -> reviewed, coverage/dwell/outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_required_path_enters_accounting_once(payload_env):
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0))
    result = await _run(service)
    assert result.fields["total_due"].status == FIELD_OUTCOME_REVIEW_REQUIRED
    assert result.fields["currency"].status == FIELD_OUTCOME_REVIEW_REQUIRED

    metrics = await _metrics(payload_env)
    assert metrics["required_review"] == 2
    assert metrics["eligible_cases"] == 2
    assert metrics["required_events"] == 2  # one event per case, first run
    assert metrics["reviewed"] == 0
    assert metrics["unresolved_backlog"] == 2
    assert metrics["review_coverage_rate"]["value"] == 0.0
    assert metrics["dwell"]["status"] == "undefined_no_resolved_cases"
    validate_review_ops_report(metrics)


@pytest.mark.asyncio
async def test_review_completion_accounted_with_exact_dwell(payload_env):
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0, T1, T2))
    result = await _run(service)

    accepted = await service.apply_review(_decision(result, "total_due", "accept"))
    assert accepted.fields["total_due"].status == FIELD_OUTCOME_ACCEPTED
    corrected = await service.apply_review(
        _decision(result, "currency", "correct", value="USD")
    )
    assert corrected.fields["currency"].status == "corrected"

    metrics = await _metrics(payload_env)
    assert metrics["required_review"] == 2
    assert metrics["reviewed"] == 2
    assert metrics["unresolved_backlog"] == 0
    assert metrics["review_coverage_rate"] == {
        "value": 1.0,
        "status": "defined",
        "count": 2,
        "denominator": 2,
    }
    assert metrics["outcomes"] == {"accepted": 1, "corrected": 1, "rejected": 0}
    # Dwell is deterministic: both entered at T0; decisions at T1/T2.
    assert metrics["dwell"] == {
        "status": "defined",
        "resolved_cases": 2,
        "measure": "decision_at_minus_first_required_at_seconds",
        "min_seconds": 300.0,
        "median_seconds": 510.0,
        "max_seconds": 720.0,
    }
    validate_review_ops_report(metrics)


@pytest.mark.asyncio
async def test_rejection_counts_as_reviewed_outcome_and_backlog_is_honest(
    payload_env,
):
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0, T1))
    result = await _run(service)

    rejected = await service.apply_review(_decision(result, "total_due", "reject"))
    assert rejected.fields["total_due"].status == "rejected"
    # currency intentionally left unresolved — a backlog, not a bypass.

    metrics = await _metrics(payload_env)
    assert metrics["reviewed"] == 1
    assert metrics["unresolved_backlog"] == 1
    assert metrics["outcomes"]["rejected"] == 1
    assert metrics["bypass_refusals"] == 0


# ---------------------------------------------------------------------------
# adversarial: replay, stale, bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_decision_does_not_double_count(payload_env):
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0, T1))
    result = await _run(service)
    first = await service.apply_review(_decision(result, "total_due", "accept"))
    before = await _metrics(payload_env)

    # Byte-identical replay of the same decision against the same
    # original result: the kernel's identity dedup rejects the duplicate
    # batch (decision + transition atomically), so the service returns
    # the same adjudicated state and nothing is counted twice.
    replay = await service.apply_review(_decision(result, "total_due", "accept"))
    assert replay.fields["total_due"].status == FIELD_OUTCOME_ACCEPTED
    after = await _metrics(payload_env)
    assert after == before
    assert after["reviewed"] == 1


@pytest.mark.asyncio
async def test_stale_review_is_rejected_and_accounted_separately(payload_env):
    factory, _store, commit_service = payload_env
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0, T3))
    result = await _run(service)

    # Supersede the reviewed publication.
    await commit_service.commit(
        KernelCommitBatch(
            workspace_id=WS,
            records=(_doc("invoice-header-v2", _invoice_header_doc(total="200.00"), "rev-h2"),),
        )
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, WS)
    )
    await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )

    with pytest.raises(StaleReviewError, match="no longer active"):
        await service.apply_review(_decision(result, "total_due", "accept"))

    # The stale attempt mutated nothing: the stored result still needs
    # review and no decision record exists.
    stored = await service.load_result(result.identity)
    assert stored.fields["total_due"].status == FIELD_OUTCOME_REVIEW_REQUIRED
    metrics = await _metrics(payload_env)
    assert metrics["stale_rejections"] == 1
    assert metrics["reviewed"] == 0
    assert metrics["unresolved_backlog"] == 2


@pytest.mark.asyncio
async def test_bypass_attempt_on_accepted_field_is_refused_and_accounted(
    payload_env,
):
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0, T1, T4))
    result = await _run(service)
    accepted = await service.apply_review(_decision(result, "total_due", "accept"))

    # Bypass probe: re-adjudicate the now-accepted field to inflate an
    # outcome. The review seam refuses; the attempt is accounted.
    with pytest.raises(ReviewError, match="already accepted"):
        await service.apply_review(_decision(accepted, "total_due", "accept"))

    metrics = await _metrics(payload_env)
    assert metrics["bypass_refusals"] == 1
    assert metrics["bypass_rate"] == {
        "value": 0.5,
        "status": "defined",
        "count": 1,
        "denominator": 2,  # one completed review + one refused bypass
    }
    assert metrics["reviewed"] == 1
    validate_review_ops_report(metrics)


# ---------------------------------------------------------------------------
# durability: reload reproduces the same accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_reproduces_identical_accounting(payload_env):
    await _publish_two_conflicts(payload_env)
    service = _service(payload_env, _clock(T0, T1))
    result = await _run(service)
    await service.apply_review(_decision(result, "total_due", "accept"))

    first = await _metrics(payload_env)
    # "Restart": reload transitions from the durable store with no
    # in-memory state, then re-derive.
    transitions = await load_review_transitions(payload_env[0], WS)
    second = derive_review_metrics(
        transitions,
        workspace_id=WS,
        schema_id="demo.invoice",
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )
    assert first == second


@pytest.mark.asyncio
async def test_no_review_work_yields_explicit_zero_denominator_not_zero(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(
        factory,
        commit_service,
        [
            ("invoice-header", _invoice_header_doc(), "rev-h1"),
            ("invoice-items", _items_doc(GOOD_ROWS), "rev-i1"),
        ],
    )
    service = _service(payload_env, _clock(T0))
    result = await _run(service)
    assert result.run_status == "accepted"

    metrics = await _metrics(payload_env)
    assert metrics["required_review"] == 0
    assert metrics["review_coverage_rate"] == {
        "value": None,
        "status": "undefined_zero_denominator",
        "count": 0,
        "denominator": 0,
    }
    assert metrics["bypass_rate"]["status"] == "undefined_zero_denominator"
    validate_review_ops_report(metrics)


# ---------------------------------------------------------------------------
# pure derivation + validation (synthetic transitions)
# ---------------------------------------------------------------------------


def _transition(kind: str, case: str, at: str, **overrides) -> ReviewTransition:
    return ReviewTransition(
        kind=kind,
        result_identity=f"sha256:{case}",
        field_path="total_due",
        publication_set_id="pub-1",
        occurred_at=at,
        **overrides,
    )


def test_orphan_decisions_are_counted_not_hidden():
    metrics = derive_review_metrics(
        [
            _transition(REVIEW_TRANSITION_ACCEPTED, "orphan", T1),
        ],
        workspace_id="ws",
        schema_id="demo.invoice",
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )
    assert metrics["required_review"] == 0
    assert metrics["reviewed"] == 0
    assert metrics["decisions_without_observed_requirement"] == 1
    assert metrics["outcomes"]["accepted"] == 1
    validate_review_ops_report(metrics)


def test_rerun_required_events_dedupe_into_one_case():
    metrics = derive_review_metrics(
        [
            _transition(REVIEW_TRANSITION_REQUIRED, "same", T0),
            _transition(REVIEW_TRANSITION_REQUIRED, "same", T1),
            _transition(REVIEW_TRANSITION_REJECTED, "same", T2),
        ],
        workspace_id="ws",
        schema_id="demo.invoice",
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )
    assert metrics["required_review"] == 1
    assert metrics["required_events"] == 2
    assert metrics["reviewed"] == 1
    # Dwell anchors on the FIRST required event (queue entry).
    assert metrics["dwell"]["min_seconds"] == 720.0
    assert metrics["dwell"]["max_seconds"] == 720.0


def test_transition_records_round_trip_and_reject_unknown_shapes():
    transition = ReviewTransition(
        kind=REVIEW_TRANSITION_ACCEPTED,
        result_identity="sha256:" + "a" * 64,
        field_path="total_due",
        publication_set_id="pub-1",
        occurred_at=T1,
        reviewer="r@example.test",
        decision_record_id="extraction.review.abcdef0123456789",
    )
    record = review_transition_record(transition)
    assert record.record_id.startswith("extraction.reviewops.")
    payload = record.properties["transition"]
    assert ReviewTransition.from_dict(payload) == transition

    corrupted = dict(payload)
    corrupted["suddenly_new"] = True
    with pytest.raises(ReviewOpsError, match="unknown review transition keys"):
        ReviewTransition.from_dict(corrupted)
    with pytest.raises(ReviewOpsError, match="invalid review transition kind"):
        ReviewTransition.from_dict({**payload, "kind": "approved"})


def test_validator_fails_closed_on_dishonest_mutations():
    base = derive_review_metrics(
        [
            _transition(REVIEW_TRANSITION_REQUIRED, "a", T0),
            _transition(REVIEW_TRANSITION_ACCEPTED, "a", T1),
        ],
        workspace_id="ws",
        schema_id="demo.invoice",
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )
    validate_review_ops_report(base)

    def mutated(**changes):
        clone = copy.deepcopy(base)
        clone.update(changes)
        return clone

    with pytest.raises(ReviewOpsError, match="missing"):
        incomplete = copy.deepcopy(base)
        del incomplete["dwell"]
        validate_review_ops_report(incomplete)
    with pytest.raises(ReviewOpsError, match="must be None when the denominator is zero"):
        invented_zero = copy.deepcopy(base)
        invented_zero["review_coverage_rate"] = {
            "value": 0.0,
            "status": "undefined_zero_denominator",
            "count": 0,
            "denominator": 0,
        }
        validate_review_ops_report(invented_zero)
    with pytest.raises(ReviewOpsError, match="backlog"):
        validate_review_ops_report(mutated(unresolved_backlog=99))
    with pytest.raises(ReviewOpsError, match="within \[0, 1\]"):
        overflow = copy.deepcopy(base)
        overflow["bypass_rate"] = {
            "value": 1.5,
            "status": "defined",
            "count": 3,
            "denominator": 2,
        }
        validate_review_ops_report(overflow)
    with pytest.raises(ReviewOpsError, match="min <= median <= max"):
        disordered = copy.deepcopy(base)
        disordered["dwell"]["min_seconds"] = 999.0
        validate_review_ops_report(disordered)
    with pytest.raises(ReviewOpsError, match="schema_version"):
        wrong_version = copy.deepcopy(base)
        wrong_version["schema_version"] = "marker.review_policy_ops.v0"
        validate_review_ops_report(wrong_version)


def test_population_identity_is_stated_and_required():
    with pytest.raises(ReviewOpsError, match="workspace_id"):
        derive_review_metrics(
            [],
            workspace_id="",
            schema_id="demo.invoice",
            policy_id=RECONCILE_POLICY_ID,
            policy_version=RECONCILE_POLICY_VERSION,
        )
    metrics = derive_review_metrics(
        [],
        workspace_id="ws",
        schema_id="demo.invoice",
        policy_id=RECONCILE_POLICY_ID,
        policy_version=RECONCILE_POLICY_VERSION,
    )
    assert metrics["schema_version"] == REVIEW_OPS_SCHEMA_VERSION
    assert metrics["population"]["accounting_unit"] == (
        "one field of one extraction result identity"
    )
