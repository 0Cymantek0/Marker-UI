"""PublicationReader pinned materialized record reads (PR77 substrate).

``get_record`` must serve the record from the materialized generation
named by the pinned set, never from independently resolved "current"
state, and must fail closed when the materialized row was tampered with.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.kernel.commit import KernelCommitService
from app.kernel.errors import PublicationIntegrityError
from app.kernel.generations import GenerationService
from app.kernel.models import KernelGenerationRecord
from app.kernel.publications import (
    PublicationService,
    open_published_reader,
)
from app.kernel.snapshots import resolve_snapshot
from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio


async def _publish(factory, service: KernelCommitService, workspace: str, marker: str):
    pubs = PublicationService(factory)
    await _commit_view(
        service,
        workspace,
        _view("view-1", {"n1": f"{marker} one", "n2": f"{marker} two"}, "rev-s1"),
    )
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


async def test_get_record_serves_pinned_materialized_member(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(factory, service, "ws-a", "alpha")

    async with await open_published_reader(factory, "ws-a") as reader:
        record = await reader.get_record("view-1")
        assert record is not None
        assert record.record_id == "view-1"
        assert record.record_class == "view_document"
        assert record.identity_hash.startswith("sha256:")
        assert record.payload["texts"]["n1"] == "alpha one"


async def test_get_record_missing_id_returns_none(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await _publish(factory, service, "ws-a", "alpha")

    async with await open_published_reader(factory, "ws-a") as reader:
        assert await reader.get_record("no-such-record") is None


async def test_get_record_is_scoped_to_pinned_set(payload_env: tuple) -> None:
    """A record committed only after the pinned publication is invisible."""
    factory, store, service = payload_env
    pubs, gen, p1 = await _publish(factory, service, "ws-a", "alpha")

    await _commit_view(
        service,
        "ws-a",
        _view("view-2", {"n1": "beta later"}, "rev-s2"),
        advance=False,
    )
    gen2 = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    p2 = await pubs.publish(materialized_generation_id=gen2.generation_id)

    async with await open_published_reader(factory, "ws-a") as reader:
        assert reader.publication_set_id == p2.publication_set_id
        assert await reader.get_record("view-2") is not None

    # A reader still pinned to the old set must not see the new record
    # even though the head has moved.
    from app.kernel.publications import open_pinned_publication

    async with await open_pinned_publication(factory, p1.publication_set_id) as old:
        assert old.publication_set_id == p1.publication_set_id
        assert await old.get_record("view-2") is None
        record = await old.get_record("view-1")
        assert record is not None
        assert record.payload["texts"]["n1"] == "alpha one"


async def test_get_record_tampered_payload_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(factory, service, "ws-a", "alpha")

    async with factory() as session:
        await session.execute(
            update(KernelGenerationRecord)
            .where(
                KernelGenerationRecord.generation_id == gen.generation_id,
                KernelGenerationRecord.record_id == "view-1",
            )
            .values(payload_json='{"tampered": true}')
        )
        await session.commit()

    async with await open_published_reader(factory, "ws-a") as reader:
        with pytest.raises(PublicationIntegrityError, match="identity hash mismatch"):
            await reader.get_record("view-1")


async def test_get_record_unreadable_payload_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    pubs, gen, ref = await _publish(factory, service, "ws-a", "alpha")

    async with factory() as session:
        await session.execute(
            update(KernelGenerationRecord)
            .where(
                KernelGenerationRecord.generation_id == gen.generation_id,
                KernelGenerationRecord.record_id == "view-1",
            )
            .values(payload_json="not json at all")
        )
        await session.commit()

    async with await open_published_reader(factory, "ws-a") as reader:
        with pytest.raises(PublicationIntegrityError, match="unreadable"):
            await reader.get_record("view-1")
