"""Versioned candidate reconciliation (PR80A).

Reconciliation turns a candidate set into field outcomes under an
explicit, versioned policy. Three honesty rules dominate:

* **Repetition is not corroboration.** Candidates from the same
  witness (same kernel record at the same revision) collapse before
  agreement is counted; a document repeating itself is one vote.
* **Conflicts stay visible.** Disagreeing candidates are never
  silently discarded — the outcome preserves every candidate and names
  the exact rule that resolved or refused to resolve the conflict.
* **No evidence, no value.** A field without grounded candidates is
  ``missing``; policy never guesses a plausible-looking filler.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_INVALID,
    FIELD_OUTCOME_MISSING,
    FIELD_OUTCOME_REVIEW_REQUIRED,
    FIELD_OUTCOME_UNRESOLVED,
    CandidateView,
    FieldOutcome,
    InvariantFinding,
    ItemOutcome,
)
from app.extraction.schema import ExtractionSchema
from app.extraction.validation import evaluate_invariants

#: Reconciliation policy identity recorded in every result context and
#: in each committed assessment's policy fields.
RECONCILE_POLICY_ID = "marker.extraction.reconcile"
RECONCILE_POLICY_VERSION = "v1"

#: Hybrid policy identity: the authority-aware proposal layer that sits
#: on top of the grounded reconciliation policy. Specialist proposals
#: never enter witness voting; only deterministic corroboration under
#: this policy can turn a proposal into an accepted value.
HYBRID_POLICY_ID = "marker.extraction.hybrid"
HYBRID_POLICY_VERSION = "v1"

#: Attributable rule ids (part of the versioned policy contract).
RULE_MISSING_NO_EVIDENCE = "missing.no_evidence.v1"
RULE_INVALID_ALL_CANDIDATES = "invalid.all_candidates_invalid.v1"
RULE_DEDUP_WITNESS_REPETITION = "dedup.witness_repetition.v1"
RULE_AGREE_DISTINCT_WITNESSES = "agree.distinct_witnesses.v1"
RULE_CONFLICT_WITNESS_COUNT = "conflict.witness_count.v1"
RULE_CONFLICT_UNRESOLVED = "conflict.preserved_unresolved.v1"
RULE_ROW_COLLAPSE_DUPLICATE = "row.collapse_duplicate_identity.v1"

#: Required-field escalation policy: unresolved/invalid conflicts and
#: missing required fields escalate to review instead of lingering as
#: silent unknowns; optional fields may stay missing on a partial run.
_ESCALATION_STATUSES = frozenset(
    {FIELD_OUTCOME_UNRESOLVED, FIELD_OUTCOME_INVALID}
)


def _distinct_witness_value(
    candidates: Sequence[CandidateView],
) -> tuple[str | int | None, list[CandidateView]]:
    """Collapse witness repetition; return the agreed value if any.

    Returns the canonical value supported by at least one candidate
    whose parse succeeded, together with the deduplicated candidate
    list (first candidate per distinct witness). ``None`` value means
    no parsable candidate existed.
    """
    deduped: list[CandidateView] = []
    seen_witnesses: set[tuple[str, str]] = set()
    value: str | int | None = None
    for candidate in candidates:
        witness = candidate.evidence[0].witness_key if candidate.evidence else None
        if witness is not None:
            if witness in seen_witnesses:
                continue
            seen_witnesses.add(witness)
        deduped.append(candidate)
        if candidate.value is not None and value is None:
            value = candidate.value
    return value, deduped


def _witness_votes(candidates: Sequence[CandidateView]) -> dict[str | int, int]:
    """Count DISTINCT witnesses per canonical value (repetition collapsed)."""
    votes: dict[str | int, int] = {}
    witnesses_per_value: dict[str | int, set[tuple[str, str]]] = {}
    for candidate in candidates:
        if candidate.value is None:
            continue
        witness = candidate.evidence[0].witness_key if candidate.evidence else ("", "")
        bucket = witnesses_per_value.setdefault(candidate.value, set())
        if witness in bucket:
            continue
        bucket.add(witness)
        votes[candidate.value] = votes.get(candidate.value, 0) + 1
    return votes


def reconcile_field(
    name: str,
    candidates: Sequence[CandidateView],
    *,
    required: bool,
) -> FieldOutcome:
    """Reconcile one field's candidate list under the versioned policy."""
    if not candidates:
        return FieldOutcome(
            status=FIELD_OUTCOME_MISSING,
            candidates=(),
            rule=RULE_MISSING_NO_EVIDENCE,
            reason="no grounded candidate was served for this field",
        )

    value, deduped = _distinct_witness_value(candidates)
    valid = [c for c in deduped if c.parse_error is None and c.value is not None]
    invalid = [c for c in deduped if c.parse_error is not None or c.value is None]

    if not valid:
        return FieldOutcome(
            status=FIELD_OUTCOME_INVALID,
            candidates=tuple(deduped),
            rule=RULE_INVALID_ALL_CANDIDATES,
            reason="every grounded candidate failed typed validation",
        )

    distinct_values = {c.value for c in valid}
    if len(distinct_values) == 1:
        winner_value = next(iter(distinct_values))
        witnesses = _witness_votes(valid)
        return FieldOutcome(
            status=FIELD_OUTCOME_ACCEPTED,
            value=winner_value,
            candidates=tuple(deduped),
            winner=winner_value,
            rule=RULE_AGREE_DISTINCT_WITNESSES,
            reason=(
                f"{len(valid)} candidate(s) agree on the value after witness "
                f"dedup ({_witness_vote_note(witnesses, winner_value)})"
            ),
        )

    # Genuine conflict: distinct values, each with their own witnesses.
    votes = _witness_votes(valid)
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], str(kv[0])))
    top_value, top_count = ranked[0]
    if len(ranked) > 1 and top_count == ranked[1][1]:
        return FieldOutcome(
            status=FIELD_OUTCOME_UNRESOLVED,
            candidates=tuple(deduped),
            rule=RULE_CONFLICT_UNRESOLVED,
            reason=(
                f"conflicting values {sorted(str(v) for v in distinct_values)} "
                f"tie at {top_count} distinct witness(es) each; policy "
                "refuses to pick without a deciding condition"
            ),
        )
    return FieldOutcome(
        status=FIELD_OUTCOME_ACCEPTED,
        value=top_value,
        candidates=tuple(deduped),
        winner=top_value,
        rule=RULE_CONFLICT_WITNESS_COUNT,
        reason=(
            f"value {top_value!r} carries {top_count} distinct witnesses vs "
            f"{ranked[1][1]} for {ranked[1][0]!r}"
        ),
    )


def _witness_vote_note(votes: Mapping[str | int, int], value: str | int) -> str:
    return f"witnesses={votes.get(value, 0)}"


def _escalate(outcome: FieldOutcome, required: bool) -> FieldOutcome:
    """Escalate unresolved/invalid/missing outcomes on required fields."""
    if required and (
        outcome.status in _ESCALATION_STATUSES or outcome.status == FIELD_OUTCOME_MISSING
    ):
        return FieldOutcome(
            status=FIELD_OUTCOME_REVIEW_REQUIRED,
            value=outcome.value,
            candidates=outcome.candidates,
            winner=outcome.winner,
            rule=outcome.rule,
            reason=outcome.reason,
        )
    return outcome


@dataclass(frozen=True)
class ReconciledExtraction:
    """Reconciled field/item outcomes plus invariant findings."""

    fields: Mapping[str, FieldOutcome]
    line_items: Mapping[str, tuple[ItemOutcome, ...]]
    invariants: tuple[InvariantFinding, ...]


def reconcile_items(
    schema: ExtractionSchema, item_name: str, rows: Sequence
) -> tuple[ItemOutcome, ...]:
    """Collapse duplicate row identities, reconcile each row's fields.

    Rows with the same identity key values from the same witness are
    the same row seen twice; rows with the same identity from DIFFERENT
    witnesses agree and collapse; rows differing on any identity key
    never merge.
    """
    item_spec = schema.line_item(item_name)
    collapsed: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        identity_tuple = tuple(sorted((k, str(v)) for k, v in row.identity.items()))
        witness = (
            next(iter(row.fields.values())).evidence[0].witness_key
            if row.fields
            else ("", "")
        )
        entry = collapsed.get(identity_tuple)
        if entry is None:
            collapsed[identity_tuple] = {
                "identity": dict(row.identity),
                "witnesses": {witness},
                "rows": [row],
            }
            order.append(identity_tuple)
        else:
            entry["witnesses"].add(witness)
            entry["rows"].append(row)

    outcomes: list[ItemOutcome] = []
    for identity_tuple in order:
        entry = collapsed[identity_tuple]
        merged_fields: dict[str, list[CandidateView]] = {}
        disagreeing = False
        for row in entry["rows"]:
            for field_name, candidate in row.fields.items():
                merged_fields.setdefault(field_name, []).append(candidate)
        field_outcomes: dict[str, FieldOutcome] = {}
        for field_name, candidates in merged_fields.items():
            spec = item_spec.field(field_name)
            outcome = reconcile_field(field_name, candidates, required=spec.required)
            field_outcomes[field_name] = outcome
        values = {
            name: (out.value, out.status)
            for name, out in field_outcomes.items()
        }
        statuses = {status for _, status in values.values()}
        non_accepted = statuses - {FIELD_OUTCOME_ACCEPTED}
        if FIELD_OUTCOME_UNRESOLVED in non_accepted or any(
            out.status == FIELD_OUTCOME_UNRESOLVED for out in field_outcomes.values()
        ):
            row_status = FIELD_OUTCOME_UNRESOLVED
        elif non_accepted:
            row_status = next(iter(non_accepted))
        else:
            # All fields accepted; identical identity rows from different
            # witnesses must still agree on every non-identity value,
            # otherwise the duplicate was a disagreement in disguise.
            for field_name, candidates in merged_fields.items():
                if field_name in item_spec.identity_keys:
                    continue
                if len({c.value for c in candidates}) > 1:
                    disagreeing = True
            row_status = FIELD_OUTCOME_UNRESOLVED if disagreeing else FIELD_OUTCOME_ACCEPTED
        dedup_rule = (
            RULE_ROW_COLLAPSE_DUPLICATE
            if len(entry["witnesses"]) > 1 or len(entry["rows"]) > 1
            else None
        )
        if dedup_rule and row_status == FIELD_OUTCOME_ACCEPTED:
            for name in field_outcomes:
                out = field_outcomes[name]
                field_outcomes[name] = FieldOutcome(
                    status=out.status,
                    value=out.value,
                    candidates=out.candidates,
                    winner=out.winner,
                    rule=RULE_ROW_COLLAPSE_DUPLICATE,
                    reason=(
                        f"row identity seen from {len(entry['witnesses'])} "
                        "distinct witness(es); repetitions collapsed"
                    ),
                )
        outcomes.append(
            ItemOutcome(
                identity=entry["identity"],
                status=row_status,
                fields=field_outcomes,
            )
        )
    return tuple(outcomes)


def reconcile(
    schema: ExtractionSchema, candidates
) -> ReconciledExtraction:
    """Reconcile a full candidate set into outcomes + invariant findings."""
    fields: dict[str, FieldOutcome] = {}
    for spec in schema.fields:
        outcome = reconcile_field(
            spec.name, candidates.scalars.get(spec.name, ()), required=spec.required
        )
        fields[spec.name] = _escalate(outcome, spec.required)

    line_items: dict[str, tuple[ItemOutcome, ...]] = {}
    for item_spec in schema.line_items:
        rows = candidates.items.get(item_spec.name, ())
        outcomes = reconcile_items(schema, item_spec.name, rows)
        escalated: list[ItemOutcome] = []
        for row in outcomes:
            if row.status in _ESCALATION_STATUSES:
                escalated_fields = {
                    name: (
                        _escalate(out, True)
                        if out.status in _ESCALATION_STATUSES
                        else out
                    )
                    for name, out in row.fields.items()
                }
                escalated.append(
                    replace(row, status=FIELD_OUTCOME_REVIEW_REQUIRED, fields=escalated_fields)
                )
            else:
                escalated.append(row)
        line_items[item_spec.name] = tuple(escalated)

    invariants = evaluate_invariants(schema, fields, line_items)
    return ReconciledExtraction(
        fields=fields, line_items=line_items, invariants=invariants
    )
