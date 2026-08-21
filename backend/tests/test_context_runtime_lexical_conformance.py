"""Dual-backend context-runtime lexical conformance (PR83B2 WS E).

The full query-serving story through the real service path —
ContinuationService fresh/continue lifecycle, authorization-first
behavior, epoch invalidation between pages, high-assurance partition
isolation — executed against real SQLite and real PostgreSQL 16
databases. The service layer itself is dialect-blind by design; these
tests prove that claim honest on both physical profiles.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    ContinuationService,
    CursorCodec,
    CursorKeyring,
    execute_query,
    parse_query_request,
)
from app.context_runtime.errors import QueryAuthorizationError
from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import GenerationService
from app.kernel.payloads import LocalPayloadStore
from app.kernel.publications import (
    HIGH_ASSURANCE_PROFILE_PREFIX,
    PublicationService,
)
from app.kernel.snapshots import resolve_snapshot
from app.services.query_policy import QueryPolicyService
from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)
from tests.test_context_runtime_authorization import _epoch
from tests.test_context_runtime_authz_retrieval import seed_domain_doc

pytestmark = pytest.mark.asyncio


@dataclasses.dataclass
class RuntimeEnv:
    backend: str
    engine: object
    session_factory: async_sessionmaker
    service: KernelCommitService
    server_version: str


@pytest.fixture(params=BACKENDS)
def backend(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def runtime_env(backend: str, tmp_path: pathlib.Path):
    async with provisioned_database(
        backend, (tmp_path / "runtime.db").as_posix()
    ) as prov:
        result = await upgrade_database(url=prov.url)
        assert result.to_revision, "bootstrap must reach a migration head"
        engine = create_async_engine(prov.url, **engine_kwargs_for(backend))
        assert engine.dialect.name == backend
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        store = LocalPayloadStore(tmp_path / "payloads")
        service = KernelCommitService(session_factory, payload_store=store)
        server_version = ""
        if backend == "postgresql":
            async with engine.connect() as conn:
                server_version = await conn.scalar(text("SELECT version()"))
        try:
            yield RuntimeEnv(
                backend=backend,
                engine=engine,
                session_factory=session_factory,
                service=service,
                server_version=server_version,
            )
        finally:
            await engine.dispose()


def _codec() -> CursorCodec:
    return CursorCodec(
        CursorKeyring({"k1": b"pr83b2-test-key-" + b"x" * 31}, current_key_id="k1")
    )


def _request(workspace: str, op_text: str = "needle", **overrides):
    body = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": [{"op": "lexical_search", "text": op_text, "limit": 25}],
        "budget": {
            "max_operations": 8,
            "max_candidates": 100,
            "max_evidence_units": 100,
            "max_output_chars": 100000,
        },
    }
    body.update(overrides)
    return parse_query_request(body)


async def _publish_needle_docs(
    env: RuntimeEnv, workspace: str, suffix: str = "1", *, advance: bool = True
):
    """One view with six needle nodes; returns the publication ref."""
    from tests.test_kernel_publication import _commit_view, _view

    await _commit_view(
        env.service,
        workspace,
        _view(
            f"view-{suffix}",
            {f"n{index}": "needle" for index in range(1, 7)},
            f"rev-{suffix}",
        ),
        advance=advance,
    )
    generation = await GenerationService(env.session_factory).build_and_activate(
        await resolve_snapshot(env.session_factory, workspace)
    )
    return await PublicationService(env.session_factory).publish(
        materialized_generation_id=generation.generation_id
    )


# ---------------------------------------------------------------------------
# Fresh + continued query through the service on a real backend
# ---------------------------------------------------------------------------


async def test_fresh_and_continued_query_traverse_completely(runtime_env) -> None:
    env = runtime_env
    assert env.backend != "postgresql" or "PostgreSQL 16" in env.server_version
    ref = await _publish_needle_docs(env, "ws-run")
    service = ContinuationService(
        env.session_factory, cursor_codec=_codec(), pin_lease_seconds=60
    )
    first = await service.fresh_query(_request("ws-run"), page_size=2)
    assert first.status == "partial"
    assert first.next_cursor
    assert first.packet is not None
    assert first.packet.publication["publication_set_id"] == ref.publication_set_id

    seen: list[tuple[str, str]] = [
        (unit.locator.record_id, unit.locator.node_id)
        for unit in first.packet.evidence
    ]
    cursor = first.next_cursor
    pages = 1
    while cursor is not None:
        continued = await service.continue_query(
            cursor, workspace_id="ws-run", page_size=2
        )
        pages += 1
        if continued.packet is not None:
            seen.extend(
                (unit.locator.record_id, unit.locator.node_id)
                for unit in continued.packet.evidence
            )
        cursor = continued.next_cursor
        assert continued.status in {"partial", "complete", "invalidated", "stale"}
        if continued.status in {"invalidated", "stale"}:
            pytest.fail(f"continuation failed unexpectedly: {continued.status}")
    # All six nodes traversed exactly once, no duplicates, no gaps.
    assert sorted(seen) == sorted(("view-1", f"n{index}") for index in range(1, 7))
    assert len(seen) == len(set(seen))
    assert pages >= 3


async def test_continuation_rejects_mutated_query_and_wrong_workspace(
    runtime_env,
) -> None:
    env = runtime_env
    await _publish_needle_docs(env, "ws-bind")
    service = ContinuationService(
        env.session_factory, cursor_codec=_codec(), pin_lease_seconds=60
    )
    first = await service.fresh_query(_request("ws-bind"), page_size=1)
    assert first.next_cursor

    wrong = await service.continue_query(
        first.next_cursor, workspace_id="ws-other"
    )
    assert wrong.status == "invalidated"
    altered = await service.continue_query(
        first.next_cursor, workspace_id="ws-bind", request=_request("ws-bind", "other")
    )
    assert altered.status == "invalidated"
    # The valid cursor was not consumed by the rejected attempts.
    valid = await service.continue_query(first.next_cursor, workspace_id="ws-bind")
    assert valid.status in {"partial", "complete"}


async def test_continuation_stays_on_old_publication_after_head_switch(
    runtime_env,
) -> None:
    env = runtime_env
    first_publication = await _publish_needle_docs(env, "ws-head")
    service = ContinuationService(
        env.session_factory, cursor_codec=_codec(), pin_lease_seconds=60
    )
    first = await service.fresh_query(_request("ws-head"), page_size=2)
    assert first.next_cursor

    await _publish_needle_docs(env, "ws-head", suffix="2", advance=False)
    continued = await service.continue_query(
        first.next_cursor, workspace_id="ws-head", page_size=2
    )
    assert continued.packet is not None
    assert (
        continued.packet.publication["publication_set_id"]
        == first_publication.publication_set_id
    )
    # A FRESH query resolves the new head instead.
    fresh = await execute_query(env.session_factory, _request("ws-head"))
    assert fresh.publication["publication_set_id"] != first_publication.publication_set_id


# ---------------------------------------------------------------------------
# Authorization-first behavior on a real backend
# ---------------------------------------------------------------------------


async def test_live_deny_epoch_invalidates_cursor_between_pages(runtime_env) -> None:
    env = runtime_env
    await _publish_needle_docs(env, "ws-policy")
    service = ContinuationService(
        env.session_factory, cursor_codec=_codec(), pin_lease_seconds=60
    )
    first = await service.fresh_query(_request("ws-policy"), page_size=1)
    assert first.next_cursor

    policy = QueryPolicyService(
        env.session_factory, env.service, workspace_id="ws-policy"
    )
    await policy.deny_record("view-1")
    denied = await service.continue_query(
        first.next_cursor, workspace_id="ws-policy"
    )
    assert denied.status == "invalidated"
    assert denied.packet is None
    # The denied record never leaks through any cursor surface.
    assert "view-1" not in denied.model_dump_json()

    # Allow + a fresh authorization epoch does not resurrect the cursor.
    await policy.allow_record("view-1")
    await env.service.commit(
        KernelCommitBatch(workspace_id="ws-policy", records=(_epoch(1, 7),))
    )
    after = await service.continue_query(
        first.next_cursor, workspace_id="ws-policy"
    )
    assert after.status == "invalidated"


async def test_high_assurance_partition_isolation_on_real_backend(runtime_env) -> None:
    """F/E: forbidden corpus is structurally outside the partition the
    high-assurance query reads; rank basis stable under forbidden
    growth; missing partition fails closed without shared fallback."""
    env = runtime_env
    await seed_domain_doc(
        env.service,
        "ws-ha",
        tag="alpha",
        domain="dom-alpha",
        texts={
            "n1": "needle alpha primary document with several common terms",
            "n2": "needle alpha secondary lighter mention",
        },
    )
    for index in range(3):
        await seed_domain_doc(
            env.service,
            "ws-ha",
            tag=f"beta{index}",
            domain="dom-beta",
            texts={"n1": f"needle beta forbidden {index} highly repetitive"},
        )
    policy = QueryPolicyService(
        env.session_factory, env.service, workspace_id="ws-ha"
    )
    await policy.deny_domain("dom-beta")

    pubs = PublicationService(env.session_factory)
    gen = await GenerationService(env.session_factory).build_and_activate(
        await resolve_snapshot(env.session_factory, "ws-ha")
    )
    await pubs.publish(materialized_generation_id=gen.generation_id)
    partition = await pubs.publish_high_assurance(
        materialized_generation_id=gen.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )

    ha_request = _request("ws-ha", assurance="high")
    ha = await execute_query(env.session_factory, ha_request)
    assert ha.publication["profile"].startswith(HIGH_ASSURANCE_PROFILE_PREFIX)
    assert ha.publication["publication_set_id"] == partition.publication_set_id
    records = {unit.locator.record_id for unit in ha.evidence}
    assert records == {"view.alpha"}
    assert all("beta" not in record for record in records)
    ha_ranks = [unit.rank for unit in ha.evidence]

    # Forbidden growth cannot change the authorized partition's basis.
    for index in range(10):
        await seed_domain_doc(
            env.service,
            "ws-ha",
            tag=f"flood{index}",
            domain="dom-beta",
            texts={"n1": f"needle beta forbidden flood {index}"},
        )
    gen2 = await GenerationService(env.session_factory).build_and_activate(
        await resolve_snapshot(env.session_factory, "ws-ha")
    )
    await pubs.publish(materialized_generation_id=gen2.generation_id)
    await pubs.publish_high_assurance(
        materialized_generation_id=gen2.generation_id,
        partition_domains=frozenset({"dom-alpha"}),
    )
    ha_after = await execute_query(env.session_factory, ha_request)
    assert [unit.locator.record_id for unit in ha_after.evidence] == [
        unit.locator.record_id for unit in ha.evidence
    ]
    assert [unit.rank for unit in ha_after.evidence] == ha_ranks

    # A workspace with no published partition fails closed — never falls
    # back to the shared index.
    with pytest.raises(QueryAuthorizationError):
        await execute_query(
            env.session_factory, _request("ws-no-partition", assurance="high")
        )
