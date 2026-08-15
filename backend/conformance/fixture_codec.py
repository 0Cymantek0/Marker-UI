"""Decode tagged JSON fixture payloads into canonical identity inputs.

The fixture corpus (``fixtures/canonical_vectors_v1.json``) is
language-neutral: plain JSON plus a small tag vocabulary expressing
the domain wrappers that JSON cannot spell directly. A future Rust or
TypeScript conformance runner implements the same tags and the same
expected bytes; nothing here depends on Python internals beyond the
canonical identity module itself.

Tag vocabulary (all tags are single-key objects):

* ``{"$set": [...]}``       — unordered semantic set
* ``{"$decimal": "1.10"}``  — high-precision canonical decimal string
* ``{"$geometry": {"kind": "point|box|polygon",
                    "raw": [numbers...] | "scaled": [ints...]}}``
* ``{"$nan": true}`` / ``{"$inf": 1}`` / ``{"$inf": -1}``
* ``{"$datetime": "..."}``  — unsupported type, must be rejected
* ``{"$pyset": [...]}``     — plain hash-ordered set, must be rejected
* ``{"$lone_surrogate": "\ud800"}`` — invalid Unicode, must be rejected

Untagged JSON numbers with a fraction or exponent decode to floats and
are expected to be rejected; that rejection is itself a fixture case.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.utils.canonical import (
    CanonicalBox,
    CanonicalPoint,
    CanonicalPolygon,
    CanonicalSet,
    CanonicalValueError,
    DecimalValue,
    canonical_json_bytes,
    record_identity_hash,
    record_identity_preimage,
    to_json_ready,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_RECORD_TYPE = "marker.fixture.record.v1"
DEFAULT_SCHEMA_VERSION = "marker.fixture.record.v1"
DEFAULT_PROFILE = "marker.canonical.v1"


def decode_payload(node: Any) -> Any:
    """Recursively convert tagged fixture JSON into domain values."""
    if isinstance(node, dict):
        if set(node) == {"$set"}:
            return CanonicalSet([decode_payload(item) for item in node["$set"]])
        if set(node) == {"$decimal"}:
            return DecimalValue(node["$decimal"])
        if set(node) == {"$geometry"}:
            return _decode_geometry(node["$geometry"])
        if set(node) == {"$nan"}:
            return float("nan")
        if set(node) == {"$inf"}:
            return float("inf") if node["$inf"] > 0 else float("-inf")
        if set(node) == {"$datetime"}:
            return datetime.fromisoformat(node["$datetime"].replace("Z", "+00:00"))
        if set(node) == {"$pyset"}:
            return set(node["$pyset"])
        if set(node) == {"$lone_surrogate"}:
            return "\ud800"
        return {key: decode_payload(value) for key, value in node.items()}
    if isinstance(node, list):
        return [decode_payload(item) for item in node]
    return node


def _decode_geometry(spec: dict[str, Any]) -> Any:
    kind = spec["kind"]
    if "raw" in spec:
        raw = spec["raw"]
        if kind == "point":
            return CanonicalPoint.from_coordinates(raw[0], raw[1])
        if kind == "box":
            return CanonicalBox.from_coordinates(*raw)
        if kind == "polygon":
            return CanonicalPolygon.from_coordinates(
                [(pair[0], pair[1]) for pair in raw]
            )
    scaled = spec["scaled"]
    if kind == "point":
        return CanonicalPoint(scaled[0], scaled[1])
    if kind == "box":
        return CanonicalBox(*scaled)
    if kind == "polygon":
        return CanonicalPolygon(
            tuple(CanonicalPoint(pair[0], pair[1]) for pair in scaled)
        )
    raise CanonicalValueError(f"unknown geometry kind {kind!r}")


def load_fixture_corpus(path: Path | None = None) -> dict[str, Any]:
    fixture_path = path or (FIXTURES_DIR / "canonical_vectors_v1.json")
    with open(fixture_path, encoding="utf-8") as handle:
        return json.load(handle)


def case_domain(case: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, str]:
    domain = {
        "record_type": case.get("record_type", DEFAULT_RECORD_TYPE),
        "schema_version": case.get("schema_version", DEFAULT_SCHEMA_VERSION),
        "canonicalization_profile": case.get(
            "canonicalization_profile", DEFAULT_PROFILE
        ),
    }
    if overrides:
        domain.update({k: v for k, v in overrides.items() if k in domain})
    return domain


def compute_expected(case: dict[str, Any]) -> dict[str, str]:
    """Compute the committed expected outputs for a positive case."""
    payload = decode_payload(case["payload"])
    domain = case_domain(case)
    ready = to_json_ready(payload)
    payload_canonical = canonical_json_bytes(ready).decode("utf-8")
    preimage = record_identity_preimage(payload=payload, **domain)
    return {
        "payload_canonical": payload_canonical,
        "preimage": preimage.decode("utf-8"),
        "identity_hash": record_identity_hash(payload=payload, **domain),
    }


def case_identity(case: dict[str, Any], overrides: dict[str, Any] | None = None) -> str:
    """Build the identity hash of a case (or a variant of it)."""
    payload_source = case["payload"]
    if overrides and "payload" in overrides:
        payload_source = overrides["payload"]
    payload = decode_payload(payload_source)
    domain = case_domain(case, overrides)
    return record_identity_hash(payload=payload, **domain)


def verify_case(case: dict[str, Any]) -> None:
    """Assert one fixture case against its committed expected outputs."""
    case_id = case["id"]
    if "expect_error" in case:
        try:
            case_identity(case)
        except CanonicalValueError as exc:
            assert case["expect_error"] in str(exc), (
                f"{case_id}: expected error containing {case['expect_error']!r}, "
                f"got {exc}"
            )
        else:
            raise AssertionError(
                f"{case_id}: expected CanonicalValueError "
                f"({case['expect_error']!r}), got success"
            )
        return

    expected = case["expect"]
    for key in ("payload_canonical", "preimage", "identity_hash"):
        assert expected.get(key), f"{case_id}: missing committed {key}"

    actual = compute_expected(case)
    for key, value in expected.items():
        assert actual[key] == value, (
            f"{case_id}: {key} drift\n  expected: {value!r}\n  actual:   {actual[key]!r}"
        )

    # Determinism: rebuild from scratch and require byte-identical results.
    assert compute_expected(case) == actual, f"{case_id}: nondeterministic output"

    default_expectation = case.get("variant_expectation", "same")
    for variant in case.get("variants", []):
        variant_id = f"{case['id']} / variant {variant.get('id', '?')}"
        expectation = variant.get("expectation", default_expectation)
        variant_hash = case_identity(case, variant)
        rebuild = case_identity(case, variant)
        assert variant_hash == rebuild, f"{variant_id}: nondeterministic"
        if expectation == "same":
            assert variant_hash == expected["identity_hash"], (
                f"{variant_id}: expected same identity as base case"
            )
        else:
            assert variant_hash != expected["identity_hash"], (
                f"{variant_id}: expected a different identity from base case"
            )
