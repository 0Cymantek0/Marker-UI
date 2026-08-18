"""Canonical, validated state helpers for PR79A continuation rows."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from app.context_runtime.contract import LexicalSearchOp, QueryRequest, parse_query_request
from app.context_runtime.errors import QueryContractError
from app.context_runtime.packets import CandidateUnit
from app.kernel.publications import LexicalSearchAfter
from app.utils.canonical import canonical_json_str, payload_byte_hash, to_json_ready

KEYSET_SCHEMA_VERSION = "marker.continuation.keyset.v1"
BUDGET_SCHEMA_VERSION = "marker.continuation.budget.v1"

PUBLICATION_BINDING_KEYS = (
    "publication_set_id",
    "workspace_id",
    "profile",
    "kernel_commit_id",
    "snapshot_id",
    "materialized_generation_id",
    "lexical_generation_id",
    "tokenizer",
    "vector_generation_id",
    "lexical_row_count",
)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return canonical_json_str(to_json_ready(value))


def coerce_request(value: QueryRequest | Mapping[str, Any]) -> QueryRequest:
    if isinstance(value, QueryRequest):
        return value
    if not isinstance(value, Mapping):
        raise QueryContractError("query request must be a mapping")
    return parse_query_request(value)


def rank_text(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("lexical rank must be finite")
    return format(value, ".17g")


def after_storage(after: LexicalSearchAfter) -> dict[str, Any]:
    value = after.as_dict()
    value["rank"] = rank_text(after.rank)
    return value


def after_from_storage(value: Mapping[str, Any]) -> LexicalSearchAfter:
    expected = {
        "publication_set_id",
        "lexical_generation_id",
        "rank",
        "row_index",
        "query_hash",
    }
    if set(value) != expected:
        raise ValueError("lexical keyset has unexpected fields")
    rank_value = value["rank"]
    if isinstance(rank_value, bool):
        raise ValueError("lexical keyset rank is malformed")
    if isinstance(rank_value, str):
        try:
            rank = float(rank_value)
        except ValueError as exc:
            raise ValueError("lexical keyset rank is malformed") from exc
    elif isinstance(rank_value, (int, float)):
        rank = float(rank_value)
    else:
        raise ValueError("lexical keyset rank is malformed")
    row_index = value["row_index"]
    if isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0:
        raise ValueError("lexical keyset row index is malformed")
    if not math.isfinite(rank):
        raise ValueError("lexical keyset rank is malformed")
    for key in ("publication_set_id", "lexical_generation_id", "query_hash"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError("lexical keyset binding is malformed")
    return LexicalSearchAfter(
        publication_set_id=value["publication_set_id"],
        lexical_generation_id=value["lexical_generation_id"],
        rank=rank,
        row_index=row_index,
        query_hash=value["query_hash"],
    )


def initial_keyset(request: QueryRequest) -> dict[str, Any]:
    operations: dict[str, Any] = {}
    for index, operation in enumerate(request.operations):
        if isinstance(operation, LexicalSearchOp):
            operations[str(index)] = {
                "kind": "lexical_search",
                "after": None,
                "authorized_count": 0,
                "started": False,
                "exhausted": False,
            }
        else:
            operations[str(index)] = {
                "kind": "record_get",
                "position": 0,
                "started": False,
                "exhausted": False,
            }
    return {"schema_version": KEYSET_SCHEMA_VERSION, "operations": operations}


def initial_budget() -> dict[str, Any]:
    return {
        "schema_version": BUDGET_SCHEMA_VERSION,
        "candidates_considered": 0,
        "evidence_units": 0,
        "output_chars": 0,
        "operations_executed": 0,
        "pages": 0,
        "work_units": 0,
        "emitted_keys": [],
    }


def validate_budget(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "candidates_considered",
        "evidence_units",
        "output_chars",
        "operations_executed",
        "pages",
        "work_units",
        "emitted_keys",
    }
    if set(value) != expected or value.get("schema_version") != BUDGET_SCHEMA_VERSION:
        raise ValueError("cursor budget state is malformed")
    result: dict[str, Any] = {}
    for key in expected - {"schema_version", "emitted_keys"}:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("cursor budget counter is malformed")
        result[key] = item
    emitted = value["emitted_keys"]
    if not isinstance(emitted, list) or not all(
        isinstance(item, str) and item for item in emitted
    ):
        raise ValueError("cursor emitted-key state is malformed")
    result["schema_version"] = BUDGET_SCHEMA_VERSION
    result["emitted_keys"] = list(emitted)
    return result


def validate_keyset(value: Mapping[str, Any], request: QueryRequest) -> dict[str, Any]:
    if set(value) != {"schema_version", "operations"}:
        raise ValueError("cursor keyset state is malformed")
    if value.get("schema_version") != KEYSET_SCHEMA_VERSION:
        raise ValueError("unsupported cursor keyset version")
    operations = value.get("operations")
    if not isinstance(operations, Mapping) or set(operations) != {
        str(index) for index in range(len(request.operations))
    }:
        raise ValueError("cursor keyset operations are malformed")
    result: dict[str, Any] = {
        "schema_version": KEYSET_SCHEMA_VERSION,
        "operations": {},
    }
    for index, operation in enumerate(request.operations):
        state = operations[str(index)]
        if not isinstance(state, Mapping):
            raise ValueError("cursor operation keyset is malformed")
        state = dict(state)
        if isinstance(operation, LexicalSearchOp):
            if set(state) != {
                "kind",
                "after",
                "authorized_count",
                "started",
                "exhausted",
            } or state.get("kind") != "lexical_search":
                raise ValueError("lexical operation keyset is malformed")
            count = state["authorized_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("lexical authorized count is malformed")
            if count > operation.limit:
                raise ValueError("lexical authorized count exceeds operation limit")
            if not isinstance(state["started"], bool) or not isinstance(
                state["exhausted"], bool
            ):
                raise ValueError("lexical operation flags are malformed")
            after = state["after"]
            if after is not None:
                if not isinstance(after, Mapping):
                    raise ValueError("lexical continuation key is malformed")
                state["after"] = after_storage(after_from_storage(after))
            result["operations"][str(index)] = state
        else:
            if set(state) != {"kind", "position", "started", "exhausted"} or state.get(
                "kind"
            ) != "record_get":
                raise ValueError("record operation keyset is malformed")
            position = state["position"]
            if isinstance(position, bool) or not isinstance(position, int) or position not in (0, 1):
                raise ValueError("record operation position is malformed")
            if not isinstance(state["started"], bool) or not isinstance(
                state["exhausted"], bool
            ):
                raise ValueError("record operation flags are malformed")
            result["operations"][str(index)] = state
    return result


def locator_key(candidate: CandidateUnit) -> str:
    return payload_byte_hash(
        canonical(candidate.locator.identity_view()).encode("utf-8")
    )


def publication_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(expected.get(key) == actual.get(key) for key in PUBLICATION_BINDING_KEYS)
