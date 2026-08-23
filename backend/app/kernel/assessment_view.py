"""Region-relative effective assessment resolution (PR88, invariant 22).

Assessments are append-only and context-relative: an outcome is only
meaningful under its exact policy id/revision, workflow class, and a
kernel commit cut. This module resolves, per assertion (the smallest
independently usable unit), the effective assessment under one declared
context — without ever collapsing regions into a document-wide boolean.

One unresolved region must not make its neighbors unusable, and one
verified region must not make its neighbors verified. Both properties
hold structurally here: resolution is strictly per-assertion, and an
assertion with no matching assessment under the requested context
resolves to the honest ``unresolved_unavailable`` state — never to a
neighbor's outcome and never to a global default.

Visibility uses the commit that CARRIES each assessment record, not the
snapshot cut the assessment declares: a later commit cannot
retroactively change what was known at an earlier cut, even when its
assessment was computed against that earlier cut. Callers loading
stored rows pair each record with its ``kernel_commit_id``.

This is a read-side view. It mints no authority, writes no records,
and never rewrites history: the effective state can change when new
assessments are committed or when the caller changes the declared
context, while every stored assessment keeps its original identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.kernel.errors import KernelError
from app.kernel.records import (
    AUTHORITY_BEARING_OUTCOMES,
    CLAIM_OUTCOME_ACCEPTED_WITH_WARNING,
    CLAIM_OUTCOME_ABSTAINED,
    CLAIM_OUTCOME_FAILED,
    CLAIM_OUTCOME_UNAVAILABLE,
    CLAIM_OUTCOME_UNCERTAIN,
    ClaimAssessmentRecord,
)

USABILITY_CLASS_USABLE_AUTHORITY = "usable_authority"
USABILITY_CLASS_USABLE_WITH_WARNING = "usable_with_warning"
USABILITY_CLASS_UNRESOLVED_UNCERTAIN = "unresolved_uncertain"
USABILITY_CLASS_UNRESOLVED_UNAVAILABLE = "unresolved_unavailable"
USABILITY_CLASS_UNRESOLVED_ABSTAINED = "unresolved_abstained"
USABILITY_CLASS_UNRESOLVED_FAILED = "unresolved_failed"
USABILITY_CLASS_UNRESOLVED_UNKNOWN_OUTCOME = "unresolved_unknown_outcome"

USABILITY_CLASSES = frozenset(
    {
        USABILITY_CLASS_USABLE_AUTHORITY,
        USABILITY_CLASS_USABLE_WITH_WARNING,
        USABILITY_CLASS_UNRESOLVED_UNCERTAIN,
        USABILITY_CLASS_UNRESOLVED_UNAVAILABLE,
        USABILITY_CLASS_UNRESOLVED_ABSTAINED,
        USABILITY_CLASS_UNRESOLVED_FAILED,
        USABILITY_CLASS_UNRESOLVED_UNKNOWN_OUTCOME,
    }
)

#: Document-level summary labels. Deliberately NOT a boolean: the
#: distinct region states stay distinguishable in every summary.
DOCUMENT_ALL_REGIONS_USABLE = "all_regions_usable"
DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS = "usable_with_unresolved_regions"
DOCUMENT_NO_USABLE_REGIONS = "no_usable_regions"
DOCUMENT_NO_RESOLVED_ASSESSMENTS = "no_resolved_assessments"

_OUTCOME_TO_USABILITY = {
    CLAIM_OUTCOME_UNCERTAIN: USABILITY_CLASS_UNRESOLVED_UNCERTAIN,
    CLAIM_OUTCOME_UNAVAILABLE: USABILITY_CLASS_UNRESOLVED_UNAVAILABLE,
    CLAIM_OUTCOME_ABSTAINED: USABILITY_CLASS_UNRESOLVED_ABSTAINED,
    CLAIM_OUTCOME_FAILED: USABILITY_CLASS_UNRESOLVED_FAILED,
}


def usability_class_for_outcome(outcome: str) -> str:
    """Map one assessment outcome to its usability class (closed)."""
    if outcome in AUTHORITY_BEARING_OUTCOMES:
        return USABILITY_CLASS_USABLE_AUTHORITY
    if outcome == CLAIM_OUTCOME_ACCEPTED_WITH_WARNING:
        return USABILITY_CLASS_USABLE_WITH_WARNING
    return _OUTCOME_TO_USABILITY.get(outcome, USABILITY_CLASS_UNRESOLVED_UNKNOWN_OUTCOME)


@dataclass(frozen=True)
class EffectiveAssessment:
    """The effective state of ONE assertion under ONE declared context."""

    assertion_ref: str
    usability_class: str
    outcome: str | None
    assessment_id: str | None
    carried_by_commit: int | None
    snapshot_commit_id: int | None
    evidence_refs: tuple[str, ...]
    policy_id: str
    policy_revision: str
    workflow_class: str
    as_of_commit: int

    def __post_init__(self) -> None:
        if self.usability_class not in USABILITY_CLASSES:
            raise KernelError(
                f"invalid usability_class: {self.usability_class!r}"
            )
        if (
            self.outcome is None
            and self.usability_class != USABILITY_CLASS_UNRESOLVED_UNAVAILABLE
        ):
            raise KernelError(
                "an absent outcome only resolves to unresolved_unavailable"
            )

    @property
    def usable(self) -> bool:
        """Usable under this context: authority-bearing or warning-cleared."""
        return self.usability_class in (
            USABILITY_CLASS_USABLE_AUTHORITY,
            USABILITY_CLASS_USABLE_WITH_WARNING,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "assertion_ref": self.assertion_ref,
            "usability_class": self.usability_class,
            "usable": self.usable,
            "outcome": self.outcome,
            "assessment_id": self.assessment_id,
            "carried_by_commit": self.carried_by_commit,
            "snapshot_commit_id": self.snapshot_commit_id,
            "evidence_refs": list(self.evidence_refs),
            "resolved_under": {
                "policy_id": self.policy_id,
                "policy_revision": self.policy_revision,
                "workflow_class": self.workflow_class,
                "as_of_commit": self.as_of_commit,
            },
        }


def resolve_effective_assessments(
    assessments: Sequence[tuple[int, ClaimAssessmentRecord]],
    *,
    policy_id: str,
    policy_revision: str,
    workflow_class: str,
    as_of_commit: int,
    assertion_refs: Sequence[str] | None = None,
) -> dict[str, EffectiveAssessment]:
    """Resolve per-assertion effective assessments under one context.

    Each input item pairs the commit that carries the assessment record
    with the record itself. Context matching is exact — policy id,
    policy revision, and workflow class must equal the declared
    context — and a record is only visible at or before ``as_of_commit``
    by its carrying commit, so later commits never rewrite what an
    earlier cut knew. Among visible matches for one assertion, the
    latest ``(carried_by_commit, record_id)`` wins deterministically.

    The default domain is every assertion referenced by the input; pass
    ``assertion_refs`` for a bounded read. Assertions with no visible
    match resolve to ``unresolved_unavailable`` — the absence of a
    status under this context, never another region's status.
    """
    for name, value in (
        ("policy_id", policy_id),
        ("policy_revision", policy_revision),
        ("workflow_class", workflow_class),
    ):
        if not isinstance(value, str) or not value:
            raise KernelError(f"invalid {name}: {value!r}")
    if (
        not isinstance(as_of_commit, int)
        or isinstance(as_of_commit, bool)
        or as_of_commit < 0
    ):
        raise KernelError(f"invalid as_of_commit: {as_of_commit!r}")

    domain: list[str] = []
    seen: set[str] = set()
    latest: dict[str, tuple[tuple[int, str], int, ClaimAssessmentRecord]] = {}
    for carried_by, record in assessments:
        if not isinstance(carried_by, int) or isinstance(carried_by, bool):
            raise KernelError(f"invalid carrying commit: {carried_by!r}")
        if not isinstance(record, ClaimAssessmentRecord):
            raise KernelError(
                "assessments must be ClaimAssessmentRecord instances"
            )
        if record.assertion_ref not in seen:
            seen.add(record.assertion_ref)
            domain.append(record.assertion_ref)
        if (
            record.policy_id != policy_id
            or record.policy_revision != policy_revision
            or record.workflow_class != workflow_class
            or carried_by > as_of_commit
        ):
            continue
        key = (carried_by, record.record_id)
        current = latest.get(record.assertion_ref)
        if current is None or key > current[0]:
            latest[record.assertion_ref] = (key, carried_by, record)

    wanted = list(assertion_refs) if assertion_refs is not None else domain
    if assertion_refs is not None:
        for ref in wanted:
            if not isinstance(ref, str) or not ref:
                raise KernelError(f"invalid assertion ref: {ref!r}")

    view: dict[str, EffectiveAssessment] = {}
    for ref in wanted:
        entry = latest.get(ref)
        if entry is None:
            view[ref] = EffectiveAssessment(
                assertion_ref=ref,
                usability_class=USABILITY_CLASS_UNRESOLVED_UNAVAILABLE,
                outcome=None,
                assessment_id=None,
                carried_by_commit=None,
                snapshot_commit_id=None,
                evidence_refs=(),
                policy_id=policy_id,
                policy_revision=policy_revision,
                workflow_class=workflow_class,
                as_of_commit=as_of_commit,
            )
            continue
        _key, carried_by, record = entry
        view[ref] = EffectiveAssessment(
            assertion_ref=ref,
            usability_class=usability_class_for_outcome(record.outcome),
            outcome=record.outcome,
            assessment_id=record.record_id,
            carried_by_commit=carried_by,
            snapshot_commit_id=record.snapshot_commit_id,
            evidence_refs=tuple(record.evidence_refs),
            policy_id=policy_id,
            policy_revision=policy_revision,
            workflow_class=workflow_class,
            as_of_commit=as_of_commit,
        )
    return view


def summarize_regions(view: Mapping[str, EffectiveAssessment]) -> dict[str, Any]:
    """Summarize a resolved view without collapsing region states.

    The summary preserves every usability class as an explicit count and
    names one honest document-level label. There is deliberately no
    ``verified``/``unverified`` boolean: such a field could not preserve
    the distinction among authority-bearing, warning-cleared, uncertain,
    unavailable, abstained, and failed regions.
    """
    counts: dict[str, int] = {name: 0 for name in sorted(USABILITY_CLASSES)}
    for effective in view.values():
        counts[effective.usability_class] += 1
    resolved = sum(
        1 for effective in view.values() if effective.assessment_id is not None
    )
    usable = sum(1 for effective in view.values() if effective.usable)
    if resolved == 0:
        state = DOCUMENT_NO_RESOLVED_ASSESSMENTS
    elif usable == len(view):
        state = DOCUMENT_ALL_REGIONS_USABLE
    elif usable == 0:
        state = DOCUMENT_NO_USABLE_REGIONS
    else:
        state = DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS
    return {
        "region_count": len(view),
        "resolved_assessments": resolved,
        "usable_regions": usable,
        "unresolved_regions": len(view) - usable,
        "usability_counts": counts,
        "document_state": state,
    }


__all__ = [
    "DOCUMENT_ALL_REGIONS_USABLE",
    "DOCUMENT_USABLE_WITH_UNRESOLVED_REGIONS",
    "DOCUMENT_NO_USABLE_REGIONS",
    "DOCUMENT_NO_RESOLVED_ASSESSMENTS",
    "EffectiveAssessment",
    "USABILITY_CLASSES",
    "USABILITY_CLASS_USABLE_AUTHORITY",
    "USABILITY_CLASS_USABLE_WITH_WARNING",
    "USABILITY_CLASS_UNRESOLVED_UNCERTAIN",
    "USABILITY_CLASS_UNRESOLVED_UNAVAILABLE",
    "USABILITY_CLASS_UNRESOLVED_ABSTAINED",
    "USABILITY_CLASS_UNRESOLVED_FAILED",
    "USABILITY_CLASS_UNRESOLVED_UNKNOWN_OUTCOME",
    "resolve_effective_assessments",
    "summarize_regions",
    "usability_class_for_outcome",
]
