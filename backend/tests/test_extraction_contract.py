"""PR80A extraction schema/result contract tests (matrix A).

Pure contract level: schema parsing, versioned identity, serialization
round-trips, and the missing/null/empty/zero/invalid value distinctions
the result vocabulary must preserve.
"""

from __future__ import annotations

import copy

import pytest

from app.extraction.contract import (
    INVOICE_SCHEMA,
    ExtractionRequest,
    ExtractionRequestError,
    register_schema,
    resolve_schema,
)
from app.extraction.results import (
    RESULT_SCHEMA_VERSION,
    CandidateView,
    EvidenceCitation,
    ExtractionResult,
    FieldOutcome,
    result_from_dict,
)
from app.extraction.schema import (
    EXTRACTION_SCHEMA_VERSION,
    ExtractionSchema,
    ExtractionSchemaError,
    FieldSpec,
    LineItemSpec,
    SchemaInvariant,
)


def _minimal_schema_dict() -> dict:
    return {
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "schema_id": "test.thing",
        "version": "1.0.0",
        "fields": [
            {"name": "total", "type": "decimal", "anchor": "Total"},
            {"name": "when", "type": "date", "anchor": "Date", "required": False},
        ],
        "line_items": [],
        "invariants": [],
    }


# ---------------------------------------------------------------------------
# schema parsing and identity
# ---------------------------------------------------------------------------


def test_valid_schema_round_trips_deterministically():
    schema = ExtractionSchema.from_dict(_minimal_schema_dict())
    again = ExtractionSchema.from_dict(schema.to_dict())
    assert again.to_dict() == schema.to_dict()
    assert again.identity == schema.identity


def test_unsupported_schema_version_fails_explicitly():
    data = _minimal_schema_dict()
    data["schema_version"] = "marker.extraction.schema.v0"
    with pytest.raises(ExtractionSchemaError, match="unsupported schema_version"):
        ExtractionSchema.from_dict(data)


def test_malformed_field_definitions_fail_closed():
    data = _minimal_schema_dict()
    data["fields"][0]["type"] = "float"
    with pytest.raises(ExtractionSchemaError, match="unsupported type"):
        ExtractionSchema.from_dict(data)

    data = _minimal_schema_dict()
    data["fields"][0].pop("anchor")
    with pytest.raises(ExtractionSchemaError, match="anchor"):
        ExtractionSchema.from_dict(data)

    data = _minimal_schema_dict()
    data["fields"][0]["unexpected"] = True
    with pytest.raises(ExtractionSchemaError, match="unknown field keys"):
        ExtractionSchema.from_dict(data)


def test_enum_field_requires_declared_values():
    with pytest.raises(ExtractionSchemaError, match="enum_values"):
        FieldSpec(name="currency", type="enum", anchor="Currency")
    FieldSpec(
        name="currency", type="enum", anchor="Currency", enum_values=("USD",)
    )


def test_identity_keys_must_name_real_fields():
    with pytest.raises(ExtractionSchemaError, match="identity_keys"):
        LineItemSpec(
            name="rows",
            anchor="ROW",
            fields=(FieldSpec(name="sku", type="string", anchor="sku"),),
            identity_keys=("nope",),
        )


def test_invariant_targets_must_exist():
    with pytest.raises(ExtractionSchemaError, match="not a scalar field"):
        ExtractionSchema(
            schema_id="test.broken",
            version="1.0.0",
            fields=(FieldSpec(name="total", type="decimal", anchor="Total"),),
            line_items=(
                LineItemSpec(
                    name="rows",
                    anchor="ROW",
                    fields=(FieldSpec(name="amount", type="decimal", anchor="amount"),),
                ),
            ),
            invariants=(
                SchemaInvariant(
                    kind="sum_equality", target="ghost", items="rows", item_field="amount"
                ),
            ),
        )


def test_schema_identity_changes_when_meaning_changes():
    base = ExtractionSchema.from_dict(_minimal_schema_dict())
    renamed = ExtractionSchema.from_dict(
        {**_minimal_schema_dict(), "version": "1.0.1"}
    )
    assert base.identity != renamed.identity


def test_registry_rejects_same_id_different_definition():
    schema = ExtractionSchema.from_dict(_minimal_schema_dict())
    register_schema(schema)
    register_schema(schema)  # idempotent re-register is fine
    mutated = copy.deepcopy(_minimal_schema_dict())
    mutated["fields"][0]["anchor"] = "Grand Total"
    with pytest.raises(ExtractionSchemaError, match="never changes meaning"):
        register_schema(ExtractionSchema.from_dict(mutated))


def test_resolve_schema_fails_closed_for_unknown_identity():
    with pytest.raises(ExtractionRequestError, match="unknown schema"):
        resolve_schema("nope", "9.9.9")


# ---------------------------------------------------------------------------
# request contract
# ---------------------------------------------------------------------------


def test_request_parses_and_rejects_unknown_keys():
    request = ExtractionRequest.from_dict(
        {
            "schema_id": INVOICE_SCHEMA.schema_id,
            "schema_version": INVOICE_SCHEMA.version,
            "workspace_id": "ws",
        }
    )
    assert request.expected_publication_set_id is None
    with pytest.raises(ExtractionRequestError, match="unknown extraction request"):
        ExtractionRequest.from_dict({**request.to_dict(), "bogus": 1})
    with pytest.raises(ExtractionRequestError, match="missing"):
        ExtractionRequest.from_dict({"schema_id": "x"})


# ---------------------------------------------------------------------------
# result value semantics (missing/null/empty/zero/invalid stay distinct)
# ---------------------------------------------------------------------------


def _citation() -> EvidenceCitation:
    return EvidenceCitation(
        record_id="doc-1",
        revision_ref="rev-1",
        text_hash="sha256:0",
        node_id="n1",
        publication_set_id="pub-1",
        materialized_generation_id="gen-1",
        packet_identity_id="sha256:p",
        op="lexical_search",
    )


def _outcome(status: str, value) -> FieldOutcome:
    return FieldOutcome(
        status=status,
        value=value,
        candidates=(
            CandidateView(
                raw_text=str(value) if value is not None else "",
                value=value,
                evidence=(_citation(),),
                derivation={"route": "test"},
            ),
        ),
    )


def _result(fields: dict) -> ExtractionResult:
    from app.extraction.results import ExtractionContext

    return ExtractionResult(
        schema_id="s",
        schema_version="1.0.0",
        schema_identity="sha256:s",
        context=ExtractionContext(
            workspace_id="ws",
            publication_set_id="pub-1",
            materialized_generation_id="gen-1",
            kernel_snapshot_commit_id=3,
            packet_identity_ids=("sha256:p",),
            policy_id="marker.extraction.reconcile",
            policy_version="v1",
        ),
        run_status="partial",
        fields=fields,
        line_items={},
        invariants=(),
    )


def test_missing_zero_and_invalid_values_stay_distinguishable():
    missing = _outcome("missing", None)
    zero = _outcome("accepted", "0")
    invalid = _outcome("invalid", None)
    assert (missing.status, missing.value) == ("missing", None)
    assert (zero.status, zero.value) == ("accepted", "0")
    assert invalid.status == "invalid"
    # A zero decimal is a real value, never "missing".
    assert zero.value == "0" and zero.value is not None


def test_result_serialization_round_trips_with_candidates():
    result = _result(
        {
            "total": _outcome("accepted", "12.30"),
            "ghost": _outcome("missing", None),
        }
    )
    data = result.to_dict()
    assert data["schema_version"] == RESULT_SCHEMA_VERSION
    rebuilt = result_from_dict(data)
    assert rebuilt.to_dict() == data
    assert rebuilt.identity == result.identity


def test_result_identity_is_stable_for_identical_semantics():
    result = _result({"total": _outcome("accepted", "12.30")})
    twin = _result({"total": _outcome("accepted", "12.30")})
    assert result.identity == twin.identity


def test_unknown_result_version_fails_closed():
    with pytest.raises(ValueError, match="unsupported result schema_version"):
        result_from_dict({"schema_version": "marker.extraction.result.v0"})


def test_line_item_serialization_does_not_truncate_rows():
    from app.extraction.results import ItemOutcome

    rows = tuple(
        ItemOutcome(
            identity={"sku": f"SKU-{i}"},
            status="accepted",
            fields={"sku": _outcome("accepted", f"SKU-{i}")},
        )
        for i in range(25)
    )
    from app.extraction.results import ExtractionContext

    result = ExtractionResult(
        schema_id="s",
        schema_version="1.0.0",
        schema_identity="sha256:s",
        context=ExtractionContext(
            workspace_id="ws",
            publication_set_id="pub-1",
            materialized_generation_id="gen-1",
            kernel_snapshot_commit_id=3,
            packet_identity_ids=("sha256:p",),
            policy_id="marker.extraction.reconcile",
            policy_version="v1",
        ),
        run_status="accepted",
        fields={},
        line_items={"items": rows},
        invariants=(),
    )
    rebuilt = result_from_dict(result.to_dict())
    assert len(rebuilt.line_items["items"]) == 25
    assert rebuilt.to_dict() == result.to_dict()
