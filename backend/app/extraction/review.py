"""Review/adjudication seam with stale-context protection (PR80A).

Review is a semantic decision bound to the exact authoritative context
it reviewed: the result identity, the schema identity, and the
publication set the result was computed against. A decision replayed
against a different context is rejected, not silently committed.

Grounding rule: a reviewer may accept only what evidence supports —
``accept`` requires at least one grounded candidate on the field.
Corrections are recorded as human-sourced outcomes that never raise
kernel authority on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    FIELD_OUTCOME_CORRECTED,
    FIELD_OUTCOME_REJECTED,
    FieldOutcome,
)

#: Review authority-rule label recorded on committed decisions.
REVIEW_AUTHORITY_RULE = "marker.extraction.review.v1"

#: Allowed review actions.
REVIEW_ACTION_ACCEPT = "accept"
REVIEW_ACTION_CORRECT = "correct"
REVIEW_ACTION_REJECT = "reject"

REVIEW_ACTIONS = frozenset(
    {REVIEW_ACTION_ACCEPT, REVIEW_ACTION_CORRECT, REVIEW_ACTION_REJECT}
)


class ReviewError(ValueError):
    """Raised when a review decision cannot be applied honestly."""


class StaleReviewError(ReviewError):
    """The decision's bound context no longer matches the target result."""


@dataclass(frozen=True)
class ReviewDecision:
    """One adjudication decision over one field of one result."""

    result_identity: str
    schema_identity: str
    publication_set_id: str
    field_path: str
    action: str
    reviewer: str
    rationale: str
    value: str | int | None = None

    def __post_init__(self) -> None:
        if self.action not in REVIEW_ACTIONS:
            raise ReviewError(
                f"invalid review action {self.action!r}; "
                f"allowed: {sorted(REVIEW_ACTIONS)}"
            )
        if not self.reviewer or not self.rationale:
            raise ReviewError("reviewer and rationale are required")
        if self.action == REVIEW_ACTION_CORRECT and self.value is None:
            raise ReviewError("a correction must carry the corrected value")
        if self.action != REVIEW_ACTION_CORRECT and self.value is not None:
            raise ReviewError("only corrections carry a value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_identity": self.result_identity,
            "schema_identity": self.schema_identity,
            "publication_set_id": self.publication_set_id,
            "field_path": self.field_path,
            "action": self.action,
            "reviewer": self.reviewer,
            "rationale": self.rationale,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewDecision:
        if not isinstance(data, Mapping):
            raise ReviewError(
                f"review decision must be a mapping, got {type(data).__name__}"
            )
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ReviewError(f"unknown review decision keys {sorted(unknown)}")
        try:
            return cls(
                result_identity=data["result_identity"],
                schema_identity=data["schema_identity"],
                publication_set_id=data["publication_set_id"],
                field_path=data["field_path"],
                action=data["action"],
                reviewer=data["reviewer"],
                rationale=data["rationale"],
                value=data.get("value"),
            )
        except KeyError as exc:
            raise ReviewError(
                f"review decision is missing {exc.args[0]!r}"
            ) from None


def apply_review(
    outcome: FieldOutcome,
    decision: ReviewDecision,
    *,
    result_identity: str,
    schema_identity: str,
    publication_set_id: str,
) -> FieldOutcome:
    """Apply one decision to one field outcome, enforcing context binding.

    Raises :class:`StaleReviewError` when the decision was recorded
    against a different result/schema/publication context, and
    :class:`ReviewError` when the action is not applicable to the
    field's current state. The original candidates and evidence trail
    are always preserved on the returned outcome.
    """
    if decision.result_identity != result_identity:
        raise StaleReviewError(
            "review decision was recorded for result "
            f"{decision.result_identity!r} but applied to {result_identity!r}"
        )
    if decision.schema_identity != schema_identity:
        raise StaleReviewError(
            "review decision was recorded against schema "
            f"{decision.schema_identity!r} but the result uses "
            f"{schema_identity!r}"
        )
    if decision.publication_set_id != publication_set_id:
        raise StaleReviewError(
            "review decision was recorded against publication "
            f"{decision.publication_set_id!r} but the current context is "
            f"{publication_set_id!r}"
        )

    review_note = {
        "action": decision.action,
        "reviewer": decision.reviewer,
        "rationale": decision.rationale,
        "authority_rule": REVIEW_AUTHORITY_RULE,
        "bound_result_identity": decision.result_identity,
    }

    if decision.action == REVIEW_ACTION_ACCEPT:
        grounded = [c for c in outcome.candidates if c.parse_error is None]
        if not grounded:
            raise ReviewError(
                "accepting a field requires at least one grounded, valid "
                "candidate; a reviewer cannot mint evidence by fiat"
            )
        if outcome.status == FIELD_OUTCOME_ACCEPTED:
            raise ReviewError("field is already accepted; nothing to adjudicate")
        return FieldOutcome(
            status=FIELD_OUTCOME_ACCEPTED,
            value=grounded[0].value,
            candidates=outcome.candidates,
            winner=grounded[0].value,
            rule=REVIEW_AUTHORITY_RULE,
            reason=(
                f"reviewer accepted the grounded candidate "
                f"{grounded[0].value!r} from {len(grounded)} candidate(s)"
            ),
            review=review_note,
        )

    if decision.action == REVIEW_ACTION_CORRECT:
        if outcome.status == FIELD_OUTCOME_ACCEPTED:
            raise ReviewError(
                "field is already accepted; correct requires an unresolved "
                "or review-required field"
            )
        return FieldOutcome(
            status=FIELD_OUTCOME_CORRECTED,
            value=decision.value,
            candidates=outcome.candidates,
            rule=REVIEW_AUTHORITY_RULE,
            reason="reviewer supplied a corrected value (human-sourced)",
            review=review_note,
        )

    # Reject: record an explicit adjudication against the value.
    return FieldOutcome(
        status=FIELD_OUTCOME_REJECTED,
        value=None,
        candidates=outcome.candidates,
        rule=REVIEW_AUTHORITY_RULE,
        reason=f"reviewer rejected the field: {decision.rationale}",
        review=review_note,
    )
