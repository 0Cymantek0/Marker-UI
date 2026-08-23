"""EvidencePacket budgets and deterministic identity (PR77).

Plan matrix V6/V7/V12-V18: structural output budgets keep whole units,
packet identity is stable for identical semantic inputs and changes
with every relevant invalidation dimension (publication set, content
revision, security/verifier/redaction/serialization context), and a
restart over the same committed published state reproduces the same
packet.
"""

from __future__ import annotations

import pytest

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
    to_json,
)
from tests.test_kernel_publication import _commit_view, _view
from app.kernel.generations import GenerationService
from app.kernel.publications import PublicationService
from app.kernel.snapshots import resolve_snapshot

pytestmark = pytest.mark.asyncio


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-a",
        "operations": operations,
    }
    base.update(overrides)
    return base


async def _publish(factory, service, workspace: str, texts: dict[str, str]):
    pubs = PublicationService(factory)
    await _commit_view(service, workspace, _view("view-1", texts, "rev-s1"))
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


async def _query(factory, operations, **overrides):
    return await execute_query(
        factory, parse_query_request(_request(operations, **overrides))
    )


# ---------------------------------------------------------------------------
# V7: output budget pressure keeps whole structural units
# ---------------------------------------------------------------------------


async def test_output_char_budget_keeps_whole_units(payload_env: tuple) -> None:
    factory, store, service = payload_env
    long_text = "needle " + "filler " * 400  # ~2.8k chars per unit
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": long_text, "n2": long_text + " two", "n3": long_text + " three"},
    )
    packet = await _query(
        factory,
        [{"op": "lexical_search", "text": "needle", "limit": 3}],
        budget={"max_output_chars": 3_200},
    )
    assert packet.status == "partial"
    assert len(packet.evidence) >= 1
    # Every included unit is whole: the full text survived uncut.
    for unit in packet.evidence:
        assert unit.text is not None and unit.text.startswith("needle ")
        assert "filler" in unit.text
    budget_omissions = [o for o in packet.omitted if o.reason == "output_budget"]
    assert budget_omissions
    assert packet.budget.output_chars <= 3_200
    assert packet.budget.truncated is True


async def test_single_unit_larger_than_whole_budget_is_refused_whole(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    huge = "needle " + "x" * 5_000
    await _publish(factory, service, "ws-a", {"n1": huge})
    packet = await _query(
        factory,
        [{"op": "lexical_search", "text": "needle"}],
        budget={"max_output_chars": 1_000},
    )
    assert packet.evidence == ()
    omission = packet.omitted[0]
    assert omission.reason == "unit_too_large"
    assert "refusing to cut" in omission.detail
    assert packet.status == "partial"


async def test_evidence_unit_count_budget(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "needle one", "n2": "needle two", "n3": "needle three"},
    )
    packet = await _query(
        factory,
        [{"op": "lexical_search", "text": "needle", "limit": 3}],
        budget={"max_evidence_units": 2},
    )
    assert len(packet.evidence) == 2
    unit_omissions = [o for o in packet.omitted if o.reason == "unit_budget"]
    assert len(unit_omissions) == 1
    assert packet.status == "partial"


async def test_duplicate_evidence_across_operations_deduplicated_explicitly(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha needle", "n2": "beta"})
    packet = await _query(
        factory,
        [
            {"op": "lexical_search", "text": "needle"},
            {"op": "lexical_search", "text": "alpha needle", "mode": "phrase"},
            {"op": "record_get", "record_id": "view-1", "node_id": "n1"},
        ],
    )
    # Same locator+hash selected three times: one unit, two duplicates.
    assert len(packet.evidence) == 1
    duplicates = [o for o in packet.omitted if o.reason == "duplicate"]
    assert len(duplicates) == 2
    assert packet.status == "complete"


async def test_repeated_same_operator_cannot_reset_budgets(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "needle one", "n2": "needle two"},
    )
    ops = [{"op": "lexical_search", "text": "needle", "limit": 1} for _ in range(3)]
    packet = await _query(factory, ops, budget={"max_candidates": 2})
    assert packet.budget.candidates_considered <= 2
    assert len(packet.evidence) <= 2


# ---------------------------------------------------------------------------
# V12: stable identity for identical inputs and state
# ---------------------------------------------------------------------------


async def test_identity_stable_for_identical_request_and_state(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    ops = [{"op": "lexical_search", "text": "needle"}]
    first = await _query(factory, ops)
    second = await _query(factory, ops)
    assert first.identity_id == second.identity_id
    assert first == second  # fully deterministic packet, no runtime noise


async def test_identity_ignores_irrelevant_request_spelling(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    plain = await _query(factory, [{"op": "lexical_search", "text": "needle"}])
    extra_ws = await _query(
        factory, [{"op": "lexical_search", "text": "  needle  "}]
    )
    assert plain.identity_id == extra_ws.identity_id


# ---------------------------------------------------------------------------
# V13: publication set / generation change invalidates identity
# ---------------------------------------------------------------------------


async def test_identity_changes_when_publication_set_changes(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", {"n1": "alpha needle"})

    before = await _query(factory, [{"op": "lexical_search", "text": "needle"}])

    await _commit_view(
        service,
        "ws-a",
        _view("view-2", {"n1": "alpha needle revised"}, "rev-s2"),
        advance=False,
    )
    gen2 = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    await pubs.publish(materialized_generation_id=gen2.generation_id)

    after = await _query(factory, [{"op": "lexical_search", "text": "needle"}])
    assert after.publication["publication_set_id"] != p1.publication_set_id
    assert before.identity_id != after.identity_id


# ---------------------------------------------------------------------------
# V14: content revision change invalidates identity
# ---------------------------------------------------------------------------


async def test_identity_changes_when_selected_revision_changes(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    first = await _query(
        factory, [{"op": "record_get", "record_id": "view-1", "node_id": "n1"}]
    )
    first_lexical = await _query(
        factory, [{"op": "lexical_search", "text": "needle"}]
    )

    # A revision change arrives as a new immutable set (sets never
    # mutate); the same logical query must change identity because the
    # selected revision_ref and content hash moved.
    await _commit_view(
        service,
        "ws-a",
        _view("view-2", {"n1": "alpha needle v2"}, "rev-s2"),
        advance=False,
    )
    gen2 = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    await pubs.publish(materialized_generation_id=gen2.generation_id)

    second = await _query(
        factory, [{"op": "record_get", "record_id": "view-2", "node_id": "n1"}]
    )
    second_lexical = await _query(
        factory, [{"op": "lexical_search", "text": "needle"}]
    )
    assert first.identity_id != second.identity_id
    assert first_lexical.identity_id != second_lexical.identity_id
    assert (
        first_lexical.evidence[0].locator.revision_ref
        != second_lexical.evidence[0].locator.revision_ref
    )


# ---------------------------------------------------------------------------
# V15/V16/V17: context identity dimensions change packet identity
# ---------------------------------------------------------------------------


async def test_identity_changes_with_every_context_dimension(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    # PR89: redaction profile names are server-resolved; the named
    # profile must be committed before it can shape a serving context.
    from app.services.redaction_policy import RedactionPolicyService

    await RedactionPolicyService(factory, service, workspace_id="ws-a").define_profile(
        "strict", [{"kind": "literal", "value": "not-present-in-corpus"}]
    )
    ops = [{"op": "lexical_search", "text": "needle"}]
    base = await _query(factory, ops)

    changed_security = await _query(
        factory, ops, context={"security_context_id": "tenant-b"}
    )
    changed_verifier = await _query(
        factory, ops, context={"verifier_policy_id": "financial_v3"}
    )
    changed_redaction = await _query(
        factory, ops, context={"redaction_profile_id": "strict"}
    )
    changed_serialization = await _query(
        factory, ops, context={"serialization_profile": "cl100k"}
    )
    for changed in (
        changed_security,
        changed_verifier,
        changed_redaction,
        changed_serialization,
    ):
        assert changed.identity_id != base.identity_id
        # Same evidence selection, different reuse identity: the context
        # seam participates in identity exactly as supplied.
        assert [u.locator.node_id for u in changed.evidence] == [
            u.locator.node_id for u in base.evidence
        ]


async def test_identity_changes_with_budget_profile_and_output_directive(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _publish(
        factory,
        service,
        "ws-a",
        {"n1": "needle one", "n2": "needle two", "n3": "needle three"},
    )
    ops = [{"op": "lexical_search", "text": "needle", "limit": 2}]
    base = await _query(factory, ops)
    tighter = await _query(factory, ops, budget={"max_evidence_units": 1})
    no_text = await _query(factory, ops, output={"include_text": False})
    assert base.identity_id != tighter.identity_id
    assert base.identity_id != no_text.identity_id
    assert no_text.evidence[0].text is None


# ---------------------------------------------------------------------------
# V18: restart over identical committed state reproduces the packet
# ---------------------------------------------------------------------------


async def test_identity_reproducible_after_restart(payload_env: tuple) -> None:
    from tests.test_kernel_publication import _fresh_factory, _db_path

    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    before = await _query(factory, [{"op": "lexical_search", "text": "needle"}])

    fresh = _fresh_factory(_db_path(factory))
    after = await _query(fresh, [{"op": "lexical_search", "text": "needle"}])
    assert after.identity_id == before.identity_id
    assert to_json(after) == to_json(before)


# ---------------------------------------------------------------------------
# serialization sanity
# ---------------------------------------------------------------------------


async def test_to_json_is_structurally_complete(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", {"n1": "alpha needle"})
    packet = await _query(factory, [{"op": "lexical_search", "text": "needle"}])
    view = to_json(packet)
    assert view["identity_id"] == packet.identity_id
    assert view["publication"]["publication_set_id"]
    assert view["evidence"][0]["text"] == "alpha needle"
    assert view["budget"]["units_included"] == 1
    assert view["context"]["serialization_profile"] == "default"
