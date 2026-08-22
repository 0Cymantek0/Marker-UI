"""Answer-evidence error contract.

Contract failures are caller-visible ``ValueError`` subclasses, mapped to
the public agent error surface at the transport adapter. Ownership and
existence failures share one fail-closed shape: a caller cannot learn
whether a foreign id exists in another workspace.
"""

from __future__ import annotations

__all__ = [
    "AnswerEvidenceError",
    "AnswerEvidenceContractError",
    "AnswerTraceConflictError",
    "AssessmentConflictError",
    "DisclosureReferenceError",
]


class AnswerEvidenceError(ValueError):
    """Base class for answer-evidence boundary violations."""


class AnswerEvidenceContractError(AnswerEvidenceError):
    """Malformed request: bad shape, lengths, states, or references."""


class DisclosureReferenceError(AnswerEvidenceContractError):
    """A disclosed-context reference is missing, duplicated, or not owned.

    The message never distinguishes 'unknown in this workspace' from
    'exists in another workspace': both fail identically.
    """


class AnswerTraceConflictError(AnswerEvidenceError):
    """A committed answer identity received a contradictory payload.

    Reusing one ``answer_ref`` with different answer content or a
    different disclosed-context set is an explicit conflict, never a
    silent overwrite.
    """


class AssessmentConflictError(AnswerEvidenceError):
    """An assessment idempotency key was reused with a different payload."""
