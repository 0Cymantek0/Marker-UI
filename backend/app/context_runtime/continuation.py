"""Typed continuation state and outcome contract (PR79A).

This module defines transport-neutral shapes only.  It does not execute a
query, advance a keyset, acquire a publication pin, or read/write cursor
rows.  Those operations belong to the later continuation service slice.

Sensitive query and authorization dimensions are represented as server-side
state.  ``canonical_cursor_state_json`` gives the persistence layer one
deterministic JSON representation without putting that state into a client
token.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.context_runtime.cursor import (
    CursorExpiredError,
    validate_cursor_expiry,
)
from app.utils.canonical import canonical_json_str, to_json_ready

__all__ = [
    "CONTINUATION_SCHEMA_VERSION",
    "CONTINUATION_OUTCOME_STATUSES",
    "CONTINUATION_TERMINAL_STATUSES",
    "OUTCOME_COMPLETE",
    "OUTCOME_EXECUTION_FAILURE",
    "OUTCOME_INVALIDATED",
    "OUTCOME_LOOP_LIMIT",
    "OUTCOME_PARTIAL",
    "OUTCOME_POLICY_FAIL_CLOSED",
    "OUTCOME_STALE",
    "CURSOR_REPLAY_CONSUMED",
    "CURSOR_REPLAY_FRESH",
    "CURSOR_REPLAY_ROTATED",
    "CURSOR_STATUS_ACTIVE",
    "CURSOR_STATUS_EXHAUSTED",
    "CURSOR_STATUS_EXPIRED",
    "CURSOR_STATUS_REVOKED",
    "ContinuationContractError",
    "ContinuationOutcome",
    "ContinuationResult",
    "CursorReplayState",
    "CursorState",
    "QueryOutcome",
    "canonical_cursor_state_json",
    "parse_continuation_outcome",
    "parse_cursor_state_json",
    "validate_cursor_state_expiry",
]

CONTINUATION_SCHEMA_VERSION = "marker.continuation.v1"

OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_INVALIDATED = "invalidated"
OUTCOME_STALE = "stale"
OUTCOME_LOOP_LIMIT = "loop_limit"
OUTCOME_POLICY_FAIL_CLOSED = "policy_fail_closed"
OUTCOME_EXECUTION_FAILURE = "execution_failure"
CONTINUATION_OUTCOME_STATUSES = frozenset(
    {
        OUTCOME_COMPLETE,
        OUTCOME_PARTIAL,
        OUTCOME_INVALIDATED,
        OUTCOME_STALE,
        OUTCOME_LOOP_LIMIT,
        OUTCOME_POLICY_FAIL_CLOSED,
        OUTCOME_EXECUTION_FAILURE,
    }
)
CONTINUATION_TERMINAL_STATUSES = frozenset(
    CONTINUATION_OUTCOME_STATUSES - {OUTCOME_COMPLETE, OUTCOME_PARTIAL}
)

CURSOR_STATUS_ACTIVE = "active"
CURSOR_STATUS_EXHAUSTED = "exhausted"
CURSOR_STATUS_EXPIRED = "expired"
CURSOR_STATUS_REVOKED = "revoked"
CURSOR_STATUSES = frozenset(
    {
        CURSOR_STATUS_ACTIVE,
        CURSOR_STATUS_EXHAUSTED,
        CURSOR_STATUS_EXPIRED,
        CURSOR_STATUS_REVOKED,
    }
)

CURSOR_REPLAY_FRESH = "fresh"
CURSOR_REPLAY_ROTATED = "rotated"
CURSOR_REPLAY_CONSUMED = "consumed"
CURSOR_REPLAY_STATES = frozenset(
    {
        CURSOR_REPLAY_FRESH,
        CURSOR_REPLAY_ROTATED,
        CURSOR_REPLAY_CONSUMED,
    }
)

CursorReplayState = Literal[
    "fresh",
    "rotated",
    "consumed",
]


class ContinuationContractError(ValueError):
    """Malformed continuation state or impossible outcome transition."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        return {str(key): item for key, item in value.items()}
    except Exception as exc:  # pragma: no cover - defensive mapping boundary
        raise ValueError(f"{label} must be an object") from exc


class CursorState(_StrictModel):
    """Complete server-side state needed to continue one query.

    The fields intentionally mirror the durable cursor row.  JSON fields are
    opaque to this foundation; the later paging service will validate their
    domain-specific contents before use.
    """

    schema_version: Literal[CONTINUATION_SCHEMA_VERSION] = (
        CONTINUATION_SCHEMA_VERSION
    )
    handle: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    query: dict[str, Any]
    snapshot: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    authorization: dict[str, Any] | None = None
    keyset: dict[str, Any]
    cumulative_budget: dict[str, int]
    page_count: int = Field(default=0, ge=0)
    expires_at: datetime
    pin_id: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["active", "exhausted", "expired", "revoked"] = (
        CURSOR_STATUS_ACTIVE
    )
    nonce: str = Field(min_length=1, max_length=128)
    replay_state: CursorReplayState = CURSOR_REPLAY_FRESH

    @field_validator(
        "query",
        "keyset",
        "cumulative_budget",
        "snapshot",
        "publication",
        "authorization",
        mode="before",
    )
    @classmethod
    def _validate_objects(cls, value: Any, info) -> Any:
        if value is None and info.field_name in {
            "snapshot",
            "publication",
            "authorization",
        }:
            return None
        return _mapping(value, label=info.field_name)

    @field_validator("cumulative_budget")
    @classmethod
    def _validate_budget_values(cls, value: dict[str, int]) -> dict[str, int]:
        for key, item in value.items():
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"cumulative_budget[{key!r}] must be a non-negative integer")
        return value

    @field_validator("expires_at")
    @classmethod
    def _normalize_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("nonce")
    @classmethod
    def _validate_nonce(cls, value: str) -> str:
        if not value or not value.isascii() or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in value
        ):
            raise ValueError("nonce must be URL-safe opaque text")
        return value

    @model_validator(mode="after")
    def _validate_state(self) -> "CursorState":
        if self.status == CURSOR_STATUS_EXPIRED and self.expires_at > datetime.now(
            timezone.utc
        ):
            raise ValueError("expired cursor state must have an expired lease")
        if self.status in {CURSOR_STATUS_EXHAUSTED, CURSOR_STATUS_REVOKED} and (
            self.replay_state == CURSOR_REPLAY_FRESH
        ):
            raise ValueError(
                "exhausted/revoked cursor state cannot retain fresh replay state"
            )
        return self


class ContinuationOutcome(_StrictModel):
    """Structured caller-visible result for one bounded page.

    ``partial`` must carry exactly one continuation token.  ``complete`` and
    all terminal failure outcomes must not carry one.  This prevents
    transports from silently treating an omitted page as exhaustive or
    manufacturing a cursor after invalidation, stale state, loop limits,
    fail-closed policy decisions, or execution failures. ``result`` and
    ``packet`` are transport-neutral payload slots; query execution will bind
    one in the later service slice.
    """

    schema_version: Literal[CONTINUATION_SCHEMA_VERSION] = (
        CONTINUATION_SCHEMA_VERSION
    )
    status: Literal[
        "complete",
        "partial",
        "invalidated",
        "stale",
        "loop_limit",
        "policy_fail_closed",
        "execution_failure",
    ]
    result: Any = None
    packet: Any = None
    next_cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    reason: str | None = Field(default=None, min_length=1, max_length=256)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def _accept_transport_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        status_aliases = {
            "invalidated_cursor": OUTCOME_INVALIDATED,
            "stale_cursor": OUTCOME_STALE,
            "loop_limit_exceeded": OUTCOME_LOOP_LIMIT,
            "policy_denied": OUTCOME_POLICY_FAIL_CLOSED,
            "execution_failed": OUTCOME_EXECUTION_FAILURE,
        }
        if data.get("status") in status_aliases:
            data["status"] = status_aliases[data["status"]]
        if "next_cursor" not in data and "cursor" in data:
            data["next_cursor"] = data.pop("cursor")
        if "result" not in data and "value" in data:
            data["result"] = data.pop("value")
        return data

    @field_validator("next_cursor")
    @classmethod
    def _reject_blank_cursor(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("next_cursor must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_transition(self) -> "ContinuationOutcome":
        if self.status == OUTCOME_PARTIAL and self.next_cursor is None:
            raise ValueError("partial continuation outcome requires next_cursor")
        if self.status != OUTCOME_PARTIAL and self.next_cursor is not None:
            raise ValueError(
                f"{self.status} continuation outcome cannot carry next_cursor"
            )
        return self

    @property
    def partial(self) -> bool:
        return self.status == OUTCOME_PARTIAL

    @property
    def complete(self) -> bool:
        return self.status == OUTCOME_COMPLETE

    @property
    def terminal(self) -> bool:
        return self.status in CONTINUATION_TERMINAL_STATUSES

    @property
    def invalidated(self) -> bool:
        return self.status == OUTCOME_INVALIDATED

    @property
    def stale(self) -> bool:
        return self.status == OUTCOME_STALE

    @property
    def loop_limited(self) -> bool:
        return self.status == OUTCOME_LOOP_LIMIT

    @property
    def policy_failed_closed(self) -> bool:
        return self.status == OUTCOME_POLICY_FAIL_CLOSED

    @property
    def execution_failed(self) -> bool:
        return self.status == OUTCOME_EXECUTION_FAILURE

    @property
    def continuation(self) -> str | None:
        return self.next_cursor


# Names used by callers that describe this as a query result rather than a
# transport continuation.  They intentionally share one strict model.
ContinuationResult = ContinuationOutcome
QueryOutcome = ContinuationOutcome


def canonical_cursor_state_json(state: CursorState | Mapping[str, Any]) -> str:
    """Serialize cursor state into deterministic JSON for local persistence."""

    if isinstance(state, CursorState):
        value: Any = state.model_dump(mode="json")
    elif isinstance(state, Mapping):
        value = dict(state)
    else:
        raise ContinuationContractError("cursor state must be CursorState or mapping")
    try:
        return canonical_json_str(to_json_ready(value))
    except Exception as exc:  # noqa: BLE001 - normalize canonical failures
        raise ContinuationContractError(f"cursor state is not canonical JSON: {exc}") from exc


def parse_cursor_state_json(value: str) -> dict[str, Any]:
    """Parse and require canonical JSON before a row can become state."""

    if not isinstance(value, str) or not value:
        raise ContinuationContractError("cursor state JSON must be non-empty text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContinuationContractError("cursor state JSON is malformed") from exc
    if not isinstance(decoded, dict):
        raise ContinuationContractError("cursor state JSON must contain an object")
    try:
        canonical = canonical_json_str(to_json_ready(decoded))
    except Exception as exc:  # noqa: BLE001 - normalize canonical failures
        raise ContinuationContractError(f"cursor state JSON is not canonical: {exc}") from exc
    if canonical != value:
        raise ContinuationContractError("cursor state JSON is not canonical")
    return decoded


def validate_cursor_state_expiry(
    state: CursorState,
    *,
    now: datetime | None = None,
) -> None:
    """Apply expiry primitive to a validated server-side cursor state."""

    try:
        validate_cursor_expiry(state.expires_at, now=now)
    except CursorExpiredError:
        raise
    except Exception as exc:  # pragma: no cover - CursorState already validates
        raise ContinuationContractError(str(exc)) from exc


def parse_continuation_outcome(value: Mapping[str, Any]) -> ContinuationOutcome:
    """Validate outcome and normalize Pydantic errors to contract errors."""

    try:
        return ContinuationOutcome.model_validate(value)
    except ValidationError as exc:
        raise ContinuationContractError(str(exc)) from exc
