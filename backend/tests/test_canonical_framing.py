"""Record identity framing unit tests.

Adversarial focus: domain separation, naive-concatenation ambiguity,
preimage inspectability, and payload-hash vs record-identity split.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.utils.canonical import (
    IDENTITY_FRAMING_VERSION,
    CanonicalBox,
    CanonicalSet,
    DecimalValue,
    payload_byte_hash,
    record_identity_hash,
    record_identity_preimage,
)
from app.utils.canonical.errors import CanonicalValueError

RT = "marker.fixture.record.v1"
SV = "marker.fixture.record.v1"
CP = "marker.canonical.v1"


def identity(payload: dict, **overrides: str) -> str:
    kwargs = {
        "record_type": RT,
        "schema_version": SV,
        "payload": payload,
        "canonicalization_profile": CP,
    }
    kwargs.update(overrides)
    return record_identity_hash(**kwargs)


def test_hash_format_and_framing_version() -> None:
    digest = identity({"a": 1})
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    int(digest[7:], 16)  # valid hex
    assert IDENTITY_FRAMING_VERSION == "marker.record_identity.v1"


def test_hash_is_sha256_of_inspectable_preimage() -> None:
    preimage = record_identity_preimage(
        record_type=RT, schema_version=SV, payload={"a": 1}
    )
    envelope = json.loads(preimage.decode("utf-8"))
    assert envelope == {
        "framing": IDENTITY_FRAMING_VERSION,
        "canonicalization_profile": CP,
        "record_type": RT,
        "schema_version": SV,
        "payload": {"a": 1},
    }
    assert identity({"a": 1}) == "sha256:" + hashlib.sha256(preimage).hexdigest()


def test_deterministic_across_calls_and_payload_key_order() -> None:
    first = identity({"b": 2, "a": 1, "deep": {"y": [1], "x": None}})
    second = identity({"deep": {"x": None, "y": [1]}, "a": 1, "b": 2})
    again = identity({"b": 2, "a": 1, "deep": {"y": [1], "x": None}})
    assert first == second == again


def test_naive_concatenation_adversary_cannot_collide() -> None:
    # Under "record_type + payload" string concatenation these two
    # materializations can be made identical; envelope framing cannot.
    left = identity({"v": "BC"}, record_type="a")
    right = identity({"v": "C"}, record_type="ab")
    assert left != right
    assert (
        record_identity_preimage(record_type="a", schema_version=SV, payload={"v": "BC"})
        != record_identity_preimage(record_type="ab", schema_version=SV, payload={"v": "C"})
    )


def test_schema_version_separation() -> None:
    assert identity({"a": 1}) != identity({"a": 1}, schema_version="marker.fixture.record.v2")


def test_record_type_separation() -> None:
    assert identity({"a": 1}) != identity({"a": 1}, record_type="marker.other.record.v1")


def test_canonicalization_profile_separation() -> None:
    assert identity({"a": 1}) != identity(
        {"a": 1}, canonicalization_profile="marker.canonical.v2"
    )


def test_geometry_profile_participates_via_payload() -> None:
    # The geometry profile string lives inside the payload's canonical
    # form, so a future profile bump changes identity automatically.
    box = CanonicalBox.from_bbox([1.0, 2.0, 3.0, 4.0])
    assert record_identity_preimage(
        record_type=RT, schema_version=SV, payload={"bbox": box}
    ) != record_identity_preimage(
        record_type=RT,
        schema_version=SV,
        payload={"bbox": {**box.canonical_value(), "profile": "marker.geometry.fixed_point.v2"}},
    )


def test_full_domain_record_hash_end_to_end() -> None:
    payload = {
        "text": "Revenue: €1,000.50",
        "amount": DecimalValue("1000.50"),
        "bbox": CanonicalBox.from_bbox([72.0, 110.5, 540.25, 660.75]),
        "tags": CanonicalSet(["finance", "page-1"]),
        "sequence": [1, 2, 3],
        "note": None,
    }
    digest = identity(payload)
    assert digest == identity(payload)  # wrapper objects are pure


def test_domain_id_validation() -> None:
    for bad in ["", "Marker.V1", "marker..v1", "v1 ", "marker/v1"]:
        with pytest.raises(CanonicalValueError, match="must match"):
            identity({"a": 1}, record_type=bad)
    with pytest.raises(CanonicalValueError, match="must match"):
        identity({"a": 1}, canonicalization_profile="not valid")


def test_payload_must_be_mapping() -> None:
    with pytest.raises(CanonicalValueError, match="payload must be a mapping"):
        record_identity_hash(record_type=RT, schema_version=SV, payload=[1, 2])  # type: ignore[arg-type]


def test_invalid_payload_values_rejected_at_framing_boundary() -> None:
    with pytest.raises(CanonicalValueError, match="floats cannot enter"):
        identity({"x": 1.5})
    with pytest.raises(CanonicalValueError, match="safe JSON identity range"):
        identity({"x": 2**60})


def test_payload_byte_hash_is_a_different_purpose() -> None:
    raw = b"some stored artifact bytes"
    assert payload_byte_hash(raw) == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert payload_byte_hash(raw) != payload_byte_hash(raw + b"x")
    # Transparency: record identity is exactly payload-byte hashing of the
    # canonical preimage. The two APIs exist so callers never confuse raw
    # stored artifact bytes with framed semantic records.
    canonical = record_identity_preimage(
        record_type=RT, schema_version=SV, payload={"a": 1}
    )
    assert payload_byte_hash(canonical) == identity({"a": 1})
    with pytest.raises(CanonicalValueError, match="expects bytes"):
        payload_byte_hash("not bytes")  # type: ignore[arg-type]
