# Canonical Identity Contract

This document is the authoritative contract for Marker UI's canonical
identity foundation (`marker.canonical.v1`): deterministic canonical
record bytes, domain-separated identity hashes, and fixed-point
geometry for identity-bearing records. It is the contract layer that
later V3.2 work (Alembic migration authority, Truth Kernel tables,
source identity, SourceAnchor) builds on.

Source of truth for the code: `backend/app/utils/canonical/`.
Source of truth for golden fixtures:
`backend/conformance/fixtures/canonical_vectors_v1.json`.

---

## Why canonical identity exists

An identity-bearing record must answer: *given the same semantic
record on two machines, do we get the same canonical bytes and the
same hash?* Before this contract, the answer was "no" by construction:
Python `json.dumps(sort_keys=True)` differs from RFC 8785 on key
ordering and number formatting; engine coordinates enter hashes as
float `repr` strings; high-precision values lose meaning through
IEEE-754 doubles; and delimiter-joined hash material is
ambiguity-prone (`A:BC` collides with `AB:C`).

The contract is two-layered, per the V3.2 canonical-record amendment:

1. **Domain canonicalization** (`values.py`, `geometry.py`) maps
   semantic values onto a tiny JSON-safe value domain.
2. **RFC 8785 JCS serialization** (`jcs.py`) turns that domain into
   deterministic bytes.

---

## Canonicalization layers and accepted values

| Value class | Canonical form | Rules |
|---|---|---|
| Strings | raw Unicode string | No NFC/NFKC/case/whitespace/line-join normalization; NFC and NFD lookalikes are distinct identities |
| Booleans, null | JSON literals | `true` / `false` / `null` |
| Bounded integers | JSON number | Only within ±(2^53 − 1); anything larger is rejected, not truncated |
| High-precision numbers | `DecimalValue` canonical decimal string | Form `-?(0|[1-9][0-9]*)(\.[0-9]+)?`; no exponent, no leading zeros, no signed zero; trailing fraction zeros are significant |
| Mappings | JSON object | Keys must be strings; sorted by UTF-16 code units at serialization |
| Ordered sequences | JSON array | Order is identity-affecting |
| Unordered semantic sets | `CanonicalSet` | Members sorted by their canonical bytes; duplicates rejected |
| Fixed-point geometry | tagged integer objects | See below |

**Explicitly rejected** (raising `CanonicalValueError`, never
stringified): all floats (including `NaN`, `±Infinity`, and
JSON-decoded `1.0`), integers beyond the safe range, plain
`set`/`frozenset`, non-string mapping keys, datetimes and any other
unsupported type, strings containing lone UTF-16 surrogates.

### Unicode policy

Strings enter identity exactly as provided. There is no
normalization, because composed and decomposed forms are *distinct
source evidence* — silently equating them would collapse distinct
records. The JCS layer escapes only `"` `\` and C0 controls
(lowercase `\u00xx`); U+007F and all non-ASCII stay raw, output is
UTF-8.

### Number and precision policy

Binary floats never enter identity. Coordinates are quantized to
fixed-point integers (below); other high-precision values become
canonical decimal strings whose trailing zeros carry significance
(`"1.10"` ≠ `"1.1"`).

### Optional/default/extension fields

Canonicalization has **no implicit default-filling**. `{}`,
`{"x": null}`, `{"x": {}}`, and omitting a key are four different
records. Callers own the decision; the serializer never invents or
drops fields. A future identity-affecting field therefore changes the
hash instead of being silently ignored.

---

## RFC 8785 (JCS) scope

`jcs.py` is a small local implementation, chosen over a third-party
package after inspection (auditability, zero dependency/lock churn,
and the fact that our value domain is far smaller than full JCS).
Because the domain layer never produces floats, the ES6
double-formatting rules of JCS are out of scope; integers serialize
as plain decimal digits, which is byte-identical to JCS for the safe
range we allow. Key sorting uses UTF-16 code-unit order (verified
against the official JCS reference test vectors in
`backend/tests/test_canonical_jcs.py`; vectors containing non-integer
numbers are exercised with the numeric parts reduced to integers).

---

## Fixed-point geometry profile (v1)

`marker.geometry.fixed_point.v1`:

* **Coordinate space**: source-document units (PDF points, pixels, or
  a declared engine space); top-left origin, Y-down axis convention
  is the consumer's declared semantics — the profile stores only
  quantized integers.
* **Scale**: 1/1000 of a source unit (`GEOMETRY_SCALE = 1000`).
* **Quantization**: exact decimal arithmetic on the *exact* input
  value — `Decimal(float)` expands the binary value exactly, so
  identical float64 bits quantize identically everywhere — then
  round-half-even to an integer. Ties (`.5` scaled) round to the even
  neighbor.
* **Valid range**: source-unit magnitude ≤ 1,000,000,000; overflow is
  rejected.
* **Shapes**: `CanonicalPoint`, `CanonicalBox` (strictly positive
  extent; degenerate/inverted boxes rejected), `CanonicalPolygon`
  (≥ 3 vertices after dropping a repeated closing vertex).
* **Canonical forms** are self-describing tagged objects, e.g.
  `{"geometry":"box","profile":"marker.geometry.fixed_point.v1","x0":72000,...}`.
  The profile string participates in identity, so a future profile
  bump changes hashes automatically.

Engine-native float `bbox`/`polygon` (Hybrid OCR targets, table
evidence cells) cross this boundary via `CanonicalBox.from_bbox` /
`CanonicalPolygon.from_coordinates`. Engine-facing float structures
are unchanged in place; the adapter is the conversion boundary.

---

## Hash framing and domain separation

Two distinct purposes, deliberately separate APIs:

* `payload_byte_hash(data: bytes) -> "sha256:<hex>"` — exact
  stored-byte hash of opaque bytes ("these are the bytes I saw").
* `record_identity_hash(record_type=..., schema_version=...,
  payload=...) -> "sha256:<hex>"` — semantic identity of a record.

The identity preimage is **not** a concatenated string. It is the
canonical JSON of an envelope:

```json
{
  "canonicalization_profile": "marker.canonical.v1",
  "framing": "marker.record_identity.v1",
  "payload": { "...": "..." },
  "record_type": "marker.thing.v1",
  "schema_version": "marker.thing.v1"
}
```

Structural JSON framing makes field boundaries unambiguous (the
`A`+`BC` vs `AB`+`C` adversary cannot collide), and
`record_identity_preimage(...)` exposes the exact bytes so identities
are independently inspectable and recomputable. Domain IDs must match
`[a-z0-9]+([._-][a-z0-9]+)*`. SHA-256 is sufficient; no exotic
cryptography.

---

## Golden fixtures and portability

`backend/conformance/fixtures/canonical_vectors_v1.json` holds 31
cases (16 golden, 15 rejection) covering ordering invariance,
set-vs-list semantics, raw Unicode lookalikes, control-character
escaping, high-precision decimals, safe-integer extremes, geometry
quantization boundaries and equivalent input forms, framing/domain
separation, and adversarial rejections. Expected bytes and digests
are **committed constants**; tests never regenerate them. The only
sanctioned regeneration is
`backend/scripts/generate_canonical_fixtures.py --write`, whose diff
must be reviewed. The fixture format (tags documented in
`backend/conformance/fixtures/README.md`) is language-neutral so a
future Rust/TypeScript runner consumes the same vectors.

Cross-platform proof: the `canonical-conformance` CI job runs the
suite on Ubuntu/Windows/macOS × Python 3.11/3.13 with nothing but
pytest installed — stdlib-only determinism, independent of locale,
path separators, float formatting, and hash-seed randomization.

---

## Compatibility stance

* Legacy identifiers are **unchanged**: `marker.chunks.v1` stable IDs
  (`sha1`-based, `stable_`-prefixed), content hashes, cache keys,
  artifact names, job IDs. The canonical layer is additive; no
  migration was performed or required.
* No database, API, CLI, or MCP behavior changed.
* New code needing identity should use this layer rather than
  inventing another local `sha256(...)` helper. Known legacy seams
  that future work may migrate deliberately (not now):
  `hybrid_ocr/collector.py` fingerprinting via float `repr`, and
  `chunking.py`'s delimiter-joined `sha1` stable IDs.

---

## What later PRs build on this

* **PR62** (Alembic migration authority): new identity-bearing tables
  can rely on this contract for stable record IDs.
* **PR63** (Kernel tables/commit manifests): canonical IDs for kernel
  records come from `record_identity_hash`, not ad-hoc schemes.
* **PR70/72+** (source identity, SourceAnchor): fixed-point geometry
  and the evidence-vs-payload hash split are ready; anchors should
  use the geometry profile rather than engine floats.

Deliberately deferred: canonical timestamp policy (records currently
carry timestamps as caller-defined strings or omit them), decimal
scale/severity rules for specific record families, and any second
language implementation. These stay on the follow-up list rather than
being silently assumed.
