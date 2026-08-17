"""High-assurance lexical isolation (PR78 H-matrix).

A high-assurance query reads only the security-domain partition
derived from trusted authorization state — a physically separate FTS5
corpus under the reserved ``ha.`` profile. These tests prove rank
non-interference: growing a forbidden corpus with overlapping terms
must not change the authorized candidate order, the authorized score
basis, or top-K membership, while the shared-index standard mode
visibly would. A missing partition fails closed; callers cannot name
or steer partitions.
"""

from __future__ import annotations

import pytest

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
)
from app.context_runtime.errors import QueryAuthorizationError
from app.kernel.generations import GenerationService
from app.kernel.publications import HIGH_ASSURANCE_PROFILE_PREFIX, PublicationService
from app.kernel.snapshots import resolve_snapshot
from app.services.query_policy import QueryPolicyService

from tests.test_context_runtime_authz_retrieval import seed_domain_doc

pytestmark = pytest.mark.asyncio

ALPHA_TEXTS = {
    "n1": "needle alpha primary document with several common terms",
    "n2": "needle alpha secondary lighter mention",
    "n3": "alpha unrelated prose filling the authorized corpus",
}


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-ha",
        "operations": operations,
    }
    base.update(overrides)
    return base


def _lexical(text: str, **overrides) -> dict:
    op: dict = {"op": "lexical_search", "text": text}
    op.update(overrides)
    return op


async def _publish_all(factory, service, *, partition: bool, beta_denied: bool = False):
    """Publish shared + (optionally) the dom-alpha partition.

    With ``beta_denied`` the forbidden domain is also denied live, so
    the trusted partition derivation ({dom-alpha}) matches what was
    published — forbidden material is forbidden by *policy*, not by
    fixture arrangement."""
    if beta_denied:
        policy = QueryPolicyService(factory, service, workspace_id="ws-ha")
        await policy.deny_domain("dom-beta")
    pubs = PublicationService(factory)
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-ha")
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    partition_ref = None
    if partition:
        partition_ref = await pubs.publish_high_assurance(
            materialized_generation_id=gen.generation_id,
            partition_domains=frozenset({"dom-alpha"}),
        )
    return pubs, gen, ref, partition_ref


async def _seed_alpha(service) -> str:
    return await seed_domain_doc(
        service, "ws-ha", tag="alpha", domain="dom-alpha", texts=dict(ALPHA_TEXTS)
    )


async def _seed_beta(service, count: int, tag_prefix: str = "beta") -> None:
    for i in range(count):
        await seed_domain_doc(
            service,
            "ws-ha",
            tag=f"{tag_prefix}{i}",
            domain="dom-beta",
            texts={"n1": f"needle beta forbidden {i} short doc"},
        )


def _order(packet):
    return [(u.locator.record_id, u.locator.node_id) for u in packet.evidence]


def _ranks(packet):
    return [u.rank for u in packet.evidence]


async def test_forbidden_growth_cannot_change_high_assurance_rank(
    payload_env: tuple,
) -> None:
    """H1+H2: adding a large forbidden corpus with overlapping terms
    leaves the authorized partition's candidate order and score basis
    untouched, while the shared-index standard mode visibly shifts."""
    factory, store, service = payload_env
    await _seed_alpha(service)
    await _seed_beta(service, 2)
    _pubs, gen1, _shared1, part1 = await _publish_all(factory, service, partition=True, beta_denied=True)

    ha_request = parse_query_request(
        _request([_lexical("needle")], assurance="high")
    )
    std_request = parse_query_request(_request([_lexical("needle")]))

    ha_before = await execute_query(factory, ha_request)
    std_before = await execute_query(factory, std_request)
    assert ha_before.publication["publication_set_id"] == part1.publication_set_id
    assert ha_before.publication["profile"].startswith(HIGH_ASSURANCE_PROFILE_PREFIX)

    # Grow only the forbidden domain, then rebuild derived state.
    await _seed_beta(service, 24, tag_prefix="flood")
    _pubs2, _gen2, _shared2, part2 = await _publish_all(factory, service, partition=True, beta_denied=True)

    ha_after = await execute_query(factory, ha_request)
    std_after = await execute_query(factory, std_request)

    # High assurance: identical authorized competition, byte-for-byte
    # score basis, pinned to the (new) partition over the same corpus.
    assert _order(ha_after) == _order(ha_before)
    assert _ranks(ha_after) == _ranks(ha_before)
    assert {u.locator.record_id for u in ha_after.evidence} == {"view.alpha"}
    assert ha_after.publication["publication_set_id"] == part2.publication_set_id

    # Standard mode over the shared index: the forbidden flood changed
    # the score basis — the measurable reason partitions exist.
    assert _ranks(std_after) != _ranks(std_before)


async def test_forbidden_top_k_pressure_cannot_evict_allowed_hit(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed_domain_doc(
        service,
        "ws-ha",
        tag="alpha",
        domain="dom-alpha",
        texts={
            "n1": "needle buried deep in a long authorized document with "
            "much diluting filler prose around the single term"
        },
    )
    await _seed_beta(service, 20, tag_prefix="hot")
    _pubs, _gen, _shared, _part = await _publish_all(factory, service, partition=True, beta_denied=True)

    packet = await execute_query(
        factory,
        parse_query_request(_request([_lexical("needle", limit=1)], assurance="high")),
    )
    # The forbidden corpus is not in the partition at all: the single
    # authorized hit is discoverable regardless of top-K pressure.
    assert len(packet.evidence) == 1
    assert packet.evidence[0].locator.record_id == "view.alpha"
    assert packet.status == "complete"


async def test_missing_partition_fails_closed_without_fallback(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await _seed_alpha(service)
    # Only the shared profile is published — no ha. partition exists.
    _pubs, _gen, _shared, _none = await _publish_all(factory, service, partition=False)

    with pytest.raises(QueryAuthorizationError, match="not published"):
        await execute_query(
            factory,
            parse_query_request(_request([_lexical("needle")], assurance="high")),
        )


async def test_partition_routing_is_authorization_bound(payload_env: tuple) -> None:
    """H5: the caller's profile field cannot steer high assurance onto
    a shared (or someone else's) corpus — routing comes from trusted
    state only."""
    factory, store, service = payload_env
    await _seed_alpha(service)
    await _seed_beta(service, 2)
    _pubs, _gen, shared, part = await _publish_all(factory, service, partition=True, beta_denied=True)

    packet = await execute_query(
        factory,
        parse_query_request(
            _request([_lexical("needle")], assurance="high", profile="default")
        ),
    )
    assert packet.publication["publication_set_id"] == part.publication_set_id
    assert packet.publication["publication_set_id"] != shared.publication_set_id
    assert {u.locator.record_id for u in packet.evidence} == {"view.alpha"}


async def test_live_deny_applies_inside_the_partition(payload_env: tuple) -> None:
    """R1 at high assurance: revocation does not wait for a partition
    rebuild — the overlay is checked per candidate inside the pinned
    partition reader."""
    factory, store, service = payload_env
    alpha = await _seed_alpha(service)
    await _seed_beta(service, 1)
    _pubs, _gen, _shared, _part = await _publish_all(factory, service, partition=True, beta_denied=True)
    policy = QueryPolicyService(factory, service, workspace_id="ws-ha")

    request = parse_query_request(
        _request([_lexical("needle", limit=5)], assurance="high")
    )
    before = await execute_query(factory, request)
    assert {u.locator.record_id for u in before.evidence} == {"view.alpha"}
    assert len(before.evidence) == 2  # n1 and n2 both match "needle"

    await policy.deny_record(alpha, basis={"reason": "revoked"})
    after = await execute_query(factory, request)
    assert after.evidence == ()
    assert after.omitted[0].reason == "no_hit"
    assert after.identity_id != before.identity_id
    # No republish happened: same partition set, narrower live policy.
    assert after.publication["publication_set_id"] == before.publication[
        "publication_set_id"
    ]


async def test_denied_domain_moves_expected_partition_fails_closed(
    payload_env: tuple,
) -> None:
    """Denying the last visible domain changes the trusted partition
    derivation; the previously published partition is no longer the
    authorized one and high assurance refuses rather than reusing it."""
    factory, store, service = payload_env
    await _seed_alpha(service)
    _pubs, _gen, _shared, _part = await _publish_all(factory, service, partition=True, beta_denied=True)
    policy = QueryPolicyService(factory, service, workspace_id="ws-ha")
    await policy.deny_domain("dom-alpha")

    with pytest.raises(QueryAuthorizationError, match="not published"):
        await execute_query(
            factory,
            parse_query_request(_request([_lexical("needle")], assurance="high")),
        )
