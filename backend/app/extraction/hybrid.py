"""Authority-aware hybrid reconciliation policy (specialist bridge C).

The grounded policy (:mod:`app.extraction.reconciliation`) runs FIRST
and UNCHANGED over source-grounded candidates — witness voting,
conflict handling, and escalation semantics are exactly PR80A's. This
policy then merges trained-specialist proposals ON TOP under one
invariant that can never be violated:

**a proposal is attributable input, never evidence.** Proposals are
never witnesses, never votes, and can never flip a grounded conflict.
Exactly one bridge from proposal to acceptance exists: deterministic
corroboration, where the production normalizer applied to the cited
raw source text reproduces the proposed value. The proof then points
at the source witness and the deterministic rule — never at the model.

Required fields keep their useful-but-unproved proposals reachable as
``review_required`` outcomes instead of collapsing to plain missing;
grounded conflicts stay visible even when a model disagrees with them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.extraction.normalization import (
    NORMALIZATION_RULESET_ID,
    NormResult,
    normalize_by_type,
)
from app.extraction.reconciliation import (
    RULE_CONFLICT_UNRESOLVED,
    RULE_INVALID_ALL_CANDIDATES,
    RULE_MISSING_NO_EVIDENCE,
    ReconciledExtraction,
    reconcile,
)
from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_MISSING,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    FIELD_OUTCOME_UNRESOLVED,
    CandidateView,
    FieldOutcome,
    ItemOutcome,
    PROPOSAL_AGREES_WITH_SOURCE,
    PROPOSAL_CORROBORATED,
    PROPOSAL_CONFLICTS_WITH_SOURCE,
    PROPOSAL_UNPARSEABLE,
    PROPOSAL_UNPROVED_REVIEW,
    ProposalView,
)
from app.extraction.schema import ExtractionSchema, FieldSpec
from app.extraction.specialist import (
    SpecialistLaneResult,
    SpecialistProposal,
    row_field_path,
)
from app.extraction.validation import evaluate_invariants, parse_typed

#: Attributable hybrid rule ids (part of the versioned policy contract).
RULE_HYBRID_PROPOSAL_REVIEW = "hybrid.proposal_review.v1"
RULE_HYBRID_PROPOSAL_ROW = "hybrid.proposal_row.v1"
RULE_HYBRID_CORROBORATED = "hybrid.corroboration.deterministic_normalization.v1"

#: Derivation route stamped on corroborated candidates.
CORROBORATION_ROUTE = "hybrid-normalize.v1"


@dataclass(frozen=True)
class _Producer:
    """Producer identity shared by one lane's proposals."""

    producer_id: str
    producer_family: str
    config_identity: str


def _typed_agree(typed_value: str | int, norm: NormResult, field_type: str) -> bool:
    """Type-aware equality between our typed parse and a normalization."""
    if not norm.ok or norm.value is None or typed_value is None:
        return False
    if field_type == "decimal":
        try:
            return Decimal(str(typed_value)) == norm.value
        except InvalidOperation:
            return False
    if field_type == "integer":
        try:
            return int(typed_value) == norm.value
        except (TypeError, ValueError):
            return False
    return str(norm.value) == str(typed_value)


def _canonical_norm_value(norm: NormResult, field_type: str) -> str | int:
    """Canonical accepted value produced by the deterministic normalizer."""
    if field_type == "decimal":
        return format(norm.value, "f")  # type: ignore[union-attr]
    return norm.value  # type: ignore[return-value]


def _view_for(
    proposal: SpecialistProposal, producer: _Producer, spec: FieldSpec
) -> ProposalView:
    """Typed-parse a proposal independently of the model's own claim."""
    raw = proposal.raw_value
    parsed = parse_typed(raw, spec) if raw is not None else None
    return ProposalView(
        producer_id=producer.producer_id,
        producer_family=producer.producer_family,
        config_identity=producer.config_identity,
        value=raw,
        typed_value=parsed.value if parsed is not None else None,
        parse_error=parsed.error if parsed is not None else None,
        flags=proposal.flags,
    )


def _dispositions_for(
    views: Sequence[ProposalView], accepted_value: str | int | None
) -> list[str]:
    """Classify views against an accepted value (agreement is not a witness)."""
    result: list[str] = []
    for view in views:
        if view.typed_value is None:
            result.append(PROPOSAL_UNPARSEABLE)
        elif accepted_value is not None and view.typed_value == accepted_value:
            result.append(PROPOSAL_AGREES_WITH_SOURCE)
        else:
            result.append(PROPOSAL_CONFLICTS_WITH_SOURCE)
    return result


def _corroborate(
    outcome: FieldOutcome,
    spec: FieldSpec,
    views: Sequence[ProposalView],
    producer: _Producer,
) -> FieldOutcome | None:
    """Attempt the single proposal→acceptance bridge.

    Deterministic corroboration: every grounded candidate that failed
    strict typed parsing must normalize (under the production
    ruleset) to one shared value, and a proposal must independently
    typed-parse to that same value. The accepted value's proof is the
    source witness plus the deterministic rule; the model's confidence
    is irrelevant. Returns ``None`` when corroboration does not apply
    or the normalized readings disagree.
    """
    failed = [c for c in outcome.candidates if c.parse_error is not None]
    if not failed:
        return None
    normalized: list[NormResult] = []
    for candidate in failed:
        norm = normalize_by_type(spec.type, candidate.raw_text, spec.enum_values)
        if norm.ok:
            normalized.append(norm)
    if not normalized:
        return None
    first = normalized[0]
    for norm in normalized[1:]:
        if not _typed_agree(_canonical_norm_value(first, spec.type), norm, spec.type):
            return None  # the normalizer itself sees a conflict — no proof
    canonical = _canonical_norm_value(first, spec.type)
    matching = [v for v in views if _typed_agree(v.typed_value, first, spec.type)]
    if not matching:
        return None

    corroborated_candidates = tuple(
        CandidateView(
            raw_text=candidate.raw_text,
            value=canonical,
            evidence=candidate.evidence,
            derivation={
                **dict(candidate.derivation),
                "route": CORROBORATION_ROUTE,
                "normalizer": NORMALIZATION_RULESET_ID,
                "proposal_producer": producer.producer_id,
            },
            parse_error=None,
        )
        for candidate in failed
    )
    witnesses = sorted(
        {cite.witness_key[0] for c in corroborated_candidates for cite in c.evidence}
    )
    proposal_views = tuple(
        replace(view, disposition=PROPOSAL_CORROBORATED)
        if _typed_agree(view.typed_value, first, spec.type)
        else replace(view, disposition=PROPOSAL_CONFLICTS_WITH_SOURCE)
        for view in views
    )
    return FieldOutcome(
        status=FIELD_OUTCOME_ACCEPTED,
        value=canonical,
        candidates=corroborated_candidates,
        winner=canonical,
        rule=RULE_HYBRID_CORROBORATED,
        reason=(
            f"specialist proposal corroborated by deterministic normalization "
            f"({NORMALIZATION_RULESET_ID}) of cited source text at "
            f"{', '.join(witnesses)}; the normalizer is the proof, the model "
            "only suggested where to look"
        ),
        proposals=proposal_views,
    )


def _merge_scalar(
    outcome: FieldOutcome,
    spec: FieldSpec,
    proposals: Sequence[SpecialistProposal],
    producer: _Producer,
) -> FieldOutcome:
    """Merge one field's proposals onto its grounded baseline outcome."""
    if not proposals:
        return outcome
    views = tuple(_view_for(p, producer, spec) for p in proposals)

    if outcome.status == FIELD_OUTCOME_ACCEPTED:
        # Grounded authority stands; model agreement is recorded but
        # never counted, and a model cannot outvote source evidence.
        dispositions = _dispositions_for(views, outcome.value)
        agrees = any(d == PROPOSAL_AGREES_WITH_SOURCE for d in dispositions)
        conflicts = any(d == PROPOSAL_CONFLICTS_WITH_SOURCE for d in dispositions)
        note = ""
        if agrees:
            note += (
                f"; {dispositions.count(PROPOSAL_AGREES_WITH_SOURCE)} specialist "
                "proposal(s) agree (recorded, never counted as witnesses)"
            )
        if conflicts:
            note += (
                f"; {dispositions.count(PROPOSAL_CONFLICTS_WITH_SOURCE)} "
                "specialist proposal(s) conflict with the grounded value "
                "(kept visible; source evidence stands)"
            )
        return replace(
            outcome,
            proposals=tuple(
                replace(view, disposition=d) for view, d in zip(views, dispositions)
            ),
            reason=(outcome.reason or "") + note,
        )

    if outcome.rule == RULE_INVALID_ALL_CANDIDATES:
        # Raw source text exists but failed strict parsing: the one
        # place deterministic corroboration can apply.
        corroborated = _corroborate(outcome, spec, views, producer)
        if corroborated is not None:
            return corroborated
        return FieldOutcome(
            status=FIELD_OUTCOME_REVIEW_REQUIRED,
            value=None,
            candidates=outcome.candidates,
            rule=RULE_HYBRID_PROPOSAL_REVIEW,
            reason=(
                "grounded candidates failed typed validation and no "
                "deterministic rule proves the specialist proposal; kept "
                "for review (a proposal is never evidence)"
            ),
            proposals=tuple(
                replace(view, disposition=PROPOSAL_UNPARSEABLE)
                if view.typed_value is None
                else replace(view, disposition=PROPOSAL_UNPROVED_REVIEW)
                for view in views
            ),
        )

    if outcome.rule == RULE_MISSING_NO_EVIDENCE:
        # No grounded evidence at all: a proposal makes the field
        # reviewable (distinct from plain missing) but never valued.
        return FieldOutcome(
            status=FIELD_OUTCOME_REVIEW_REQUIRED,
            value=None,
            candidates=(),
            rule=RULE_HYBRID_PROPOSAL_REVIEW,
            reason=(
                "no grounded candidate was served; a specialist proposal "
                "awaits review (it cannot create source authority)"
            ),
            proposals=tuple(
                replace(view, disposition=PROPOSAL_UNPARSEABLE)
                if view.typed_value is None
                else replace(view, disposition=PROPOSAL_UNPROVED_REVIEW)
                for view in views
            ),
        )

    if outcome.rule == RULE_CONFLICT_UNRESOLVED:
        # Live grounded conflict (possibly escalated to review by the
        # required-field policy): proposals stay visible but cannot
        # pick a winner or soften the conflict.
        return replace(
            outcome,
            proposals=tuple(
                replace(view, disposition=PROPOSAL_CONFLICTS_WITH_SOURCE)
                for view in views
            ),
            reason=(
                (outcome.reason or "")
                + "; specialist proposal(s) recorded; a model cannot "
                "resolve a grounded conflict"
            ),
        )

    # corrected/rejected (human adjudication) and anything else:
    # attach for visibility, change nothing.
    return replace(
        outcome,
        proposals=tuple(
            replace(view, disposition=PROPOSAL_UNPROVED_REVIEW) for view in views
        ),
    )


def _row_status(fields: Mapping[str, FieldOutcome]) -> str:
    """Deterministic row status over field outcomes (spec order kept by caller)."""
    statuses = [out.status for out in fields.values()]
    if FIELD_OUTCOME_UNRESOLVED in statuses:
        return FIELD_OUTCOME_UNRESOLVED
    for status in statuses:
        if status != FIELD_OUTCOME_ACCEPTED:
            return status
    return FIELD_OUTCOME_ACCEPTED


def _merge_rows(
    schema: ExtractionSchema,
    item_name: str,
    rows: tuple[ItemOutcome, ...],
    row_proposals: Mapping[tuple, list[SpecialistProposal]],
    producer: _Producer,
) -> tuple[ItemOutcome, ...]:
    """Merge row proposals onto grounded rows; append proposal-only rows."""
    item_spec = schema.line_item(item_name)
    merged: list[ItemOutcome] = []
    by_identity: dict[tuple, int] = {}
    for row in rows:
        key = tuple(sorted((k, str(v)) for k, v in row.identity.items()))
        by_identity[key] = len(merged)
        merged.append(row)

    for key in sorted(row_proposals):
        proposals = row_proposals[key]
        identity = {name: value for name, value in key}
        index = by_identity.get(key)
        if index is None:
            # No grounded counterpart: a proposal-only row is visible
            # for review and can never be accepted.
            fields: dict[str, FieldOutcome] = {}
            for spec in item_spec.fields:
                spec_proposals = [
                    p for p in proposals if p.path.endswith(f".{spec.name}")
                ]
                views = tuple(_view_for(p, producer, spec) for p in spec_proposals)
                if views:
                    fields[spec.name] = FieldOutcome(
                        status=FIELD_OUTCOME_REVIEW_REQUIRED,
                        rule=RULE_HYBRID_PROPOSAL_ROW,
                        reason=(
                            "specialist proposed a row with no grounded "
                            "counterpart; every value awaits review"
                        ),
                        proposals=tuple(
                            replace(
                                view,
                                disposition=(
                                    PROPOSAL_UNPARSEABLE
                                    if view.typed_value is None
                                    else PROPOSAL_UNPROVED_REVIEW
                                ),
                            )
                            for view in views
                        ),
                    )
                else:
                    fields[spec.name] = FieldOutcome(
                        status=FIELD_OUTCOME_MISSING,
                        rule=RULE_HYBRID_PROPOSAL_ROW,
                        reason="no grounded row and no proposal for this field",
                    )
            merged.append(
                ItemOutcome(
                    identity=identity,
                    status=FIELD_OUTCOME_REVIEW_REQUIRED,
                    fields=fields,
                )
            )
            continue

        row = merged[index]
        updated_fields: dict[str, FieldOutcome] = {}
        for spec in item_spec.fields:
            baseline = row.fields.get(
                spec.name,
                FieldOutcome(
                    status=FIELD_OUTCOME_MISSING, rule=RULE_MISSING_NO_EVIDENCE
                ),
            )
            spec_proposals = [
                p for p in proposals if p.path.endswith(f".{spec.name}")
            ]
            updated_fields[spec.name] = _merge_scalar(
                baseline, spec, spec_proposals, producer
            )
        merged[index] = replace(
            row, fields=updated_fields, status=_row_status(updated_fields)
        )

    return tuple(merged)


def reconcile_hybrid(
    schema: ExtractionSchema,
    candidates: Any,
    lane_result: SpecialistLaneResult | None,
    *,
    workspace_id: str,
    publication_set_id: str,
) -> ReconciledExtraction:
    """Reconcile grounded candidates, then merge specialist proposals.

    ``workspace_id``/``publication_set_id`` are the run's authoritative
    context: proposals whose provenance was recorded against a
    different workspace or publication are refused wholesale (stale
    replay protection) instead of being attached to this result.
    """
    baseline = reconcile(schema, candidates)
    if lane_result is None or not lane_result.proposals:
        return baseline
    provenance = lane_result.provenance
    if (
        provenance is None
        or provenance.workspace_id != workspace_id
        or provenance.publication_set_id != publication_set_id
    ):
        # Refused: the lane's context binding does not match this run.
        return baseline

    producer = _Producer(
        producer_id=lane_result.producer_id or "unknown",
        producer_family=lane_result.producer_family or "unknown",
        config_identity=lane_result.config_identity or provenance.config_identity,
    )

    scalar_proposals: dict[str, list[SpecialistProposal]] = {}
    row_proposals: dict[str, dict[tuple, list[SpecialistProposal]]] = {}
    for proposal in lane_result.proposals:
        if proposal.identity is None:
            scalar_proposals.setdefault(proposal.path, []).append(proposal)
            continue
        item_name = proposal.path.split("[", 1)[0]
        key = tuple(
            sorted((name, str(value)) for name, value in proposal.identity.items())
        )
        expected_prefix = row_field_path(item_name, proposal.identity)
        if not proposal.path.startswith(expected_prefix + "."):
            continue  # path/identity mismatch: refuse the malformed proposal
        row_proposals.setdefault(item_name, {}).setdefault(key, []).append(proposal)

    fields: dict[str, FieldOutcome] = {}
    for spec in schema.fields:
        if spec.name not in baseline.fields:
            continue
        fields[spec.name] = _merge_scalar(
            baseline.fields[spec.name],
            spec,
            scalar_proposals.get(spec.name, ()),
            producer,
        )

    line_items: dict[str, tuple[ItemOutcome, ...]] = {}
    for item_spec in schema.line_items:
        rows = baseline.line_items.get(item_spec.name, ())
        line_items[item_spec.name] = _merge_rows(
            schema,
            item_spec.name,
            rows,
            row_proposals.get(item_spec.name, {}),
            producer,
        )

    invariants = evaluate_invariants(schema, fields, line_items)
    return ReconciledExtraction(
        fields=fields, line_items=line_items, invariants=invariants
    )
