"""Transport-neutral agent adapter over the snapshot-safe query service (PR79B).

This module is the agent-facing seam over ``ContinuationService``. It keeps the
typed ``marker.query.v1`` contract authoritative, maps continuation outcomes
into the machine-readable ``marker.query_result.v1`` envelope, and threads a
trusted caller principal into the durable cursor binding. It never re-plans,
re-executes, or re-pages queries itself: the context runtime stays the only
query authority.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime import (
    ContinuationService,
    CursorCodec,
    CursorKeyring,
    parse_query_request,
)
from app.context_runtime.continuation import ContinuationOutcome
from app.context_runtime.errors import (
    QueryBudgetError,
    QueryContractError,
    UnsupportedOperatorError,
)
from app.context_runtime.packets import to_json as packet_to_json
from app.core.config import DATA_DIR
from app.database import async_session_factory
from app.db_migration import verify_database_ready
from app.errors import UsageError

__all__ = [
    "QUERY_RESULT_SCHEMA_VERSION",
    "configure_query_runtime",
    "reset_query_runtime",
    "run_agent_query",
]

logger = logging.getLogger(__name__)

QUERY_RESULT_SCHEMA_VERSION = "marker.query_result.v1"

_KEY_DERIVATION_LABEL = b"marker.query.cursor.v1"
_DERIVED_KEY_ID = "derived"
_EPHEMERAL_KEY_ID = "ephemeral"

_session_factory: async_sessionmaker = async_session_factory
_db_ready = False
_service: ContinuationService | None = None


def configure_query_runtime(session_factory: async_sessionmaker) -> None:
    """Point the query runtime at a specific session factory (tests/tools)."""

    global _session_factory, _service
    _session_factory = session_factory
    _service = None


def reset_query_runtime() -> None:
    """Drop cached service state so the next call re-resolves configuration."""

    global _service
    _service = None


def _cursor_keyring() -> CursorKeyring:
    """Resolve the cursor HMAC key from trusted server-side configuration.

    Precedence: explicit ``MARKER_QUERY_CURSOR_KEY``, then the deployment
    encryption key (env or generated key file), then an ephemeral process key.
    The ephemeral fallback keeps local no-config use working at the cost of
    continuation chains dying with the process, which matches the short cursor
    TTL; it is logged so operators can see the limitation.
    """

    material = os.getenv("MARKER_QUERY_CURSOR_KEY", "").strip()
    if not material:
        material = _stored_encryption_key()
    if material:
        key = hashlib.sha256(
            material.encode("utf-8") + _KEY_DERIVATION_LABEL
        ).digest()
        return CursorKeyring({_DERIVED_KEY_ID: key}, current_key_id=_DERIVED_KEY_ID)
    logger.warning(
        "No ENCRYPTION_KEY or MARKER_QUERY_CURSOR_KEY configured; using an "
        "ephemeral query cursor key. Continuation chains will not survive a "
        "server restart."
    )
    return CursorKeyring(
        {_EPHEMERAL_KEY_ID: secrets.token_bytes(32)},
        current_key_id=_EPHEMERAL_KEY_ID,
    )


def _stored_encryption_key() -> str:
    raw = os.environ.get("ENCRYPTION_KEY", "").strip()
    if raw:
        return raw
    key_path = DATA_DIR / ".encryption_key"
    try:
        return key_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _ensure_db_ready() -> None:
    global _db_ready
    if _db_ready or _session_factory is not async_session_factory:
        return
    await verify_database_ready()
    _db_ready = True


def _continuation_service() -> ContinuationService:
    global _service
    if _service is None:
        _service = ContinuationService(
            _session_factory,
            cursor_codec=CursorCodec(_cursor_keyring()),
        )
    return _service


def _outcome_envelope(outcome: ContinuationOutcome) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    if outcome.result:
        result = dict(outcome.result)
        packet = result.get("packet")
        if packet is not None:
            result["packet"] = packet_to_json(packet)
    return {
        "schema_version": QUERY_RESULT_SCHEMA_VERSION,
        "status": outcome.status,
        "result": result,
        "next_cursor": outcome.next_cursor,
        "reason": outcome.reason,
        "error_code": outcome.error_code,
    }


async def run_agent_query(
    *,
    query: Mapping[str, Any] | None = None,
    continuation: str | None = None,
    workspace_id: str | None = None,
    page_size: int | None = None,
    principal_id: str | None = None,
) -> dict[str, Any]:
    """Run one fresh or continued query and return the transport envelope.

    Exactly one of ``query`` (a ``marker.query.v1`` request mapping) and
    ``continuation`` (an opaque server-issued cursor token) must be provided.
    Contract failures (unsupported operators, budget violations, malformed
    requests) raise ``UsageError``; operational and continuation states come
    back as structured ``marker.query_result.v1`` outcomes instead of
    exceptions.
    """

    await _ensure_db_ready()
    if (query is None) == (continuation is None):
        raise UsageError(
            "Provide exactly one of 'query' (fresh marker.query.v1 request) "
            "or 'continuation' (server-issued cursor token)."
        )
    service = _continuation_service()
    try:
        if continuation is not None:
            if not workspace_id:
                raise UsageError(
                    "workspace_id is required when continuing a cursor."
                )
            outcome = await service.continue_query(
                continuation,
                workspace_id=workspace_id,
                page_size=page_size,
                principal_id=principal_id,
            )
        else:
            assert query is not None
            if not isinstance(query, Mapping):
                raise UsageError("'query' must be a marker.query.v1 object.")
            if workspace_id and query.get("workspace_id") != workspace_id:
                raise UsageError(
                    "workspace_id does not match the query request workspace."
                )
            # Pre-validate through the authoritative contract so caller
            # mistakes surface as explicit usage errors instead of being
            # collapsed into the service's operational failure outcome.
            parsed_request = parse_query_request(dict(query))
            outcome = await service.fresh_query(
                parsed_request,
                page_size=page_size,
                principal_id=principal_id,
            )
    except (
        QueryContractError,
        UnsupportedOperatorError,
        QueryBudgetError,
    ) as exc:
        raise UsageError(str(exc)) from exc
    return _outcome_envelope(outcome)
