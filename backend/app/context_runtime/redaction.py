"""Trusted server-side redaction resolution (PR89).

``QuerySecurityContext.redaction_profile_id`` is a caller-supplied
*name*, never a rule source: the effective redaction state for a
request is derived here from committed kernel truth only — the latest
``redaction_profile`` record per ``(workspace, profile_id)``. A named
profile that does not exist fails closed instead of degrading to an
unrestricted ruleset, and an omitted name resolves to the workspace's
``default`` profile when one is committed (otherwise no redaction is
defined and none is applied). Nothing a caller sends can weaken or
escape the committed rules, and anything malformed in the policy
lineage fails closed as :class:`QueryAuthorizationError`.

Redaction is a release-time projection, not a content mutation: the
immutable source and the published lexical generations physically
retain the bytes, and every serving path projects the *current*
effective rules over whatever derived state it reads. Retained derived
bytes are therefore never equivalent to releasable bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.errors import QueryAuthorizationError
from app.kernel.models import KernelRecord
from app.kernel.records import (
    REDACTION_RULE_KIND_LITERAL,
    REDACTION_RULE_KIND_PATTERN,
    normalize_redaction_rules,
)
from app.utils.canonical import canonical_json_bytes, payload_byte_hash, to_json_ready

__all__ = [
    "DEFAULT_REDACTION_PROFILE_ID",
    "EffectiveRedaction",
    "NO_REDACTION",
    "resolve_effective_redaction",
    "terms_survive",
]

#: The profile a request resolves to when the caller names none. If the
#: workspace never committed one, no redaction is defined — nothing to
#: project. A caller can never escape a committed ``default`` by simply
#: omitting the name.
DEFAULT_REDACTION_PROFILE_ID = "default"


@dataclass(frozen=True)
class EffectiveRedaction:
    """One request's effective redaction state, resolved from trusted
    committed records.

    ``revision`` is the kernel commit id of the newest record for the
    profile — a monotonic marker that changes on every policy
    transition (including relaxations that remove rules). ``rules``
    holds the compiled literals/patterns; ``rules_digest`` is the
    caller-safe identity of exactly those rules.
    """

    profile_id: str | None
    revision: int
    rules_digest: str
    literals: tuple[tuple[str, str], ...] = ()
    patterns: tuple[tuple[re.Pattern[str], str], ...] = ()

    def is_noop(self) -> bool:
        return not self.literals and not self.patterns

    def redact_text(self, text: str) -> str:
        """Project the effective rules over one releasable text span.

        Every match is replaced by its rule's placeholder; placeholders
        are fixed-width messages that never echo the redacted material.
        """
        if self.is_noop() or not text:
            return text
        projected = text
        for value, placeholder in self.literals:
            if value in projected:
                projected = projected.replace(value, placeholder)
        for pattern, placeholder in self.patterns:
            projected = pattern.sub(placeholder, projected)
        return projected

    def identity_view(self) -> dict[str, Any]:
        """Caller-safe identity dimensions: enough to invalidate packet
        reuse and cursors on any policy transition, never enough to
        reveal the rules behind the digest."""
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "rules_digest": self.rules_digest,
        }


#: No redaction defined for the context: the workspace never committed
#: a profile that binds this request.
NO_REDACTION = EffectiveRedaction(
    profile_id=None, revision=0, rules_digest="", literals=(), patterns=()
)


def _load_rules(record_id: str, payload_json: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(payload_json)
    except Exception as exc:
        raise QueryAuthorizationError(
            f"record={record_id!r} class=redaction_profile: policy payload "
            f"unreadable: {exc}; refusing to resolve redaction from corrupt "
            "state"
        ) from exc
    if not isinstance(payload, dict):
        raise QueryAuthorizationError(
            f"record={record_id!r} class=redaction_profile: policy payload "
            "is not an object; refusing to resolve redaction from corrupt "
            "state"
        )
    rules = payload.get("rules", [])
    try:
        return normalize_redaction_rules(rules)
    except Exception as exc:
        raise QueryAuthorizationError(
            f"record={record_id!r}: committed redaction rules are invalid "
            f"({exc}); refusing to resolve redaction from corrupt state"
        ) from exc


def _compile(rules: list[dict[str, Any]], profile_id: str, revision: int) -> EffectiveRedaction:
    literals: list[tuple[str, str]] = []
    patterns: list[tuple[re.Pattern[str], str]] = []
    for rule in rules:
        if rule["kind"] == REDACTION_RULE_KIND_LITERAL:
            literals.append((rule["value"], rule["placeholder"]))
        elif rule["kind"] == REDACTION_RULE_KIND_PATTERN:
            flags = 0
            for flag in rule.get("flags", ()):
                flags |= {"IGNORECASE": re.IGNORECASE}[flag]
            patterns.append((re.compile(rule["pattern"], flags), rule["placeholder"]))
    digest = (
        payload_byte_hash(canonical_json_bytes(to_json_ready(rules))) if rules else ""
    )
    return EffectiveRedaction(
        profile_id=profile_id,
        revision=revision,
        rules_digest=digest,
        literals=tuple(literals),
        patterns=tuple(patterns),
    )


async def resolve_effective_redaction(
    session_factory: async_sessionmaker,
    workspace_id: str,
    profile_id: str | None,
) -> EffectiveRedaction:
    """Resolve the current effective redaction for one request context.

    Trusted-state-derived: committed kernel records only, latest per
    profile by causal commit order. A caller-*named* profile that has no
    committed record fails closed; ``None`` resolves the workspace's
    ``default`` when one exists. The linearizable effective boundary is
    the kernel commit of the redaction record: any read that begins
    after that commit observes the new rules.
    """
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(
                        KernelRecord.id,
                        KernelRecord.kernel_commit_id,
                        KernelRecord.payload_json,
                    ).where(
                        KernelRecord.workspace_id == workspace_id,
                        KernelRecord.record_class == "redaction_profile",
                    )
                )
            )
            .all()
        )

    latest: dict[str, tuple[int, str, str]] = {}
    for record_id, commit_id, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except Exception as exc:
            raise QueryAuthorizationError(
                f"record={record_id!r} class=redaction_profile: policy "
                f"payload unreadable: {exc}; refusing to resolve redaction "
                "from corrupt state"
            ) from exc
        if not isinstance(payload, dict):
            raise QueryAuthorizationError(
                f"record={record_id!r} class=redaction_profile: policy "
                "payload is not an object; refusing to resolve redaction "
                "from corrupt state"
            )
        named = payload.get("profile_id")
        if not isinstance(named, str) or not named:
            raise QueryAuthorizationError(
                f"record={record_id!r}: malformed redaction profile record "
                "(profile_id); refusing to resolve redaction from corrupt "
                "state"
            )
        key = (commit_id, record_id)
        current = latest.get(named)
        if current is None or key > (current[0], current[1]):
            latest[named] = (commit_id, record_id, payload_json)

    resolved_id = profile_id if profile_id is not None else DEFAULT_REDACTION_PROFILE_ID
    chosen = latest.get(resolved_id)
    if chosen is None:
        if profile_id is None:
            return NO_REDACTION
        raise QueryAuthorizationError(
            f"unknown redaction profile {profile_id!r} for workspace "
            f"{workspace_id!r}; refusing to serve under an uncommitted "
            "redaction identity"
        )

    commit_id, record_id, payload_json = chosen
    rules = _load_rules(record_id, payload_json)
    return _compile(rules, resolved_id, commit_id)


def terms_survive(query_text: str, redacted_text: str) -> bool:
    """Whether any query term still occurs in the redacted text.

    A lexical hit that matched *only* redacted material must be dropped
    rather than returned as a placeholder row: the row's existence
    would itself confirm the redacted content. Tokens are whitespace
    terms that contain at least two searchable characters; a query with
    no usable tokens is treated as surviving (defensive keep, the
    caller-visible behavior is then governed by the projection alone).
    """
    usable = False
    for token in query_text.split():
        if sum(1 for ch in token if ch.isalnum()) < 2:
            continue
        usable = True
        if token.lower() in redacted_text.lower():
            return True
    return not usable
