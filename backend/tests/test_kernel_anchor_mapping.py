"""Cross-revision anchor mapping contract tests (PR82A)."""

from __future__ import annotations

import pytest

from app.kernel.anchor_mapping import (
    ANCHOR_MAPPING_CASCADE_VERSION,
    AnchorMappingDecisionRecord,
    MAPPING_DISPOSITION_EXACT,
    MAPPING_DISPOSITION_MAPPED_DETERMINISTIC,
    MAPPING_DISPOSITION_MAPPED_REVIEWED,
    MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
    MAPPING_DISPOSITION_STALE,
    MAPPING_DISPOSITION_UNRESOLVED,
    SourceAnchorMappingRecord,
    effective_disposition,
    map_anchor,
)
from app.kernel.anchors import (
    COORDINATE_SPACE_PDF_PAGE_POINTS,
    GeometrySelector,
    NativeSelector,
    PositionSelector,
    SourceAnchorRecord,
    TextQuoteSelector,
)
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import KernelError
from app.utils.canonical import CanonicalBox

REVISION_N = "rev-0001"
REVISION_N1 = "rev-0002"

PDF_BOX = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72, 640, 272, 660]),
    space=COORDINATE_SPACE_PDF_PAGE_POINTS,
    boundary_convention="region_inclusive",
)

PDF_BOX_SHIFTED = GeometrySelector(
    geometry=CanonicalBox.from_bbox([72, 600, 272, 620]),
    space=COORDINATE_SPACE_PDF_PAGE_POINTS,
    boundary_convention="region_inclusive",
)

BOOKMARK = NativeSelector(
    provider="ooxml",
    native_kind="bookmark",
    native_id="bm-total",
    package_path="word/document.xml",
)


def make_anchor(
    revision: str = REVISION_N,
    *,
    record_id: str = "anchor-evt-1",
    locator: str | None = "pdf:page:1",
    **selectors,
):
    return SourceAnchorRecord(
        record_id=record_id,
        content_revision_ref=revision,
        locator=locator,
        selectors=dict(selectors) or {"quote": TextQuoteSelector(quote="seed")},
    )


def outcome_for(old, new_anchors, source=REVISION_N, target=REVISION_N1):
    return map_anchor(
        old, new_anchors, source_revision_ref=source, target_revision_ref=target
    )


class TestRecordValidation:
    def test_disposition_vocabulary_is_closed(self):
        with pytest.raises(KernelError, match="unknown mapping disposition"):
            SourceAnchorMappingRecord(
                source_revision_ref=REVISION_N,
                target_revision_ref=REVISION_N1,
                source_anchor_id="anchor-1",
                disposition="probably-fine",
                rule_id="rule_x_v1",
                rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
                target_anchor_id="anchor-2",
                confidence="1",
            )

    def test_mapped_reviewed_cannot_be_minted_by_the_cascade(self):
        with pytest.raises(KernelError, match="decision-only disposition"):
            SourceAnchorMappingRecord(
                source_revision_ref=REVISION_N,
                target_revision_ref=REVISION_N1,
                source_anchor_id="anchor-1",
                disposition=MAPPING_DISPOSITION_MAPPED_REVIEWED,
                rule_id="rule_x_v1",
                rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
                target_anchor_id="anchor-2",
                confidence="1",
            )

    def test_exact_requires_target_and_forbids_candidates(self):
        base = dict(
            source_revision_ref=REVISION_N,
            target_revision_ref=REVISION_N1,
            source_anchor_id="anchor-1",
            disposition=MAPPING_DISPOSITION_EXACT,
            rule_id="native_identity_v1",
            rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
            confidence="1",
        )
        with pytest.raises(KernelError, match="requires a target anchor id"):
            SourceAnchorMappingRecord(**base)
        with pytest.raises(KernelError, match="cannot carry candidates"):
            SourceAnchorMappingRecord(**base, target_anchor_id="a2", candidates=("a3",))

    def test_candidate_disposition_requires_candidates(self):
        with pytest.raises(KernelError, match="requires at least one candidate"):
            SourceAnchorMappingRecord(
                source_revision_ref=REVISION_N,
                target_revision_ref=REVISION_N1,
                source_anchor_id="anchor-1",
                disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
                rule_id="quote_duplicates_v1",
                rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
                confidence="0.5",
            )

    def test_confidence_must_lie_in_unit_interval(self):
        base = dict(
            source_revision_ref=REVISION_N,
            target_revision_ref=REVISION_N1,
            source_anchor_id="anchor-1",
            disposition=MAPPING_DISPOSITION_STALE,
            rule_id="no_match_v1",
            rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
            confidence="0",
        )
        SourceAnchorMappingRecord(**{**base, "confidence": "1.0"})
        with pytest.raises(KernelError, match="must lie in"):
            SourceAnchorMappingRecord(**{**base, "confidence": "1.2"})
        with pytest.raises(KernelError, match="must lie in"):
            SourceAnchorMappingRecord(**{**base, "confidence": "-0.1"})

    def test_same_revision_mapping_is_refused(self):
        # ACL-only changes remint authorization state, never content
        # identity: there is nothing to map.
        with pytest.raises(KernelError, match="cannot map a content revision onto itself"):
            outcome_for(
                make_anchor(), [make_anchor(REVISION_N)], source=REVISION_N, target=REVISION_N
            )
        with pytest.raises(KernelError, match="ACL-only"):
            SourceAnchorMappingRecord(
                source_revision_ref=REVISION_N,
                target_revision_ref=REVISION_N,
                source_anchor_id="anchor-1",
                disposition=MAPPING_DISPOSITION_STALE,
                rule_id="no_match_v1",
                rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
                confidence="0",
            )

    def test_source_anchor_must_be_bound_to_source_revision(self):
        with pytest.raises(KernelError, match="not bound to the declared source revision"):
            outcome_for(make_anchor(REVISION_N1), [make_anchor(REVISION_N1)])

    def test_new_anchors_must_be_bound_to_target_revision(self):
        with pytest.raises(KernelError, match="not bound to the declared target revision"):
            outcome_for(make_anchor(), [make_anchor(REVISION_N)])

    def test_from_payload_rejects_unknown_fields_and_round_trips(self):
        old = make_anchor(native=BOOKMARK, quote=TextQuoteSelector(quote="Total 42"))
        new = [make_anchor(REVISION_N1, native=BOOKMARK, quote=TextQuoteSelector(quote="Total 43"))]
        record = SourceAnchorMappingRecord.from_outcome(
            outcome_for(old, new),
            source_revision_ref=REVISION_N,
            target_revision_ref=REVISION_N1,
            source_anchor_id="anchor-old",
        )
        payload = record.identity_payload()
        rebuilt = SourceAnchorMappingRecord.from_payload(payload)
        assert rebuilt.mapping_id() == record.mapping_id()
        with pytest.raises(KernelError, match="unknown mapping payload fields"):
            SourceAnchorMappingRecord.from_payload({**payload, "embedding": [0.1]})


class TestCascade:
    def test_native_identity_carries_exact_across_text_edit(self):
        old = make_anchor(native=BOOKMARK, quote=TextQuoteSelector(quote="Total 42"))
        new = [
            make_anchor(REVISION_N1, native=BOOKMARK, quote=TextQuoteSelector(quote="Total 47")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_EXACT
        assert outcome.rule_id == "native_identity_v1"
        assert outcome.target_anchor_id == new[0].anchor_id()
        assert outcome.confidence.text == "1"

    def test_unique_byte_exact_quote_maps_deterministically(self):
        old = make_anchor(quote=TextQuoteSelector(quote="Quarterly revenue", prefix="Table 2:", suffix="(USD)"))
        new = [
            make_anchor(REVISION_N1, locator="pdf:page:9", quote=TextQuoteSelector(quote="Intro text")),
            make_anchor(REVISION_N1, locator="pdf:page:2", quote=TextQuoteSelector(quote="Quarterly revenue", prefix="Table 2:", suffix="(USD)")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_DETERMINISTIC
        assert outcome.rule_id == "quote_unique_v1"
        assert outcome.target_anchor_id == new[1].anchor_id()

    def test_unique_quote_with_contradictory_context_stays_candidate(self):
        old = make_anchor(quote=TextQuoteSelector(quote="Total amount", prefix="Invoice 1"))
        new = [
            make_anchor(REVISION_N1, quote=TextQuoteSelector(quote="Total amount", prefix="Invoice 2")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE
        assert outcome.target_anchor_id is None

    def test_duplicate_identical_text_yields_ranked_candidates_not_a_pick(self):
        old = make_anchor(quote=TextQuoteSelector(quote="See section 4"))
        new = [
            make_anchor(REVISION_N1, locator="pdf:page:3", quote=TextQuoteSelector(quote="See section 4")),
            make_anchor(REVISION_N1, locator="pdf:page:7", quote=TextQuoteSelector(quote="See section 4")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE
        assert outcome.rule_id == "quote_duplicates_v1"
        assert set(outcome.candidates) == {a.anchor_id() for a in new}
        assert outcome.target_anchor_id is None

    def test_paraphrase_never_becomes_exact_or_deterministic(self):
        old = make_anchor(quote=TextQuoteSelector(quote="The quick brown fox jumps over the lazy dog"))
        new = [
            make_anchor(REVISION_N1, quote=TextQuoteSelector(quote="A swift auburn fox leaps above the idle hound")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition in (MAPPING_DISPOSITION_STALE, MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE)
        assert outcome.disposition != MAPPING_DISPOSITION_EXACT
        assert outcome.disposition != MAPPING_DISPOSITION_MAPPED_DETERMINISTIC
        # This paraphrase sits below the candidate threshold: stale.
        assert outcome.disposition == MAPPING_DISPOSITION_STALE

    def test_slight_edit_is_fuzzy_candidate_not_mapping(self):
        old = make_anchor(quote=TextQuoteSelector(quote="Revenue grew 12 percent year over year"))
        new = [
            make_anchor(REVISION_N1, quote=TextQuoteSelector(quote="Revenue grew 13 percent year over year")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE
        assert outcome.rule_id == "quote_fuzzy_v1"
        assert outcome.candidates == (new[0].anchor_id(),)

    def test_whitespace_or_case_change_is_normalized_candidate(self):
        old = make_anchor(quote=TextQuoteSelector(quote="Annual Report"))
        new = [
            make_anchor(REVISION_N1, quote=TextQuoteSelector(quote="annual  report")),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE
        assert outcome.rule_id == "quote_normalized_v1"

    def test_merged_region_is_partial_candidate(self):
        old = make_anchor(quote=TextQuoteSelector(quote="Liabilities section body"))
        new = [
            make_anchor(
                REVISION_N1,
                quote=TextQuoteSelector(quote="Liabilities section body and the notes that follow it"),
            ),
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE
        assert outcome.rule_id == "quote_partial_v1"

    def test_deleted_target_is_stale(self):
        old = make_anchor(quote=TextQuoteSelector(quote="Deleted paragraph about budgets"))
        new = [make_anchor(REVISION_N1, quote=TextQuoteSelector(quote="Unrelated content"))]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_STALE
        assert outcome.rule_id == "no_match_v1"
        assert outcome.target_anchor_id is None

    def test_positional_only_anchor_is_unresolved(self):
        old = make_anchor(
            position=PositionSelector(scope="content_bytes", start=10, end=20)
        )
        new = [
            make_anchor(REVISION_N1, position=PositionSelector(scope="content_bytes", start=10, end=20))
        ]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_UNRESOLVED
        assert outcome.rule_id == "insufficient_evidence_v1"

    def test_identical_geometry_is_candidate_never_exact(self):
        old = make_anchor(geometry=PDF_BOX)
        new = [make_anchor(REVISION_N1, geometry=PDF_BOX)]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE
        assert outcome.rule_id == "geometry_approximate_v1"

    def test_geometry_only_drift_stays_unresolved(self):
        old = make_anchor(geometry=PDF_BOX)
        new = [make_anchor(REVISION_N1, geometry=PDF_BOX_SHIFTED)]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_UNRESOLVED

    def test_native_id_disappeared_falls_back_to_quote(self):
        old = make_anchor(native=BOOKMARK, quote=TextQuoteSelector(quote="Stable text"))
        new = [make_anchor(REVISION_N1, quote=TextQuoteSelector(quote="Stable text"))]
        outcome = outcome_for(old, new)
        assert outcome.disposition == MAPPING_DISPOSITION_MAPPED_DETERMINISTIC
        assert outcome.rule_id == "quote_unique_v1"

    def test_outcome_is_independent_of_input_order_and_replayable(self):
        old = make_anchor(locator="pdf:page:5", quote=TextQuoteSelector(quote="Duplicated line"))
        new = [
            make_anchor(REVISION_N1, locator="pdf:page:5", quote=TextQuoteSelector(quote="Duplicated line")),
            make_anchor(REVISION_N1, locator="pdf:page:1", quote=TextQuoteSelector(quote="Duplicated line")),
        ]
        first = outcome_for(old, list(reversed(new)))
        second = outcome_for(old, new)
        assert first == second
        record_a = SourceAnchorMappingRecord.from_outcome(
            first, source_revision_ref=REVISION_N, target_revision_ref=REVISION_N1,
            source_anchor_id="anchor-old",
        )
        record_b = SourceAnchorMappingRecord.from_outcome(
            second, source_revision_ref=REVISION_N, target_revision_ref=REVISION_N1,
            source_anchor_id="anchor-old",
        )
        assert record_a.mapping_id() == record_b.mapping_id()
        # Candidate order is deterministic: page-5 locator agreement wins.
        assert first.candidates[0] == new[0].anchor_id()


class TestMappingDecisions:
    def _mapping(self) -> SourceAnchorMappingRecord:
        return SourceAnchorMappingRecord(
            source_revision_ref=REVISION_N,
            target_revision_ref=REVISION_N1,
            source_anchor_id="anchor-old",
            disposition=MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE,
            rule_id="quote_duplicates_v1",
            rule_version=ANCHOR_MAPPING_CASCADE_VERSION,
            candidates=("anchor-a", "anchor-b"),
            confidence="0.5",
        )

    def test_candidate_stays_candidate_without_decisions(self):
        assert effective_disposition(self._mapping(), []) == MAPPING_DISPOSITION_MAPPED_SEMANTIC_CANDIDATE

    def test_human_review_promotes_candidate_to_reviewed(self):
        mapping = self._mapping()
        decision = AnchorMappingDecisionRecord(
            mapping_ref=mapping.mapping_id(),
            effective_disposition=MAPPING_DISPOSITION_MAPPED_REVIEWED,
            decided_by="human_review",
            note="confirmed target anchor-a",
        )
        assert effective_disposition(mapping, [decision]) == MAPPING_DISPOSITION_MAPPED_REVIEWED

    def test_cascade_cannot_mint_reviewed(self):
        with pytest.raises(KernelError, match="only be minted by a human_review"):
            AnchorMappingDecisionRecord(
                mapping_ref="map-1",
                effective_disposition=MAPPING_DISPOSITION_MAPPED_REVIEWED,
                decided_by="deterministic_cascade",
            )

    def test_human_review_must_state_reviewed(self):
        with pytest.raises(KernelError, match="must state an effective disposition"):
            AnchorMappingDecisionRecord(
                mapping_ref="map-1",
                effective_disposition=MAPPING_DISPOSITION_STALE,
                decided_by="human_review",
            )

    def test_supersession_chain_latest_decision_wins(self):
        mapping = self._mapping()
        first = AnchorMappingDecisionRecord(
            record_id="dec-1",
            mapping_ref=mapping.mapping_id(),
            effective_disposition=MAPPING_DISPOSITION_MAPPED_REVIEWED,
            decided_by="human_review",
        )
        second = AnchorMappingDecisionRecord(
            record_id="dec-2",
            mapping_ref=mapping.mapping_id(),
            effective_disposition=MAPPING_DISPOSITION_STALE,
            decided_by="deterministic_cascade",
            supersedes_decision_ref="dec-1",
        )
        assert effective_disposition(mapping, [first, second]) == MAPPING_DISPOSITION_STALE
        assert effective_disposition(mapping, [second, first]) == MAPPING_DISPOSITION_STALE

    def test_forked_decision_chain_fails_closed(self):
        mapping = self._mapping()
        first = AnchorMappingDecisionRecord(
            record_id="dec-1",
            mapping_ref=mapping.mapping_id(),
            effective_disposition=MAPPING_DISPOSITION_MAPPED_REVIEWED,
            decided_by="human_review",
        )
        fork_a = AnchorMappingDecisionRecord(
            record_id="dec-2a",
            mapping_ref=mapping.mapping_id(),
            effective_disposition=MAPPING_DISPOSITION_STALE,
            decided_by="deterministic_cascade",
            supersedes_decision_ref="dec-1",
        )
        fork_b = AnchorMappingDecisionRecord(
            record_id="dec-2b",
            mapping_ref=mapping.mapping_id(),
            effective_disposition=MAPPING_DISPOSITION_STALE,
            decided_by="deterministic_cascade",
            supersedes_decision_ref="dec-1",
        )
        with pytest.raises(KernelError, match="single linear history"):
            effective_disposition(mapping, [first, fork_a, fork_b])

    def test_decision_for_another_mapping_is_rejected(self):
        mapping = self._mapping()
        foreign = AnchorMappingDecisionRecord(
            mapping_ref="map-other",
            effective_disposition=MAPPING_DISPOSITION_MAPPED_REVIEWED,
            decided_by="human_review",
        )
        with pytest.raises(KernelError, match="does not belong to mapping"):
            effective_disposition(mapping, [foreign])


@pytest.mark.asyncio
async def test_mapping_and_decision_records_commit_and_replay(payload_env):
    factory, _store, service = payload_env
    old = make_anchor(record_id="anchor-old", native=BOOKMARK, quote=TextQuoteSelector(quote="Total 42"))
    new = [make_anchor(REVISION_N1, record_id="anchor-new", native=BOOKMARK, quote=TextQuoteSelector(quote="Total 47"))]
    outcome = outcome_for(old, new)
    mapping = SourceAnchorMappingRecord.from_outcome(
        outcome,
        source_revision_ref=REVISION_N,
        target_revision_ref=REVISION_N1,
        source_anchor_id=old.anchor_id(),
    )
    decision = AnchorMappingDecisionRecord(
        mapping_ref=mapping.mapping_id(),
        effective_disposition=MAPPING_DISPOSITION_EXACT,
        decided_by="deterministic_cascade",
    )
    receipt = await service.commit(
        KernelCommitBatch(
            workspace_id="ws-pr82-mapping",
            records=(old, *new, mapping, decision),
            producer={"op": "pr82-anchor-mapping"},
        )
    )
    assert receipt.record_count == 4
    # The committed truth is inspectable: mapping + decision rows persist
    # beside the anchors they speak about.
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow

    async with factory() as session:
        rows = (
            await session.execute(
                select(KernelRecordRow.record_class).where(
                    KernelRecordRow.workspace_id == "ws-pr82-mapping"
                )
            )
        ).scalars().all()
    assert rows.count("source_anchor_mapping") == 1
    assert rows.count("anchor_mapping_decision") == 1
    assert rows.count("source_anchor") == 2
    # Re-committing the identical computed mapping is refused as already
    # committed truth — replay converges instead of minting a second fact.
    with pytest.raises(KernelError, match="already committed"):
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-pr82-mapping",
                records=(mapping,),
                producer={"op": "pr82-anchor-mapping-replay"},
            )
        )
