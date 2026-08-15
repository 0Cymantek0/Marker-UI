"""Materialized generation tests (V3.2 PR65A, plan matrices 10.2 + 10.6).

Deterministic identity/content over the same declared inputs, cut
isolation from later commits, atomic activation, reader pinning,
restart recovery, immutability, tamper detection, and the structural
guarantee that generation reads never replay raw kernel history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import (
    GenerationIntegrityError,
    GenerationStateError,
    UnknownGenerationError,
)
from app.kernel.generations import (
    GENERATION_STATE_ACTIVE,
    GENERATION_STATE_SUPERSEDED,
    GENERATION_STATE_VALIDATED,
    GenerationReader,
    GenerationService,
    open_current_generation,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.records import (
    EDGE_KIND_EVIDENCE_FOR,
    ClaimAssertionRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.snapshots import (
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    resolve_snapshot,
)

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


def _observation(tag: str, payload: bytes | None = None) -> ObservationRecord:
    return ObservationRecord(
        observer="obs", derivation={"tag": tag}, payload_bytes=payload
    )


async def commit_one(service: KernelCommitService, key: str) -> None:
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion(key),))
    )


async def seed_two_cuts(service: KernelCommitService) -> None:
    """ws-a: commit 1 (assertion+observation+edge), commit 2 (assertion)."""
    a1 = _assertion("a1")
    o1 = _observation("o1", payload=b"evidence" * 8)
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(a1, o1),
            edges=(
                KernelEdge(
                    edge_kind=EDGE_KIND_EVIDENCE_FOR,
                    source_ref=o1.record_id,
                    target_ref=a1.record_id,
                ),
            ),
        )
    )
    await commit_one(service, "a2")


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


def _sql(db_path: Path, statement: str, params: tuple = ()) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(statement, params)
        conn.commit()


def _fresh_factory(db_path: Path) -> async_sessionmaker:
    """A brand-new engine over the same durable file (restart view)."""
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


async def test_same_inputs_same_generation_and_digest(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)

    snapshot = await resolve_snapshot(factory, "ws-a")
    first = await gen_service.build_and_activate(snapshot)
    first_rows = await GenerationReader(factory, first.generation_id).count_records()

    rebuilt = await gen_service.build(snapshot)
    assert rebuilt.generation_id == first.generation_id
    assert rebuilt.content_digest == first.content_digest
    assert rebuilt.state == GENERATION_STATE_ACTIVE
    # immutable rows untouched: no duplicates, no rewrites
    assert await GenerationReader(factory, first.generation_id).count_records() == first_rows
    assert (await verify_generation(factory, first.generation_id)).ok


async def test_different_config_different_generation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, "ws-a")

    plain = await gen_service.build_and_activate(snapshot)
    tuned = await gen_service.build_and_activate(
        snapshot, config={"read_profile": "outline"}
    )
    assert plain.generation_id != tuned.generation_id
    assert tuned.state == GENERATION_STATE_ACTIVE
    assert (
        await gen_service.get_generation(plain.generation_id)
    ).state == GENERATION_STATE_SUPERSEDED


async def test_historical_rebuild_after_newer_commits_matches(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)

    pinned = await resolve_snapshot(factory, "ws-a", at_commit=1)
    original = await gen_service.build(pinned)

    for key in ("a3", "a4", "a5"):
        await commit_one(service, key)

    rebuilt = await gen_service.build(pinned)
    assert rebuilt.generation_id == original.generation_id
    assert rebuilt.content_digest == original.content_digest
    assert rebuilt.kernel_commit_id == 1
    assert rebuilt.record_count == 2  # the newer commits never leaked in


async def test_degraded_snapshot_builds_degraded_generation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    record = _observation("bytes", payload=b"will-vanish" * 6)
    receipt = await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(record,))
    )
    path = store.object_path(receipt.payload_blob_keys[0])
    path.chmod(0o600)
    path.unlink()
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(
        factory,
        "ws-a",
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
        payload_store=store,
    )
    assert snapshot.completeness == "degraded"
    ref = await gen_service.build_and_activate(snapshot)
    assert ref.completeness == "degraded"
    assert ref.payload_state_counts["missing"] == 1


# ---------------------------------------------------------------------------
# lifecycle, activation, pinning, restart
# ---------------------------------------------------------------------------


async def test_lifecycle_build_validate_activate_supersede(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)

    first = await gen_service.build(
        await resolve_snapshot(factory, "ws-a", at_commit=1)
    )
    assert first.state == GENERATION_STATE_VALIDATED
    assert await resolve_current_generation(factory, "ws-a") is None
    activated = await gen_service.activate(first.generation_id)
    assert activated.state == GENERATION_STATE_ACTIVE
    assert (
        await resolve_current_generation(factory, "ws-a")
    ).generation_id == first.generation_id

    # idempotent activation
    again = await gen_service.activate(first.generation_id)
    assert again.state == GENERATION_STATE_ACTIVE

    second = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    assert second.state == GENERATION_STATE_ACTIVE
    first_after = await gen_service.get_generation(first.generation_id)
    assert first_after.state == GENERATION_STATE_SUPERSEDED
    current = await resolve_current_generation(factory, "ws-a")
    assert current.generation_id == second.generation_id
    # old generation remains immutable and readable
    old_reader = GenerationReader(factory, first.generation_id)
    assert await old_reader.count_records() == 2
    assert (await verify_generation(factory, first.generation_id)).ok


async def test_reader_pinned_while_new_generation_activates(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)

    gen_a = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a", at_commit=1)
    )
    reader_a = await open_current_generation(factory, "ws-a")

    gen_b = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    assert gen_b.generation_id != gen_a.generation_id

    # the pinned reader stays on A for its whole life
    assert (await reader_a.summary()).generation_id == gen_a.generation_id
    assert await reader_a.count_records() == 2
    # a new reader resolves B
    reader_b = await open_current_generation(factory, "ws-a")
    assert (await reader_b.summary()).generation_id == gen_b.generation_id
    assert await reader_b.count_records() == 3


async def test_restart_recovers_current_from_durable_state(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    active = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )

    fresh = _fresh_factory(_db_path(factory))
    recovered = await resolve_current_generation(fresh, "ws-a")
    assert recovered is not None
    assert recovered.generation_id == active.generation_id
    assert recovered.content_digest == active.content_digest

    reader = GenerationReader(fresh, recovered.generation_id)
    assert await reader.count_records() == 3
    assert (await reader.summary()).state == GENERATION_STATE_ACTIVE

    empty_ws = await resolve_current_generation(fresh, "ws-never")
    assert empty_ws is None


async def test_activation_rejects_unvalidated_generation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)

    # staged residue from a crash between staging and validation
    snapshot = await resolve_snapshot(factory, "ws-a")
    with pytest.raises(Exception):
        await gen_service.build(snapshot, _inject_fault_at="gen-staged")
    staged = (await gen_service.list_generations(state="staged"))[0]

    with pytest.raises(GenerationStateError):
        await gen_service.activate(staged.generation_id)
    assert await resolve_current_generation(factory, "ws-a") is None

    # resume: explicit validation then activation succeeds
    validated = await gen_service.validate(staged.generation_id)
    assert validated.state == GENERATION_STATE_VALIDATED
    activated = await gen_service.activate(staged.generation_id)
    assert activated.state == GENERATION_STATE_ACTIVE


async def test_unknown_generation_rejected(payload_env: tuple) -> None:
    factory, store, service = payload_env
    gen_service = GenerationService(factory)
    with pytest.raises(UnknownGenerationError):
        await gen_service.get_generation("sha256:" + "00" * 32)


# ---------------------------------------------------------------------------
# empty workspace generation
# ---------------------------------------------------------------------------


async def test_empty_workspace_generation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    gen_service = GenerationService(factory)
    snapshot = await resolve_snapshot(factory, "ws-empty")
    assert snapshot.kernel_commit_id == 0

    ref = await gen_service.build_and_activate(snapshot)
    assert ref.record_count == 0 and ref.commit_count == 0
    reader = await open_current_generation(factory, "ws-empty")
    assert reader is not None
    assert await reader.count_records() == 0
    assert await reader.get_record("nope") is None
    assert (await verify_generation(factory, ref.generation_id)).ok


# ---------------------------------------------------------------------------
# tamper detection
# ---------------------------------------------------------------------------


async def test_validation_rejects_tampered_staged_generation(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    prior = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )

    # stage a newer generation, then crash before validation
    await commit_one(service, "a3")
    with pytest.raises(Exception):
        await gen_service.build(
            await resolve_snapshot(factory, "ws-a"),
            _inject_fault_at="gen-validate-begin",
        )
    staged = (await gen_service.list_generations(state="staged"))[-1]

    # tamper with the staged material
    _sql(
        _db_path(factory),
        "UPDATE kernel_generation_records SET payload_json = ? "
        "WHERE generation_id = ?",
        ('{"forged": true}', staged.generation_id),
    )

    with pytest.raises(GenerationIntegrityError):
        await gen_service.validate(staged.generation_id)
    failed = await gen_service.get_generation(staged.generation_id)
    assert failed.state == "failed"
    # prior accepted generation remains authoritative
    current = await resolve_current_generation(factory, "ws-a")
    assert current.generation_id == prior.generation_id
    assert (await verify_generation(factory, prior.generation_id)).ok

    # the failed generation is identifiable residue and rebuildable
    assert [g.generation_id for g in await gen_service.list_generations(state="failed")]
    rebuilt = await gen_service.build(await resolve_snapshot(factory, "ws-a"))
    assert rebuilt.state == GENERATION_STATE_VALIDATED


async def test_post_activation_tamper_detected_on_read_and_verify(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    ref = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    reader = GenerationReader(factory, ref.generation_id)

    victim = (await reader.list_records(limit=1))[0].record_id
    _sql(
        _db_path(factory),
        "UPDATE kernel_generation_records SET payload_json = ? "
        "WHERE generation_id = ? AND record_id = ?",
        ('{"tampered": "after-activation"}', ref.generation_id, victim),
    )

    verification = await verify_generation(factory, ref.generation_id)
    assert not verification.ok
    assert any("identity" in p or "digest" in p for p in verification.problems)
    with pytest.raises(GenerationIntegrityError):
        await reader.get_record(victim)
    with pytest.raises(GenerationIntegrityError):
        await reader.list_records(limit=10)


async def test_manifest_count_tamper_detected_by_verify(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    ref = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )
    _sql(
        _db_path(factory),
        "UPDATE kernel_generations SET record_count = 99 WHERE generation_id = ?",
        (ref.generation_id,),
    )
    verification = await verify_generation(factory, ref.generation_id)
    assert not verification.ok
    assert any("counts" in p for p in verification.problems)


# ---------------------------------------------------------------------------
# ready reads: bounded and replay-free (matrix 10.6)
# ---------------------------------------------------------------------------


async def test_generation_reads_never_replay_kernel_history(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    ref = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-a")
    )

    with (
        patch("app.kernel.replay.replay") as mock_replay,
        patch("app.kernel.replay.verify_history") as mock_verify,
    ):
        reader = await open_current_generation(factory, "ws-a")
        await reader.summary()
        await reader.get_record((await reader.list_records(limit=1))[0].record_id)
        await reader.list_records(limit=5)
        await reader.count_records()
        await reader.list_edges()
        mock_replay.assert_not_called()
        mock_verify.assert_not_called()
    assert ref.state == GENERATION_STATE_ACTIVE


async def test_reader_record_lookup_and_enumeration(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await seed_two_cuts(service)
    gen_service = GenerationService(factory)
    await gen_service.build_and_activate(await resolve_snapshot(factory, "ws-a"))
    reader = await open_current_generation(factory, "ws-a")

    page = await reader.list_records(limit=2)
    assert len(page) == 2
    assert all(r.kernel_commit_id <= 2 for r in page)
    fetched = await reader.get_record(page[0].record_id)
    assert fetched is not None and fetched.identity_hash == page[0].identity_hash

    claims = await reader.list_records(record_class="claim_assertion", limit=10)
    assert len(claims) == 2
    assert await reader.count_records(record_class="claim_assertion") == 2
    edges = await reader.list_edges()
    assert len(edges) == 1 and edges[0].edge_kind == EDGE_KIND_EVIDENCE_FOR

    with pytest.raises(Exception):
        await reader.list_records(limit=0)
