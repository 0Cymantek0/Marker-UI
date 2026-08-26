"""Invariant 37 declaration suite: per-destination external-effect
semantics derived from real destination primitives.

Three layers of proof:

* the derivation rules are total and pinned (every semantics label is
  reachable; the real destination vectors map to the declared labels);
* the registry never hand-assigns a semantics label and every declared
  primitive resolves against real, importable production code;
* each declared capability FACT is exercised against the real
  primitive: fenced acceptance (transactional linearization, duplicate
  convergence, divergent/stale rejection) on both first-class database
  backends; the filesystem writer's collision-avoiding redelivery and
  partial-set behavior; the compatibility projection's guarded replay.

If a primitive changes behavior, the fact-test here fails and the
declaration in ``app.kernel.effect_semantics`` must be re-derived —
labels may never drift from facts.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.kernel import fencing
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.effect_semantics import (
    AT_LEAST_ONCE,
    AT_MOST_ONCE,
    DESTINATIONS,
    EXACTLY_ONCE,
    RECONCILIATION_REQUIRED,
    DestinationCapabilities,
    declare_destination,
    declared_destinations,
    derive_semantics,
)
from app.kernel.errors import PublicationConflictError, StaleFenceError
from app.kernel.models import KernelPublication
from app.kernel.outbox import OutboxIntent, list_outbox
from app.kernel.records import ClaimAssertionRecord
from app.models.job import ConversionJob
from app.services.kernel_runtime import KernelRuntimeCoordinator
from app.services.output_writer import write_conversion_output
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.pg_provisioning import (
    BACKENDS,
    engine_kwargs_for,
    provisioned_database,
)


def _resolve_dotted(path: str):
    """Import the longest module prefix, then walk remaining attributes."""
    parts = path.split(".")
    for split in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attr in parts[split:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(f"cannot resolve dotted path: {path}")


def _caps(**overrides) -> DestinationCapabilities:
    base: dict = {
        "linearizes_in_transaction": False,
        "suppresses_duplicate_delivery": False,
        "rejects_divergent_effect": False,
        "derived_from_kernel_truth": False,
        "requires_reconciliation": False,
    }
    base.update(overrides)
    return DestinationCapabilities(**base)


class TestDerivationRules:
    """The rule matrix is total, pinned, and fact-driven."""

    def test_every_semantics_label_is_reachable(self) -> None:
        reached = {
            derive_semantics(_caps(requires_reconciliation=True)),
            derive_semantics(
                _caps(
                    derived_from_kernel_truth=True,
                    linearizes_in_transaction=True,
                    suppresses_duplicate_delivery=True,
                )
            ),
            derive_semantics(
                _caps(
                    linearizes_in_transaction=True,
                    suppresses_duplicate_delivery=True,
                    rejects_divergent_effect=True,
                )
            ),
            derive_semantics(_caps(linearizes_in_transaction=True)),
            derive_semantics(_caps()),
        }
        assert {label.value for label in reached} == {
            EXACTLY_ONCE,
            AT_LEAST_ONCE,
            AT_MOST_ONCE,
            RECONCILIATION_REQUIRED,
        }

    def test_reconciliation_requirement_dominates_all_other_strengths(self) -> None:
        # Even a transactional, deduplicating, divergent-rejecting
        # destination that can leave orphans declares reconciliation.
        strong_but_orphaning = _caps(
            linearizes_in_transaction=True,
            suppresses_duplicate_delivery=True,
            rejects_divergent_effect=True,
            requires_reconciliation=True,
        )
        assert derive_semantics(strong_but_orphaning).value == RECONCILIATION_REQUIRED

    def test_atomic_without_identity_primitives_is_at_most_once(self) -> None:
        assert (
            derive_semantics(_caps(linearizes_in_transaction=True)).value
            == AT_MOST_ONCE
        )

    def test_non_atomic_without_reconciliation_needs_is_at_least_once(self) -> None:
        assert derive_semantics(_caps()).value == AT_LEAST_ONCE

    def test_real_destination_vectors_map_to_declared_labels(self) -> None:
        assert (
            declare_destination("kernel.accepted_publication").semantics.value
            == EXACTLY_ONCE
        )
        assert (
            declare_destination("filesystem.conversion_output").semantics.value
            == RECONCILIATION_REQUIRED
        )
        assert (
            declare_destination("compatibility.conversion_job_row").semantics.value
            == EXACTLY_ONCE
        )


class TestRegistryIntegrity:
    """The registry is the declaration; semantics are never hand-assigned."""

    def test_registry_semantics_always_match_derivation(self) -> None:
        for entry in DESTINATIONS:
            assert entry.semantics is derive_semantics(entry.capabilities), (
                f"{entry.destination}: declared semantics diverged from "
                "derived facts — re-derive, do not hand-assign"
            )

    def test_destination_ids_are_unique_and_lookup_is_exact(self) -> None:
        ids = declared_destinations()
        assert len(ids) == len(set(ids))
        for destination in ids:
            assert declare_destination(destination).destination == destination
        with pytest.raises(KeyError):
            declare_destination("not.a.real.destination")

    def test_declared_primitives_resolve_against_real_code(self) -> None:
        # Binds the declaration table to production code: a renamed or
        # removed primitive fails here.
        for entry in DESTINATIONS:
            obj = _resolve_dotted(entry.primitive)
            assert callable(obj)

    def test_reconciliation_note_required_exactly_when_orphans_possible(self) -> None:
        for entry in DESTINATIONS:
            if entry.capabilities.requires_reconciliation:
                assert entry.reconciliation, (
                    f"{entry.destination} requires a reconciliation note"
                )
            else:
                assert entry.reconciliation == ""


# ---------------------------------------------------------------------------
# Capability facts: fenced accepted publication (both database backends)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(params=BACKENDS, ids=BACKENDS)
async def semantics_env(request, tmp_path: Path):
    """Migrated kernel DB + commit service on each first-class backend."""
    from app.db_migration import upgrade_database

    backend = request.param
    async with provisioned_database(
        backend, (tmp_path / "semantics.db").as_posix()
    ) as prov:
        await upgrade_database(url=prov.url)
        engine_kwargs = engine_kwargs_for(backend)
        if backend == "sqlite":
            engine_kwargs["connect_args"]["timeout"] = 30
        engine = create_async_engine(prov.url, **engine_kwargs)
        assert engine.dialect.name == backend
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        try:
            yield SimpleNamespace(
                factory=factory,
                service=KernelCommitService(factory),
                backend=backend,
            )
        finally:
            await engine.dispose()


async def _new_work(env, *, tag: str) -> int:
    """Commit one record + outbox intent atomically; return work id."""
    await env.service.commit(
        KernelCommitBatch(
            workspace_id="sem",
            records=(
                ClaimAssertionRecord(
                    claim_key=f"sem-{tag}",
                    subject="doc:x.pdf",
                    predicate="declared",
                    value=1,
                ),
            ),
            outbox=(OutboxIntent(work_kind="materialize", payload={"tag": tag}),),
        )
    )
    rows = await list_outbox(env.factory, workspace_id="sem")
    return rows[-1].id


def _descriptor(tag: str, sha: str = "a" * 64) -> dict:
    return {
        "kind": "conversion.result",
        "schema": 1,
        "job_id": f"job-{tag}",
        "result_text": {"bytes": 4, "sha256": sha},
    }


async def _publication_rows(env) -> list[KernelPublication]:
    async with env.factory() as session:
        return (
            (
                await session.execute(
                    select(KernelPublication).where(
                        KernelPublication.workspace_id == "sem"
                    )
                )
            )
            .scalars()
            .all()
        )


class TestAcceptedPublicationCapabilityFacts:
    """Facts backing kernel.accepted_publication = exactly_once."""

    pytestmark = pytest.mark.asyncio

    async def test_linearizes_in_transaction_single_publication_row(
        self, semantics_env
    ) -> None:
        env = semantics_env
        work_id = await _new_work(env, tag="linear")
        lease = await fencing.acquire(
            env.factory, work_id=work_id, owner_id="sem-owner", lease_seconds=60.0
        )
        assert lease is not None
        outcome = await fencing.accept(
            env.factory, work_id=work_id, fencing_token=lease.fencing_token, result=_descriptor("linear")
        )
        assert outcome.already_accepted is False
        rows = await _publication_rows(env)
        assert len(rows) == 1
        lease_after = await fencing.get_lease(env.factory, work_id)
        assert lease_after.state == "accepted"

    async def test_suppresses_duplicate_delivery_same_result_converges(
        self, semantics_env
    ) -> None:
        env = semantics_env
        work_id = await _new_work(env, tag="dup")
        lease = await fencing.acquire(
            env.factory, work_id=work_id, owner_id="sem-owner", lease_seconds=60.0
        )
        result = _descriptor("dup")
        first = await fencing.accept(
            env.factory, work_id=work_id, fencing_token=lease.fencing_token, result=result
        )
        second = await fencing.accept(
            env.factory, work_id=work_id, fencing_token=lease.fencing_token, result=result
        )
        assert first.already_accepted is False
        assert second.already_accepted is True
        assert (
            second.publication.publication_id == first.publication.publication_id
        )
        assert len(await _publication_rows(env)) == 1

    async def test_rejects_divergent_effect_state_unchanged(
        self, semantics_env
    ) -> None:
        env = semantics_env
        work_id = await _new_work(env, tag="diverge")
        lease = await fencing.acquire(
            env.factory, work_id=work_id, owner_id="sem-owner", lease_seconds=60.0
        )
        accepted = await fencing.accept(
            env.factory,
            work_id=work_id,
            fencing_token=lease.fencing_token,
            result=_descriptor("diverge", sha="1" * 64),
        )
        with pytest.raises(PublicationConflictError):
            await fencing.accept(
                env.factory,
                work_id=work_id,
                fencing_token=lease.fencing_token,
                result=_descriptor("diverge", sha="2" * 64),
            )
        rows = await _publication_rows(env)
        assert len(rows) == 1
        assert rows[0].result_hash == accepted.publication.result_hash

    async def test_stale_fencing_token_never_reaches_comparison(
        self, semantics_env
    ) -> None:
        env = semantics_env
        work_id = await _new_work(env, tag="stale")
        lease = await fencing.acquire(
            env.factory, work_id=work_id, owner_id="sem-owner", lease_seconds=60.0
        )
        with pytest.raises(StaleFenceError):
            await fencing.accept(
                env.factory,
                work_id=work_id,
                fencing_token=lease.fencing_token + 99,
                result=_descriptor("stale"),
            )
        assert await _publication_rows(env) == []


# ---------------------------------------------------------------------------
# Capability facts: filesystem conversion output writer
# ---------------------------------------------------------------------------


RESULT = {
    "text": "# Declared Output\n\nEffect semantics body.",
    "extension": "md",
    "images": {},
    "metadata": {"pages": 1},
}


class TestFilesystemOutputCapabilityFacts:
    """Facts backing filesystem.conversion_output = reconciliation_required."""

    def test_redelivery_writes_new_collision_avoided_set(self, tmp_path: Path) -> None:
        # No identity/deduplication primitive: the same delivered effect
        # twice leaves TWO complete sets on disk (the predecessor is
        # orphaned, not overwritten and not swept).
        first = write_conversion_output(
            dict(RESULT), source_name="doc.pdf", output_base=tmp_path, job_id="j1"
        )
        second = write_conversion_output(
            dict(RESULT), source_name="doc.pdf", output_base=tmp_path, job_id="j1"
        )
        assert first.final_path != second.final_path
        assert first.final_path.is_file() and second.final_path.is_file()
        assert (
            first.final_path.read_text(encoding="utf-8")
            == second.final_path.read_text(encoding="utf-8")
            == RESULT["text"]
        )
        # Both manifests persist: the predecessor set is fully orphaned.
        assert first.manifest_path.is_file() and second.manifest_path.is_file()

    def test_interrupted_set_surfaces_error_and_leaves_partial_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No cross-file transaction: failing partway through the set
        # leaves the already-written primary file on disk. The error
        # must surface — a partial set can never be reported as success.
        import app.services.output_writer as ow

        original_write_json = ow._write_json_atomic

        def _fail_manifest(path: Path, data: dict) -> None:
            if path.name.endswith(".marker.json"):
                raise OSError("injected manifest write interruption")
            original_write_json(path, data)

        monkeypatch.setattr(ow, "_write_json_atomic", _fail_manifest)
        with pytest.raises(OSError, match="injected manifest write interruption"):
            write_conversion_output(
                dict(RESULT), source_name="doc.pdf", output_base=tmp_path, job_id="j2"
            )
        text_files = list(tmp_path.glob("*.md"))
        assert len(text_files) == 1  # partial set: text present…
        assert list(tmp_path.glob("*.marker.json")) == []  # …manifest absent

    def test_per_file_writes_leave_no_temporary_residue(
        self, tmp_path: Path
    ) -> None:
        written = write_conversion_output(
            dict(RESULT), source_name="doc.pdf", output_base=tmp_path, job_id="j3"
        )
        assert written.final_path.is_file()
        assert written.manifest_path.is_file()
        assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Capability facts: compatibility row projection
# ---------------------------------------------------------------------------


def _fake_publication(work_id: int, result_path: Path) -> fencing.Publication:
    return fencing.Publication(
        publication_id=f"pub-{work_id}",
        workspace_id="sem",
        work_id=work_id,
        work_kind="conversion.execute",
        result={
            "kind": "conversion.result",
            "schema": 1,
            "job_id": "job-proj",
            "output_format": "markdown",
            "result_path": str(result_path),
        },
        result_hash="0" * 64,
        fencing_token=1,
        owner_id="sem-owner",
        accepted_at=None,
    )


class TestCompatibilityProjectionCapabilityFacts:
    """Facts backing compatibility.conversion_job_row = exactly_once derived."""

    pytestmark = pytest.mark.asyncio

    async def _coordinator(self, env) -> KernelRuntimeCoordinator:
        return KernelRuntimeCoordinator(
            None, session_factory=env.factory, workspace_id="sem"
        )

    async def _make_row(self, env, status: str = "pending") -> None:
        async with env.factory() as session:
            session.add(
                ConversionJob(
                    id="job-proj",
                    filename="doc.pdf",
                    original_name="doc.pdf",
                    status=status,
                    input_format="pdf",
                    output_format="markdown",
                    config_json="{}",
                    queue_backend="kernel",
                )
            )
            await session.commit()

    async def test_projection_projects_accepted_truth_once(
        self, semantics_env, tmp_path: Path
    ) -> None:
        env = semantics_env
        await self._make_row(env)
        result_path = tmp_path / "proj.md"
        result_path.write_text("projected truth", encoding="utf-8")
        coordinator = await self._coordinator(env)
        assert await coordinator._project_publication(
            "job-proj", _fake_publication(11, result_path)
        )
        async with env.factory() as session:
            row = await session.get(ConversionJob, "job-proj")
        assert row.status == "completed"
        assert row.result_text == "projected truth"

    async def test_replay_on_terminal_row_is_a_guarded_no_op(
        self, semantics_env, tmp_path: Path
    ) -> None:
        env = semantics_env
        await self._make_row(env)
        result_path = tmp_path / "proj.md"
        result_path.write_text("projected truth", encoding="utf-8")
        coordinator = await self._coordinator(env)
        assert await coordinator._project_publication(
            "job-proj", _fake_publication(12, result_path)
        )
        # Replay after the row is terminal converges without rewriting.
        assert not await coordinator._project_publication(
            "job-proj", _fake_publication(12, result_path)
        )

    async def test_projection_refuses_to_overwrite_terminal_failure(
        self, semantics_env, tmp_path: Path
    ) -> None:
        # A cancelled/failed row can never be flipped completed by a
        # late projection — terminal truth is guarded.
        env = semantics_env
        await self._make_row(env, status="cancelled")
        result_path = tmp_path / "proj.md"
        result_path.write_text("late truth", encoding="utf-8")
        coordinator = await self._coordinator(env)
        assert not await coordinator._project_publication(
            "job-proj", _fake_publication(13, result_path)
        )
        async with env.factory() as session:
            row = await session.get(ConversionJob, "job-proj")
        assert row.status == "cancelled"
        assert row.result_text is None
