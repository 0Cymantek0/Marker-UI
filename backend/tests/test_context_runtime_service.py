"""PR79A continuation lifecycle integration coverage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import app.context_runtime.continuation_paging as continuation_paging

from app.context_runtime import QUERY_SCHEMA_VERSION, ContinuationService, CursorCodec, CursorKeyring, parse_query_request
from app.kernel.generations import GenerationService
from app.kernel.commit import KernelCommitBatch
from app.kernel.models import KernelQueryCursor
from app.kernel.errors import RetentionContractError
from app.kernel.publications import (
    PublicationService,
    acquire_publication_pin,
    active_publication_pins,
)
from app.kernel.snapshots import resolve_snapshot
from app.services.query_policy import QueryPolicyService
from tests.test_context_runtime_authorization import _epoch
from tests.test_context_runtime_authz_retrieval import seed_domain_doc
from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio


def _codec() -> CursorCodec:
    return CursorCodec(
        CursorKeyring({"k1": b"pr79a-test-key-" + b"x" * 32}, current_key_id="k1")
    )


def _request(workspace: str = "ws-cont"):
    return parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": workspace,
            "operations": [
                {"op": "lexical_search", "text": "needle", "limit": 25}
            ],
            "budget": {
                "max_operations": 8,
                "max_candidates": 100,
                "max_evidence_units": 100,
                "max_output_chars": 100000,
            },
        }
    )


async def _publish(
    factory, service, workspace: str, suffix: str = "", *, advance: bool = True
):
    await _commit_view(
        service,
        workspace,
        _view(
            f"view{suffix or '-1'}",
            {f"n{index}": "needle" for index in range(1, 7)},
            f"rev{suffix or '-1'}",
        ),
        advance=advance,
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    return await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )


async def test_continuation_reconstructs_three_pages_and_rotates_nonce(payload_env):
    factory, _store, commit_service = payload_env
    publication = await _publish(factory, commit_service, "ws-cont")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request(), page_size=2)

    assert first.status == "partial"
    assert first.next_cursor
    locators = [
        (unit.locator.record_id, unit.locator.node_id)
        for unit in first.packet.evidence
    ]
    cursor = first.next_cursor
    while cursor:
        page = await service.continue_query(cursor, workspace_id="ws-cont", page_size=2)
        locators.extend(
            (unit.locator.record_id, unit.locator.node_id)
            for unit in (page.packet.evidence if page.packet else ())
        )
        cursor = page.next_cursor
        if page.status == "complete":
            assert page.packet is not None
            break

    assert len(locators) == 6
    assert len(set(locators)) == 6
    assert first.packet.publication["publication_set_id"] == publication.publication_set_id
    assert all(
        unit.locator.publication_set_id == publication.publication_set_id
        for unit in first.packet.evidence
    )


async def test_continuation_stays_on_old_publication_after_head_switch(payload_env):
    factory, _store, commit_service = payload_env
    first_publication = await _publish(factory, commit_service, "ws-head")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-head"), page_size=2)
    assert first.next_cursor

    second_publication = await _publish(
        factory, commit_service, "ws-head", "-new", advance=False
    )
    continued = await service.continue_query(
        first.next_cursor, workspace_id="ws-head", page_size=2
    )

    assert continued.packet is not None
    assert continued.packet.publication["publication_set_id"] == first_publication.publication_set_id
    assert continued.packet.publication["publication_set_id"] != second_publication.publication_set_id


async def test_tamper_replay_and_expiry_are_structured_and_release_pin(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-abuse")
    now = [datetime.now(timezone.utc)]
    service = ContinuationService(
        factory,
        cursor_codec=_codec(),
        ttl_seconds=5,
        pin_lease_seconds=60,
        clock=lambda: now[0],
    )
    first = await service.fresh_query(_request("ws-abuse"), page_size=1)
    assert first.next_cursor
    tampered = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    invalid = await service.continue_query(tampered, workspace_id="ws-abuse")
    assert invalid.status == "invalidated"

    advanced = await service.continue_query(first.next_cursor, workspace_id="ws-abuse", page_size=1)
    assert advanced.next_cursor
    replay = await service.continue_query(first.next_cursor, workspace_id="ws-abuse")
    assert replay.status == "invalidated"

    now[0] += timedelta(seconds=6)
    expired = await service.continue_query(advanced.next_cursor, workspace_id="ws-abuse")
    assert expired.status == "stale"
    async with factory() as session:
        rows = (await session.execute(select(KernelQueryCursor))).scalars().all()
    assert all(row.pin_id is None for row in rows if row.status != "active")
    assert not await active_publication_pins(
        factory, publication_set_id=first.packet.publication["publication_set_id"]
    )


async def test_live_deny_epoch_and_allow_do_not_resurrect_cursor(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-policy")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-policy"), page_size=1)
    assert first.next_cursor

    policy = QueryPolicyService(factory, commit_service, workspace_id="ws-policy")
    await policy.deny_record("view-1")
    denied = await service.continue_query(first.next_cursor, workspace_id="ws-policy")
    assert denied.status == "invalidated"
    assert denied.packet is None
    assert "view-1" not in denied.model_dump_json()

    await policy.allow_record("view-1")
    await commit_service.commit(
        KernelCommitBatch(workspace_id="ws-policy", records=(_epoch(1, 7),))
    )
    after_allow = await service.continue_query(first.next_cursor, workspace_id="ws-policy")
    assert after_allow.status == "invalidated"


async def test_wrong_workspace_and_query_binding_do_not_consume_valid_cursor(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-binding")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-binding"), page_size=1)
    assert first.next_cursor

    wrong_workspace = await service.continue_query(
        first.next_cursor, workspace_id="ws-other"
    )
    assert wrong_workspace.status == "invalidated"
    assert "ws-binding" not in wrong_workspace.model_dump_json()
    malformed = await service.continue_query("garbage", workspace_id="ws-other")
    assert (wrong_workspace.reason, wrong_workspace.error_code) == (
        malformed.reason,
        malformed.error_code,
    )

    # Rebuild typed request because Pydantic models are frozen.
    altered_request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-binding",
            "operations": [{"op": "lexical_search", "text": "different"}],
        }
    )
    mismatch = await service.continue_query(
        first.next_cursor, workspace_id="ws-binding", request=altered_request
    )
    assert mismatch.status == "invalidated"
    valid = await service.continue_query(first.next_cursor, workspace_id="ws-binding")
    assert valid.status in {"partial", "complete"}


async def test_high_assurance_missing_partition_fails_closed_without_fallback(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-high")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-high",
            "assurance": "high",
            "operations": [{"op": "lexical_search", "text": "needle"}],
        }
    )
    outcome = await service.fresh_query(request, page_size=1)
    assert outcome.status == "policy_fail_closed"
    assert outcome.packet is None


async def test_malformed_cursor_and_output_budget_fail_closed_without_overflow(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-malformed")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    for token in ("", "garbage", "A" * 4096):
        outcome = await service.continue_query(token, workspace_id="ws-malformed")
        assert outcome.status == "invalidated"
        assert outcome.packet is None

    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-malformed",
            "operations": [{"op": "lexical_search", "text": "needle"}],
            "budget": {
                "max_operations": 8,
                "max_candidates": 100,
                "max_evidence_units": 100,
                "max_output_chars": 1,
            },
        }
    )
    output_limited = await service.fresh_query(request, page_size=1)
    assert output_limited.status == "complete"
    assert output_limited.packet is not None
    assert output_limited.packet.budget.output_chars <= 1
    assert output_limited.next_cursor is None


async def test_cumulative_budgets_and_hard_chain_limit_stop_pagination(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-budget")
    service = ContinuationService(
        factory, cursor_codec=_codec(), pin_lease_seconds=60, max_chain_pages=2
    )
    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-budget",
            "operations": [{"op": "lexical_search", "text": "needle", "limit": 25}],
            "budget": {
                "max_operations": 8,
                "max_candidates": 100,
                "max_evidence_units": 3,
                "max_output_chars": 100000,
            },
        }
    )
    first = await service.fresh_query(request, page_size=1)
    assert first.status == "partial"
    assert first.next_cursor
    second = await service.continue_query(first.next_cursor, workspace_id="ws-budget", page_size=1)
    assert second.status == "loop_limit"
    assert second.next_cursor is None
    assert second.result["cumulative_budget"]["evidence_units"] <= 3

    candidate_limited = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-budget",
            "operations": [{"op": "lexical_search", "text": "needle"}],
            "budget": {
                "max_operations": 8,
                "max_candidates": 2,
                "max_evidence_units": 100,
                "max_output_chars": 100000,
            },
        }
    )
    bounded = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first_bounded = await bounded.fresh_query(candidate_limited, page_size=1)
    assert first_bounded.next_cursor
    second_bounded = await bounded.continue_query(
        first_bounded.next_cursor, workspace_id="ws-budget", page_size=1
    )
    assert second_bounded.result["cumulative_budget"]["candidates_considered"] <= 2
    assert second_bounded.next_cursor is None


async def test_record_get_position_is_durable_across_pages(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-record")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-record",
            "operations": [
                {"op": "record_get", "record_id": "view-1", "node_id": "n1"},
                {"op": "record_get", "record_id": "view-1", "node_id": "n2"},
                {"op": "record_get", "record_id": "view-1", "node_id": "n3"},
            ],
        }
    )
    outcome = await service.fresh_query(request, page_size=1)
    seen = []
    while True:
        seen.extend(unit.locator.node_id for unit in (outcome.packet.evidence if outcome.packet else ()))
        if outcome.next_cursor is None:
            break
        outcome = await service.continue_query(
            outcome.next_cursor, workspace_id="ws-record", page_size=1
        )
    assert seen == ["n1", "n2", "n3"]


async def test_concurrent_same_cursor_allows_one_nonce_consumer(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-race")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-race"), page_size=1)
    assert first.next_cursor
    results = await asyncio.gather(
        service.continue_query(first.next_cursor, workspace_id="ws-race", page_size=1),
        service.continue_query(first.next_cursor, workspace_id="ws-race", page_size=1),
    )
    assert sum(result.status == "invalidated" for result in results) == 1
    assert sum(result.status in {"partial", "complete"} for result in results) == 1


async def test_replay_does_not_revoke_an_inflight_nonce_claim(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-inflight")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-inflight"), page_size=1)
    assert first.next_cursor
    envelope = service.cursor_codec.decode(first.next_cursor)
    assert await service.store.claim(envelope.handle, envelope.nonce)

    replay = await service.continue_query(first.next_cursor, workspace_id="ws-inflight")
    assert replay.status == "invalidated"
    row = await service.store.load(envelope.handle)
    assert row is not None
    assert row.status == "active"
    assert row.replay_state == "consumed"
    assert row.pin_id is not None

    await service._finish_claimed(row, "revoked")


async def test_cursor_pins_never_outlive_cursor_expiry(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-pin-bound")
    service = ContinuationService(
        factory,
        cursor_codec=_codec(),
        ttl_seconds=30,
        pin_lease_seconds=300,
    )
    first = await service.fresh_query(_request("ws-pin-bound"), page_size=1)
    assert first.next_cursor
    envelope = service.cursor_codec.decode(first.next_cursor)
    row = await service.store.load(envelope.handle)
    assert row is not None and row.pin_id is not None
    pins = await active_publication_pins(factory)
    pin = next(pin for pin in pins if pin.pin_id == row.pin_id)
    cursor_expiry = row.expires_at
    if cursor_expiry.tzinfo is None:
        cursor_expiry = cursor_expiry.replace(tzinfo=timezone.utc)
    assert pin.expires_at <= cursor_expiry

    second = await service.continue_query(
        first.next_cursor, workspace_id="ws-pin-bound", page_size=1
    )
    assert second.next_cursor
    row = await service.store.load(envelope.handle)
    assert row is not None and row.pin_id is not None
    pins = await active_publication_pins(factory)
    pin = next(pin for pin in pins if pin.pin_id == row.pin_id)
    cursor_expiry = row.expires_at
    if cursor_expiry.tzinfo is None:
        cursor_expiry = cursor_expiry.replace(tzinfo=timezone.utc)
    assert pin.expires_at <= cursor_expiry


async def test_forbidden_crowd_does_not_spend_authorized_candidate_budget(payload_env):
    factory, _store, commit_service = payload_env
    crowd = {f"n{index}": f"needle denied {index}" for index in range(12)}
    await seed_domain_doc(
        commit_service,
        "ws-recall",
        tag="crowd",
        domain="dom-denied",
        texts=crowd,
    )
    authorized = await seed_domain_doc(
        commit_service,
        "ws-recall",
        tag="allowed",
        domain="dom-allowed",
        texts={
            "n1": "needle buried in a longer authorized document with filler words"
        },
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-recall")
    )
    await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )
    policy = QueryPolicyService(factory, commit_service, workspace_id="ws-recall")
    await policy.deny_domain("dom-denied")
    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-recall",
            "operations": [{"op": "lexical_search", "text": "needle", "limit": 5}],
            "budget": {
                "max_operations": 8,
                "max_candidates": 1,
                "max_evidence_units": 5,
                "max_output_chars": 100000,
            },
        }
    )
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    outcome = await service.fresh_query(request, page_size=1)
    assert outcome.packet is not None
    assert [unit.locator.record_id for unit in outcome.packet.evidence] == [authorized]
    assert outcome.result["cumulative_budget"]["candidates_considered"] == 1
    assert outcome.result["cumulative_budget"]["work_units"] > 1


async def test_tampered_snapshot_state_fails_stale_and_releases_pin(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-state")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-state"), page_size=1)
    assert first.next_cursor
    envelope = service.cursor_codec.decode(first.next_cursor)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(KernelQueryCursor)
                .where(KernelQueryCursor.handle == envelope.handle)
                .values(snapshot_json='{"snapshot_id":"wrong"}')
            )
    outcome = await service.continue_query(first.next_cursor, workspace_id="ws-state")
    assert outcome.status == "stale"
    assert outcome.packet is None
    row = await service.store.load(envelope.handle)
    assert row is not None and row.pin_id is None


async def test_unexpected_continuation_failure_is_not_reported_as_stale(
    payload_env,
    monkeypatch,
):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-failure")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-failure"), page_size=1)
    assert first.next_cursor

    async def fail_page(*args, **kwargs):
        raise RuntimeError("internal-only diagnostic")

    monkeypatch.setattr(service.pager, "run_async", fail_page)
    outcome = await service.continue_query(
        first.next_cursor, workspace_id="ws-failure", page_size=1
    )
    assert outcome.status == "execution_failure"
    assert "internal-only" not in outcome.model_dump_json()


async def test_storage_read_failure_maps_to_structured_execution_failure(
    payload_env,
    monkeypatch,
):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-store-failure")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    first = await service.fresh_query(_request("ws-store-failure"), page_size=1)
    assert first.next_cursor

    async def fail_load(*args, **kwargs):
        raise RuntimeError("database diagnostic")

    monkeypatch.setattr(service.store, "load", fail_load)
    outcome = await service.continue_query(
        first.next_cursor, workspace_id="ws-store-failure"
    )
    assert outcome.status == "execution_failure"
    assert "database diagnostic" not in outcome.model_dump_json()


async def test_work_budget_is_distinct_and_caller_visible(
    payload_env,
    monkeypatch,
):
    factory, _store, commit_service = payload_env
    await seed_domain_doc(
        commit_service,
        "ws-work-cap",
        tag="denied",
        domain="dom-denied",
        texts={f"n{index}": f"needle denied {index}" for index in range(6)},
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-work-cap")
    )
    publication = await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )
    policy = QueryPolicyService(factory, commit_service, workspace_id="ws-work-cap")
    await policy.deny_domain("dom-denied")
    monkeypatch.setattr(
        continuation_paging,
        "LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION",
        2,
    )

    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)
    outcome = await service.fresh_query(_request("ws-work-cap"), page_size=1)
    assert outcome.status == "complete"
    assert outcome.error_code == "work_budget"
    assert outcome.packet is not None and outcome.packet.status == "partial"
    assert any(omission.reason == "work_budget" for omission in outcome.packet.omitted)
    assert not await active_publication_pins(
        factory, publication_set_id=publication.publication_set_id
    )


async def test_sweeper_reclaims_abandoned_and_stale_claim_rows(payload_env):
    factory, _store, commit_service = payload_env
    await _publish(factory, commit_service, "ws-sweep")
    now = [datetime.now(timezone.utc)]
    service = ContinuationService(
        factory,
        cursor_codec=_codec(),
        ttl_seconds=10,
        pin_lease_seconds=60,
        claim_timeout_seconds=1,
        clock=lambda: now[0],
    )
    abandoned = await service.fresh_query(_request("ws-sweep"), page_size=1)
    claimed = await service.fresh_query(_request("ws-sweep"), page_size=1)
    assert abandoned.next_cursor and claimed.next_cursor
    abandoned_envelope = service.cursor_codec.decode(abandoned.next_cursor)
    claimed_envelope = service.cursor_codec.decode(claimed.next_cursor)
    assert await service.store.claim(claimed_envelope.handle, claimed_envelope.nonce)

    now[0] += timedelta(seconds=2)
    assert await service.reclaim_expired_cursors() == 1
    assert await service.store.load(claimed_envelope.handle) is None
    assert await service.store.load(abandoned_envelope.handle) is not None

    now[0] += timedelta(seconds=9)
    assert await service.reclaim_expired_cursors() == 1
    assert await service.store.load(abandoned_envelope.handle) is None


async def test_publication_pin_absolute_expiry_cannot_exceed_declared_lease(
    payload_env,
):
    factory, _store, commit_service = payload_env
    publication = await _publish(factory, commit_service, "ws-pin-api")
    with pytest.raises(RetentionContractError, match="declared lease"):
        await acquire_publication_pin(
            factory,
            publication.publication_set_id,
            lease_seconds=1,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
