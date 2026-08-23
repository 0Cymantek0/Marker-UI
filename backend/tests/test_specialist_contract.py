"""Specialist candidate provenance contract tests (bridge workstream A).

These tests pin the representation facts the hybrid path depends on:

* source citations and model provenance are DIFFERENT things — a
  proposal view can never masquerade as an ``EvidenceCitation``;
* proposal/lane-report serialization round-trips deterministically;
* result identity is stable across runtime observations (latency,
  attempts, tokens, cache hits) but changes with semantic content;
* deterministic PR80A-only results serialize byte-identically to their
  pre-specialist shape (no key drift).
"""

from __future__ import annotations

import pytest

from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    ProposalView,
    SpecialistLaneReport,
    SpecialistProvenance,
    SpecialistRuntime,
    _field_outcome_from_dict,
    result_from_dict,
)


def _provenance(**overrides) -> SpecialistProvenance:
    payload = dict(
        workspace_id="ws-a",
        publication_set_id="pub-1",
        packet_identity_id="pkt-1",
        schema_identity="sha256:schema",
        route="specialist.v1",
        contract_version="marker.specialist.output.v1",
        config_identity="cfg-abc",
        context_fingerprint="fp-1",
        context_unit_count=7,
        context_char_count=512,
    )
    payload.update(overrides)
    return SpecialistProvenance(**payload)


def _proposal(**overrides) -> ProposalView:
    payload = dict(
        producer_id="openai-compatible:m1",
        producer_family="m1",
        config_identity="cfg-abc",
        value="89.99",
        typed_value="89.99",
        flags=(),
    )
    payload.update(overrides)
    return ProposalView(**payload)


class TestProposalView:
    def test_round_trips_deterministically(self):
        view = _proposal(flags=("unit_price_conflict",))
        rebuilt = ProposalView.from_dict(view.to_dict())
        assert rebuilt == view
        assert rebuilt.to_dict() == view.to_dict()

    def test_bad_disposition_fails_closed(self):
        with pytest.raises(ValueError, match="disposition"):
            _proposal(disposition="source_exact")

    def test_view_is_not_a_citation(self):
        # A proposal carries producer identity, never a source locator:
        # nothing in its shape can satisfy EvidenceCitation's contract.
        view = _proposal()
        assert not hasattr(view, "record_id")
        assert not hasattr(view, "revision_ref")
        assert not hasattr(view, "witness_key")

    def test_field_outcome_omits_proposals_key_when_empty(self):
        outcome = _field_outcome_from_dict(
            {
                "status": FIELD_OUTCOME_ACCEPTED,
                "value": "1",
                "candidates": [],
            }
        )
        assert "proposals" not in outcome.to_dict()
        assert outcome.proposals == ()

    def test_field_outcome_includes_proposals_when_present(self):
        outcome = _field_outcome_from_dict(
            {
                "status": FIELD_OUTCOME_REVIEW_REQUIRED,
                "candidates": [],
                "proposals": [_proposal().to_dict()],
            }
        )
        payload = outcome.to_dict()
        assert len(payload["proposals"]) == 1
        assert ProposalView.from_dict(payload["proposals"][0]) == _proposal()


class TestSpecialistProvenance:
    def test_round_trips(self):
        provenance = _provenance()
        assert SpecialistProvenance.from_dict(provenance.to_dict()) == provenance

    def test_disclosure_is_context_not_entailment(self):
        # The provenance names what was SEEN; the test pins that its
        # fields describe the context, never a value witness.
        payload = _provenance().to_dict()
        assert "record_id" not in payload
        assert payload["context_unit_count"] == 7


class TestLaneReport:
    def test_semantic_payload_excludes_runtime_and_error_detail(self):
        report = SpecialistLaneReport(
            status="ok",
            policy_id="marker.extraction.hybrid",
            policy_version="v1",
            producer_id="openai-compatible:m1",
            producer_family="m1",
            config_identity="cfg-abc",
            provenance=_provenance(),
            proposal_count=3,
            runtime=SpecialistRuntime(
                latency_ms=812, attempts=2, prompt_tokens=1200, from_cache=True
            ),
            error_detail="HTTP 429 retried",
        )
        semantic = report.semantic_payload()
        assert "runtime" not in semantic
        assert "error_detail" not in semantic
        assert semantic["proposal_count"] == 3
        full = report.to_dict()
        assert full["runtime"]["latency_ms"] == 812
        assert full["error_detail"] == "HTTP 429 retried"

    def test_round_trips_through_from_dict(self):
        report = SpecialistLaneReport(
            status="ok",
            policy_id="marker.extraction.hybrid",
            policy_version="v1",
            producer_id="openai-compatible:m1",
            runtime=SpecialistRuntime(latency_ms=5, attempts=1),
        )
        assert SpecialistLaneReport.from_dict(report.to_dict()) == report


def _result_with_specialist(report, *, value="10.00"):
    from app.extraction.results import (
        ExtractionContext,
        ExtractionResult,
        FieldOutcome,
    )

    context = ExtractionContext(
        workspace_id="ws-a",
        publication_set_id="pub-1",
        materialized_generation_id="",
        kernel_snapshot_commit_id=1,
        packet_identity_ids=("pkt-1",),
        policy_id="marker.extraction.reconcile",
        policy_version="v1",
    )
    return ExtractionResult(
        schema_id="demo.invoice",
        schema_version="1.0.0",
        schema_identity="sha256:schema",
        context=context,
        run_status="accepted",
        fields={
            "total_due": FieldOutcome(
                status=FIELD_OUTCOME_ACCEPTED,
                value=value,
                rule="agree.distinct_witnesses.v1",
                proposals=(_proposal(),),
            )
        },
        line_items={},
        invariants=(),
        specialist=report,
    )


def _report(**overrides) -> SpecialistLaneReport:
    payload = dict(
        status="ok",
        policy_id="marker.extraction.hybrid",
        policy_version="v1",
        producer_id="openai-compatible:m1",
        producer_family="m1",
        config_identity="cfg-abc",
        proposal_count=1,
    )
    payload.update(overrides)
    return SpecialistLaneReport(**payload)


class TestResultIdentitySemantics:
    def test_runtime_observations_do_not_change_identity(self):
        base = _result_with_specialist(
            _report(runtime=SpecialistRuntime(latency_ms=10, attempts=1))
        )
        replayed = _result_with_specialist(
            _report(
                runtime=SpecialistRuntime(
                    latency_ms=9999, attempts=3, from_cache=True
                ),
                error_detail=None,
            )
        )
        assert base.identity == replayed.identity

    def test_semantic_change_changes_identity(self):
        base = _result_with_specialist(_report())
        other_producer = _result_with_specialist(_report(producer_id="other:m2"))
        other_status = _result_with_specialist(_report(status="provider_failure"))
        assert base.identity != other_producer.identity
        assert base.identity != other_status.identity

    def test_proposal_value_change_changes_identity(self):
        base = _result_with_specialist(_report())
        changed = _result_with_specialist(_report(), value="77.77")
        assert base.identity != changed.identity

    def test_specialist_absent_keeps_pre_specialist_serialization(self):
        result = _result_with_specialist(_report())
        stripped = type(result)(
            schema_id=result.schema_id,
            schema_version=result.schema_version,
            schema_identity=result.schema_identity,
            context=result.context,
            run_status=result.run_status,
            fields={
                name: type(out)(
                    status=out.status,
                    value=out.value,
                    candidates=out.candidates,
                    rule=out.rule,
                )
                for name, out in result.fields.items()
            },
            line_items={},
            invariants=(),
        )
        payload = stripped.to_dict()
        assert "specialist" not in payload
        assert all(
            "proposals" not in out for out in payload["fields"].values()
        )

    def test_full_result_round_trips_with_specialist(self):
        result = _result_with_specialist(
            _report(provenance=_provenance(), runtime=SpecialistRuntime(latency_ms=1, attempts=1))
        )
        rebuilt = result_from_dict(result.to_dict())
        assert rebuilt == result
        assert rebuilt.identity == result.identity
        assert rebuilt.specialist.proposal_count == 1
        assert rebuilt.fields["total_due"].proposals[0].producer_id == "openai-compatible:m1"
