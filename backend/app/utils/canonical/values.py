"""Domain canonicalization: semantic values -> JCS-safe values.

The identity contract is two-layered:

1. this module maps identity-bearing *semantic* values (high-precision
   decimals, schema-declared unordered sets, fixed-point geometry) onto
   a tiny JSON-safe value domain;
2. :mod:`app.utils.canonical.jcs` serializes that domain to
   deterministic RFC 8785-compatible bytes.

Policies enforced here:

* **Raw Unicode.** Strings enter identity exactly as provided. No NFC,
  NFKC, case folding, whitespace, or line-joining normalization ever
  happens; composed and decomposed lookalikes stay distinct.
* **No binary floats.** ``float`` is rejected. High-precision numbers
  must use :class:`DecimalValue` (canonical decimal string); engine
  coordinates must go through canonical fixed-point geometry.
* **Explicit sets.** Plain ``set``/``frozenset`` are rejected because
  their iteration order is hash-seed dependent. Use
  :class:`CanonicalSet`, which sorts members by their canonical bytes.
* **Visible failure.** Anything unsupported (datetimes, objects,
  bytes, NaN-bearing types) raises :class:`CanonicalValueError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from .errors import CanonicalValueError
from .jcs import MAX_SAFE_INTEGER, MIN_SAFE_INTEGER, canonical_json_bytes

#: Identity-affecting version of the domain canonicalization profile.
CANONICALIZATION_PROFILE = "marker.canonical.v1"

_DECIMAL_TEXT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


@runtime_checkable
class CanonicalValue(Protocol):
    """A wrapper that knows its own canonical JSON-ready form."""

    def canonical_value(self) -> Any: ...


@dataclass(frozen=True)
class DecimalValue:
    """A high-precision number carried as a canonical decimal string.

    Canonical text form: optional ``-``, no leading zeros, optional
    fraction with at least one digit, no exponent, no signed zero.
    Trailing fraction zeros are preserved because digit significance
    is semantic (``"1.10"`` and ``"1.1"`` hash differently).
    """

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not _DECIMAL_TEXT_RE.fullmatch(self.text):
            raise CanonicalValueError(
                f"invalid canonical decimal string {self.text!r}; expected "
                "form like '-123.450' (no exponent, no leading zeros, "
                "no signed zero)"
            )
        if self.text.startswith("-") and set(self.text) <= {"-", "0", "."}:
            raise CanonicalValueError(
                f"signed zero {self.text!r} is not canonical; use '0'"
            )

    @classmethod
    def from_decimal(cls, value: Decimal) -> DecimalValue:
        if not isinstance(value, Decimal):
            raise CanonicalValueError(
                f"from_decimal expects Decimal, got {type(value).__name__}"
            )
        if not value.is_finite():
            raise CanonicalValueError(
                f"non-finite decimal {value!r} cannot enter identity"
            )
        text = format(value, "f")
        if text.startswith("-") and set(text) <= {"-", "0", "."}:
            text = text[1:]  # collapse signed zero ("-0.0" -> "0.0")
        return cls(text)

    def canonical_value(self) -> str:
        return self.text


@dataclass(frozen=True)
class CanonicalSet:
    """An unordered semantic collection with deterministic ordering.

    Members are serialized to canonical bytes and sorted by those
    bytes; two members with identical canonical bytes are a contract
    violation (duplicates are rejected, not collapsed, so a silent
    semantic merge can never hide inside an identity hash).
    """

    items: tuple[Any, ...]

    def __init__(self, items: Iterable[Any]) -> None:
        object.__setattr__(self, "items", tuple(items))

    def canonical_value(self) -> list[Any]:
        members = [to_json_ready(item) for item in self.items]
        encodings = [canonical_json_bytes(member) for member in members]
        if len(set(encodings)) != len(encodings):
            raise CanonicalValueError(
                "CanonicalSet contains duplicate members under canonical "
                "encoding; identity-relevant sets must not carry duplicates"
            )
        return [member for _, member in sorted(zip(encodings, members))]


def to_json_ready(value: Any) -> Any:
    """Convert an identity-bearing semantic value to the JCS-safe domain.

    Raises :class:`CanonicalValueError` for anything the canonical
    identity contract does not define, including all floats.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        # bool already handled above; only genuine ints reach here.
        if not (MIN_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER):
            raise CanonicalValueError(
                f"integer {value} outside the safe JSON identity range; "
                "use DecimalValue for high-precision numbers"
            )
        return value
    if isinstance(value, DecimalValue):
        return value.canonical_value()
    if isinstance(value, Decimal):
        return DecimalValue.from_decimal(value).canonical_value()
    if isinstance(value, float):
        raise CanonicalValueError(
            "floats cannot enter canonical identity; use DecimalValue for "
            "high-precision numbers and canonical fixed-point geometry for "
            "coordinates"
        )
    if isinstance(value, (set, frozenset)):
        raise CanonicalValueError(
            "plain set/frozenset has hash-seed-dependent iteration order; "
            "wrap it in CanonicalSet to declare it as an unordered identity set"
        )
    if isinstance(value, CanonicalValue):
        result = value.canonical_value()
        # Geometry/sets return mappings/lists that may nest wrappers.
        return to_json_ready(result)
    if isinstance(value, Mapping):
        ready: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalValueError(
                    f"object keys must be strings, got {type(key).__name__}"
                )
            ready[key] = to_json_ready(item)
        return ready
    if isinstance(value, (list, tuple)):
        return [to_json_ready(item) for item in value]
    raise CanonicalValueError(
        f"type {type(value).__name__} is not a canonical identity value; "
        "supported: None/bool/str/bounded int/DecimalValue/CanonicalSet/"
        "canonical geometry/mapping/sequence"
    )
