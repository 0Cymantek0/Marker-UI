"""Versioned structured-extraction schema contract (PR80A).

An extraction schema names the fields a program wants out of published
evidence: scalar fields, repeated line-item structures, and the
deterministic business invariants that make a result acceptable. The
schema is versioned data — not code — so a stored result can always be
revalidated against the exact schema identity that produced it.

Deliberate scope (PR80A non-claim): this is one narrow schema family
(``marker.extraction.schema.v1``), not a general extraction DSL. Field
types cover what the deterministic route can parse honestly; anything
richer belongs to a later schema version, never to silent coercion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.utils.canonical import record_identity_hash, to_json_ready

#: Wire identity of this schema family. A schema definition carrying a
#: different version parses only under its own family module.
EXTRACTION_SCHEMA_VERSION = "marker.extraction.schema.v1"

#: Field types the deterministic PR80A route can parse and validate.
FIELD_TYPES = frozenset({"string", "integer", "decimal", "date", "enum"})

#: Invariant kinds executable by :mod:`app.extraction.validation`.
INVARIANT_KINDS = frozenset({"sum_equality"})


class ExtractionSchemaError(ValueError):
    """Raised when a schema definition is malformed or unsupported."""


@dataclass(frozen=True)
class FieldSpec:
    """One scalar field: type, requiredness, and its evidence anchor.

    ``anchor`` is the literal source label the deterministic route keys
    on (e.g. ``"Invoice Number"`` for text ``Invoice Number: INV-1``).
    It is part of schema identity: changing how a field is located is a
    semantic change, not a cosmetic one.
    """

    name: str
    type: str
    anchor: str
    required: bool = True
    enum_values: tuple[str, ...] = ()
    pattern: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ExtractionSchemaError(f"invalid field name: {self.name!r}")
        if self.type not in FIELD_TYPES:
            raise ExtractionSchemaError(
                f"field {self.name!r}: unsupported type {self.type!r}; "
                f"supported: {sorted(FIELD_TYPES)}"
            )
        if not isinstance(self.anchor, str) or not self.anchor.strip():
            raise ExtractionSchemaError(
                f"field {self.name!r}: a non-empty anchor label is required"
            )
        if self.type == "enum" and not self.enum_values:
            raise ExtractionSchemaError(
                f"field {self.name!r}: enum fields must declare enum_values"
            )
        if self.type != "enum" and self.enum_values:
            raise ExtractionSchemaError(
                f"field {self.name!r}: enum_values is only valid for enum fields"
            )
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ExtractionSchemaError(
                    f"field {self.name!r}: invalid pattern: {exc}"
                ) from exc

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "anchor": self.anchor,
            "required": self.required,
        }
        if self.enum_values:
            data["enum_values"] = list(self.enum_values)
        if self.pattern is not None:
            data["pattern"] = self.pattern
        return data


@dataclass(frozen=True)
class LineItemSpec:
    """A repeated record structure extracted as identified rows.

    ``identity_keys`` names the fields whose combined value identifies a
    row across documents: reconciliation collapses two candidates that
    agree on every identity key and every field, and never merges rows
    that differ on an identity key. An explicit identity rule is what
    keeps duplicate-row handling attributable instead of fuzzy.
    """

    name: str
    anchor: str
    fields: tuple[FieldSpec, ...]
    identity_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ExtractionSchemaError(f"invalid line item name: {self.name!r}")
        if not isinstance(self.anchor, str) or not self.anchor.strip():
            raise ExtractionSchemaError(
                f"line item {self.name!r}: a non-empty row anchor is required"
            )
        if not self.fields:
            raise ExtractionSchemaError(
                f"line item {self.name!r}: at least one field is required"
            )
        names = [spec.name for spec in self.fields]
        if len(set(names)) != len(names):
            raise ExtractionSchemaError(
                f"line item {self.name!r}: duplicate field names {sorted(names)}"
            )
        for spec in self.fields:
            if spec.required is not True:
                raise ExtractionSchemaError(
                    f"line item {self.name!r} field {spec.name!r}: row fields "
                    "are always required; optional row fields are a later "
                    "schema-family decision"
                )
        unknown = set(self.identity_keys) - set(names)
        if unknown:
            raise ExtractionSchemaError(
                f"line item {self.name!r}: identity_keys name unknown "
                f"fields {sorted(unknown)}"
            )
        if not self.identity_keys:
            # Default identity rule: the full row value identifies the row.
            object.__setattr__(self, "identity_keys", tuple(names))

    def field(self, name: str) -> FieldSpec:
        for spec in self.fields:
            if spec.name == name:
                return spec
        raise ExtractionSchemaError(
            f"line item {self.name!r} has no field {name!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "anchor": self.anchor,
            "fields": [spec.to_dict() for spec in self.fields],
            "identity_keys": list(self.identity_keys),
        }


@dataclass(frozen=True)
class SchemaInvariant:
    """One executable business invariant over accepted output values.

    ``sum_equality``: the scalar field ``target`` must equal the sum of
    ``field`` across accepted rows of line item ``items``, within the
    declared decimal tolerance. Money is compared as exact decimals;
    ``tolerance`` exists for rounded human totals, not to hide errors.
    """

    kind: str
    target: str
    items: str
    item_field: str
    tolerance: str = "0"

    def __post_init__(self) -> None:
        if self.kind not in INVARIANT_KINDS:
            raise ExtractionSchemaError(
                f"unsupported invariant kind {self.kind!r}; "
                f"supported: {sorted(INVARIANT_KINDS)}"
            )
        for name in (self.target, self.items, self.item_field):
            if not isinstance(name, str) or not name:
                raise ExtractionSchemaError(
                    f"invariant {self.kind!r}: target/items/item_field must be "
                    "non-empty field names"
                )
        try:
            tolerance = Decimal(self.tolerance)
        except InvalidOperation as exc:
            raise ExtractionSchemaError(
                f"invariant {self.kind!r}: tolerance must be a decimal string"
            ) from exc
        if tolerance < 0:
            raise ExtractionSchemaError(
                f"invariant {self.kind!r}: tolerance must be non-negative"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "items": self.items,
            "item_field": self.item_field,
            "tolerance": self.tolerance,
        }


@dataclass(frozen=True)
class ExtractionSchema:
    """A named, versioned extraction program definition.

    Identity is the canonical hash of the full definition: renaming a
    field, changing an anchor, or tightening an invariant mints a new
    schema identity, so a stored result can never silently change
    meaning when the schema evolves.
    """

    schema_id: str
    version: str
    fields: tuple[FieldSpec, ...] = ()
    line_items: tuple[LineItemSpec, ...] = ()
    invariants: tuple[SchemaInvariant, ...] = ()

    def __post_init__(self) -> None:
        if not self.schema_id or not re.fullmatch(r"[a-z][a-z0-9_.-]*", self.schema_id):
            raise ExtractionSchemaError(f"invalid schema_id: {self.schema_id!r}")
        if not self.version or not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            raise ExtractionSchemaError(
                f"schema {self.schema_id!r}: version must be semver-like, "
                f"got {self.version!r}"
            )
        names = [spec.name for spec in self.fields]
        if len(set(names)) != len(names):
            raise ExtractionSchemaError(
                f"schema {self.schema_id!r}: duplicate scalar fields {sorted(names)}"
            )
        item_names = [item.name for item in self.line_items]
        if len(set(item_names)) != len(item_names):
            raise ExtractionSchemaError(
                f"schema {self.schema_id!r}: duplicate line items "
                f"{sorted(item_names)}"
            )
        for invariant in self.invariants:
            if invariant.target not in names:
                raise ExtractionSchemaError(
                    f"schema {self.schema_id!r}: invariant target "
                    f"{invariant.target!r} is not a scalar field"
                )
            if invariant.items not in item_names:
                raise ExtractionSchemaError(
                    f"schema {self.schema_id!r}: invariant items "
                    f"{invariant.items!r} is not a line item"
                )
            item = self.line_item(invariant.items)
            if invariant.item_field not in {spec.name for spec in item.fields}:
                raise ExtractionSchemaError(
                    f"schema {self.schema_id!r}: invariant item_field "
                    f"{invariant.item_field!r} is not a field of "
                    f"{invariant.items!r}"
                )

    # -- accessors -------------------------------------------------------

    def field(self, name: str) -> FieldSpec:
        for spec in self.fields:
            if spec.name == name:
                return spec
        raise ExtractionSchemaError(f"schema has no scalar field {name!r}")

    def line_item(self, name: str) -> LineItemSpec:
        for item in self.line_items:
            if item.name == name:
                return item
        raise ExtractionSchemaError(f"schema has no line item {name!r}")

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "schema_id": self.schema_id,
            "version": self.version,
            "fields": [spec.to_dict() for spec in self.fields],
            "line_items": [item.to_dict() for item in self.line_items],
            "invariants": [inv.to_dict() for inv in self.invariants],
        }

    @property
    def identity(self) -> str:
        """Domain-separated identity hash of the full definition."""
        return record_identity_hash(
            record_type="marker.extraction.schema",
            schema_version=EXTRACTION_SCHEMA_VERSION,
            payload=to_json_ready(self.to_dict()),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExtractionSchema:
        """Parse a schema definition, failing closed on anything unknown."""
        if not isinstance(data, Mapping):
            raise ExtractionSchemaError(
                f"schema definition must be a mapping, got {type(data).__name__}"
            )
        version = data.get("schema_version")
        if version != EXTRACTION_SCHEMA_VERSION:
            raise ExtractionSchemaError(
                f"unsupported schema_version {version!r}; this parser "
                f"understands {EXTRACTION_SCHEMA_VERSION!r}"
            )
        allowed = {
            "schema_version",
            "schema_id",
            "version",
            "fields",
            "line_items",
            "invariants",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ExtractionSchemaError(
                f"unknown schema definition keys {sorted(unknown)}"
            )
        try:
            fields = tuple(
                _field_from_dict(entry, context="fields")
                for entry in data.get("fields") or ()
            )
            line_items = tuple(
                _line_item_from_dict(entry) for entry in data.get("line_items") or ()
            )
            invariants = tuple(
                SchemaInvariant(
                    kind=entry["kind"],
                    target=entry["target"],
                    items=entry["items"],
                    item_field=entry["item_field"],
                    tolerance=str(entry.get("tolerance", "0")),
                )
                for entry in data.get("invariants") or ()
            )
            schema = cls(
                schema_id=str(data["schema_id"]),
                version=str(data["version"]),
                fields=fields,
                line_items=line_items,
                invariants=invariants,
            )
        except KeyError as exc:
            raise ExtractionSchemaError(
                f"schema definition is missing {exc.args[0]!r}"
            ) from None
        return schema


def _field_from_dict(entry: Any, *, context: str) -> FieldSpec:
    if not isinstance(entry, Mapping):
        raise ExtractionSchemaError(
            f"{context}: field definitions must be mappings, got "
            f"{type(entry).__name__}"
        )
    allowed = {"name", "type", "anchor", "required", "enum_values", "pattern"}
    unknown = set(entry) - allowed
    if unknown:
        raise ExtractionSchemaError(
            f"{context}: unknown field keys {sorted(unknown)}"
        )
    return FieldSpec(
        name=entry["name"],
        type=entry["type"],
        anchor=entry["anchor"],
        required=bool(entry.get("required", True)),
        enum_values=tuple(str(v) for v in entry.get("enum_values") or ()),
        pattern=entry.get("pattern"),
    )


def _line_item_from_dict(entry: Any) -> LineItemSpec:
    if not isinstance(entry, Mapping):
        raise ExtractionSchemaError(
            f"line_items: definitions must be mappings, got {type(entry).__name__}"
        )
    allowed = {"name", "anchor", "fields", "identity_keys"}
    unknown = set(entry) - allowed
    if unknown:
        raise ExtractionSchemaError(f"line_items: unknown keys {sorted(unknown)}")
    return LineItemSpec(
        name=entry["name"],
        anchor=entry["anchor"],
        fields=tuple(
            _field_from_dict(item, context=f"line item {entry.get('name')!r}")
            for item in entry.get("fields") or ()
        ),
        identity_keys=tuple(str(k) for k in entry.get("identity_keys") or ()),
    )
