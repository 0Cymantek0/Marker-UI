# Canonical identity golden fixtures

`canonical_vectors_v1.json` is the committed golden corpus for the
`marker.canonical.v1` identity contract (canonical serialization, hash
framing, fixed-point geometry). It is **language neutral**: a Rust or
TypeScript conformance runner consumes the same file and must reproduce
the same expected bytes and digests. The contract itself is documented
in `docs/reference/canonical-identity.md`.

## File format

Top level:

| Key | Meaning |
|---|---|
| `$schema` | Always `marker.canonical.fixtures.v1`; bump on incompatible format changes |
| `canonicalization_profile` | Domain profile the expected outputs assume |
| `framing` | Envelope framing version |
| `cases` | Array of cases |

Each case:

| Key | Meaning |
|---|---|
| `id` | Unique, stable identifier |
| `category` | `ordering` / `unicode` / `structure` / `numbers` / `geometry` / `framing` / `rejection` / `composite` |
| `record_type`, `schema_version`, `canonicalization_profile` | Optional domain overrides (default `marker.fixture.record.v1` / `marker.canonical.v1`) |
| `payload` | Tagged fixture encoding of the semantic record (see below) |
| `variants` | Optional array of `{id, expectation?, payload?/record_type?/schema_version?/canonicalization_profile?}` overrides |
| `variant_expectation` | Default variant relation to the base identity: `same` or `different` (per-variant `expectation` overrides) |
| `expect` | Committed golden outputs (positive cases): `payload_canonical` (RFC 8785 bytes of the payload), `preimage` (framing envelope bytes), `identity_hash` (`sha256:<hex>`) |
| `expect_error` | Rejection cases: substring of the expected `CanonicalValueError` message |

## Payload tag vocabulary

Tags are single-key JSON objects; untagged JSON maps directly to the
semantic value:

| Tag | Meaning |
|---|---|
| `{"$set": [...]}` | Unordered semantic set; member order must not affect identity |
| `{"$decimal": "1.10"}` | High-precision number as canonical decimal string |
| `{"$geometry": {"kind": "point"\|"box"\|"polygon", "raw": [...]}` | Coordinates in source units (ints/floats/decimal strings), quantized by the fixed-point profile before hashing |
| `{"$geometry": {"kind": ..., "scaled": [...]}` | Already-quantized fixed-point integers (thousandths) |
| `{"$nan": true}`, `{"$inf": 1}`, `{"$inf": -1}` | Non-finite floats; must be rejected |
| `{"$datetime": "..."}` | Unsupported type; must be rejected |
| `{"$pyset": [...]}` | Hash-ordered native set; must be rejected |
| `{"$lone_surrogate": true}` | Lone UTF-16 surrogate; must be rejected |

Untagged JSON numbers with a fraction or exponent (e.g. `1.5`) decode
to binary floats and are expected to be rejected — that is part of the
contract.

## Regeneration

Expected outputs are constants, not computed at test time. The only
sanctioned regeneration path is:

```bash
python backend/scripts/generate_canonical_fixtures.py --write
```

Run it only after an intentional contract change and review the diff:
hash movement means every dependent identity changed.

## Running the conformance suite

Stdlib + pytest only; no app dependencies, database, or models:

```bash
cd backend
python -m pytest conformance -q
```
