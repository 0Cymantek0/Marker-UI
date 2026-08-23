"""Authority-aware hybrid reconciliation policy tests (bridge workstream C).

Pure policy tests: synthetic grounded candidates with real-shaped
citations, synthetic lane results with honest provenance, no kernel.
They pin the three properties the bridge exists for:

* a proposal alone NEVER creates an accepted value;
* model agreement NEVER counts as an independent witness;
* the only proposal→acceptance bridge is deterministic corroboration
  over cited raw source text, proven by the normalizer — not the model.
"""

from __future__ import annotations

from app.extraction.contract import INVOICE_SCHEMA
from app.extraction.extractor import CandidateSet, ItemCandidate
from app.extraction.hybrid import (
    RULE_HYBRID_CORROBORATED,
    RULE_HYBRID_PROPOSAL_REVIEW,
    RULE_HYBRID_PROPOSAL_ROW,
    reconcile_hybrid,
)
from app.extraction.reconciliation import (
    RULE_AGREE_DISTINCT_WITNESSES,
    RULE_CONFLICT_UNRESOLVED,
    RULE_INVALID_ALL_CANDIDATES,
    RULE_MISSING_NO_EVIDENCE,
    reconcile,
)
from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    FIELD_OUTCOME_UNRESOLVED,
    PROPOSAL_AGREES_WITH_SOURCE,
    PROPOSAL_CORROBORATED,
    PROPOSAL_CONFLICTS_WITH_SOURCE,
    PROPOSAL_UNPROVED_REVIEW,
    CandidateView,
    EvidenceCitation,
    ProposalView,
    SpecialistProvenance,
)
from app.extraction.specialist import (
    LANE_OK,
    SpecialistLaneResult,
    SpecialistProposal,
)
from app.extraction.validation import parse_typed

WS = "ws-a"
PUB = "pub-1"


def _cite(record_id: str = "doc-1") -> EvidenceCitation:
    return EvidenceCitation(
        record_id=record_id,
        revision_ref="rev-1",
        text_hash=f"hash-{record_id}",
        node_id=None,
        publication_set_id=PUB,
        materialized_generation_id="gen-1",
        packet_identity_id="pkt-1",
        op="lexical_search",
    )


def _scalar_candidate(
    field: str,
    raw: str,
    *,
    record_id: str = "doc-1",
    anchor: str = "Total Due",
) -> CandidateView:
    spec = INVOICE_SCHEMA.field(field)
    parsed = parse_typed(raw, spec)
    return CandidateView(
        raw_text=raw,
        value=parsed.value,
        evidence=(_cite(record_id),),
        derivation={"route": "anchor.v1", "anchor": anchor, "field": field},
        parse_error=parsed.error,
    )


def _row_candidate(
    sku: str,
    columns: dict[str, str],
    *,
    record_id: str = "doc-1",
) -> ItemCandidate:
    """Build one grounded row candidate from column raw texts (in spec order)."""
    item = INVOICE_SCHEMA.line_item("items")
    fields: dict[str, CandidateView] = {}
    for spec in item.fields:
        raw = columns.get(spec.name)
        if raw is None:
            continue
        parsed = parse_typed(raw, spec)
        fields[spec.name] = CandidateView(
            raw_text=raw,
            value=parsed.value,
            evidence=(_cite(record_id),),
            derivation={
                "route": "anchor.v1",
                "anchor": item.anchor,
                "field": f"items.{spec.name}",
            },
            parse_error=parsed.error,
        )
    identity = {}
    for key in item.identity_keys:
        if key in fields:
            identity[key] = fields[key].value
    return ItemCandidate(identity=identity, fields=fields)


def _candidate_set(
    scalars: dict[str, list[CandidateView]] | None = None,
    rows: list[ItemCandidate] | None = None,
) -> CandidateSet:
    scalar_map = scalars or {}
    return CandidateSet(
        scalars={
            spec.name: tuple(scalar_map.get(spec.name, ()))
            for spec in INVOICE_SCHEMA.fields
        },
        items={"items": tuple(rows or ())},
    )


def _provenance(**overrides) -> SpecialistProvenance:
    payload = dict(
        workspace_id=WS,
        publication_set_id=PUB,
        packet_identity_id="pkt-1",
        schema_identity=INVOICE_SCHEMA.identity,
        route="specialist.v1",
        contract_version="marker.specialist.output.v1",
        config_identity="cfg-1",
        context_fingerprint="fp-1",
        context_unit_count=3,
        context_char_count=100,
    )
    payload.update(overrides)
    return SpecialistProvenance(**payload)


def _proposal(
    path: str,
    raw_value: str | None,
    *,
    flags: tuple[str, ...] = (),
    identity: dict | None = None,
) -> SpecialistProposal:
    return SpecialistProposal(
        path=path,
        raw_value=raw_value,
        flags=flags,
        identity=identity,
        provenance=_provenance(),
    )


def _lane(proposals, *, status: str = LANE_OK, provenance=None) -> SpecialistLaneResult:
    return SpecialistLaneResult(
        status=status,
        producer_id="openai-compatible:m1",
        producer_family="m1",
        config_identity="cfg-1",
        provenance=provenance or _provenance(),
        proposals=tuple(proposals),
    )


def _run_hybrid(candidates: CandidateSet, lane_result, *, pub: str = PUB, ws: str = WS):
    return reconcile_hybrid(
        INVOICE_SCHEMA, candidates, lane_result, workspace_id=ws, publication_set_id=pub
    )


class TestBaselineEquality:
    def test_no_lane_keeps_pure_pr80a_semantics(self):
        candidates = _candidate_set(
            scalars={"invoice_number": [_scalar_candidate("invoice_number", "INV-1")]},
            rows=[_row_candidate("SKU-1", {
                "sku": "SKU-1", "description": "Widget", "quantity": "2",
                "unit_price": "9.99", "amount": "19.98",
            })],
        )
        hybrid = reconcile_hybrid(
            INVOICE_SCHEMA, candidates, None, workspace_id=WS, publication_set_id=PUB
        )
        baseline = reconcile(INVOICE_SCHEMA, candidates)
        assert hybrid.fields == baseline.fields
        assert hybrid.line_items == baseline.line_items
        assert hybrid.invariants == baseline.invariants

    def test_lane_without_proposals_changes_nothing(self):
        candidates = _candidate_set(
            scalars={"invoice_number": [_scalar_candidate("invoice_number", "INV-1")]}
        )
        hybrid = _run_hybrid(candidates, _lane([]))
        baseline = reconcile(INVOICE_SCHEMA, candidates)
        assert hybrid.fields == baseline.fields

    def test_proposals_from_foreign_publication_are_refused(self):
        candidates = _candidate_set(
            scalars={"invoice_number": [_scalar_candidate("invoice_number", "INV-1")]}
        )
        stale = _lane(
            [_proposal("total_due", "99.99")],
            provenance=_provenance(publication_set_id="pub-OTHER"),
        )
        hybrid = _run_hybrid(candidates, stale)
        outcome = hybrid.fields["total_due"]
        # refused wholesale: the outcome stays the plain escalated
        # missing (required field) with NO proposals attached.
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.rule == RULE_MISSING_NO_EVIDENCE
        assert outcome.proposals == ()

    def test_proposals_from_foreign_workspace_are_refused(self):
        candidates = _candidate_set(
            scalars={"invoice_number": [_scalar_candidate("invoice_number", "INV-1")]}
        )
        foreign = _lane(
            [_proposal("total_due", "99.99")],
            provenance=_provenance(workspace_id="ws-OTHER"),
        )
        hybrid = _run_hybrid(candidates, foreign)
        assert hybrid.fields["total_due"].proposals == ()


class TestProposalOnlyNeverAccepted:
    def test_required_missing_with_proposal_becomes_review_not_value(self):
        candidates = _candidate_set()
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "154.97")]))
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.value is None
        assert outcome.rule == RULE_HYBRID_PROPOSAL_REVIEW
        assert len(outcome.proposals) == 1
        assert outcome.proposals[0].typed_value == "154.97"
        assert outcome.proposals[0].disposition == PROPOSAL_UNPROVED_REVIEW
        assert outcome.proposals[0].producer_id == "openai-compatible:m1"

    def test_optional_missing_with_proposal_is_distinct_from_plain_missing(self):
        candidates = _candidate_set()  # po_number absent everywhere
        plain = reconcile(INVOICE_SCHEMA, candidates)
        hybrid = _run_hybrid(candidates, _lane([_proposal("po_number", "PO-77")]))
        assert plain.fields["po_number"].status == "missing"
        hybrid_outcome = hybrid.fields["po_number"]
        assert hybrid_outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert hybrid_outcome.proposals[0].typed_value == "PO-77"

    def test_same_producer_agreement_is_not_independent_proof(self):
        # Two identical proposals from ONE producer (same family) must
        # not behave like two witnesses: the field stays reviewable.
        candidates = _candidate_set()
        hybrid = _run_hybrid(
            candidates,
            _lane([_proposal("total_due", "1.00"), _proposal("total_due", "1.00")]),
        )
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.value is None
        assert len(outcome.proposals) == 2

    def test_invalid_typed_proposal_stays_unparseable(self):
        candidates = _candidate_set()
        hybrid = _run_hybrid(candidates, _lane([_proposal("invoice_date", "not a date")]))
        outcome = hybrid.fields["invoice_date"]
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.proposals[0].disposition == "unparseable"
        assert outcome.proposals[0].parse_error is not None


class TestGroundedAuthorityStands:
    def test_agreeing_proposal_does_not_inflate_witnesses(self):
        candidates = _candidate_set(
            scalars={"total_due": [_scalar_candidate("total_due", "154.97")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "154.97")]))
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_ACCEPTED
        assert outcome.rule == RULE_AGREE_DISTINCT_WITNESSES
        assert outcome.value == "154.97"
        assert len(outcome.proposals) == 1
        assert outcome.proposals[0].disposition == PROPOSAL_AGREES_WITH_SOURCE
        assert "never counted as witnesses" in outcome.reason
        assert len(outcome.witness_keys) == 1

    def test_conflicting_proposal_cannot_outvote_source(self):
        candidates = _candidate_set(
            scalars={"total_due": [_scalar_candidate("total_due", "154.97")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "777.77")]))
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_ACCEPTED
        assert outcome.value == "154.97"
        assert outcome.proposals[0].disposition == PROPOSAL_CONFLICTS_WITH_SOURCE
        assert "source evidence stands" in outcome.reason

    def test_unresolved_grounded_conflict_survives_a_model_opinion(self):
        candidates = _candidate_set(
            scalars={
                "total_due": [
                    _scalar_candidate("total_due", "154.97", record_id="doc-1"),
                    _scalar_candidate("total_due", "777.77", record_id="doc-2"),
                ]
            }
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "777.77")]))
        outcome = hybrid.fields["total_due"]
        # required-field policy escalated the tie to review, but the
        # conflict rule and both candidates survive untouched.
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.rule == RULE_CONFLICT_UNRESOLVED
        assert len(outcome.candidates) == 2
        assert outcome.proposals[0].disposition == PROPOSAL_CONFLICTS_WITH_SOURCE
        assert "cannot resolve a grounded conflict" in outcome.reason


class TestDeterministicCorroboration:
    def test_us_date_corroborated_from_cited_raw_text(self):
        candidates = _candidate_set(
            scalars={"invoice_date": [_scalar_candidate("invoice_date", "03/15/2026")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("invoice_date", "2026-03-15")]))
        outcome = hybrid.fields["invoice_date"]
        assert outcome.status == FIELD_OUTCOME_ACCEPTED
        assert outcome.value == "2026-03-15"
        assert outcome.rule == RULE_HYBRID_CORROBORATED
        assert outcome.proposals[0].disposition == PROPOSAL_CORROBORATED
        # the proof is the citation + the deterministic normalizer
        assert outcome.candidates[0].evidence[0].record_id == "doc-1"
        assert outcome.candidates[0].derivation["route"] == "hybrid-normalize.v1"
        assert "deterministic normalization" in outcome.reason

    def test_eu_decimal_corroborated(self):
        candidates = _candidate_set(
            scalars={"total_due": [_scalar_candidate("total_due", "2.045,00")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "2045.00")]))
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_ACCEPTED
        assert outcome.value == "2045.00"

    def test_us_thousands_decimal_corroborated(self):
        candidates = _candidate_set(
            scalars={"total_due": [_scalar_candidate("total_due", "3,750.00")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "3750.00")]))
        assert hybrid.fields["total_due"].value == "3750.00"

    def test_currency_synonym_corroborated(self):
        candidates = _candidate_set(
            scalars={"currency": [_scalar_candidate("currency", "US Dollars")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("currency", "USD")]))
        assert hybrid.fields["currency"].value == "USD"
        assert hybrid.fields["currency"].rule == RULE_HYBRID_CORROBORATED

    def test_thousands_integer_corroborated_as_int(self):
        row = _row_candidate("SKU-1", {"sku": "SKU-1", "quantity": "1,500"})
        candidates = _candidate_set(rows=[row])
        lane = _lane(
            [_proposal("items[sku=SKU-1].quantity", "1500", identity={"sku": "SKU-1"})]
        )
        hybrid = _run_hybrid(candidates, lane)
        field = hybrid.line_items["items"][0].fields["quantity"]
        assert field.status == FIELD_OUTCOME_ACCEPTED
        assert field.value == 1500
        assert field.rule == RULE_HYBRID_CORROBORATED

    def test_two_failed_witnesses_agreeing_both_corroborate(self):
        candidates = _candidate_set(
            scalars={
                "total_due": [
                    _scalar_candidate("total_due", "154,97", record_id="doc-1"),
                    _scalar_candidate("total_due", "154,97", record_id="doc-2"),
                ]
            }
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "154.97")]))
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_ACCEPTED
        assert len(outcome.witness_keys) == 2

    def test_normalizer_conflict_blocks_corroboration(self):
        # two raws that normalize to DIFFERENT values: no deterministic
        # proof exists, so the model proposal cannot pick a side.
        candidates = _candidate_set(
            scalars={
                "total_due": [
                    _scalar_candidate("total_due", "154,97", record_id="doc-1"),
                    _scalar_candidate("total_due", "777,77", record_id="doc-2"),
                ]
            }
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "154.97")]))
        outcome = hybrid.fields["total_due"]
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.value is None

    def test_wrong_proposal_value_is_not_corroborated(self):
        candidates = _candidate_set(
            scalars={"invoice_date": [_scalar_candidate("invoice_date", "03/15/2026")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("invoice_date", "2026-12-31")]))
        outcome = hybrid.fields["invoice_date"]
        assert outcome.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert outcome.proposals[0].disposition == PROPOSAL_UNPROVED_REVIEW

    def test_unrecognizable_raw_is_not_corroborated(self):
        candidates = _candidate_set(
            scalars={"invoice_date": [_scalar_candidate("invoice_date", "N/A")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("invoice_date", "2026-03-15")]))
        assert hybrid.fields["invoice_date"].status == FIELD_OUTCOME_REVIEW_REQUIRED

    def test_valid_parse_is_never_corroboration_target(self):
        # If strict parsing already succeeded, a differing proposal is
        # a conflict — there is nothing to corroborate.
        candidates = _candidate_set(
            scalars={"total_due": [_scalar_candidate("total_due", "154.97")]}
        )
        hybrid = _run_hybrid(candidates, _lane([_proposal("total_due", "999.99")]))
        assert hybrid.fields["total_due"].value == "154.97"


class TestProposalRows:
    def test_fabricated_unit_price_stays_reviewable_never_accepted(self):
        # The PR80B inv-013 shape: the source row is structurally
        # broken (no unit_price column), so NO grounded raw exists; the
        # model's derived 29.99 can only ever be a reviewable proposal.
        rows = [_row_candidate("SKU-7002", {
            "sku": "SKU-7002", "description": "Filter cartridge",
            "quantity": "3", "amount": "89.97",
        })]
        candidates = _candidate_set(rows=rows)
        lane = _lane([
            _proposal("items[sku=SKU-7002].unit_price", "29.99", identity={"sku": "SKU-7002"}),
        ])
        hybrid = _run_hybrid(candidates, lane)
        proposal_rows = [
            row for row in hybrid.line_items["items"]
            if row.identity.get("sku") == "SKU-7002"
        ]
        assert len(proposal_rows) == 1
        row = proposal_rows[0]
        assert row.status == FIELD_OUTCOME_REVIEW_REQUIRED
        unit_price = row.fields["unit_price"]
        assert unit_price.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert unit_price.value is None
        assert unit_price.proposals[0].typed_value == "29.99"
        assert unit_price.proposals[0].disposition == PROPOSAL_UNPROVED_REVIEW

    def test_matching_row_field_merges_like_scalar(self):
        rows = [_row_candidate("SKU-1", {
            "sku": "SKU-1", "description": "Widget", "quantity": "2",
            "unit_price": "9.99", "amount": "19.98",
        })]
        candidates = _candidate_set(rows=rows)
        lane = _lane([
            _proposal("items[sku=SKU-1].unit_price", "9.99", identity={"sku": "SKU-1"}),
            _proposal("items[sku=SKU-1].description", "Widget Pro", identity={"sku": "SKU-1"}),
        ])
        hybrid = _run_hybrid(candidates, lane)
        row = hybrid.line_items["items"][0]
        assert row.fields["unit_price"].proposals[0].disposition == PROPOSAL_AGREES_WITH_SOURCE
        desc = row.fields["description"]
        assert desc.status == FIELD_OUTCOME_ACCEPTED  # grounded value stands
        assert desc.proposals[0].disposition == PROPOSAL_CONFLICTS_WITH_SOURCE

    def test_row_field_corroboration_path(self):
        rows = [_row_candidate("SKU-1", {
            "sku": "SKU-1", "description": "Widget", "quantity": "2",
            "unit_price": "9,99", "amount": "19,98",
        })]
        candidates = _candidate_set(rows=rows)
        lane = _lane([
            _proposal("items[sku=SKU-1].unit_price", "9.99", identity={"sku": "SKU-1"}),
            _proposal("items[sku=SKU-1].amount", "19.98", identity={"sku": "SKU-1"}),
        ])
        hybrid = _run_hybrid(candidates, lane)
        row = hybrid.line_items["items"][0]
        assert row.fields["unit_price"].rule == RULE_HYBRID_CORROBORATED
        assert row.fields["unit_price"].value == "9.99"
        assert row.fields["amount"].value == "19.98"
        assert row.status == FIELD_OUTCOME_ACCEPTED

    def test_duplicate_proposal_rows_collapse_into_one_review_row(self):
        candidates = _candidate_set()
        lane = _lane([
            _proposal("items[sku=SKU-9].unit_price", "1.00", identity={"sku": "SKU-9"}),
            _proposal("items[sku=SKU-9].unit_price", "2.00", identity={"sku": "SKU-9"}),
        ])
        hybrid = _run_hybrid(candidates, lane)
        rows = [
            row for row in hybrid.line_items["items"]
            if row.identity.get("sku") == "SKU-9"
        ]
        assert len(rows) == 1
        assert rows[0].status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert len(rows[0].fields["unit_price"].proposals) == 2

    def test_row_conflict_flag_travels_to_proposals(self):
        rows = [_row_candidate("SKU-1", {
            "sku": "SKU-1", "description": "Widget", "quantity": "2",
            "unit_price": "9.99", "amount": "19.98",
        })]
        candidates = _candidate_set(rows=rows)
        lane = _lane([
            _proposal(
                "items[sku=SKU-1].amount", "19.98",
                flags=("row_conflict",), identity={"sku": "SKU-1"},
            ),
        ])
        hybrid = _run_hybrid(candidates, lane)
        view = hybrid.line_items["items"][0].fields["amount"].proposals[0]
        assert view.flags == ("row_conflict",)

    def test_proposal_only_row_fields_without_proposal_stay_missing(self):
        candidates = _candidate_set()
        lane = _lane([
            _proposal("items[sku=SKU-9].sku", "SKU-9", identity={"sku": "SKU-9"}),
        ])
        hybrid = _run_hybrid(candidates, lane)
        row = next(
            row for row in hybrid.line_items["items"]
            if row.identity.get("sku") == "SKU-9"
        )
        assert row.status == FIELD_OUTCOME_REVIEW_REQUIRED
        assert row.fields["sku"].rule == RULE_HYBRID_PROPOSAL_ROW
        assert row.fields["quantity"].status == "missing"
        assert row.fields["sku"].proposals[0].typed_value == "SKU-9"


class TestCorroborationImprovesTheRun:
    def test_fully_corroborated_document_reaches_satisfied_invariant(self):
        rows = [
            _row_candidate("SKU-1", {
                "sku": "SKU-1", "description": "Widget", "quantity": "2",
                "unit_price": "9,99", "amount": "19,98",
            }),
            _row_candidate("SKU-2", {
                "sku": "SKU-2", "description": "Gadget", "quantity": "1",
                "unit_price": "15,00", "amount": "15,00",
            }),
        ]
        candidates = _candidate_set(
            scalars={
                "invoice_number": [_scalar_candidate("invoice_number", "INV-1")],
                "invoice_date": [_scalar_candidate("invoice_date", "March 15, 2026")],
                "currency": [_scalar_candidate("currency", "euros")],
                "total_due": [_scalar_candidate("total_due", "34,98")],
            },
            rows=rows,
        )
        baseline = reconcile(INVOICE_SCHEMA, candidates)
        assert baseline.fields["total_due"].status != FIELD_OUTCOME_ACCEPTED

        lane = _lane([
            _proposal("invoice_date", "2026-03-15"),
            _proposal("currency", "EUR"),
            _proposal("total_due", "34.98"),
            _proposal("items[sku=SKU-1].unit_price", "9.99", identity={"sku": "SKU-1"}),
            _proposal("items[sku=SKU-1].amount", "19.98", identity={"sku": "SKU-1"}),
            _proposal("items[sku=SKU-2].unit_price", "15.00", identity={"sku": "SKU-2"}),
            _proposal("items[sku=SKU-2].amount", "15.00", identity={"sku": "SKU-2"}),
        ])
        hybrid = _run_hybrid(candidates, lane)
        assert hybrid.fields["invoice_date"].value == "2026-03-15"
        assert hybrid.fields["currency"].value == "EUR"
        total = hybrid.fields["total_due"]
        assert total.status == FIELD_OUTCOME_ACCEPTED
        assert total.value == "34.98"
        assert total.rule == RULE_HYBRID_CORROBORATED
        finding = hybrid.invariants[0]
        assert finding.finding == "satisfied"
        # every accepted value's proof chain ends at a real citation
        for outcome in hybrid.fields.values():
            if outcome.status == FIELD_OUTCOME_ACCEPTED:
                assert outcome.candidates[0].evidence[0].record_id


class TestDeterminism:
    def test_same_inputs_produce_identical_payloads(self):
        candidates = _candidate_set(
            scalars={"invoice_date": [_scalar_candidate("invoice_date", "03/15/2026")]}
        )
        lane = _lane([_proposal("invoice_date", "2026-03-15")])
        first = _run_hybrid(candidates, lane)
        second = _run_hybrid(candidates, lane)
        assert first.fields["invoice_date"].to_dict() == (
            second.fields["invoice_date"].to_dict()
        )
