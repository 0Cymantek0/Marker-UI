"""PR83C1 Workstream E: disaster restore drill on fresh services.

The drill removes every hidden dependence before the oracle runs: the
original database is dropped, the original object namespaces deleted,
the restored topology lives in a fresh PostgreSQL database and fresh
object namespaces seeded only from the verified backup. Passing can
therefore only mean the recovery point actually restores coherent
state. Tail semantics, corrupted-component refusal, missing-dependency
degradation, and post-recovery writability are each proved separately.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.generations import GenerationService
from app.kernel.object_store import S3StoreConfig, s3_request_headers, s3_url
from app.kernel.publications import PublicationService, open_published_reader
from app.kernel.records import ObservationRecord
from app.kernel.recovery import (
    capture_recovery_point,
    enumerate_source_closure,
    load_recovery_point,
    verify_recovery,
)
from app.kernel.snapshots import resolve_snapshot
from tests.recovery_drills import recovery_workspace, restore_to_fresh_services
from tests.s3_provisioning import require_s3_env

pytestmark = pytest.mark.asyncio


async def _capture(ws, backup_root: Path):
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
    )


async def test_disaster_restore_to_fresh_services_passes_oracle(
    tmp_path: Path,
) -> None:
    """R10 + R18: full destructive restore, oracle green, then a new
    legitimate commit lands under the restored authority."""
    async with recovery_workspace(tmp_path) as ws:
        manifest = await _capture(ws, tmp_path / "backups")
        loaded = load_recovery_point(tmp_path / "backups", manifest.recovery_point_id)

        target = await restore_to_fresh_services(ws, loaded, tmp_path)
        try:
            report = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=manifest,
                payload_store=target.payload_store,
                source_store=target.source_store,
                expected_query=ws.query_expectation,
            )
            assert report.ready, report.problems
            # every component is explicitly green, not merely silent
            names = {c.name for c in report.checks}
            assert names == {
                "database",
                "cut",
                "payload_closure",
                "source_closure",
                "publication",
                "ownership",
            }

            # source continuity: a fresh node materializes committed
            # bytes from the RESTORED namespace, original path unneeded
            block = ws.source_blocks[0]
            materialized = await target.source_store.materialize_to(
                block["blob_key"],
                block["suffix"],
                tmp_path / "fresh-node" / "source.pdf",
            )
            assert materialized.stat().st_size == block["byte_length"]

            # post-recovery write under the restored authority (R18)
            restored_commits = KernelCommitService(
                target.session_factory, payload_store=target.payload_store
            )
            receipt = await restored_commits.commit(
                KernelCommitBatch(
                    workspace_id=ws.workspace_id,
                    records=(
                        ObservationRecord(
                            observer="drill.post-restore",
                            derivation={"step": "post-restore-write"},
                            payload_bytes=b"POST-RESTORE-TRUTH-" + b"z" * 32,
                        ),
                    ),
                )
            )
            assert receipt.kernel_commit_id == manifest.kernel_cut + 1
            # and the published query still serves deterministically
            reader = await open_published_reader(
                target.session_factory, ws.workspace_id
            )
            assert reader is not None
            try:
                hits = await reader.search(
                    ws.query_expectation["text"], ws.query_expectation["mode"]
                )
                assert [h.record_id for h in hits] == ws.query_expectation[
                    "expected_record_ids"
                ]
            finally:
                await reader.close()
        finally:
            await target.close()


async def test_restore_to_earlier_point_has_exact_tail_semantics(
    tmp_path: Path,
) -> None:
    """R11: a recovery point captured at K1 restores K1 exactly — state
    written afterward (K2) is absent, never half-present, and the RPO
    commit delta is explicit."""
    async with recovery_workspace(tmp_path) as ws:
        rp1 = await _capture(ws, tmp_path / "backups")

        # post-point writes: a new payload-backed commit + new source
        tail_payload = b"TAIL-AFTER-RP1-" + b"t" * 40
        await ws.commit(
            KernelCommitBatch(
                workspace_id=ws.workspace_id,
                records=(
                    ObservationRecord(
                        observer="drill.tail",
                        derivation={"step": "tail"},
                        payload_bytes=tail_payload,
                    ),
                ),
            )
        )
        tail_source = tmp_path / "source-tail.pdf"
        tail_source.write_bytes(b"%PDF-1.4 TAIL SOURCE")
        await ws.acquire_source(tail_source, job_id="drill-src-tail")
        assert await ws.head() == rp1.kernel_cut + 2

        rp2 = await _capture(ws, tmp_path / "backups")
        assert rp2.kernel_cut == rp1.kernel_cut + 2
        assert rp2.recovery_point_id != rp1.recovery_point_id

        # disaster: restore the EARLIER declared point
        loaded = load_recovery_point(tmp_path / "backups", rp1.recovery_point_id)
        target = await restore_to_fresh_services(ws, loaded, tmp_path)
        try:
            report = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=rp1,
                payload_store=target.payload_store,
                source_store=target.source_store,
                expected_query=ws.query_expectation,
            )
            assert report.ready, report.problems

            # tail is absent — no record, no source revision, no head row
            async with target.session_factory() as session:
                head = await session.scalar(
                    text("SELECT head_kernel_commit_id FROM kernel_commit_heads")
                )
                tail_records = await session.scalar(
                    text(
                        "SELECT count(*) FROM kernel_records "
                        "WHERE kernel_commit_id > :cut"
                    ),
                    {"cut": rp1.kernel_cut},
                )
                assert head == rp1.kernel_cut
                assert tail_records == 0
            # the tail source revision is not part of restored closure
            closure = await enumerate_source_closure(
                target.session_factory, ws.workspace_id, rp1.kernel_cut
            )
            tail_keys = {o["blob_key"] for o in rp2.source_store["objects"]} - {
                o["blob_key"] for o in rp1.source_store["objects"]
            }
            assert tail_keys
            assert not (tail_keys & {ref.blob_key for ref in closure})
        finally:
            await target.close()


async def test_missing_required_payload_object_degrades_not_ready(
    tmp_path: Path,
) -> None:
    """R12: restore with one required payload byte missing reports
    degraded and is never ready — no fallback fabricates completeness."""
    async with recovery_workspace(tmp_path) as ws:
        manifest = await _capture(ws, tmp_path / "backups")
        loaded = load_recovery_point(tmp_path / "backups", manifest.recovery_point_id)

        target = await restore_to_fresh_services(ws, loaded, tmp_path)
        try:
            victim = manifest.payload_store["objects"][0]["blob_key"]
            await target.payload_store.delete_object(victim)
            report = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=manifest,
                payload_store=target.payload_store,
                source_store=target.source_store,
                expected_query=ws.query_expectation,
            )
            assert not report.ready
            payload_check = report.check("payload_closure")
            assert not payload_check.ok
            assert victim in payload_check.detail
            # the cut resolution is honestly degraded too: replayable
            # completeness cannot hold with required bytes missing
            assert not report.check("cut").ok
            assert report.check("database").ok
        finally:
            await target.close()


async def _corrupt_source_object(target, ref: dict) -> None:
    """Overwrite one restored source artifact with wrong bytes, acting
    as an independent writer against the restored namespace."""
    endpoint, access_key, secret_key = require_s3_env()
    config = S3StoreConfig(
        endpoint_url=endpoint,
        bucket=_target_bucket(target),
        access_key_id=access_key,
        secret_access_key=secret_key,
        prefix="kernel-sources",
    )
    hex_digest = ref["blob_key"].removeprefix("sha256:")
    path = f"/{config.bucket}/{config.prefix}/{hex_digest[:2]}/{hex_digest}{ref['suffix']}"
    data = b"CORRUPTED-BYTES"
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = s3_request_headers(config, "PUT", path, body=data)
        response = await client.put(s3_url(config, path), headers=headers, content=data)
        assert response.status_code in (200, 201), response.text


def _target_bucket(target) -> str:
    return target.source_store._config.bucket


async def test_corrupt_restored_source_object_fails_closed(
    tmp_path: Path,
) -> None:
    """R13: a corrupted source artifact makes source closure fail; there
    is no path fallback to the original external file."""
    async with recovery_workspace(tmp_path) as ws:
        manifest = await _capture(ws, tmp_path / "backups")
        loaded = load_recovery_point(tmp_path / "backups", manifest.recovery_point_id)
        target = await restore_to_fresh_services(ws, loaded, tmp_path)
        try:
            victim = manifest.source_store["objects"][0]
            await _corrupt_source_object(target, victim)
            report = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=manifest,
                payload_store=target.payload_store,
                source_store=target.source_store,
            )
            assert not report.ready
            assert not report.check("source_closure").ok
        finally:
            await target.close()


async def test_missing_lexical_physical_state_holds_readiness(
    tmp_path: Path,
) -> None:
    """R14: a restored publication pointer whose physical lexical table
    is gone cannot be advertised as ready — the oracle fails the
    publication component until the state is rebuilt."""
    async with recovery_workspace(tmp_path) as ws:
        manifest = await _capture(ws, tmp_path / "backups")
        loaded = load_recovery_point(tmp_path / "backups", manifest.recovery_point_id)
        target = await restore_to_fresh_services(ws, loaded, tmp_path)
        try:
            fts_table = manifest.publications[0].fts_table
            async with target.session_factory() as session:
                await session.execute(text(f'DROP TABLE IF EXISTS "{fts_table}"'))
                await session.commit()
            report = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=manifest,
                payload_store=target.payload_store,
                source_store=target.source_store,
                expected_query=ws.query_expectation,
            )
            assert not report.ready
            assert not report.check("publication").ok
            # everything else still reports honestly
            assert report.check("database").ok
            assert report.check("cut").ok

            # honest rebuild path: rebuild the derived serving state
            # from restored kernel truth AT THE RECORDED PUBLICATION'S
            # CUT (the intended lineage), then republish and re-verify
            recorded_cut = manifest.publications[0].kernel_commit_id
            snapshot = await resolve_snapshot(
                target.session_factory, ws.workspace_id, at_commit=recorded_cut
            )
            generation = await GenerationService(target.session_factory).build_and_activate(
                snapshot
            )
            await PublicationService(target.session_factory).publish(
                materialized_generation_id=generation.generation_id
            )
            rebuilt = await verify_recovery(
                target.session_factory,
                database_url=target.database_url,
                workspace_id=ws.workspace_id,
                manifest=manifest,
                payload_store=target.payload_store,
                source_store=target.source_store,
                expected_query=ws.query_expectation,
            )
            assert rebuilt.ready, rebuilt.problems
        finally:
            await target.close()
