"""Typed errors for the bounded query / EvidencePacket core (PR77).

These errors are deliberately separate from kernel errors: the query
contract layer validates a *request* before any kernel read happens,
while kernel integrity errors (e.g. ``PublicationIntegrityError``,
``LexicalQueryError``) keep their authoritative meanings and propagate
unchanged through execution.
"""

from __future__ import annotations

__all__ = [
    "QueryError",
    "QueryAuthorizationError",
    "QueryBudgetError",
    "QueryContractError",
    "UnsupportedOperatorError",
]


class QueryError(Exception):
    """Base class for typed query-contract failures."""


class QueryAuthorizationError(QueryError):
    """Trusted authorization state could not be resolved, or a required
    authorization-bound resource (for example a high-assurance
    partition) is not available. Resolution fails closed: the query is
    refused rather than falling back to weaker or unrestricted
    retrieval."""


class QueryContractError(QueryError):
    """The request is not a valid typed query: malformed structure,
    unknown fields/operators, bad values, or an unsupported schema
    version. The request was rejected before any execution."""


class UnsupportedOperatorError(QueryError):
    """The request names a real but intentionally unimplemented operator
    (for example vector search). There is no fallback: the caller must
    issue a different typed request."""


class QueryBudgetError(QueryError):
    """The request is structurally valid but exceeds a configured
    operation/cost bound before execution starts."""
