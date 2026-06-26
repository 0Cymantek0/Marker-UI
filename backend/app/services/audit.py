"""Redacting audit sink for security-relevant events."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.audit import AuditEvent

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
_MAX_STRING_LENGTH = 512


async def record_audit_event(
    db: AsyncSession | None,
    *,
    event_type: str,
    actor: str | None = None,
    surface: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    status: str = "success",
    payload: Mapping[str, Any] | None = None,
) -> AuditEvent | None:
    """Persist one redacted audit event without exposing secret payloads."""

    event = AuditEvent(
        event_type=event_type,
        actor=actor,
        surface=surface,
        resource_type=resource_type,
        resource_id=resource_id,
        status=status,
        redacted_payload_json=json.dumps(
            redact_payload(dict(payload or {})),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    try:
        if db is not None:
            db.add(event)
            await db.flush()
            return event
        async with async_session_factory() as session:
            session.add(event)
            await session.commit()
            return event
    except Exception as exc:  # noqa: BLE001 - audit must not break user work.
        logger.debug("Audit event write failed: %s", exc)
        return None


def redact_payload(value: Any, *, key: str | None = None) -> Any:
    if key and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): redact_payload(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, str):
        if _looks_like_url_key(key):
            return _redact_url(value)
        if len(value) > _MAX_STRING_LENGTH:
            return f"{value[:_MAX_STRING_LENGTH]}..."
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_payload(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _looks_like_url_key(key: str | None) -> bool:
    return bool(key and "url" in key.lower())


def _redact_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value[:_MAX_STRING_LENGTH]
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunparse((parsed.scheme, hostname, parsed.path, "", "", ""))
