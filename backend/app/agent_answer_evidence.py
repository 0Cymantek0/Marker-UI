"""Transport-neutral agent adapter over the answer-evidence service (PR85).

Mirrors the ``agent_query`` adapter pattern: this module is the
agent-facing seam over :class:`AnswerEvidenceService`. It never validates
domain rules itself — the service is the boundary authority — it maps
contract/conflict failures onto the public agent error surface
(``UsageError``) and threads the trusted caller principal into durable
ownership the same way query cursors do.

The answer boundary this adapter serves is honest about what Marker UI
can observe: answers are produced by an external agent, Marker UI records
exactly which disclosed pages the answer may be bound to, and support
assessments are independent append-only judgments that never rewrite the
committed answer.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.answer_evidence import AnswerEvidenceService
from app.answer_evidence.errors import AnswerEvidenceError
from app.database import async_session_factory
from app.db_migration import verify_database_ready
from app.errors import UsageError

__all__ = [
    "ANSWER_EVIDENCE_SCHEMA_VERSION",
    "configure_answer_evidence_runtime",
    "read_agent_answer_trace",
    "record_agent_answer_assessment",
    "record_agent_answer_trace",
    "record_agent_disclosure",
    "reset_answer_evidence_runtime",
]

logger = logging.getLogger(__name__)

ANSWER_EVIDENCE_SCHEMA_VERSION = "marker.answer_evidence.v1"

_session_factory: async_sessionmaker = async_session_factory
_db_ready = False
_service: AnswerEvidenceService | None = None


def configure_answer_evidence_runtime(
    session_factory: async_sessionmaker,
) -> None:
    """Point the answer-evidence runtime at a specific session factory."""

    global _session_factory, _service
    _session_factory = session_factory
    _service = None


def reset_answer_evidence_runtime() -> None:
    """Drop cached service state so the next call re-resolves configuration."""

    global _service
    _service = None


async def _ensure_db_ready() -> None:
    global _db_ready
    if _db_ready or _session_factory is not async_session_factory:
        return
    await verify_database_ready()
    _db_ready = True


def _service_instance() -> AnswerEvidenceService:
    global _service
    if _service is None:
        _service = AnswerEvidenceService(_session_factory)
    return _service


def _envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Transport envelope: one schema_version for the seam, the record's
    own schema preserved as ``record_schema_version``."""

    body = dict(payload)
    record_schema = body.pop("schema_version", None)
    envelope: dict[str, Any] = {
        "schema_version": ANSWER_EVIDENCE_SCHEMA_VERSION,
        **body,
    }
    if record_schema is not None:
        envelope["record_schema_version"] = record_schema
    return envelope


async def record_agent_disclosure(
    *,
    packet: Mapping[str, Any],
    workspace_id: str,
    delivery_status: str,
    principal_id: str | None = None,
) -> str:
    """Durably record one delivered packet page; return its disclosure id.

    Called by the query adapter at the delivery boundary, before the
    page is returned to the caller. Propagates storage failures: a page
    that cannot be recorded as disclosed is not delivered as one.
    """

    await _ensure_db_ready()
    try:
        view = await _service_instance().record_disclosure(
            packet=packet,
            workspace_id=workspace_id,
            principal_id=principal_id,
            delivery_status=delivery_status,
        )
    except AnswerEvidenceError as exc:
        raise UsageError(str(exc)) from exc
    return view["disclosure_id"]


async def record_agent_answer_trace(
    *,
    workspace_id: str,
    answer_ref: str,
    answer: str,
    disclosure_ids: Sequence[str],
    principal_id: str | None = None,
) -> dict[str, Any]:
    """Commit one external answer bound to its disclosed context.

    Idempotent for an identical replay of ``(answer_ref, answer,
    ordered disclosure set)``; any different body or context set for the
    same ``answer_ref`` is an explicit usage conflict.
    """

    await _ensure_db_ready()
    try:
        view = await _service_instance().commit_trace(
            workspace_id=workspace_id,
            answer_ref=answer_ref,
            answer_content=answer,
            disclosure_ids=disclosure_ids,
            principal_id=principal_id,
        )
    except AnswerEvidenceError as exc:
        raise UsageError(str(exc)) from exc
    return _envelope(view)


async def read_agent_answer_trace(
    *,
    workspace_id: str,
    trace_id: str | None = None,
    answer_ref: str | None = None,
) -> dict[str, Any]:
    """Read one committed trace with its ordered disclosures and the
    full append-only assessment history."""

    await _ensure_db_ready()
    try:
        view = await _service_instance().read_trace(
            workspace_id=workspace_id,
            trace_id=trace_id,
            answer_ref=answer_ref,
        )
    except AnswerEvidenceError as exc:
        raise UsageError(str(exc)) from exc
    return _envelope(view)


async def record_agent_answer_assessment(
    *,
    workspace_id: str,
    trace_id: str,
    verdict: str,
    claims: Sequence[Mapping[str, Any]],
    assessor: Mapping[str, Any],
    assessment_key: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Append one independent support assessment for a committed trace.

    The assessment never modifies the committed answer; it validates
    against it (spans, quote digests, delivered-evidence citations) and
    is stored with its own provenance in ``assessor`` so tool, model,
    and human judgments stay distinguishable.
    """

    await _ensure_db_ready()
    try:
        view = await _service_instance().record_assessment(
            workspace_id=workspace_id,
            trace_id=trace_id,
            verdict=verdict,
            claims=claims,
            assessor=assessor,
            assessment_key=assessment_key,
            rationale=rationale,
        )
    except AnswerEvidenceError as exc:
        raise UsageError(str(exc)) from exc
    return _envelope(view)
