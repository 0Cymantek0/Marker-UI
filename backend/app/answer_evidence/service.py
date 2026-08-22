"""Answer-evidence service: the answer/disclosure boundary authority (PR85).

This service owns three write/read seams over the durable store:

* :meth:`record_disclosure` — called at the moment a query-result page
  crosses the boundary to the caller. The canonical EvidencePacket JSON
  is captured as answer-time truth; nothing is ever recomputed from
  later retrieval state.
* :meth:`commit_trace` — binds one externally produced answer to an
  ordered list of server-minted disclosures. Idempotent per
  ``(workspace, answer_ref)``; contradictory content or context is an
  explicit conflict.
* :meth:`record_assessment` — appends an independent support judgment.
  Assessments reference the immutable trace and can never mutate the
  answer: there is no code path that writes ``kernel_answer_traces``
  after commit.

Tenancy fails closed: every lookup is workspace-scoped, and a reference
to another workspace's disclosure is indistinguishable from a reference
to one that never existed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.models import KernelAnswerSupportAssessment

from . import domain
from .domain import (
    ANSWER_TRACE_SCHEMA_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    UNASSESSED,
    AssessorIdentity,
    answer_content_digest,
    assessment_payload_digest,
    canonical_claims_json,
    claims_view,
    context_fingerprint,
    new_assessment_id,
    new_disclosure_id,
    new_trace_id,
    validate_answer_content,
    validate_answer_ref,
    validate_assessment,
    validate_workspace_id,
)
from .errors import (
    AnswerEvidenceContractError,
    AnswerTraceConflictError,
    AssessmentConflictError,
    DisclosureReferenceError,
)
from .store import AnswerEvidenceStore

__all__ = ["AnswerEvidenceService"]

logger = logging.getLogger(__name__)

_MAX_RATIONALE_CHARS = domain.MAX_RATIONALE_CHARS
_MAX_ASSESSMENT_KEY_LENGTH = domain.MAX_ASSESSMENT_KEY_LENGTH
_MAX_ID_LENGTH = domain.MAX_ID_LENGTH

#: Page-level outcome statuses that constitute a disclosure event.
DELIVERY_STATUSES = frozenset({"complete", "partial", "loop_limit"})


def _utc_now_iso(value: Any) -> str:
    # Model timestamps are timezone-aware UTC datetimes; render stably.
    return value.isoformat() if value is not None else None


class AnswerEvidenceService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory
        self.store = AnswerEvidenceStore(session_factory)

    # ------------------------------------------------------------------
    # disclosure (delivery boundary)
    # ------------------------------------------------------------------

    async def record_disclosure(
        self,
        *,
        packet: Mapping[str, Any],
        workspace_id: str,
        principal_id: str | None = None,
        delivery_status: str,
    ) -> dict[str, Any]:
        """Durably record one delivered EvidencePacket page.

        ``packet`` is the packet's own serializable view
        (``packets.to_json``); ``delivery_status`` is the page-level
        outcome status at delivery (``complete`` / ``partial`` /
        ``loop_limit``), which is answer-time truth the packet body alone
        does not carry. The row is written before the caller can see the
        page: if this write fails, the page was not delivered as a
        disclosable result and the failure propagates.
        """

        validate_workspace_id(workspace_id)
        if delivery_status not in DELIVERY_STATUSES:
            raise AnswerEvidenceContractError(
                "delivery_status must be one of "
                f"{sorted(DELIVERY_STATUSES)}"
            )
        if principal_id is not None and (
            not isinstance(principal_id, str)
            or not 1 <= len(principal_id) <= domain.MAX_WORKSPACE_ID_LENGTH
        ):
            raise AnswerEvidenceContractError(
                "principal_id must be 1-128 characters or null"
            )
        packet_id = packet.get("identity_id")
        if not isinstance(packet_id, str) or not packet_id:
            raise AnswerEvidenceContractError(
                "packet view must carry its deterministic identity_id"
            )
        packet_json = _canonical(packet)
        disclosure_id = new_disclosure_id()
        row = await self.store.insert_disclosure(
            disclosure_id=disclosure_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            packet_id=packet_id,
            packet_json=packet_json,
            delivery_status=delivery_status,
        )
        return {
            "disclosure_id": row.disclosure_id,
            "packet_id": row.packet_id,
            "delivery_status": row.delivery_status,
            "created_at": _utc_now_iso(row.created_at),
        }

    # ------------------------------------------------------------------
    # answer trace (immutable binding)
    # ------------------------------------------------------------------

    async def commit_trace(
        self,
        *,
        workspace_id: str,
        answer_ref: str,
        answer_content: str,
        disclosure_ids: Sequence[str],
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind one answer to its ordered disclosed context, idempotently.

        Replaying the same ``(answer_ref, answer content, ordered
        disclosure set)`` returns the committed trace unchanged.
        Replaying the same ``answer_ref`` with different content or a
        different context set raises :class:`AnswerTraceConflictError`;
        history is never silently rewritten.
        """

        validate_workspace_id(workspace_id)
        validate_answer_ref(answer_ref)
        validate_answer_content(answer_content)
        ids = self._validate_disclosure_ids(disclosure_ids)

        disclosures = await self.store.load_disclosures(
            workspace_id=workspace_id, disclosure_ids=ids
        )
        if len(disclosures) != len(ids):
            raise DisclosureReferenceError(
                "one or more disclosure ids are unknown in this workspace; "
                "disclosed-context references must name pages this "
                "workspace actually received"
            )
        ordered = [disclosures[i] for i in ids]
        digest = answer_content_digest(answer_content)
        fingerprint = context_fingerprint(
            answer_digest=digest,
            ordered_disclosures=[
                {"disclosure_id": row.disclosure_id, "packet_id": row.packet_id}
                for row in ordered
            ],
        )

        existing = await self.store.load_trace(
            workspace_id=workspace_id, answer_ref=answer_ref
        )
        if existing is not None:
            return await self._resolve_existing_trace(existing, digest, fingerprint)

        trace_id = new_trace_id()
        try:
            await self.store.insert_trace(
                trace_id=trace_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                answer_ref=answer_ref,
                answer_digest=digest,
                answer_content=answer_content,
                context_fingerprint=fingerprint,
                ordered_disclosure_ids=ids,
            )
        except IntegrityError:
            # A concurrent commit claimed (workspace, answer_ref) first.
            existing = await self.store.load_trace(
                workspace_id=workspace_id, answer_ref=answer_ref
            )
            if existing is None:
                raise
            return await self._resolve_existing_trace(
                existing, digest, fingerprint
            )
        return await self.read_trace(workspace_id=workspace_id, trace_id=trace_id)

    async def _resolve_existing_trace(
        self, existing, digest: str, fingerprint: str
    ) -> dict[str, Any]:
        if (
            existing.answer_digest == digest
            and existing.context_fingerprint == fingerprint
        ):
            logger.info(
                "answer trace %s replayed identically; returning committed trace",
                existing.trace_id,
            )
            return await self._full_trace_view(existing)
        raise AnswerTraceConflictError(
            f"answer_ref {existing.answer_ref!r} is already committed with a "
            "different answer body or disclosed-context set; use a new "
            "answer_ref for a corrected answer instead of replaying this one"
        )

    async def read_trace(
        self,
        *,
        workspace_id: str,
        trace_id: str | None = None,
        answer_ref: str | None = None,
    ) -> dict[str, Any]:
        """Full trace view: answer, ordered disclosures, assessments."""

        validate_workspace_id(workspace_id)
        row = await self.store.load_trace(
            workspace_id=workspace_id, trace_id=trace_id, answer_ref=answer_ref
        )
        if row is None:
            raise AnswerEvidenceContractError(
                "answer trace not found in this workspace"
            )
        return await self._full_trace_view(row)

    # ------------------------------------------------------------------
    # support assessment (independent, append-only)
    # ------------------------------------------------------------------

    async def record_assessment(
        self,
        *,
        workspace_id: str,
        trace_id: str,
        verdict: str,
        claims: Sequence[Mapping[str, Any]],
        assessor: Mapping[str, Any],
        assessment_key: str,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Append one support judgment about one trace.

        The assessment validates against the immutable committed truth:
        claim spans must cover the stored answer, optional quote digests
        must match the stored text, and every cited evidence locator
        must exist inside a disclosure bound to the trace. The stored
        answer is never touched — there is no mutation path.
        """

        validate_workspace_id(workspace_id)
        if not isinstance(trace_id, str) or not 1 <= len(trace_id) <= _MAX_ID_LENGTH:
            raise AnswerEvidenceContractError("trace_id must be 1-128 characters")
        if (
            not isinstance(assessment_key, str)
            or not 1 <= len(assessment_key) <= _MAX_ASSESSMENT_KEY_LENGTH
            or assessment_key != assessment_key.strip()
        ):
            raise AnswerEvidenceContractError(
                f"assessment_key must be 1-{_MAX_ASSESSMENT_KEY_LENGTH} "
                "characters without surrounding whitespace"
            )
        if not isinstance(rationale, str) or len(rationale) > _MAX_RATIONALE_CHARS:
            raise AnswerEvidenceContractError(
                f"rationale must be text of at most {_MAX_RATIONALE_CHARS} characters"
            )
        try:
            assessor_model = AssessorIdentity.model_validate(dict(assessor))
        except Exception as exc:
            raise AnswerEvidenceContractError(
                f"invalid assessor identity: {exc}"
            ) from exc

        trace = await self.store.load_trace(
            workspace_id=workspace_id, trace_id=trace_id
        )
        if trace is None:
            raise AnswerEvidenceContractError(
                "answer trace not found in this workspace"
            )
        links = await self.store.load_trace_links(trace_id)
        disclosure_rows = await self.store.load_disclosures(
            workspace_id=workspace_id,
            disclosure_ids=[link.disclosure_id for link in links],
        )
        bound_packets = {
            link.disclosure_id: json.loads(
                disclosure_rows[link.disclosure_id].packet_json
            )
            for link in links
            if link.disclosure_id in disclosure_rows
        }
        verdict, parsed_claims = validate_assessment(
            verdict=verdict,
            claims=claims,
            answer_content=trace.answer_content,
            bound_disclosures=bound_packets,
        )
        payload_digest = assessment_payload_digest(
            verdict=verdict,
            claims=claims_view(parsed_claims),
            assessor=assessor_model.view(),
            rationale=rationale,
        )

        existing = await self.store.load_assessment_by_key(
            trace_id=trace_id, assessment_key=assessment_key
        )
        if existing is not None:
            return self._resolve_existing_assessment(existing, payload_digest)

        row = KernelAnswerSupportAssessment(
            assessment_id=new_assessment_id(),
            workspace_id=workspace_id,
            trace_id=trace_id,
            assessment_key=assessment_key,
            seq=await self.store.next_assessment_seq(trace_id),
            verdict=verdict,
            payload_digest=payload_digest,
            claims_json=canonical_claims_json(parsed_claims),
            assessor_kind=assessor_model.kind,
            assessor_id=assessor_model.assessor_id,
            procedure=assessor_model.procedure,
            procedure_version=assessor_model.procedure_version,
            rationale=rationale,
        )
        try:
            await self.store.insert_assessment(row)
        except IntegrityError:
            loser = await self.store.load_assessment_by_key(
                trace_id=trace_id, assessment_key=assessment_key
            )
            if loser is not None:
                return self._resolve_existing_assessment(loser, payload_digest)
            # Sequence race with a different concurrent key: recompute
            # the next sequence once so the append converges.
            row.seq = await self.store.next_assessment_seq(trace_id)
            try:
                await self.store.insert_assessment(row)
            except IntegrityError:
                loser = await self.store.load_assessment_by_key(
                    trace_id=trace_id, assessment_key=assessment_key
                )
                if loser is not None:
                    return self._resolve_existing_assessment(loser, payload_digest)
                raise
        return await self.read_trace(workspace_id=workspace_id, trace_id=trace_id)

    def _resolve_existing_assessment(
        self, existing: KernelAnswerSupportAssessment, payload_digest: str
    ) -> dict[str, Any]:
        if existing.payload_digest == payload_digest:
            logger.info(
                "assessment %s replayed identically; returning committed judgment",
                existing.assessment_id,
            )
            return self._assessment_view(existing)
        raise AssessmentConflictError(
            f"assessment_key {existing.assessment_key!r} is already recorded "
            "with a different verdict, claims, assessor, or rationale; use a "
            "new assessment_key for a revised judgment"
        )

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    async def _full_trace_view(self, row) -> dict[str, Any]:
        links = await self.store.load_trace_links(row.trace_id)
        disclosure_rows = await self.store.load_disclosures(
            workspace_id=row.workspace_id,
            disclosure_ids=[link.disclosure_id for link in links],
        )
        assessments = await self.store.load_assessments(
            workspace_id=row.workspace_id, trace_id=row.trace_id
        )
        view = self._trace_view(row)
        view["disclosures"] = [
            {
                "disclosure_id": link.disclosure_id,
                "packet_id": disclosure_rows[link.disclosure_id].packet_id,
                "delivery_status": disclosure_rows[
                    link.disclosure_id
                ].delivery_status,
                "packet": json.loads(
                    disclosure_rows[link.disclosure_id].packet_json
                ),
            }
            for link in links
            if link.disclosure_id in disclosure_rows
        ]
        view["assessments"] = [
            self._assessment_view(a) for a in assessments
        ]
        view["current_assessment"] = (
            self._assessment_view(assessments[-1]) if assessments else None
        )
        view["assessment_state"] = (
            assessments[-1].verdict if assessments else UNASSESSED
        )
        return view

    @staticmethod
    def _trace_view(row) -> dict[str, Any]:
        return {
            "schema_version": ANSWER_TRACE_SCHEMA_VERSION,
            "trace_id": row.trace_id,
            "workspace_id": row.workspace_id,
            "answer_ref": row.answer_ref,
            "answer_digest": row.answer_digest,
            "answer": row.answer_content,
            "context_fingerprint": row.context_fingerprint,
            "created_at": _utc_now_iso(row.created_at),
        }

    @staticmethod
    def _assessment_view(row: KernelAnswerSupportAssessment) -> dict[str, Any]:
        return {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": row.assessment_id,
            "trace_id": row.trace_id,
            "assessment_key": row.assessment_key,
            "seq": row.seq,
            "verdict": row.verdict,
            "claims": json.loads(row.claims_json),
            "assessor": {
                "kind": row.assessor_kind,
                "assessor_id": row.assessor_id,
                "procedure": row.procedure,
                "procedure_version": row.procedure_version,
            },
            "rationale": row.rationale,
            "created_at": _utc_now_iso(row.created_at),
        }

    @staticmethod
    def _validate_disclosure_ids(disclosure_ids: Sequence[str]) -> list[str]:
        if not isinstance(disclosure_ids, Sequence) or isinstance(
            disclosure_ids, (str, bytes)
        ):
            raise AnswerEvidenceContractError(
                "disclosure_ids must be a list of disclosure ids"
            )
        ids: list[str] = []
        seen: set[str] = set()
        for value in disclosure_ids:
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= _MAX_ID_LENGTH
            ):
                raise AnswerEvidenceContractError(
                    "each disclosure id must be 1-128 characters"
                )
            if value in seen:
                raise AnswerEvidenceContractError(
                    f"duplicate disclosure id {value!r}; the ordered context "
                    "set names each delivered page once"
                )
            seen.add(value)
            ids.append(value)
        return ids


def _canonical(packet: Mapping[str, Any]) -> str:
    """Stable storage serialization of one delivered packet view.

    Deliberately not the JCS identity profile: packet views carry
    finite float ranks, which the canonical identity contract rejects
    (and which the packet's own ``identity_id`` already excludes). The
    packet identity is stored separately as ``packet_id``; this JSON
    only needs deterministic bytes (sorted keys, fixed separators, no
    NaN/Infinity) for lossless answer-time reconstruction.
    """

    try:
        return json.dumps(
            dict(packet),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AnswerEvidenceContractError(
            f"packet view is not serializable JSON: {exc}"
        ) from exc
