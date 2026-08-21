"""Typed query contract tests (PR77, plan V1 contract level + V3/V4/V5).

The contract is finite and fail-closed: unknown fields, unknown
operators, unimplemented operators, malformed values, and over-budget
operation counts are all rejected before any execution, and lexical
text can never carry FTS5 grammar.
"""

from __future__ import annotations

import pytest

from app.context_runtime.contract import (
    MAX_LEXICAL_TEXT_CHARS,
    QUERY_SCHEMA_VERSION,
    SUPPORTED_OPERATIONS,
    compile_lexical_match,
    normalized_query,
    parse_query_request,
)
from app.context_runtime.errors import (
    QueryBudgetError,
    QueryContractError,
    UnsupportedOperatorError,
)
from app.kernel.errors import LexicalQueryError


def _request(**overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-a",
        "operations": [
            {"op": "lexical_search", "text": "alpha beta"},
            {"op": "record_get", "record_id": "view-1"},
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# parsing: supported, malformed, unknown, unsupported
# ---------------------------------------------------------------------------


def test_parse_valid_request_with_defaults() -> None:
    request = parse_query_request(_request())
    assert request.workspace_id == "ws-a"
    assert len(request.operations) == 2
    lexical = request.operations[0]
    assert lexical.op == "lexical_search"
    assert lexical.mode == "all_terms"
    assert lexical.limit == 25
    record = request.operations[1]
    assert record.op == "record_get"
    assert record.node_id is None
    assert request.output.include_text is True
    assert request.context.serialization_profile == "default"
    assert request.budget.max_operations == 8


def test_parse_rejects_non_mapping() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(["not", "a", "mapping"])


def test_parse_rejects_wrong_schema_version() -> None:
    with pytest.raises(QueryContractError, match="schema version"):
        parse_query_request(_request(schema_version="marker.query.v0"))


def test_parse_rejects_unknown_operator() -> None:
    with pytest.raises(QueryContractError, match="unknown operator"):
        parse_query_request(
            _request(operations=[{"op": "sql_injection", "sql": "1=1"}])
        )


def test_parse_rejects_unknown_fields() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(_request(unexpected_field="x"))


def test_parse_rejects_unknown_operation_fields() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(
            _request(
                operations=[{"op": "record_get", "record_id": "r", "extra": 1}]
            )
        )


@pytest.mark.parametrize(
    "op", sorted({"vector_search", "structural_traverse", "aggregate", "rerank"})
)
def test_parse_rejects_unimplemented_operators_without_fallback(op: str) -> None:
    with pytest.raises(UnsupportedOperatorError, match="not implemented") as exc_info:
        parse_query_request(_request(operations=[{"op": op}]))
    assert "lexical_search" in str(exc_info.value)


def test_parse_rejects_empty_or_malformed_operations() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations=[]))
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations="lexical_search"))
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations=[{"text": "no op key"}]))
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations=[{"op": 42}]))


def test_parse_rejects_operation_without_required_fields() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations=[{"op": "lexical_search"}]))
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations=[{"op": "record_get"}]))


def test_parse_rejects_invalid_workspace_and_profile() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(_request(workspace_id=""))
    with pytest.raises(QueryContractError, match="profile"):
        parse_query_request(_request(profile="Invalid Profile"))


# ---------------------------------------------------------------------------
# budgets at the contract (V5: deterministic pre-execution rejection)
# ---------------------------------------------------------------------------


def test_parse_rejects_operation_count_over_budget() -> None:
    ops = [{"op": "record_get", "record_id": f"r{i}"} for i in range(5)]
    budget = {"max_operations": 4}
    with pytest.raises(QueryBudgetError, match="operations"):
        parse_query_request(_request(operations=ops, budget=budget))


def test_parse_rejects_hard_capped_operation_lists() -> None:
    ops = [{"op": "record_get", "record_id": f"r{i}"} for i in range(33)]
    with pytest.raises(QueryContractError):
        parse_query_request(_request(operations=ops))


@pytest.mark.parametrize(
    "budget",
    [
        {"max_operations": 0},
        {"max_candidates": -1},
        {"max_evidence_units": 0},
        {"max_output_chars": 0},
        {"max_operations": 10_000},
    ],
)
def test_parse_rejects_malformed_budget_values(budget: dict) -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(_request(budget=budget))


# ---------------------------------------------------------------------------
# lexical text validation (V3: typed text, never raw FTS grammar)
# ---------------------------------------------------------------------------


def test_parse_rejects_empty_and_whitespace_text() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(
            _request(operations=[{"op": "lexical_search", "text": ""}])
        )
    with pytest.raises(QueryContractError):
        parse_query_request(
            _request(operations=[{"op": "lexical_search", "text": "   "}])
        )


def test_parse_rejects_overlong_literal() -> None:
    with pytest.raises(QueryContractError, match="longer than"):
        parse_query_request(
            _request(
                operations=[
                    {"op": "lexical_search", "text": "a" * (MAX_LEXICAL_TEXT_CHARS + 1)}
                ]
            )
        )


def test_parse_rejects_tokens_without_searchable_characters() -> None:
    with pytest.raises(QueryContractError, match="no searchable characters"):
        parse_query_request(
            _request(operations=[{"op": "lexical_search", "text": 'hello "!!!'}])
        )


def test_parse_normalizes_text_to_nfc() -> None:
    decomposed = "cafe\u0301 radial"  # e + combining acute
    request = parse_query_request(
        _request(operations=[{"op": "lexical_search", "text": decomposed}])
    )
    assert request.operations[0].text == "café radial"


def test_parse_rejects_out_of_range_limit() -> None:
    with pytest.raises(QueryContractError):
        parse_query_request(
            _request(
                operations=[
                    {"op": "lexical_search", "text": "x", "limit": 0}
                ]
            )
        )
    with pytest.raises(QueryContractError):
        parse_query_request(
            _request(
                operations=[
                    {"op": "lexical_search", "text": "x", "limit": 100_000}
                ]
            )
        )


# ---------------------------------------------------------------------------
# FTS5 compilation: user bytes can never become MATCH grammar
# ---------------------------------------------------------------------------


def test_compile_all_terms_quotes_every_token() -> None:
    assert compile_lexical_match("alpha beta", "all_terms") == '"alpha" AND "beta"'


def test_compile_any_terms_uses_or() -> None:
    assert compile_lexical_match("alpha beta", "any_term") == '"alpha" OR "beta"'


def test_compile_phrase_is_one_quoted_phrase() -> None:
    assert compile_lexical_match("alpha beta", "phrase") == '"alpha beta"'


def test_compile_neutralizes_fts_operators_and_quotes() -> None:
    adversarial = 'NEAR(a b) AND "x" OR column:filter NOT *'
    compiled = compile_lexical_match(adversarial, "all_terms")
    # Every dangerous byte lives inside a doubled-quote phrase; the
    # expression contains no unquoted bareword operators.
    for token in adversarial.split():
        assert f'"{token.replace(chr(34), chr(34) * 2)}"' in compiled
    assert compiled.count('"') % 2 == 0


def test_compile_rejects_empty_text() -> None:
    # Compilation authority moved to the kernel lexical layer (PR83B2);
    # empty input fails closed with the kernel's typed query error.
    with pytest.raises(LexicalQueryError):
        compile_lexical_match("   ", "all_terms")


# ---------------------------------------------------------------------------
# normalized query: deterministic canonical form (packet identity input)
# ---------------------------------------------------------------------------


def test_normalized_query_is_deterministic_and_semantic() -> None:
    first = parse_query_request(_request())
    second = parse_query_request(_request())
    assert normalized_query(first) == normalized_query(second)

    changed = parse_query_request(
        _request(
            context={
                "security_context_id": "tenant-a",
                "serialization_profile": "cl100k",
            }
        )
    )
    normalized = normalized_query(changed)
    assert normalized["context"]["security_context_id"] == "tenant-a"
    assert normalized["context"]["serialization_profile"] == "cl100k"


def test_normalized_query_changes_with_every_semantic_dimension() -> None:
    base = normalized_query(parse_query_request(_request()))
    assert (
        normalized_query(
            parse_query_request(
                _request(operations=[{"op": "lexical_search", "text": "alpha"}])
            )
        )
        != base
    )
    assert (
        normalized_query(
            parse_query_request(
                _request(
                    operations=[
                        {"op": "lexical_search", "text": "alpha", "mode": "phrase"}
                    ]
                )
            )
        )
        != base
    )
    assert normalized_query(parse_query_request(_request(profile="other"))) != base
    assert (
        normalized_query(parse_query_request(_request(output={"include_text": False})))
        != base
    )


def test_supported_operations_are_exactly_the_implemented_set() -> None:
    assert SUPPORTED_OPERATIONS == frozenset({"lexical_search", "record_get"})
