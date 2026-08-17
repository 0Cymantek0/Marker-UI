"""Bounded query execution over one pinned PublicationSet (PR77).

Plan matrix V1/V2/V3/V8/V9/V10 plus candidate-budget honesty: exact
and lexical selection through the typed request, adversarial FTS-like
text treated as literals, corrupt index failing closed, and a
publication-head switch mid-execution leaving the in-flight packet
attributable to exactly the original pinned set.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
)
from app.context_runtime.contract import compile_lexical_match
from app.kernel.errors import PublicationIntegrityError
from app.kernel.generations import GenerationService
from app.kernel.publications import PublicationService, fts_table_name
from app.kernel.snapshots import resolve_snapshot
from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio


def _db_path(factory: async_sessionmaker):
    from pathlib import Path

    return Path(factory.kw["bind"].url.database)


async def _publish(factory, service, workspace: str, texts: dict[str, str]):
    pubs = PublicationService(factory)
    await _commit_view(service, workspace, _view("view-1", texts, "rev-s1"))
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


def _lexical(text: str, **overrides) -> dict:
    op = {"op": "lexical_search", "text": text}
    op.update(overrides)
    return _request([op])


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-a",
        "operations": operations,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# V1: exact selector
# ---------------------------------------------------------------------------


async def test_record_get_resolves_to_pinned_record_and_revision(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(
        factory, service, "ws-a", {"n1": "alpha one", "n2": "alpha two"}
    )
    request = parse_query_request(
        _request([{"op": "record_get", "record_id": "view-1", "node_id": "n1"}])
    )
    packet = await execute_query(factory, request)

    assert packet.status == "complete"
    assert packet.publication_status == "published"
    assert packet.publication["publication_set_id"] == ref.publication_set_id
    assert packet.publication["materialized_generation_id"] == gen.generation_id
    assert len(packet.evidence) == 1
    unit = packet.evidence[0]
    assert unit.locator.record_id == "view-1"
    assert unit.locator.node_id == "n1"
    assert unit.locator.revision_ref.startswith("sha256:")
    assert unit.text == "alpha one"
    assert unit.rank is None
    assert packet.budget.operations_executed == 1
    assert packet.budget.units_included == 1


async def test_record_get_whole_record_unit(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(
        factory, service, "ws-a", {"n1": "alpha one"}
    )
    request = parse_query_request(
        _request([{"op": "record_get", "record_id": "view-1"}])
    )
    packet = await execute_query(factory, request)
    unit = packet.evidence[0]
    assert unit.locator.node_id is None
    assert unit.text is None
    assert unit.locator.text_hash.startswith("sha256:")


async def test_record_get_missing_record_and_missing_node_are_explicit(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha one"})
    request = parse_query_request(
        _request(
            [
                {"op": "record_get", "record_id": "absent"},
                {"op": "record_get", "record_id": "view-1", "node_id": "zz"},
            ]
        )
    )
    packet = await execute_query(factory, request)
    assert packet.evidence == ()
    reasons = [o.reason for o in packet.omitted]
    assert reasons == ["not_found", "node_not_found"]
    assert packet.status == "complete"


# ---------------------------------------------------------------------------
# V2: plain lexical query through the typed request
# ---------------------------------------------------------------------------


async def test_lexical_query_returns_source_resolvable_evidence(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(
        factory, service, "ws-a", {"n1": "alpha needle", "n2": "beta other"}
    )
    request = parse_query_request(_lexical("needle"))
    packet = await execute_query(factory, request)

    assert packet.status == "complete"
    assert len(packet.evidence) == 1
    unit = packet.evidence[0]
    assert unit.locator.node_id == "n1"
    assert unit.locator.record_id == "view-1"
    assert unit.locator.row_index is not None
    assert unit.text == "alpha needle"
    assert unit.locator.publication_set_id == ref.publication_set_id
    assert unit.locator.lexical_generation_id == ref.lexical_generation_id
    assert packet.budget.candidates_considered == 1


async def test_lexical_modes_all_any_phrase(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "alpha needle", "n2": "beta needle", "n3": "alpha beta"},
    )
    all_packet = await execute_query(
        factory, parse_query_request(_lexical("alpha needle"))
    )
    assert {u.locator.node_id for u in all_packet.evidence} == {"n1"}

    any_packet = await execute_query(
        factory, parse_query_request(_lexical("alpha needle", mode="any_term"))
    )
    # OR semantics: n1 has both terms, n2 has "needle", n3 has "alpha".
    assert {u.locator.node_id for u in any_packet.evidence} == {"n1", "n2", "n3"}

    phrase_packet = await execute_query(
        factory, parse_query_request(_lexical("alpha needle", mode="phrase"))
    )
    assert {u.locator.node_id for u in phrase_packet.evidence} == {"n1"}


async def test_lexical_limit_reports_withheld_matches(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "needle one", "n2": "needle two", "n3": "needle three"},
    )
    request = parse_query_request(_lexical("needle", limit=2))
    packet = await execute_query(factory, request)
    assert len(packet.evidence) == 2
    assert packet.status == "partial"
    omission = packet.omitted[0]
    assert omission.reason == "candidate_budget"
    assert "beyond the requested limit" in omission.detail


# ---------------------------------------------------------------------------
# V3: adversarial FTS-like text is literal content, never grammar
# ---------------------------------------------------------------------------


async def test_fts_operators_in_text_are_treated_as_literals(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {
            "n1": "the NEAR(alpha beta) construct",
            "n2": "plain alpha content",
            "n3": "unrelated gamma words",
        },
    )
    # Phrase mode over operator-shaped text: the quoted phrase means
    # adjacency of the *literal* tokens NEAR alpha beta — never an
    # FTS5 NEAR() proximity operator.
    request = parse_query_request(_lexical("NEAR(alpha beta)", mode="phrase"))
    packet = await execute_query(factory, request)
    assert {u.locator.node_id for u in packet.evidence} == {"n1"}

    # all_terms mode: operator-shaped tokens are plain required terms.
    boolean_packet = await execute_query(
        factory, parse_query_request(_lexical("NEAR(alpha beta)", mode="all_terms"))
    )
    assert {u.locator.node_id for u in boolean_packet.evidence} == {"n1"}


async def test_column_filter_and_boolean_words_are_literals(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "column:filter AND NOT * raw syntax", "n2": "other text"},
    )
    request = parse_query_request(_lexical("column:filter AND", mode="phrase"))
    packet = await execute_query(factory, request)
    assert {u.locator.node_id for u in packet.evidence} == {"n1"}


async def test_compiled_expression_matches_reader_language() -> None:
    """The compiled form is exactly what the PR76 reader accepts — no
    second grammar between the contract and the engine."""
    compiled = compile_lexical_match('NEAR(a b) OR "c"', "all_terms")
    assert compiled == '"NEAR(a" AND "b)" AND "OR" AND """c"""'


# ---------------------------------------------------------------------------
# V8: empty / no-hit outcomes stay honest
# ---------------------------------------------------------------------------


async def test_no_hit_query_yields_valid_empty_packet(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(factory, service, "ws-a", {"n1": "alpha"})
    request = parse_query_request(_lexical("zzz-no-such-term"))
    packet = await execute_query(factory, request)
    assert packet.status == "complete"
    assert packet.evidence == ()
    assert packet.omitted[0].reason == "no_hit"
    assert packet.publication["publication_set_id"] == ref.publication_set_id
    assert packet.identity_id.startswith("sha256:")


async def test_unpublished_workspace_is_explicit_not_an_error(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    request = parse_query_request(_lexical("anything"))
    packet = await execute_query(factory, request)
    assert packet.publication_status == "unpublished"
    assert packet.publication is None
    assert packet.evidence == ()
    assert all(o.reason == "unpublished" for o in packet.omitted)
    assert packet.identity_id.startswith("sha256:")


# ---------------------------------------------------------------------------
# V9: corrupt lexical state fails closed through the query layer
# ---------------------------------------------------------------------------


async def test_tampered_fts_text_fails_closed_without_fallback(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    table = fts_table_name(ref.lexical_generation_id)
    with sqlite3.connect(_db_path(factory)) as conn:
        conn.execute(f'UPDATE "{table}" SET text = \'tampered bytes\' WHERE rowid = 0')
        conn.commit()

    request = parse_query_request(_lexical("tampered"))
    with pytest.raises(PublicationIntegrityError, match="tampered"):
        await execute_query(factory, request)


# ---------------------------------------------------------------------------
# V10: publication head moves mid-query — no mixed generations
# ---------------------------------------------------------------------------


async def test_head_switch_mid_execution_keeps_single_set_attribution(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(
        factory, service, "ws-a", {"n1": "needle alpha"}
    )

    # Prepare a second, unpublished cut that will become current while
    # the query is executing.
    await _commit_view(
        service, "ws-a", _view("view-2", {"n1": "needle beta"}, "rev-s2"),
        advance=False,
    )
    gen2 = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )

    async def switch_head_after_first_operation(index: int) -> None:
        if index == 0:
            await pubs.publish(materialized_generation_id=gen2.generation_id)

    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle"},
                {"op": "record_get", "record_id": "view-1"},
            ]
        )
    )
    packet = await execute_query(
        factory, request, _after_operation=switch_head_after_first_operation
    )

    # The in-flight packet is entirely attributable to the original set.
    assert packet.publication["publication_set_id"] == p1.publication_set_id
    assert packet.publication["materialized_generation_id"] == gen1.generation_id
    for unit in packet.evidence:
        assert unit.locator.publication_set_id == p1.publication_set_id
    assert {u.locator.record_id for u in packet.evidence} == {"view-1"}

    # A subsequent query observes the new set.
    follow_up = await execute_query(factory, parse_query_request(_lexical("needle")))
    assert follow_up.publication["publication_set_id"] != p1.publication_set_id
    assert {u.locator.record_id for u in follow_up.evidence} == {"view-2"}


# ---------------------------------------------------------------------------
# candidate budget across operations
# ---------------------------------------------------------------------------


async def test_candidate_budget_bounds_accumulation_across_operations(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "needle one", "n2": "needle two", "n3": "haystack words"},
    )
    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle", "limit": 2},
                {"op": "lexical_search", "text": "haystack"},
            ],
            budget={"max_candidates": 3},
        )
    )
    packet = await execute_query(factory, request)
    assert packet.budget.candidates_considered == 3
    assert len(packet.evidence) == 3
    assert packet.status == "partial"
    assert any(o.reason == "candidate_budget" for o in packet.omitted)


async def test_candidate_budget_exhaustion_skips_later_retrieval(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "needle one", "n2": "needle two"},
    )
    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle", "limit": 2},
                {"op": "lexical_search", "text": "needle"},
            ],
            budget={"max_candidates": 2},
        )
    )
    packet = await execute_query(factory, request)
    assert len(packet.evidence) == 2
    assert packet.budget.operations_executed == 1
    skipped = [o for o in packet.omitted if o.operation_index == 1]
    assert skipped and skipped[0].reason == "candidate_budget"
    assert packet.status == "partial"
