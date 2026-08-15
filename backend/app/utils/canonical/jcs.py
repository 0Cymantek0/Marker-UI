"""RFC 8785 (JCS)-compatible deterministic JSON serialization.

Deliberately limited to the value domain accepted by the canonical
identity layer (see ``values.py``): ``None``, ``bool``, bounded
integers, raw ``str``, ``list``/``tuple`` sequences, and ``dict``
mappings with string keys. Floats are rejected outright so no
identity-bearing value can ever depend on IEEE-754 formatting;
integers are restricted to the exactly representable ``2^53 - 1``
range where plain ``str(int)`` is identical to the ES6 number
serialization JCS mandates.

String escaping follows RFC 8785 section 3.2.2.2: only ``"``, ``\\``
and the C0 control characters are escaped; all other characters
(including U+007F and non-ASCII) are emitted raw and the result is
UTF-8 encoded. Object keys are sorted by UTF-16 code unit order, not
by Unicode code point order.
"""

from __future__ import annotations

from typing import Any

from .errors import CanonicalValueError

MAX_SAFE_INTEGER = (2**53) - 1
MIN_SAFE_INTEGER = -(2**53) + 1

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _utf16_sort_key(key: str) -> bytes:
    """UTF-16 code-unit order (JCS); tolerant while sorting so the lone
    surrogate rejection surfaces from escaping, not from sort()."""
    return key.encode("utf-16-be", "surrogatepass")


def _escape_string(value: str) -> str:
    parts = ['"']
    for ch in value:
        escape = _SHORT_ESCAPES.get(ch)
        if escape is not None:
            parts.append(escape)
        elif "\ud800" <= ch <= "\udfff":
            raise CanonicalValueError(
                "strings must be valid Unicode scalar values; lone "
                f"surrogate U+{ord(ch):04X} cannot enter canonical bytes"
            )
        elif ch < "\u0020":
            # Other C0 controls use lowercase hex; U+007F stays raw per JCS.
            parts.append(f"\\u{ord(ch):04x}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if not (MIN_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER):
            raise CanonicalValueError(
                f"integer {value} outside the safe JSON identity range "
                f"[{MIN_SAFE_INTEGER}, {MAX_SAFE_INTEGER}]; represent "
                "high-precision values with DecimalValue instead"
            )
        out.append(str(value))
    elif isinstance(value, float):
        raise CanonicalValueError(
            "floats are rejected by the canonical JSON serializer; convert "
            "high-precision values to DecimalValue and coordinates to "
            "canonical fixed-point geometry before hashing"
        )
    elif isinstance(value, str):
        out.append(_escape_string(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value.keys():
            if not isinstance(key, str):
                raise CanonicalValueError(
                    f"object keys must be strings, got {type(key).__name__}"
                )
        out.append("{")
        for index, key in enumerate(sorted(value.keys(), key=_utf16_sort_key)):
            if index:
                out.append(",")
            out.append(_escape_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise CanonicalValueError(
            f"type {type(value).__name__} is not a canonical JSON value; "
            "wrap domain values with the canonical identity wrappers "
            "(DecimalValue, CanonicalSet, canonical geometry) before serializing"
        )


def canonical_json_str(value: Any) -> str:
    """Serialize a JSON-ready value to the canonical JCS string form."""
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-ready value to canonical JCS bytes (UTF-8)."""
    return canonical_json_str(value).encode("utf-8")
