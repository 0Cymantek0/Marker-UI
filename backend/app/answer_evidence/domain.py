"""Answer-evidence domain contract: disclosed context, answer trace, and
support assessment as three separate durable truths (PR85).

This package implements the masterplan §9C.11 separation:

* retrieval provenance stays in ``EvidencePacket`` / ``AgentRetrievalOutcome``
  and is never mutated into an answer-correctness claim;
* a :class:`ContextDisclosure` records one page of context that Marker UI
  actually delivered to the caller — the strongest disclosure fact this
  architecture can observe, because answer generation is external;
* an :class:`AnswerContextTrace` binds one externally produced answer to
  an ordered list of disclosures, immutably;
* an :class:`AnswerSupportAssessment` is a later, independent judgment
  with its own provenance that can never rewrite the answer or the trace.

Marker UI does not host the generation model, so "what the model attended
to" is not observable here. The honest claim is *delivered context*:
stronger than "candidates retrieved", weaker than "the model used these
tokens". Nothing in this module or its storage may phrase a trace as
proof that an answer is entailed by its context.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.canonical import (
    canonical_json_str,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

from .errors import AnswerEvidenceContractError

__all__ = [
    "ANSWER_EVIDENCE_SCHEMA_VERSION",
    "ANSWER_TRACE_SCHEMA_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "ASSESSMENT_VERDICTS",
    "AssessmentVerdict",
    "ASSESSOR_KINDS",
    "MAX_ANSWER_CHARS",
    "MAX_CLAIMS",
    "MAX_ANSWER_REF_LENGTH",
    "MAX_ASSESSMENT_KEY_LENGTH",
    "MAX_ASSESSOR_ID_LENGTH",
    "MAX_PROCEDURE_LENGTH",
    "MAX_PROCEDURE_VERSION_LENGTH",
    "MAX_RATIONALE_CHARS",
    "MAX_CLAIM_NOTE_CHARS",
    "MAX_CLAIM_ID_LENGTH",
    "UNASSESSED",
    "EvidenceRef",
    "ClaimSpan",
    "AssessedClaim",
    "AssessorIdentity",
    "new_disclosure_id",
    "new_trace_id",
    "new_assessment_id",
    "answer_content_digest",
    "context_fingerprint",
    "assessment_payload_digest",
    "validate_answer_content",
    "validate_answer_ref",
    "validate_workspace_id",
    "validate_assessment",
]

#: Framing identity of the answer-evidence records themselves.
ANSWER_EVIDENCE_SCHEMA_VERSION = "marker.answer_evidence.v1"
ANSWER_TRACE_SCHEMA_VERSION = "marker.answer_trace.v1"
ASSESSMENT_SCHEMA_VERSION = "marker.answer_support.v1"

#: The complete verdict vocabulary. ``unassessed`` is deliberately NOT a
#: verdict: absence of assessment is the absence of a record, never a
#: synthetic "supported by default" state.
ASSESSMENT_VERDICTS = frozenset({"supported", "unsupported", "uncertain"})
AssessmentVerdict = Literal["supported", "unsupported", "uncertain"]
UNASSESSED = "unassessed"

#: Legitimate assessor provenance classes. An assessment always names who
#: or what produced it; the lineage is itself evidence (§9C.11).
ASSESSOR_KINDS = frozenset({"human", "model", "tool", "rule"})

MAX_ANSWER_CHARS = 65_536
MAX_CLAIMS = 64
MAX_ANSWER_REF_LENGTH = 256
MAX_ASSESSMENT_KEY_LENGTH = 256
MAX_ASSESSOR_ID_LENGTH = 256
MAX_PROCEDURE_LENGTH = 256
MAX_PROCEDURE_VERSION_LENGTH = 64
MAX_RATIONALE_CHARS = 4096
MAX_CLAIM_NOTE_CHARS = 1024
MAX_CLAIM_ID_LENGTH = 256
MAX_ID_LENGTH = 128
MAX_WORKSPACE_ID_LENGTH = 128


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


def _token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(24)}"


def new_disclosure_id() -> str:
    """Fresh opaque identity for one delivered-context record."""

    return _token("dsc_")


def new_trace_id() -> str:
    """Fresh opaque identity for one answer-context trace."""

    return _token("trc_")


def new_assessment_id() -> str:
    """Fresh opaque identity for one support assessment."""

    return _token("asm_")


def validate_workspace_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_WORKSPACE_ID_LENGTH
        or value != value.strip()
    ):
        raise AnswerEvidenceContractError(
            "workspace_id must be 1-128 characters without surrounding whitespace"
        )
    return value


def validate_answer_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_ANSWER_REF_LENGTH
        or value != value.strip()
    ):
        raise AnswerEvidenceContractError(
            f"answer_ref must be 1-{MAX_ANSWER_REF_LENGTH} characters "
            "without surrounding whitespace"
        )
    return value


def validate_answer_content(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnswerEvidenceContractError(
            "answer content must be non-empty text"
        )
    if len(value) > MAX_ANSWER_CHARS:
        raise AnswerEvidenceContractError(
            f"answer content longer than {MAX_ANSWER_CHARS} characters; "
            "commit a bounded answer body"
        )
    return value


def answer_content_digest(content: str) -> str:
    """Digest over the exact committed answer text."""

    return payload_byte_hash(content.encode("utf-8"))


def context_fingerprint(
    *,
    answer_digest: str,
    ordered_disclosures: Sequence[Mapping[str, str]],
) -> str:
    """Deterministic identity of (answer, ordered disclosed context).

    Any change to the answer content or to the ordered disclosure set —
    including order alone — changes the fingerprint, so replaying one
    ``answer_ref`` against a different context is always detectable.
    """

    return record_identity_hash(
        record_type="marker.answer_evidence.trace_context",
        schema_version="1.0.0",
        payload={
            "answer_digest": answer_digest,
            "disclosures": [
                {
                    "disclosure_id": item["disclosure_id"],
                    "packet_id": item["packet_id"],
                }
                for item in ordered_disclosures
            ],
        },
    )


def assessment_payload_digest(
    *,
    verdict: str,
    claims: Sequence[Mapping[str, Any]],
    assessor: Mapping[str, Any],
    rationale: str,
) -> str:
    """Digest over everything an assessment says, for idempotent retries."""

    return record_identity_hash(
        record_type="marker.answer_evidence.assessment_payload",
        schema_version="1.0.0",
        payload={
            "verdict": verdict,
            "claims": to_json_ready(list(claims)),
            "assessor": to_json_ready(dict(assessor)),
            "rationale": rationale,
        },
    )


class EvidenceRef(_StrictModel):
    """Reference from one assessed claim back to disclosed evidence.

    ``disclosure_id`` must name a disclosure already bound to the trace;
    the locator fields must match an evidence unit inside that
    disclosure's packet, so an assessment can never cite evidence that
    was not actually delivered for the answer.
    """

    disclosure_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    record_id: str = Field(min_length=1, max_length=256)
    view_id: str = Field(min_length=1, max_length=256)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)

    def locator_view(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "view_id": self.view_id,
            "node_id": self.node_id,
        }


class ClaimSpan(_StrictModel):
    """Half-open character span ``[start, end)`` into the committed answer.

    ``quote_digest`` is optional; when supplied it must equal the digest
    of the stored answer slice, so a claim cannot quote text the answer
    never contained.
    """

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote_digest: str | None = Field(
        default=None, min_length=1, max_length=128
    )

    def covers(self, answer: str) -> bool:
        return self.start < self.end <= len(answer)

    def quote_matches(self, answer: str) -> bool:
        if self.quote_digest is None:
            return True
        return (
            payload_byte_hash(answer[self.start : self.end].encode("utf-8"))
            == self.quote_digest
        )


class AssessedClaim(_StrictModel):
    """One material answer claim and its support verdict."""

    claim_id: str = Field(min_length=1, max_length=MAX_CLAIM_ID_LENGTH)
    span: ClaimSpan
    verdict: AssessmentVerdict
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=32)
    note: str | None = Field(default=None, max_length=MAX_CLAIM_NOTE_CHARS)

    @field_validator("evidence", "note")
    @classmethod
    def _reject_empty_note(cls, value: Any, info) -> Any:
        if info.field_name == "note" and value is not None and not value.strip():
            raise ValueError("note must be non-blank text when provided")
        return value


class AssessorIdentity(_StrictModel):
    """Provenance of who/what produced an assessment."""

    kind: Literal["human", "model", "tool", "rule"]
    assessor_id: str = Field(min_length=1, max_length=MAX_ASSESSOR_ID_LENGTH)
    procedure: str = Field(min_length=1, max_length=MAX_PROCEDURE_LENGTH)
    procedure_version: str = Field(
        min_length=1, max_length=MAX_PROCEDURE_VERSION_LENGTH
    )

    def view(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "assessor_id": self.assessor_id,
            "procedure": self.procedure,
            "procedure_version": self.procedure_version,
        }


def validate_assessment(
    *,
    verdict: str,
    claims: Sequence[Mapping[str, Any]] | Sequence[AssessedClaim],
    answer_content: str,
    bound_disclosures: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[AssessedClaim]]:
    """Validate an assessment against the immutable answer/trace truth.

    ``bound_disclosures`` maps disclosure_id -> packet view for every
    disclosure bound to the trace. Every claim span must cover the stored
    answer, every optional quote digest must match the stored slice, and
    every evidence reference must resolve to a unit that was actually
    delivered inside the referenced disclosure.
    """

    if verdict not in ASSESSMENT_VERDICTS:
        raise AnswerEvidenceContractError(
            f"verdict must be one of {sorted(ASSESSMENT_VERDICTS)}; "
            f"an unassessed trace simply has no assessment record"
        )
    if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
        raise AnswerEvidenceContractError("claims must be a list")
    if len(claims) > MAX_CLAIMS:
        raise AnswerEvidenceContractError(
            f"at most {MAX_CLAIMS} material claims per assessment"
        )
    if verdict == "supported" and not claims:
        raise AnswerEvidenceContractError(
            "a 'supported' verdict must enumerate the material claims it covers"
        )
    parsed: list[AssessedClaim] = []
    seen_claim_ids: set[str] = set()
    for raw in claims:
        try:
            claim = raw if isinstance(raw, AssessedClaim) else AssessedClaim.model_validate(raw)
        except Exception as exc:
            raise AnswerEvidenceContractError(f"invalid assessed claim: {exc}") from exc
        if claim.claim_id in seen_claim_ids:
            raise AnswerEvidenceContractError(
                f"duplicate claim_id {claim.claim_id!r}"
            )
        seen_claim_ids.add(claim.claim_id)
        if not claim.span.covers(answer_content):
            raise AnswerEvidenceContractError(
                f"claim {claim.claim_id!r} span [{claim.span.start},"
                f"{claim.span.end}) does not cover the committed answer "
                f"({len(answer_content)} characters)"
            )
        if not claim.span.quote_matches(answer_content):
            raise AnswerEvidenceContractError(
                f"claim {claim.claim_id!r} quote_digest does not match the "
                "committed answer text at its span"
            )
        if claim.verdict == "supported" and not claim.evidence:
            raise AnswerEvidenceContractError(
                f"supported claim {claim.claim_id!r} must cite evidence"
            )
        for ref in claim.evidence:
            packet = bound_disclosures.get(ref.disclosure_id)
            if packet is None:
                raise AnswerEvidenceContractError(
                    f"claim {claim.claim_id!r} cites disclosure "
                    f"{ref.disclosure_id!r} which is not bound to this trace"
                )
            if not _unit_exists(packet, ref):
                raise AnswerEvidenceContractError(
                    f"claim {claim.claim_id!r} cites evidence that was not "
                    f"delivered in disclosure {ref.disclosure_id!r}"
                )
        parsed.append(claim)
    return verdict, parsed


def _unit_exists(packet: Mapping[str, Any], ref: EvidenceRef) -> bool:
    for unit in packet.get("evidence", ()):
        if (
            unit.get("record_id") == ref.record_id
            and unit.get("view_id") == ref.view_id
            and unit.get("node_id") == ref.node_id
        ):
            return True
    return False


def claims_view(claims: Sequence[AssessedClaim]) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim.claim_id,
            "span": {
                "start": claim.span.start,
                "end": claim.span.end,
                "quote_digest": claim.span.quote_digest,
            },
            "verdict": claim.verdict,
            "evidence": [
                {"disclosure_id": ref.disclosure_id, **ref.locator_view()}
                for ref in claim.evidence
            ],
            "note": claim.note,
        }
        for claim in claims
    ]


def canonical_claims_json(claims: Sequence[AssessedClaim]) -> str:
    return canonical_json_str(to_json_ready(claims_view(claims)))
