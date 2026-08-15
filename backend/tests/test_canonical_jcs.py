"""JCS serializer unit tests, including official RFC 8785 test vectors.

Reference vectors come from the JCS reference implementation test data
(https://github.com/cyberphone/json-canonicalization, testdata/).
Vectors whose inputs contain non-integer numbers are included with the
numeric parts removed: floats are outside the canonical identity value
domain by design, so the serializer's number formatting is only ever
exercised on safe-range integers.
"""

from __future__ import annotations

import pytest

from app.utils.canonical import canonical_json_bytes, canonical_json_str
from app.utils.canonical.errors import CanonicalValueError

MAX_SAFE = (2**53) - 1


def test_official_vector_arrays() -> None:
    value = [56, {"d": True, "10": None, "1": []}]
    assert canonical_json_str(value) == '[56,{"1":[],"10":null,"d":true}]'


def test_official_vector_structures() -> None:
    # Original vector uses 56.0; identity domain carries it as int 56.
    value = {
        "1": {"f": {"f": "hi", "F": 5}, "\n": 56},
        "10": {},
        "": "empty",
        "a": {},
        "111": [{"e": "yes", "E": "no"}],
        "A": {},
    }
    assert canonical_json_str(value) == (
        '{"":"empty","1":{"\\n":56,"f":{"F":5,"f":"hi"}},'
        '"10":{},"111":[{"E":"no","e":"yes"}],"A":{},"a":{}}'
    )


def test_official_vector_french_locale_independence() -> None:
    value = {
        "peach": "This sorting order",
        "péché": "is wrong according to French",
        "pêche": "but canonicalization MUST",
        "sin": "ignore locale",
    }
    assert canonical_json_str(value) == (
        '{"peach":"This sorting order",'
        '"péché":"is wrong according to French",'
        '"pêche":"but canonicalization MUST",'
        '"sin":"ignore locale"}'
    )


def test_official_vector_unicode_raw_combining_mark() -> None:
    value = {"Unnormalized Unicode": "A\u030a"}
    assert canonical_json_str(value) == '{"Unnormalized Unicode":"Å"}'


def test_official_vector_values_string_and_literals() -> None:
    value = {
        "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"/",
        "literals": [None, True, False],
    }
    # Parsed string chars: € $ \x0f \n A ' B " \ \ " /
    expected_string = (
        "€$" + "\\u000f" + "\\n" + "A'B" + '\\"' + "\\\\" + "\\\\" + '\\"' + "/"
    )
    assert canonical_json_str(value) == (
        '{"literals":[null,true,false],"string":"' + expected_string + '"}'
    )


def test_official_vector_weird_keys_and_astral_sort() -> None:
    value = {
        "\u20ac": "Euro Sign",
        "\r": "Carriage Return",
        "\u000a": "Newline",
        "1": "One",
        "\u0080": "Control\u007f",
        "\U0001F602": "Smiley",
        "\u00f6": "Latin Small Letter O With Diaeresis",
        "\ufb33": "Hebrew Letter Dalet With Dagesh",
        "</script>": "Browser Challenge",
    }
    assert canonical_json_str(value) == (
        '{"\\n":"Newline","\\r":"Carriage Return","1":"One",'
        '"</script>":"Browser Challenge",'
        '"\u0080":"Control\u007f",'
        '"ö":"Latin Small Letter O With Diaeresis",'
        '"€":"Euro Sign",'
        '"😂":"Smiley",'
        '"דּ":"Hebrew Letter Dalet With Dagesh"}'
    )


def test_key_order_is_utf16_code_unit_order_not_code_point() -> None:
    # 'k' < astral smiley < U+E000 < U+FF11 under UTF-16 code units,
    # but code-point order differs (smiley would sort last).
    value = {"\uff11": 4, "\ue000": 3, "\U0001F600": 1, "k": 2}
    assert canonical_json_str(value) == '{"k":2,"\U0001F600":1,"\ue000":3,"\uff11":4}'


def test_string_escaping_rules() -> None:
    assert canonical_json_str('a"b\\c\bd\fe\ng\rh\ti') == (
        '"a\\"b\\\\c\\bd\\fe\\ng\\rh\\ti"'
    )
    # Other C0 controls: lowercase hex; U+007F DEL stays raw per JCS.
    assert canonical_json_str("\u0000\u001f\u0001") == '"\\u0000\\u001f\\u0001"'
    assert canonical_json_str("\u007f") == '"\u007f"'


def test_lone_surrogate_rejected() -> None:
    with pytest.raises(CanonicalValueError, match="lone surrogate"):
        canonical_json_str("\ud800x")
    with pytest.raises(CanonicalValueError, match="lone surrogate"):
        canonical_json_str({"k\ud800": 1})


def test_safe_integer_bounds() -> None:
    assert canonical_json_str(MAX_SAFE) == str(MAX_SAFE)
    assert canonical_json_str(-MAX_SAFE) == str(-MAX_SAFE)
    with pytest.raises(CanonicalValueError, match="safe JSON identity range"):
        canonical_json_str(MAX_SAFE + 1)
    with pytest.raises(CanonicalValueError, match="safe JSON identity range"):
        canonical_json_str(-MAX_SAFE - 1)


def test_float_rejected() -> None:
    with pytest.raises(CanonicalValueError, match="floats are rejected"):
        canonical_json_str(1.0)
    with pytest.raises(CanonicalValueError, match="floats are rejected"):
        canonical_json_str([1, 2.5])


def test_non_string_key_rejected() -> None:
    with pytest.raises(CanonicalValueError, match="keys must be strings"):
        canonical_json_str({1: "a"})


def test_unsupported_type_rejected() -> None:
    with pytest.raises(CanonicalValueError, match="not a canonical JSON value"):
        canonical_json_str(b"bytes")


def test_bytes_are_utf8_and_idempotent() -> None:
    value = {"k": "café 😀", "n": [1, -2, 0]}
    text = canonical_json_str(value)
    assert canonical_json_bytes(value) == text.encode("utf-8")
    # Canonical output re-serializes to itself.
    import json

    assert canonical_json_str(json.loads(text)) == text


def test_repeated_serialization_is_byte_identical() -> None:
    value = {"z": ["é", "\u0001", True, None], "a": {"nested": [-1, 0]}}
    first = canonical_json_bytes(value)
    for _ in range(3):
        assert canonical_json_bytes(value) == first
