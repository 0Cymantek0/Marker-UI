"""RedactionProfileRecord kernel-boundary validation (PR89)."""

from __future__ import annotations

import pytest

from app.kernel.errors import KernelError
from app.kernel.records import (
    RedactionProfileRecord,
    normalize_redaction_rule,
    normalize_redaction_rules,
)

pytestmark = pytest.mark.asyncio


def test_literal_rule_normalizes_with_default_placeholder() -> None:
    rule = normalize_redaction_rule({"kind": "literal", "value": "MU_RED_x"})
    assert rule == {
        "kind": "literal",
        "value": "MU_RED_x",
        "placeholder": "[redacted]",
    }


def test_pattern_rule_compiles_and_keeps_declared_flags() -> None:
    rule = normalize_redaction_rule(
        {"kind": "pattern", "pattern": r"SSN-\d{3}", "flags": ["IGNORECASE"]}
    )
    assert rule["kind"] == "pattern"
    assert rule["flags"] == ["IGNORECASE"]
    assert rule["placeholder"] == "[redacted]"


def test_placeholder_may_not_echo_value_or_match_pattern() -> None:
    with pytest.raises(KernelError):
        normalize_redaction_rule(
            {"kind": "literal", "value": "MU_RED_x", "placeholder": "see MU_RED_x"}
        )
    with pytest.raises(KernelError):
        normalize_redaction_rule(
            {"kind": "pattern", "pattern": r".*", "placeholder": "x"}
        )


@pytest.mark.parametrize(
    "rule",
    [
        "not-a-mapping",
        {"kind": "unknown", "value": "x"},
        {"kind": "literal"},
        {"kind": "literal", "value": ""},
        {"kind": "pattern", "pattern": "("},
        {"kind": "pattern", "pattern": "x", "flags": ["MULTILINE"]},
        {"kind": "literal", "value": "x", "placeholder": ""},
        {"kind": "literal", "value": "x", "placeholder": "a\\b"},
    ],
)
def test_malformed_rules_fail_closed(rule) -> None:
    with pytest.raises(KernelError):
        normalize_redaction_rule(rule)


def test_rule_list_is_bounded_and_type_checked() -> None:
    with pytest.raises(KernelError):
        normalize_redaction_rules("literal-only")
    with pytest.raises(KernelError):
        normalize_redaction_rules([{"kind": "literal", "value": "x"}] * 65)
    assert len(normalize_redaction_rules(())) == 0


async def test_record_validates_profile_id_and_rules() -> None:
    record = RedactionProfileRecord(
        profile_id="strict",
        rules=({"kind": "literal", "value": "MU_RED_x"},),
        redaction_basis={"operator": "test"},
    )
    assert record.record_class == "redaction_profile"
    assert record.record_type == "marker.kernel.redaction_profile.v1"
    payload = record.identity_payload()
    assert payload["profile_id"] == "strict"
    assert payload["rules"] == [
        {"kind": "literal", "value": "MU_RED_x", "placeholder": "[redacted]"}
    ]
    assert payload["supersedes"] is None


def test_record_rejects_bad_profile_id_and_bad_supersedes() -> None:
    with pytest.raises(KernelError):
        RedactionProfileRecord(profile_id="Bad Id", rules=())
    with pytest.raises(KernelError):
        RedactionProfileRecord(
            profile_id="strict", rules=(), supersedes="not a ref!"
        )
