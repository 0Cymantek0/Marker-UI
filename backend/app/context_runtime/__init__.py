"""Context Runtime: bounded typed queries and EvidencePackets (PR77/PR78).

Server-side query core standing on the PR76 publication substrate:
a typed, finite request contract (never agent-authored SQL/FTS5
syntax), execution pinned to exactly one PublicationSet per query,
authorization derived from trusted committed policy state before any
candidate can compete for delivery, structural whole-unit budgets, and
deterministic invalidation-aware EvidencePacket identity.

Transport-agnostic by design: ``execute_query`` is the internal
application-service callable a future agent/tool transport wraps.
No externally reachable retrieval endpoint is exposed at this layer on
purpose — publishing one requires the PR79 transport work to carry the
authorization contract, not just the query contract.
"""

from app.context_runtime.authorization import (
    ASSURANCE_HIGH,
    ASSURANCE_STANDARD,
    AUTHORIZATION_PROFILE_LOCAL,
    EffectiveAuthorization,
    resolve_effective_authorization,
)
from app.context_runtime.contract import (
    DEFAULT_QUERY_BUDGET,
    FUTURE_OPERATIONS,
    MAX_LEXICAL_LIMIT,
    MAX_LEXICAL_TEXT_CHARS,
    MAX_OPERATIONS_HARD,
    QUERY_SCHEMA_VERSION,
    SUPPORTED_OPERATIONS,
    LexicalSearchOp,
    QueryAssurance,
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
    QueryAuthorizationError,
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
    "ASSURANCE_HIGH",
    "ASSURANCE_STANDARD",
    "AUTHORIZATION_PROFILE_LOCAL",
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
    "EffectiveAuthorization",
    "EvidenceLocator",
    "EvidencePacket",
    "EvidenceUnit",
    "LexicalSearchOp",
    "OmittedEvidence",
    "OutputDirective",
    "QueryAssurance",
    "QueryAuthorizationError",
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
    "resolve_effective_authorization",
    "to_json",
    "validate_request_budget",
]
