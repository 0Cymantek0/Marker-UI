"""Domain canonicalization (values layer) unit tests.

Adversarial focus: raw Unicode policy, float rejection, decimal
precision, semantic set ordering, and default/omission rules.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal

import pytest

from app.utils.canonical import CanonicalSet, DecimalValue, to_json_ready
from app.utils.canonical.jcs import canonical_json_bytes
from app.utils.canonical.values import CANONICALIZATION_PROFILE


def test_profile_version_is_declared() -> None:
    assert CANONICALIZATION_PROFILE == "marker.canonical.v1"


def test_raw_unicode_nfc_nfd_stay_distinct() -> None:
    nfc = unicodedata.normalize("NFC", "caf\u00e9")
    nfd = unicodedata.normalize("NFD", "caf\u00e9")
    assert nfc != nfd  # distinct source evidence
    assert canonical_json_bytes({"t": nfc}) != canonical_json_bytes({"t": nfd})


def test_no_hidden_normalization_of_whitespace_or_case() -> None:
    values = ["A b", "a b", "A\u00a0b", "A\u2009b", "AB", "A‍B"]
    encoded = {canonical_json_bytes({"t": v}) for v in values}
    assert len(encoded) == len(values)


def test_floats_rejected_everywhere() -> None:
    with pytest.raises(Exception, match="floats cannot enter canonical identity"):
        to_json_ready(0.1)
    with pytest.raises(Exception, match="floats cannot enter canonical identity"):
        to_json_ready({"nested": [1, 2.5]})
    # A JSON-decoded "1.0" is a float in Python and must be declared too.
    with pytest.raises(Exception, match="floats cannot enter canonical identity"):
        to_json_ready({"x": float(1)})


def test_plain_set_rejected_in_favor_of_canonical_set() -> None:
    with pytest.raises(Exception, match="CanonicalSet"):
        to_json_ready({"tags": {"a", "b"}})
    with pytest.raises(Exception, match="CanonicalSet"):
        to_json_ready({"tags": frozenset({"a", "b"})})


def test_big_integers_outside_safe_range_rejected() -> None:
    too_big = (2**53) - 1 + 1
    with pytest.raises(Exception, match="safe JSON identity range"):
        to_json_ready({"n": too_big})


def test_bool_and_int_are_distinct() -> None:
    assert to_json_ready(True) is True
    assert to_json_ready(1) == 1
    assert canonical_json_bytes({"v": True}) != canonical_json_bytes({"v": 1})


def test_decimal_value_canonical_text_rules() -> None:
    assert DecimalValue("0").text == "0"
    assert DecimalValue("-123.450").text == "-123.450"
    assert DecimalValue("123456789012345678901234567890.123").text == (
        "123456789012345678901234567890.123"
    )
    for bad in ["", "1e3", "01", "+1", ".5", "1.", "1_000", " 1"]:
        with pytest.raises(Exception, match="invalid canonical decimal"):
            DecimalValue(bad)
    with pytest.raises(Exception, match="signed zero"):
        DecimalValue("-0")
    with pytest.raises(Exception, match="signed zero"):
        DecimalValue("-0.000")


def test_decimal_value_from_decimal() -> None:
    assert DecimalValue.from_decimal(Decimal("1E+3")).text == "1000"
    assert DecimalValue.from_decimal(Decimal("0.500")).text == "0.500"
    assert DecimalValue.from_decimal(Decimal("-0.0")).text == "0.0"
    assert DecimalValue.from_decimal(Decimal("-1.5")).text == "-1.5"
    with pytest.raises(Exception, match="non-finite decimal"):
        DecimalValue.from_decimal(Decimal("NaN"))
    with pytest.raises(Exception, match="non-finite decimal"):
        DecimalValue.from_decimal(Decimal("Infinity"))
    with pytest.raises(Exception, match="expects Decimal"):
        DecimalValue.from_decimal(1.5)  # type: ignore[arg-type]


def test_decimal_trailing_zeros_are_significant() -> None:
    assert (
        canonical_json_bytes(to_json_ready({"amount": DecimalValue("1.10")}))
        != canonical_json_bytes(to_json_ready({"amount": DecimalValue("1.1")}))
    )


def test_bare_decimal_accepted_and_exact() -> None:
    assert to_json_ready(Decimal("9007199254740993")) == "9007199254740993"
    assert to_json_ready({"d": Decimal("0.1")}) == {"d": "0.1"}


def test_canonical_set_ordering_is_deterministic() -> None:
    a = CanonicalSet(["b", "a", "c", "aa"])
    b = CanonicalSet(["aa", "c", "b", "a"])
    assert a.canonical_value() == ["a", "aa", "b", "c"]
    assert canonical_json_bytes(to_json_ready(a)) == canonical_json_bytes(
        to_json_ready(b)
    )


def test_canonical_set_members_sorted_by_canonical_bytes() -> None:
    # Byte order, not lexical text order for non-ASCII members.
    members = CanonicalSet(["é", "z", "A"])
    assert members.canonical_value() == ["A", "z", "é"]


def test_canonical_set_duplicates_rejected() -> None:
    with pytest.raises(Exception, match="duplicate members"):
        CanonicalSet(["a", "a"]).canonical_value()
    with pytest.raises(Exception, match="duplicate members"):
        CanonicalSet([{"k": 1}, {"k": 1}]).canonical_value()


def test_ordered_sequence_permutation_changes_identity_input() -> None:
    assert to_json_ready([1, 2, 3]) != to_json_ready([3, 2, 1])
    assert canonical_json_bytes((1, 2)) != canonical_json_bytes((2, 1))


def test_mapping_key_permutation_does_not_change_bytes() -> None:
    first = {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}
    second = {"nested": {"y": 2, "x": 1}, "b": 2, "a": 1}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_empty_and_null_values_are_explicit_data() -> None:
    # {} vs {"x": null} vs omitted x are three different records;
    # canonicalization has no implicit default-filling.
    encodings = {
        canonical_json_bytes({}),
        canonical_json_bytes({"x": None}),
        canonical_json_bytes({"x": {}}),
        canonical_json_bytes({"x": []}),
        canonical_json_bytes({"x": ""}),
    }
    assert len(encodings) == 5


def test_unsupported_types_fail_visibly() -> None:
    from datetime import datetime, timezone

    with pytest.raises(Exception, match="not a canonical identity value"):
        to_json_ready(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(Exception, match="keys must be strings"):
        to_json_ready({2: "x"})


def test_nested_wrappers_resolve() -> None:
    value = {
        "tags": CanonicalSet([DecimalValue("2.5"), "a"]),
        "grid": [[DecimalValue("0.001"), 1], []],
    }
    assert to_json_ready(value) == {"tags": ["2.5", "a"], "grid": [["0.001", 1], []]}
