"""Answer-evidence boundary package (PR85).

Separates three durable truths that retrieval provenance alone cannot
express: what context was actually delivered at the answer boundary
(:class:`~app.answer_evidence.service.AnswerEvidenceService.record_disclosure`),
which immutable answer that context binds to (``commit_trace``), and how
well a later independent judgment thinks the answer is supported
(``record_assessment``). See ``domain`` for the contract and ``service``
for the boundary authority.
"""

from __future__ import annotations

from .domain import (
    ANSWER_EVIDENCE_SCHEMA_VERSION,
    ANSWER_TRACE_SCHEMA_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    ASSESSMENT_VERDICTS,
    ASSESSOR_KINDS,
    UNASSESSED,
)
from .errors import (
    AnswerEvidenceContractError,
    AnswerEvidenceError,
    AnswerTraceConflictError,
    AssessmentConflictError,
    DisclosureReferenceError,
)
from .service import AnswerEvidenceService
from .store import AnswerEvidenceStore

__all__ = [
    "ANSWER_EVIDENCE_SCHEMA_VERSION",
    "ANSWER_TRACE_SCHEMA_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "ASSESSMENT_VERDICTS",
    "ASSESSOR_KINDS",
    "UNASSESSED",
    "AnswerEvidenceContractError",
    "AnswerEvidenceError",
    "AnswerEvidenceService",
    "AnswerEvidenceStore",
    "AnswerTraceConflictError",
    "AssessmentConflictError",
    "DisclosureReferenceError",
]
