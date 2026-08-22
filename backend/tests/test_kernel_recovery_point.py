"""PR83C1 Workstream B: coherent recovery-point capture over real services.

Every test here runs against real PostgreSQL and the real S3-compatible
object store (strict-mode aware skipping; the strict industrial runner
refuses the skip). The suite proves the capture contract: an exact
semantic cut, verified payload + source closure, GC-linearized copy
safety, crash-safe discoverability, and convergent retry — plus the
truthful refusals (degraded cut, tampered manifest, damaged dump).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.gc import execute_collection, plan_collection
from app.kernel.models import KernelPayloadRetirement
from app.kernel.payloads import payload_byte_hash
from app.kernel.records import ObservationRecord
from app.kernel.recovery import (
    CAPTURE_FAULT_PHASES,
    PHASE_CAP_PAYLOAD_COPIED,
    RecoveryCaptureError,
    RecoveryManifestError,
    capture_recovery_point,
    enumerate_payload_closure,
    load_recovery_point,
    verify_backup_objects,
)
from tests.recovery_drills import recovery_workspace

pytestmark = pytest.mark.asyncio

GC_RACE_WORKSPACE = "recovery-gc-race"


async def _capture(ws, backup_root: Path, **kwargs):
    payload_backup, source_backup = ws.ensure_backup_stores()
    return await capture_recovery_point(
        ws.session_factory,
        workspace_id=ws.workspace_id,
        payload_store=ws.payload_store,
        source_store=ws.source_store,
        backup_payload_store=payload_backup,
        backup_source_store=source_backup,
        pg_tools=ws.pg_tools,
        database_name=ws.database_name,
        backup_root=backup_root,
        **kwargs,
    )


async def test_capture_produces_verified_manifest(tmp_path: Path) -> None:
    async with recovery_workspace(tmp_path) as ws:
        backup_root = tmp_path / "backups"
        manifest = await _capture(ws, backup_root)
        assert manifest.kernel_cut == await ws.head()
        assert manifest.kernel_cut > 0
        assert manifest.required_payload_state == "replayable"
        assert manifest.snapshot_id.startswith("sha256:")
        # database artifact evidence
        assert manifest.database["dump_bytes"] > 0
        dump_path = (
            backup_root / manifest.recovery_point_id.split(":", 1)[1] / "database.pgdump"
        )
        assert dump_path.is_file()
        assert dump_path.stat().st_size == manifest.database["dump_bytes"]
        assert "PostgreSQL" in manifest.database["pg_version"]
        # closure evidence: 2 payload objects + 2 source artifacts
        assert manifest.durations.payload_object_count == 2
        assert manifest.durations.source_object_count == 2
        assert len(manifest.payload_store["objects"]) == 2
        assert len(manifest.source_store["objects"]) == 2
        expected_source_keys = {
            block["blob_key"] for block in ws.source_blocks
        }
        assert {o["blob_key"] for o in manifest.source_store["objects"]} == (
            expected_source_keys
        )
        # publication evidence
        assert len(manifest.publications) == 1
        assert manifest.publications[0].publication_set_id
        assert manifest.publications[0].lexical_generation_id
        assert manifest.publications[0].fts_table
        # durations recorded (operational tax)
        assert manifest.durations.quiesce_seconds > 0
        assert manifest.durations.dump_seconds > 0

        loaded = load_recovery_point(backup_root, manifest.recovery_point_id)
        assert loaded.manifest.recovery_point_id == manifest.recovery_point_id
        report = await verify_backup_objects(
            loaded,
            backup_payload_store=ws._backup_payload,
            backup_source_store=ws._backup_source,
        )
        assert report.ready, report.problems


async def test_capture_is_idempotent_and_convergent(tmp_path: Path) -> None:
    async with recovery_workspace(tmp_path) as ws:
        backup_root = tmp_path / "backups"
        first = await _capture(ws, backup_root)
        second = await _capture(ws, backup_root)
        # same semantic point -> same identity, converged (not recaptured)
        assert second.recovery_point_id == first.recovery_point_id
        assert second.captured_at == first.captured_at
        assert len(list(backup_root.iterdir())) == 1


async def test_interrupted_capture_is_not_discoverable(tmp_path: Path) -> None:
    async with recovery_workspace(tmp_path) as ws:
        backup_root = tmp_path / "backups"
        with pytest.raises(RecoveryCaptureError, match="rec-payload-copied"):
            await _capture(ws, backup_root, _inject_fault_at=PHASE_CAP_PAYLOAD_COPIED)
        # nothing complete exists: capture cleanup removes its staging,
        # and whatever remains is never a discoverable recovery point
        entries = [e.name for e in backup_root.iterdir()]
        assert all(e.startswith(".staging-") for e in entries)
        # a staging residue, if present, is not loadable under any identity
        for entry in backup_root.iterdir():
            with pytest.raises(RecoveryManifestError):
                load_recovery_point(backup_root, f"sha256:{entry.name}")

        # retry converges to a complete, verified recovery point — and
        # discards any hard-crash staging residue (kill -9 leaves it)
        (backup_root / ".staging-deadbeef").mkdir()
        (backup_root / ".staging-deadbeef" / "junk").write_bytes(b"x")
        manifest = await _capture(ws, backup_root)
        loaded = load_recovery_point(backup_root, manifest.recovery_point_id)
        report = await verify_backup_objects(
            loaded,
            backup_payload_store=ws._backup_payload,
            backup_source_store=ws._backup_source,
        )
        assert report.ready, report.problems
        assert not any(
            e.name.startswith(".staging-") for e in backup_root.iterdir()
        )


async def test_gc_cannot_delete_closure_inside_quiescence(tmp_path: Path) -> None:
    """R7: a GC run that is fully eligible still cannot delete bytes a
    capture window protects — the payload-decision advisory scope is the
    linearization point, and after the window closes the collector may
    take the originals while the backup copies stand."""
    from app.kernel.recovery import _CaptureQuiescence

    async with recovery_workspace(tmp_path) as ws:
        # a second workspace with NO generation/publication roots: its
        # payload bytes are GC-eligible by plan, yet still required by
        # any recovery point over that workspace's cut
        orphan_payload = b"GC-RACE-ORPHAN-" + b"x" * 48
        orphan_key = payload_byte_hash(orphan_payload)
        service = KernelCommitService(
            ws.session_factory, payload_store=ws.payload_store
        )
        await service.commit(
            KernelCommitBatch(
                workspace_id=GC_RACE_WORKSPACE,
                records=(
                    ObservationRecord(
                        observer="drill.gc-race",
                        derivation={"step": "gc-race"},
                        payload_bytes=orphan_payload,
                    ),
                ),
            )
        )
        plan = await plan_collection(ws.session_factory, ws.payload_store)
        assert orphan_key in plan.candidate_registry_keys

        session = ws.session_factory()
        try:
            async with _CaptureQuiescence(session):
                collector = asyncio.create_task(
                    execute_collection(ws.session_factory, ws.payload_store, plan)
                )
                await asyncio.sleep(1.0)
                # the collector is blocked on the advisory scope: no
                # tombstone has been linearized
                assert not collector.done(), (
                    "execute_collection must block on the payload-decision "
                    "advisory scope while a capture holds it"
                )
                async with ws.session_factory() as check:
                    retirements = len(
                        (await check.execute(select(KernelPayloadRetirement.blob_key))).all()
                    )
                assert retirements == 0
                # the protected bytes are still readable for the copy
                data = await ws.payload_store.read(orphan_key)
                assert data == orphan_payload
            # window closed: the collector proceeds and may take originals
            report = await asyncio.wait_for(collector, timeout=30.0)
            assert report is not None
            check = await ws.payload_store.check_object(orphan_key)
            assert not check.available
        finally:
            await session.close()


async def _closure_payload_keys(ws) -> list[dict]:
    refs = await enumerate_payload_closure(
        ws.session_factory, ws.workspace_id, await ws.head()
    )
    return [r.as_dict() for r in refs]


async def test_capture_refuses_degraded_cut(tmp_path: Path) -> None:
    async with recovery_workspace(tmp_path) as ws:
        # destroy one required payload byte before capture
        objects = [o["blob_key"] for o in await _closure_payload_keys(ws)]
        await ws.payload_store.delete_object(objects[0])
        backup_root = tmp_path / "backups"
        with pytest.raises(RecoveryCaptureError, match="degraded|complete"):
            await _capture(ws, backup_root)
        # and no recovery point became discoverable
        if backup_root.exists():
            assert all(e.name.startswith(".staging-") for e in backup_root.iterdir())


async def test_manifest_tamper_and_dump_damage_refused(tmp_path: Path) -> None:
    async with recovery_workspace(tmp_path) as ws:
        backup_root = tmp_path / "backups"
        manifest = await _capture(ws, backup_root)
        final = backup_root / manifest.recovery_point_id.split(":", 1)[1]

        # tampered manifest: declared cut no longer matches identity
        raw = json.loads((final / "recovery-manifest.json").read_text())
        raw["kernel_cut"] = manifest.kernel_cut + 1
        (final / "recovery-manifest.json").write_text(json.dumps(raw))
        with pytest.raises(RecoveryManifestError):
            load_recovery_point(backup_root, manifest.recovery_point_id)

        # restore the manifest, then truncate the dump
        raw["kernel_cut"] = manifest.kernel_cut
        (final / "recovery-manifest.json").write_text(json.dumps(raw))
        dump = final / "database.pgdump"
        dump.write_bytes(dump.read_bytes()[:-64])
        with pytest.raises(RecoveryManifestError, match="digest"):
            load_recovery_point(backup_root, manifest.recovery_point_id)


async def test_fault_phase_vocabulary_is_exact(tmp_path: Path) -> None:
    """Every declared capture fault phase fires where it claims — the
    crash-window vocabulary is pinned, not aspirational."""
    async with recovery_workspace(tmp_path) as ws:
        for phase in sorted(CAPTURE_FAULT_PHASES):
            backup_root = tmp_path / f"backups-{phase}"
            with pytest.raises(RecoveryCaptureError):
                await _capture(ws, backup_root, _inject_fault_at=phase)
            # a fault at ANY phase leaves no discoverable recovery point
            if backup_root.exists():
                assert all(
                    e.name.startswith(".staging-") for e in backup_root.iterdir()
                )
