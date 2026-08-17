"""Typed, bounded query contract (PR77).

The public query surface is a *typed request object*, never
agent-authored SQL or FTS5 syntax. This module defines the finite
operator set actually implemented, validates requests fail-closed
(unknown fields, unknown operators, unimplemented operators, malformed
values, over-budget operation counts), normalizes the request into a
deterministic canonical form for EvidencePacket identity, and compiles
lexical intent into a safely quoted FTS5 MATCH expression where caller
text can never become FTS5 grammar.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from app.context_runtime.errors import (
    QueryBudgetError,
    QueryContractError,
    UnsupportedOperatorError,
)
from app.kernel.publications import validate_publication_profile

__all__ = [
    "DEFAULT_QUERY_BUDGET",
    "FUTURE_OPERATIONS",
    "MAX_LEXICAL_LIMIT",
    "MAX_LEXICAL_TEXT_CHARS",
    "MAX_OPERATIONS_HARD",
    "QUERY_SCHEMA_VERSION",
    "SUPPORTED_OPERATIONS",
    "LexicalMode",
    "LexicalSearchOp",
    "QueryBudget",
    "QueryRequest",
    "QuerySecurityContext",
    "OutputDirective",
    "RecordGetOp",
    "compile_lexical_match",
    "normalized_query",
    "parse_query_request",
    "validate_request_budget",
]

#: Schema identity of the typed query contract itself. A request naming
#: anything else is rejected before execution.
QUERY_SCHEMA_VERSION = "marker.query.v1"

#: The complete, finite set of operators this version implements.
SUPPORTED_OPERATIONS = frozenset({"lexical_search", "record_get"})

#: Real operators of the masterplan's bounded query algebra that this
#: slice intentionally does not implement. They fail explicitly as
#: unsupported — never a silent lexical/text fallback.
FUTURE_OPERATIONS = frozenset(
    {
        "vector_search",
        "visual_search",
        "structural_traverse",
        "relation_traverse",
        "field_predicate",
        "aggregate",
        "compare_revisions",
        "rerank",
        "evidence_select",
    }
)

#: Absolute request-shape caps (independent of the per-request budget):
#: adversarial request sizes are rejected at the contract, not clamped.
MAX_OPERATIONS_HARD = 32
MAX_LEXICAL_LIMIT = 200
MAX_LEXICAL_TEXT_CHARS = 512


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


LexicalMode = Literal["all_terms", "any_term", "phrase"]


class LexicalSearchOp(_StrictModel):
    """Typed text search intent. ``text`` is plain text: the server
    tokenizes and quotes it, so punctuation, quotes, and boolean-looking
    words can never become FTS5 operators."""

    op: Literal["lexical_search"]
    text: str
    mode: LexicalMode = "all_terms"
    limit: int = Field(default=25, ge=1, le=MAX_LEXICAL_LIMIT)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", value)
        # Collapse whitespace: leading/trailing/repeated whitespace is
        # not semantic lexical intent, and the canonical form must make
        # "  needle  " and "needle" the same query for packet identity.
        normalized = " ".join(normalized.split())
        if not normalized:
            raise ValueError("lexical text must contain non-whitespace characters")
        if len(normalized) > MAX_LEXICAL_TEXT_CHARS:
            raise ValueError(
                f"lexical text longer than {MAX_LEXICAL_TEXT_CHARS} characters"
            )
        for token in normalized.split():
            if not any(ch.isalnum() for ch in token):
                raise ValueError(
                    f"lexical term {token!r} contains no searchable characters"
                )
        return normalized


class RecordGetOp(_StrictModel):
    """Exact selection of one published record, optionally focused on
    one content node of a view document."""

    op: Literal["record_get"]
    record_id: str = Field(min_length=1, max_length=256)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)


class OutputDirective(_StrictModel):
    include_text: bool = True


class QuerySecurityContext(_StrictModel):
    """Caller-supplied opaque identity dimensions for packet reuse.

    These fields are *identity seams*, not authorization proof: PR78
    will bind real policy semantics to them. They participate in
    EvidencePacket identity exactly as supplied."""

    security_context_id: str | None = Field(default=None, max_length=256)
    verifier_policy_id: str | None = Field(default=None, max_length=256)
    redaction_profile_id: str | None = Field(default=None, max_length=256)
    serialization_profile: str = Field(default="default", max_length=64)


class QueryBudget(_StrictModel):
    """Structural execution bounds. All values are hard limits: the
    executor never silently exceeds one, it reports omissions."""

    max_operations: int = Field(default=8, ge=1, le=MAX_OPERATIONS_HARD)
    max_candidates: int = Field(default=200, ge=1)
    max_evidence_units: int = Field(default=50, ge=1)
    max_output_chars: int = Field(default=100_000, ge=1)


DEFAULT_QUERY_BUDGET = QueryBudget()


Operation = Annotated[Union[LexicalSearchOp, RecordGetOp], Field(discriminator="op")]


class QueryRequest(_StrictModel):
    schema_version: str
    workspace_id: str = Field(min_length=1, max_length=256)
    profile: str = Field(default="default", min_length=1, max_length=64)
    operations: list[Operation] = Field(min_length=1, max_length=MAX_OPERATIONS_HARD)
    output: OutputDirective = Field(default_factory=OutputDirective)
    context: QuerySecurityContext = Field(default_factory=QuerySecurityContext)
    budget: QueryBudget = Field(default_factory=QueryBudget)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != QUERY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported query schema version {value!r}; expected "
                f"{QUERY_SCHEMA_VERSION!r}"
            )
        return value

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, value: str) -> str:
        try:
            validate_publication_profile(value)
        except Exception as exc:
            raise ValueError(f"invalid publication profile: {exc}") from exc
        return value


def parse_query_request(data: Mapping[str, Any]) -> QueryRequest:
    """Validate one raw request mapping into a :class:`QueryRequest`.

    Classification is fail-closed and explicit:

    - malformed structure / unknown fields / bad values →
      :class:`QueryContractError`;
    - a real but unimplemented operator → :class:`UnsupportedOperatorError`
      (no fallback);
    - a well-formed request whose operation count exceeds its budget →
      :class:`QueryBudgetError`.
    """
    if not isinstance(data, Mapping):
        raise QueryContractError("query request must be a mapping")
    raw_ops = data.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise QueryContractError("operations must be a non-empty list")
    for entry in raw_ops:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("op"), str):
            raise QueryContractError(
                "each operation must be an object with a string 'op'"
            )
        op_name = entry["op"]
        if op_name in SUPPORTED_OPERATIONS:
            continue
        if op_name in FUTURE_OPERATIONS:
            raise UnsupportedOperatorError(
                f"operator {op_name!r} is not implemented in this version; "
                f"supported operators: {sorted(SUPPORTED_OPERATIONS)}"
            )
        raise QueryContractError(
            f"unknown operator {op_name!r}; supported operators: "
            f"{sorted(SUPPORTED_OPERATIONS)}"
        )
    try:
        request = QueryRequest.model_validate(dict(data))
    except ValidationError as exc:
        raise QueryContractError(f"invalid query request: {exc}") from exc
    validate_request_budget(request)
    return request


def validate_request_budget(request: QueryRequest) -> None:
    """Reject structurally valid requests that are over budget before
    any expensive execution happens."""
    if len(request.operations) > request.budget.max_operations:
        raise QueryBudgetError(
            f"request has {len(request.operations)} operations; budget "
            f"allows {request.budget.max_operations}"
        )


def normalized_query(request: QueryRequest) -> dict[str, Any]:
    """Deterministic canonical form of the request for packet identity.

    Every value is normalized (NFC text, fixed key order, explicit
    defaults) so two requests that mean the same thing produce the same
    normalized form — and any semantic difference changes it.
    """
    operations: list[dict[str, Any]] = []
    for op in request.operations:
        if isinstance(op, LexicalSearchOp):
            operations.append(
                {
                    "op": "lexical_search",
                    "text": op.text,
                    "mode": op.mode,
                    "limit": op.limit,
                }
            )
        else:
            operations.append(
                {
                    "op": "record_get",
                    "record_id": op.record_id,
                    "node_id": op.node_id,
                }
            )
    return {
        "schema_version": request.schema_version,
        "workspace_id": request.workspace_id,
        "profile": request.profile,
        "operations": operations,
        "output": {"include_text": request.output.include_text},
        "context": {
            "security_context_id": request.context.security_context_id,
            "verifier_policy_id": request.context.verifier_policy_id,
            "redaction_profile_id": request.context.redaction_profile_id,
            "serialization_profile": request.context.serialization_profile,
        },
        "budget": {
            "max_operations": request.budget.max_operations,
            "max_candidates": request.budget.max_candidates,
            "max_evidence_units": request.budget.max_evidence_units,
            "max_output_chars": request.budget.max_output_chars,
        },
    }


def _quote(term: str) -> str:
    """Quote one term as an FTS5 phrase so its bytes can never be
    parsed as MATCH grammar (inner quotes are doubled per FTS5)."""
    return '"' + term.replace('"', '""') + '"'


def compile_lexical_match(text: str, mode: LexicalMode) -> str:
    """Compile typed lexical intent into a bounded, safely quoted FTS5
    MATCH expression.

    ``text`` must already be validated (NFC, non-empty, every token
    contains a searchable character). Tokens are whitespace-separated;
    each becomes a quoted phrase, so ``OR``, ``NEAR(...)``, column
    filters, and bareword operators inside user text are always literal
    content.
    """
    tokens = text.split()
    if not tokens:
        raise QueryContractError("lexical text must contain non-whitespace characters")
    if mode == "phrase":
        return _quote(" ".join(tokens))
    joiner = " AND " if mode == "all_terms" else " OR "
    return joiner.join(_quote(token) for token in tokens)
