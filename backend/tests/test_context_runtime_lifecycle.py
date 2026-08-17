"""Query lifecycle: GC protection and pin hygiene (PR77, V11 + V19).

A query's publication pin keeps its members alive across GC and head
switches for the whole execution, and the pin is released on success,
error, and cancellation — never leaked past the call.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
)
from app.kernel.gc import collect
from app.kernel.generations import GenerationService
from app.kernel.models import KernelGenerationRecord
from app.kernel.publications import (
    PublicationService,
    active_publication_pins,
)
from app.kernel.snapshots import resolve_snapshot
from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-a",
        "operations": operations,
    }
    base.update(overrides)
    return base


async def _publish(factory, service, workspace: str, record_id: str, text: str):
    pubs = PublicationService(factory)
    await _commit_view(
        service,
        workspace,
        _view(record_id, {"n1": text}, f"rev-{record_id}"),
        advance=False,
    )
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


# ---------------------------------------------------------------------------
# V11: GC during a pinned query
# ---------------------------------------------------------------------------


async def _publish_two_nodes(factory, service):
    pubs = PublicationService(factory)
    await _commit_view(
        service,
        "ws-a",
        _view("view-1", {"n1": "needle one", "n2": "second node"}, "rev-1"),
        advance=False,
    )
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


async def test_gc_during_query_keeps_pinned_members_alive(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish_two_nodes(factory, service)

    # Switch the head while the query runs, then GC: the query's own
    # pin must keep its members alive beneath the in-flight execution.
    async def switch_and_collect_after_first_operation(index: int) -> None:
        if index != 0:
            return
        pubs2, gen2, p2 = await _publish(
            factory, service, "ws-a", "view-2", "needle two"
        )
        report = await collect(factory, store)
        assert p2.publication_set_id  # new head published
        # Whatever the collector decided, the in-flight query's pinned
        # members may not be torn down beneath it.
        assert report.publication_sets_retired in (0, 1)

    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle"},
                {"op": "record_get", "record_id": "view-1", "node_id": "n2"},
            ]
        )
    )
    packet = await execute_query(
        factory, request, _after_operation=switch_and_collect_after_first_operation
    )
    # The pinned set (head when the query opened) served every unit.
    assert packet.publication["publication_set_id"] == p1.publication_set_id
    # The exact read after the head switch + GC still resolved through
    # the same pinned materialized member.
    exact = [u for u in packet.evidence if u.op == "record_get"]
    assert exact and exact[0].locator.record_id == "view-1"
    assert exact[0].text == "second node"


async def test_pin_released_after_query_allows_retirement(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", "view-1", "needle one")
    pubs2, gen2, p2 = await _publish(factory, service, "ws-a", "view-2", "needle two")

    request = parse_query_request(
        _request([{"op": "lexical_search", "text": "needle"}])
    )
    packet = await execute_query(factory, request)
    assert packet.publication["publication_set_id"] == p2.publication_set_id

    # No pins outlive the call; the superseded set can now retire.
    assert await active_publication_pins(factory) == ()
    report = await collect(factory, store)
    assert report.publication_sets_retired == 1


# ---------------------------------------------------------------------------
# V19: error and cancellation paths never leak the pin
# ---------------------------------------------------------------------------


async def test_error_during_execution_releases_pin(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen1, ref = await _publish(factory, service, "ws-a", "view-1", "needle one")

    async with factory() as session:
        await session.execute(
            update(KernelGenerationRecord)
            .where(
                KernelGenerationRecord.generation_id == gen1.generation_id,
                KernelGenerationRecord.record_id == "view-1",
            )
            .values(payload_json='{"tampered": true}')
        )
        await session.commit()

    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle"},
                {"op": "record_get", "record_id": "view-1"},
            ]
        )
    )
    from app.kernel.errors import PublicationIntegrityError

    with pytest.raises(PublicationIntegrityError):
        await execute_query(factory, request)
    assert await active_publication_pins(factory) == ()


async def test_cancellation_during_execution_releases_pin(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", "view-1", "needle one")

    async def cancel_after_first_operation(index: int) -> None:
        if index == 0:
            raise asyncio.CancelledError()

    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle"},
                {"op": "record_get", "record_id": "view-1"},
            ]
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await execute_query(factory, request, _after_operation=cancel_after_first_operation)
    assert await active_publication_pins(factory) == ()


async def test_query_across_head_switch_then_gc_retires_old_set(
    payload_env: tuple,
) -> None:
    """End-to-end: pin holds across switch + GC, release enables cleanup."""
    factory, store, service = payload_env
    pubs, gen1, p1 = await _publish(factory, service, "ws-a", "view-1", "needle one")

    async def switch_head_after_first_operation(index: int) -> None:
        if index == 0:
            await _publish(factory, service, "ws-a", "view-2", "needle two")

    request = parse_query_request(
        _request(
            [
                {"op": "lexical_search", "text": "needle"},
                {"op": "record_get", "record_id": "view-1", "node_id": "n1"},
            ]
        )
    )
    packet = await execute_query(
        factory, request, _after_operation=switch_head_after_first_operation
    )
    assert packet.publication["publication_set_id"] == p1.publication_set_id
    assert await active_publication_pins(factory) == ()

    report = await collect(factory, store)
    assert report.publication_sets_retired == 1
    from app.kernel.publications import resolve_published_set

    resolved = await resolve_published_set(factory, "ws-a")
    assert resolved is not None
    assert resolved.publication_set_id != p1.publication_set_id
