"""Deterministic keyset paging over pinned lexical generations (PR79A)."""

from __future__ import annotations

import dataclasses
import math
import sqlite3
from pathlib import Path

import pytest

from app.kernel.errors import PublicationIntegrityError
from app.kernel.generations import GenerationService
from app.kernel.publications import (
    LexicalSearchAfter,
    PublicationService,
    open_published_reader,
)
from app.kernel.snapshots import resolve_snapshot
from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio


def _db_path(factory) -> Path:
    return Path(factory.kw["bind"].url.database)


async def _publish(factory, service, workspace: str, suffix: str = ""):
    await _commit_view(
        service,
        workspace,
        _view(
            f"view{suffix or '-1'}",
            {
                "n1": "needle",
                "n2": "needle",
                "n3": "needle",
                "n4": "needle",
            },
            f"rev{suffix or '-1'}",
        ),
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    publication = await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )
    return publication


async def test_search_after_equal_rank_boundary_has_no_duplicate_or_skip(
    payload_env: tuple,
) -> None:
    factory, _store, service = payload_env
    publication = await _publish(factory, service, "ws-keyset")
    reader = await open_published_reader(
        factory, "ws-keyset", pin_lease_seconds=None
    )
    assert reader is not None
    try:
        first = await reader.search_after('"needle"', limit=2)
        second = await reader.search_after(
            '"needle"', limit=2, after=first.next_after
        )
        full = await reader.search('"needle"', limit=20)

        assert first.has_more
        assert not second.has_more
        assert second.next_after is None
        assert first.hits[0].rank == first.hits[1].rank
        assert second.hits[0].rank == second.hits[1].rank
        all_pages = first.hits + second.hits
        assert [hit.row_index for hit in all_pages] == [
            hit.row_index for hit in full
        ]
        assert len({hit.row_index for hit in all_pages}) == len(full)
        assert first.next_after is not None
        assert "offset" not in first.next_after.as_dict()
        assert first.next_after.publication_set_id == publication.publication_set_id
        assert (
            first.next_after.lexical_generation_id
            == publication.lexical_generation_id
        )
    finally:
        await reader.close()


async def test_search_after_rejects_cross_generation_and_tampered_state(
    payload_env: tuple,
) -> None:
    factory, _store, service = payload_env
    first_publication = await _publish(factory, service, "ws-keyset")
    first_reader = await open_published_reader(
        factory, "ws-keyset", pin_lease_seconds=None
    )
    assert first_reader is not None
    try:
        first_page = await first_reader.search_after('"needle"', limit=2)
        assert first_page.next_after is not None

        # A cursor from a different immutable publication must not be usable.
        await _commit_view(
            service,
            "ws-keyset",
            _view("view-2", {"n1": "needle"}, "rev-2"),
            advance=False,
        )
        generation = await GenerationService(factory).build_and_activate(
            await resolve_snapshot(factory, "ws-keyset")
        )
        second_publication = await PublicationService(factory).publish(
            materialized_generation_id=generation.generation_id
        )
        second_reader = await open_published_reader(
            factory, "ws-keyset", pin_lease_seconds=None
        )
        assert second_reader is not None
        try:
            with pytest.raises(PublicationIntegrityError, match="pinned"):
                await second_reader.search_after(
                    '"needle"', limit=2, after=first_page.next_after
                )
        finally:
            await second_reader.close()

        tampered = dataclasses.replace(
            first_page.next_after,
            rank=first_page.next_after.rank + 1.0,
        )
        with pytest.raises(PublicationIntegrityError, match="anchor rank"):
            await first_reader.search_after('"needle"', after=tampered, limit=2)
        assert first_publication.publication_set_id != second_publication.publication_set_id
    finally:
        await first_reader.close()


async def test_search_after_tampered_index_fails_closed(payload_env: tuple) -> None:
    factory, _store, service = payload_env
    publication = await _publish(factory, service, "ws-keyset")
    reader = await open_published_reader(
        factory, "ws-keyset", pin_lease_seconds=None
    )
    assert reader is not None
    try:
        first = await reader.search_after('"needle"', limit=1)
        assert first.next_after is not None
        full = await reader.search('"needle"', limit=10)
        tampered_row = full[1].row_index
        lexical = await PublicationService(factory).get_lexical_generation(
            publication.lexical_generation_id
        )
        with sqlite3.connect(_db_path(factory)) as connection:
            connection.execute(
                f'UPDATE "{lexical.fts_table}" SET text = ? WHERE rowid = ?',
                ("needle tampered", tampered_row),
            )
            connection.commit()
        with pytest.raises(PublicationIntegrityError, match="tampered|hash|rank"):
            await reader.search_after('"needle"', after=first.next_after, limit=2)
    finally:
        await reader.close()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rank": math.nan}, "malformed"),
        ({"rank": math.inf}, "malformed"),
        ({"row_index": -1}, "malformed"),
        ({"row_index": True}, "malformed"),
        ({"query_hash": "wrong-query"}, "query binding"),
    ],
)
async def test_search_after_rejects_malformed_or_cross_query_keysets(
    payload_env: tuple,
    changes: dict,
    message: str,
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-keyset-invalid")
    reader = await open_published_reader(
        factory, "ws-keyset-invalid", pin_lease_seconds=None
    )
    assert reader is not None
    try:
        first = await reader.search_after('"needle"', limit=1)
        assert first.next_after is not None
        invalid = dataclasses.replace(first.next_after, **changes)
        with pytest.raises(PublicationIntegrityError, match=message):
            await reader.search_after('"needle"', limit=1, after=invalid)
    finally:
        await reader.close()


async def test_search_after_mapping_contract_is_strict_and_offset_free() -> None:
    value = {
        "publication_set_id": "set-1",
        "lexical_generation_id": "lex-1",
        "rank": -1.25,
        "row_index": 7,
        "query_hash": "sha256-query",
    }
    continuation = LexicalSearchAfter.from_mapping(value)
    assert continuation.as_dict() == value
    assert "offset" not in continuation.as_dict()
    with pytest.raises(ValueError, match="exactly"):
        LexicalSearchAfter.from_mapping({**value, "offset": 7})
