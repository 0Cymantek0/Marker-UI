"""Canonical identity foundation (marker.canonical.v1).

Deterministic canonical record bytes and domain-separated identity
hashes for identity-bearing Marker UI records:

* :mod:`app.utils.canonical.values` — domain canonicalization
  (raw Unicode policy, decimal strings, semantic sets, geometry);
* :mod:`app.utils.canonical.jcs` — RFC 8785-compatible JSON bytes;
* :mod:`app.utils.canonical.framing` — record identity / payload
  byte hash separation with an inspectable preimage;
* :mod:`app.utils.canonical.geometry` — fixed-point geometry
  (``marker.geometry.fixed_point.v1``).

Stdlib only. The contract and its policies are documented in
``docs/reference/canonical-identity.md``; the golden fixture corpus
lives in ``backend/conformance/fixtures/``.
"""

from __future__ import annotations

from .errors import CanonicalValueError
from .framing import (
    IDENTITY_FRAMING_VERSION,
    payload_byte_hash,
    record_identity_hash,
    record_identity_preimage,
)
from .geometry import (
    GEOMETRY_PROFILE,
    GEOMETRY_SCALE,
    MAX_ABS_UNIT,
    CanonicalBox,
    CanonicalPoint,
    CanonicalPolygon,
)
from .jcs import canonical_json_bytes, canonical_json_str
from .values import (
    CANONICALIZATION_PROFILE,
    CanonicalSet,
    DecimalValue,
    to_json_ready,
)

__all__ = [
    "CANONICALIZATION_PROFILE",
    "CanonicalBox",
    "CanonicalPoint",
    "CanonicalPolygon",
    "CanonicalSet",
    "CanonicalValueError",
    "DecimalValue",
    "GEOMETRY_PROFILE",
    "GEOMETRY_SCALE",
    "IDENTITY_FRAMING_VERSION",
    "MAX_ABS_UNIT",
    "canonical_json_bytes",
    "canonical_json_str",
    "payload_byte_hash",
    "record_identity_hash",
    "record_identity_preimage",
    "to_json_ready",
]
