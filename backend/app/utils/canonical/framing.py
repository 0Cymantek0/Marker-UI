"""Record identity framing: domain separation for canonical hashes.

Two hash purposes live here and must never be conflated:

* :func:`payload_byte_hash` — exact stored-byte hash of opaque bytes
  (``payload_sha256``-style evidence: "these are the bytes I saw").
* :func:`record_identity_hash` — semantic identity of a *record*
  under an explicit domain (record type, schema version,
  canonicalization profile). Same canonical payload under a different
  domain yields a different identity by construction.

The identity preimage is not a delimiter-joined string (the classic
``A:BC`` == ``AB:C`` ambiguity). It is the RFC 8785 canonical JSON of
a framing *envelope* object that carries the domain fields and the
payload structurally, so field boundaries are unambiguous and the
preimage itself is inspectable via :func:`record_identity_preimage`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .errors import CanonicalValueError
from .jcs import canonical_json_bytes
from .values import CANONICALIZATION_PROFILE, to_json_ready

#: Identity-affecting version of the framing envelope itself.
IDENTITY_FRAMING_VERSION = "marker.record_identity.v1"

_DOMAIN_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def _validate_domain_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _DOMAIN_ID_RE.fullmatch(value):
        raise CanonicalValueError(
            f"{name} must match '[a-z0-9]+([._-][a-z0-9]+)*' "
            f"(e.g. 'marker.chunks.v1'), got {value!r}"
        )
    return value


def record_identity_preimage(
    *,
    record_type: str,
    schema_version: str,
    payload: Mapping[str, Any],
    canonicalization_profile: str = CANONICALIZATION_PROFILE,
) -> bytes:
    """Build the canonical, inspectable preimage for a record identity.

    The envelope's canonical JSON bytes are the hash input. Every
    domain field participates, so changing any of them changes the
    hash; the payload is embedded structurally, so naive-concatenation
    collisions are impossible.
    """
    _validate_domain_id("record_type", record_type)
    _validate_domain_id("schema_version", schema_version)
    _validate_domain_id("canonicalization_profile", canonicalization_profile)
    if not isinstance(payload, Mapping):
        raise CanonicalValueError(
            f"payload must be a mapping, got {type(payload).__name__}"
        )
    envelope = {
        "framing": IDENTITY_FRAMING_VERSION,
        "canonicalization_profile": canonicalization_profile,
        "record_type": record_type,
        "schema_version": schema_version,
        "payload": to_json_ready(dict(payload)),
    }
    return canonical_json_bytes(envelope)


def record_identity_hash(
    *,
    record_type: str,
    schema_version: str,
    payload: Mapping[str, Any],
    canonicalization_profile: str = CANONICALIZATION_PROFILE,
) -> str:
    """Compute the domain-separated identity hash of a record.

    Returns ``sha256:<hexdigest>`` of the framing preimage. Geometry
    inside the payload carries its own profile tag, so geometry
    profile bumps also change the hash.
    """
    preimage = record_identity_preimage(
        record_type=record_type,
        schema_version=schema_version,
        payload=payload,
        canonicalization_profile=canonicalization_profile,
    )
    return "sha256:" + hashlib.sha256(preimage).hexdigest()


def payload_byte_hash(data: bytes) -> str:
    """Hash exact stored bytes (payload evidence), not record identity."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CanonicalValueError(
            f"payload_byte_hash expects bytes, got {type(data).__name__}"
        )
    return "sha256:" + hashlib.sha256(bytes(data)).hexdigest()
