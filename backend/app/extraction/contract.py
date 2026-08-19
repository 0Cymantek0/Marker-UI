"""Extraction request boundary and schema registry (PR80A).

A run names exactly one schema identity and the workspace/publication
context it targets. Schemas are registered in code for this slice: the
registry is the single source of schema truth, and a request naming an
unknown schema fails closed instead of improvising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.extraction.schema import (
    ExtractionSchema,
    ExtractionSchemaError,
    FieldSpec,
    LineItemSpec,
    SchemaInvariant,
)

#: Errors that make a request unexecutable as asked.
class ExtractionRequestError(ValueError):
    """Raised when an extraction request is malformed or unresolvable."""


@dataclass(frozen=True)
class ExtractionRequest:
    """One extraction run request.

    ``expected_publication_set_id`` is optional: when set, the run
    refuses to produce a current-result against a different active
    publication (stale-context honesty) instead of silently extracting
    from whatever is now live.
    """

    schema_id: str
    schema_version: str
    workspace_id: str
    expected_publication_set_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "expected_publication_set_id": self.expected_publication_set_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExtractionRequest:
        if not isinstance(data, Mapping):
            raise ExtractionRequestError(
                f"extraction request must be a mapping, got {type(data).__name__}"
            )
        allowed = {
            "schema_id",
            "schema_version",
            "workspace_id",
            "expected_publication_set_id",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ExtractionRequestError(
                f"unknown extraction request keys {sorted(unknown)}"
            )
        try:
            schema_id = str(data["schema_id"])
            schema_version = str(data["schema_version"])
            workspace_id = str(data["workspace_id"])
        except KeyError as exc:
            raise ExtractionRequestError(
                f"extraction request is missing {exc.args[0]!r}"
            ) from None
        if not schema_id or not workspace_id:
            raise ExtractionRequestError("schema_id and workspace_id are required")
        expected = data.get("expected_publication_set_id")
        if expected is not None and not isinstance(expected, str):
            raise ExtractionRequestError(
                "expected_publication_set_id must be a string when present"
            )
        return cls(
            schema_id=schema_id,
            schema_version=schema_version,
            workspace_id=workspace_id,
            expected_publication_set_id=expected,
        )


#: The PR80A proof schema: an invoice-style document with scalar
#: header fields, repeated line items under an explicit identity rule,
#: and one sum invariant. Narrow by design — this slice proves the
#: evidence/reconciliation spine, not schema generality.
INVOICE_SCHEMA = ExtractionSchema(
    schema_id="demo.invoice",
    version="1.0.0",
    fields=(
        FieldSpec(name="invoice_number", type="string", anchor="Invoice Number"),
        FieldSpec(name="invoice_date", type="date", anchor="Invoice Date"),
        FieldSpec(name="currency", type="enum", anchor="Currency", enum_values=("USD", "EUR", "GBP")),
        FieldSpec(name="po_number", type="string", anchor="PO Number", required=False),
        FieldSpec(name="total_due", type="decimal", anchor="Total Due"),
    ),
    line_items=(
        LineItemSpec(
            name="items",
            anchor="LINEITEM",
            fields=(
                FieldSpec(name="sku", type="string", anchor="sku"),
                FieldSpec(name="description", type="string", anchor="description"),
                FieldSpec(name="quantity", type="integer", anchor="quantity"),
                FieldSpec(name="unit_price", type="decimal", anchor="unit price"),
                FieldSpec(name="amount", type="decimal", anchor="amount"),
            ),
            identity_keys=("sku",),
        ),
    ),
    invariants=(
        SchemaInvariant(
            kind="sum_equality",
            target="total_due",
            items="items",
            item_field="amount",
            tolerance="0.01",
        ),
    ),
)

_REGISTRY: dict[tuple[str, str], ExtractionSchema] = {
    (INVOICE_SCHEMA.schema_id, INVOICE_SCHEMA.version): INVOICE_SCHEMA,
}


def register_schema(schema: ExtractionSchema) -> None:
    """Register a schema under its own identity (idempotent re-register)."""
    key = (schema.schema_id, schema.version)
    existing = _REGISTRY.get(key)
    if existing is not None and existing.identity != schema.identity:
        raise ExtractionSchemaError(
            f"schema {key[0]}@{key[1]} is already registered with a "
            "different definition; a schema identity never changes meaning"
        )
    _REGISTRY[key] = schema


def resolve_schema(schema_id: str, schema_version: str) -> ExtractionSchema:
    """Resolve a registered schema or fail closed."""
    schema = _REGISTRY.get((schema_id, schema_version))
    if schema is None:
        raise ExtractionRequestError(
            f"unknown schema {schema_id!r}@{schema_version!r}; registered: "
            f"{sorted(f'{k[0]}@{k[1]}' for k in _REGISTRY)}"
        )
    return schema
