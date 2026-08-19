"""Transport-neutral adapter for durable semantic event resume (PR79B).

The kernel event log is the authoritative per-(workspace, stream) sequence.
This adapter exposes a bounded, ordered page of that log plus the server-side
latest sequence, so a client that disconnects can resume from its last
delivered ``semantic_sequence`` and recover exactly the missing tail. It adds
no buffering, no second history, and no delivery authority of its own.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import async_session_factory
from app.db_migration import verify_database_ready
from app.errors import UsageError
from app.kernel import events as kernel_events
from app.kernel.errors import InvalidEventError

__all__ = [
    "DEFAULT_EVENT_PAGE_LIMIT",
    "EVENTS_RESULT_SCHEMA_VERSION",
    "MAX_EVENT_PAGE_LIMIT",
    "configure_events_runtime",
    "read_agent_events",
]

EVENTS_RESULT_SCHEMA_VERSION = "marker.events.v1"
DEFAULT_EVENT_PAGE_LIMIT = 100
MAX_EVENT_PAGE_LIMIT = 500
MAX_WORKSPACE_ID_LENGTH = 128

_session_factory: async_sessionmaker = async_session_factory
_db_ready = False


def configure_events_runtime(session_factory: async_sessionmaker) -> None:
    """Point the events adapter at a specific session factory (tests/tools)."""

    global _session_factory
    _session_factory = session_factory


async def _ensure_db_ready() -> None:
    global _db_ready
    if _db_ready or _session_factory is not async_session_factory:
        return
    await verify_database_ready()
    _db_ready = True


async def read_agent_events(
    *,
    workspace_id: str,
    stream: str = kernel_events.DEFAULT_STREAM,
    after_sequence: int = 0,
    limit: int = DEFAULT_EVENT_PAGE_LIMIT,
) -> dict[str, Any]:
    """Read one ordered page of durable semantic events for resume."""

    await _ensure_db_ready()
    if (
        not isinstance(workspace_id, str)
        or not 1 <= len(workspace_id.strip()) <= MAX_WORKSPACE_ID_LENGTH
        or workspace_id != workspace_id.strip()
    ):
        raise UsageError(
            "workspace_id must be 1-128 characters without surrounding "
            "whitespace."
        )
    if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
        raise UsageError("after_sequence must be a non-negative integer.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_EVENT_PAGE_LIMIT
    ):
        raise UsageError(
            f"limit must be an integer from 1 to {MAX_EVENT_PAGE_LIMIT}."
        )
    try:
        kernel_events.validate_stream(stream)
    except InvalidEventError as exc:
        raise UsageError(str(exc)) from exc

    rows = await kernel_events.replay(
        _session_factory,
        workspace_id=workspace_id,
        stream=stream,
        after_sequence=after_sequence,
        limit=limit,
    )
    latest = await kernel_events.get_latest_sequence(
        _session_factory,
        workspace_id=workspace_id,
        stream=stream,
    )
    last_delivered = rows[-1].semantic_sequence if rows else after_sequence
    return {
        "schema_version": EVENTS_RESULT_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "stream": stream,
        "events": [
            {
                "semantic_sequence": row.semantic_sequence,
                "event_type": row.event_type,
                "durability": row.durability,
                "payload": row.payload,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "latest_sequence": latest,
        "next_after_sequence": last_delivered,
        "has_more": latest > last_delivered,
    }
