"""Effective-redaction resolution and policy commit service (PR89)."""

from __future__ import annotations

import pytest

from app.context_runtime.errors import QueryAuthorizationError
from app.context_runtime.redaction import (
    NO_REDACTION,
    EffectiveRedaction,
    resolve_effective_redaction,
    terms_survive,
)
from app.services.redaction_policy import RedactionPolicyService

pytestmark = pytest.mark.asyncio

SENTINEL = "MU_RED_7f3a9c2e4b"


@pytest.mark.asyncio
async def _service(payload_env, workspace: str = "ws-redpol") -> RedactionPolicyService:
    factory, _store, commit_service = payload_env
    return RedactionPolicyService(factory, commit_service, workspace_id=workspace)


@pytest.mark.asyncio
async def test_no_profiles_means_no_redaction(payload_env) -> None:
    factory, _store, _commit = payload_env
    resolved = await resolve_effective_redaction(factory, "ws-fresh", None)
    assert resolved == NO_REDACTION
    assert resolved.is_noop()
    assert resolved.redact_text(f"contains {SENTINEL}") == f"contains {SENTINEL}"


@pytest.mark.asyncio
async def test_named_unknown_profile_fails_closed(payload_env) -> None:
    factory, _store, _commit = payload_env
    with pytest.raises(QueryAuthorizationError):
        await resolve_effective_redaction(factory, "ws-fresh", "attacker-invented")


@pytest.mark.asyncio
async def test_omitted_name_resolves_committed_default(payload_env) -> None:
    factory, _store, _commit = payload_env
    policy = await _service(payload_env)
    await policy.define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    resolved = await resolve_effective_redaction(factory, "ws-redpol", None)
    assert resolved.profile_id == "default"
    assert resolved.revision > 0
    assert resolved.redact_text(f"x {SENTINEL} y") == "x [redacted] y"


@pytest.mark.asyncio
async def test_latest_revision_wins_and_relaxation_rotates_identity(
    payload_env,
) -> None:
    factory, _store, _commit = payload_env
    policy = await _service(payload_env)
    await policy.define_profile(
        "default",
        [
            {"kind": "literal", "value": SENTINEL},
            {"kind": "pattern", "pattern": r"ACCT-\d{4}", "flags": ["IGNORECASE"]},
        ],
    )
    strict = await resolve_effective_redaction(factory, "ws-redpol", None)
    assert strict.redact_text(f"{SENTINEL} and acct-1234") == (
        "[redacted] and [redacted]"
    )

    # Relaxation: a newer revision that drops every rule. The revision
    # marker changes, so packet/cursor identities rotate even though the
    # rule set became weaker.
    await policy.define_profile("default", [])
    relaxed = await resolve_effective_redaction(factory, "ws-redpol", None)
    assert relaxed.is_noop()
    assert relaxed.revision > strict.revision
    assert relaxed.identity_view() != strict.identity_view()


@pytest.mark.asyncio
async def test_profiles_are_isolated_by_name(payload_env) -> None:
    factory, _store, _commit = payload_env
    policy = await _service(payload_env)
    await policy.define_profile("strict", [{"kind": "literal", "value": SENTINEL}])
    await policy.define_profile("open", [])

    strict = await resolve_effective_redaction(factory, "ws-redpol", "strict")
    open_profile = await resolve_effective_redaction(factory, "ws-redpol", "open")
    assert not strict.is_noop()
    assert open_profile.is_noop()
    assert strict.identity_view() != open_profile.identity_view()


@pytest.mark.asyncio
async def test_placeholder_never_echoes_material(payload_env) -> None:
    factory, _store, _commit = payload_env
    policy = await _service(payload_env)
    await policy.define_profile(
        "default",
        [
            {
                "kind": "pattern",
                "pattern": r"\bMU_RED_[0-9a-f]+\b",
                "placeholder": "«withheld»",
            }
        ],
    )
    resolved = await resolve_effective_redaction(factory, "ws-redpol", None)
    projected = resolved.redact_text(f"prefix {SENTINEL} suffix")
    assert SENTINEL not in projected
    assert projected == "prefix «withheld» suffix"


def test_terms_survive_governs_existence_leak() -> None:
    assert terms_survive(SENTINEL, "x [redacted] y") is False
    assert terms_survive(f"{SENTINEL} needle", "needle [redacted]") is True
    assert terms_survive("a", "anything") is True  # no usable tokens: keep


def test_identity_view_is_scoped_and_stable() -> None:
    one = EffectiveRedaction(
        profile_id="p", revision=7, rules_digest="sha256:abc", literals=(("x", "[r]"),)
    )
    two = EffectiveRedaction(
        profile_id="p", revision=7, rules_digest="sha256:abc", literals=(("x", "[r]"),)
    )
    assert one.identity_view() == two.identity_view()
    assert set(one.identity_view()) == {"profile_id", "revision", "rules_digest"}
