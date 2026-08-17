"""Context Runtime: bounded typed queries and EvidencePackets (PR77).

Server-side query core standing on the PR76 publication substrate:
a typed, finite request contract (never agent-authored SQL/FTS5
syntax), execution pinned to exactly one PublicationSet per query,
structural whole-unit budgets, and deterministic invalidation-aware
EvidencePacket identity.

Transport-agnostic by design: ``execute_query`` is the internal
application-service callable a future agent/tool transport (and the
PR78 authorization-first layer) wraps. No externally reachable
retrieval endpoint is exposed at this layer on purpose — publishing
one before authorization exists would permanently bypass PR78.
"""

from app.context_runtime.contract import (
    DEFAULT_QUERY_BUDGET,
    FUTURE_OPERATIONS,
    MAX_LEXICAL_LIMIT,
    MAX_LEXICAL_TEXT_CHARS,
    MAX_OPERATIONS_HARD,
    QUERY_SCHEMA_VERSION,
    SUPPORTED_OPERATIONS,
    LexicalSearchOp,
    QueryBudget,
    QueryRequest,
    QuerySecurityContext,
    OutputDirective,
    RecordGetOp,
    compile_lexical_match,
    normalized_query,
    parse_query_request,
    validate_request_budget,
)
from app.context_runtime.errors import (
    QueryBudgetError,
    QueryContractError,
    QueryError,
    UnsupportedOperatorError,
)
from app.context_runtime.executor import execute_query
from app.context_runtime.packets import (
    EVIDENCE_PACKET_SCHEMA_VERSION,
    BudgetReport,
    CandidateUnit,
    EvidenceLocator,
    EvidencePacket,
    EvidenceUnit,
    OmittedEvidence,
    assemble_packet,
    packet_identity_dimensions,
    to_json,
)

__all__ = [
    "DEFAULT_QUERY_BUDGET",
    "EVIDENCE_PACKET_SCHEMA_VERSION",
    "FUTURE_OPERATIONS",
    "MAX_LEXICAL_LIMIT",
    "MAX_LEXICAL_TEXT_CHARS",
    "MAX_OPERATIONS_HARD",
    "QUERY_SCHEMA_VERSION",
    "SUPPORTED_OPERATIONS",
    "BudgetReport",
    "CandidateUnit",
    "EvidenceLocator",
    "EvidencePacket",
    "EvidenceUnit",
    "LexicalSearchOp",
    "OmittedEvidence",
    "OutputDirective",
    "QueryBudget",
    "QueryBudgetError",
    "QueryContractError",
    "QueryError",
    "QueryRequest",
    "QuerySecurityContext",
    "RecordGetOp",
    "UnsupportedOperatorError",
    "assemble_packet",
    "compile_lexical_match",
    "execute_query",
    "normalized_query",
    "packet_identity_dimensions",
    "parse_query_request",
    "to_json",
    "validate_request_budget",
]
