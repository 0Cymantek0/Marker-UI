"""Synthetic Truth Kernel workload characterization (V3.2 PR63A +
PR64 + PR65A).

Runs a repeatable workload against a throwaway SQLite database and
content-addressed payload store, then prints a JSON report.

PR63A baseline fields: commit/record/edge counts, database and WAL
bytes, p50/p95 commit latency, replay + verification time, observed
SQLITE_BUSY/head-contention retries, SQLite runtime version/journal
mode.

PR64 durability fields (plan workstream F): payload staging latency
distribution for representative payload sizes, bytes written versus
logical payload bytes (write amplification incl. dedup reuse), commit
latency with and without payload-bearing records, availability scan and
restart-reconciliation cost, orphan/temp counts after an injected
pre-commit failure, and filesystem object-count growth for the workload.

PR65A generation fields: snapshot resolution latency (metadata-only and
inspectable), generation build/validate/activate latency, ready-read
p50/p95 (current resolution, manifest summary, record lookup, record
page), generation storage growth per source kernel record, deterministic
rebuild digest equality and cost, restart current-generation resolution
latency, and staging residue after an injected build fault.

PR65B retention/GC fields: dry-run plan counts (roots, live objects,
candidates, superseded generations), destructive pass outcomes
(rescued/tombstoned/swept/already-absent/failed, bytes reclaimed,
generations retired), mark/recheck/sweep durations, tracemalloc peak
during a full pass, availability summary after collection, and restart
tombstone-reconciliation cost on a fresh engine.

PR66 fencing/publication fields: uncontended claim-and-fence and
accepted-publication latency, fenced acknowledgement cost, a concurrent
dispatch stress (contending workers each claim -> accept -> complete),
stale-fence rejection cost after takeover, durable rows added per work
item, and restart authority/publication resolution latency on a fresh
engine.

No hard threshold is imposed; the report is the measured operating
envelope later PRs compare against and may then optimize.

Usage (from ``backend/``)::

    python scripts/kernel_characterization.py
    python scripts/kernel_characterization.py --commits 200 --records 25
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.db_migration import upgrade_database  # noqa: E402
from app.kernel import fencing  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.errors import InjectedFaultError, StaleFenceError  # noqa: E402
from app.kernel.gc import (  # noqa: E402
    execute_collection,
    plan_collection,
    reconcile_retirements,
)
from app.kernel.generations import (  # noqa: E402
    GenerationReader,
    GenerationService,
    resolve_current_generation,
    verify_generation,
)
from app.kernel.outbox import OutboxIntent  # noqa: E402
from app.kernel.payloads import LocalPayloadStore  # noqa: E402
from app.kernel.reconcile import reconcile_after_restart, verify_payload_availability  # noqa: E402
from app.kernel.records import (  # noqa: E402
    EDGE_KIND_EVIDENCE_FOR,
    ClaimAssertionRecord,
    KernelEdge,
    NativeFactRecord,
    NativeObjectRecord,
    ObservationRecord,
)
from app.kernel.replay import replay, verify_history  # noqa: E402
from app.kernel.retention import (  # noqa: E402
    ROOT_KIND_SNAPSHOT_HOLD,
    declare_hold,
)
from app.kernel.snapshots import (  # noqa: E402
    PAYLOAD_REQUIREMENT_INSPECTABLE,
    PAYLOAD_REQUIREMENT_METADATA_ONLY,
    resolve_snapshot,
)


def build_batch(index: int, records_per_commit: int, payload_bytes: bytes):
    records = []
    for j in range(records_per_commit):
        slot = j % 4
        if slot == 0:
            records.append(
                NativeObjectRecord(
                    source_uri=f"file:///docs/doc-{index}-{j}.pdf",
                    locator=f"pdf:obj:{j}",
                    media_type="application/pdf",
                    extractor_name="marker",
                    extractor_version="1.0.0",
                )
            )
        elif slot == 1:
            records.append(
                ObservationRecord(
                    observer="marker",
                    derivation={"commit": index, "record": j, "stage": "layout"},
                    payload_bytes=payload_bytes,
                )
            )
        elif slot == 2:
            records.append(
                ClaimAssertionRecord(
                    claim_key=f"claim-{index}-{j}",
                    subject=f"doc:doc-{index}-{j}.pdf",
                    predicate="contains_table",
                    value=True,
                )
            )
        else:
            records.append(
                NativeFactRecord(
                    native_object_ref=records[j - 3].record_id
                    if j >= 3
                    else records[0].record_id,
                    property_name="page.count",
                    raw_representation=str(index * 100 + j),
                    typed_interpretation=index * 100 + j,
                    extractor_name="marker",
                    extractor_version="1.0.0",
                )
            )
    edges = (
        KernelEdge(
            edge_kind=EDGE_KIND_EVIDENCE_FOR,
            source_ref=records[1].record_id,
            target_ref=records[2].record_id,
        ),
    )
    return KernelCommitBatch(
        workspace_id="bench", records=tuple(records), edges=edges
    )


async def _generation_section(db_dir: Path, url: str, db_path: Path) -> dict:
    """PR65A measurement block over the finished workload database."""
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalPayloadStore(db_dir / "payloads")
    commit_service = KernelCommitService(factory, payload_store=store)
    gen_service = GenerationService(factory)

    def samples(times: list[float]) -> dict:
        ordered = sorted(times)
        return {
            "samples": len(ordered),
            "p50_ms": round(statistics.median(ordered) * 1000, 2),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
            "mean_ms": round(statistics.mean(ordered) * 1000, 2),
        }

    # snapshot resolution: metadata-only vs inspectable (payload re-hash)
    meta_times: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        await resolve_snapshot(factory, "bench")
        meta_times.append(time.perf_counter() - t0)
    insp_times: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        insp_snapshot = await resolve_snapshot(
            factory,
            "bench",
            required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
            payload_store=store,
        )
        insp_times.append(time.perf_counter() - t0)

    # build (stage+validate) and activate as separate latency steps
    t0 = time.perf_counter()
    built = await gen_service.build(insp_snapshot)
    build_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    active = await gen_service.activate(built.generation_id)
    activate_seconds = time.perf_counter() - t0

    # deterministic rebuild equality + cost
    t0 = time.perf_counter()
    rebuilt = await gen_service.build(insp_snapshot)
    rebuild_seconds = time.perf_counter() - t0
    rebuild_digest_equal = rebuilt.content_digest == active.content_digest

    # ready-read paths (no kernel replay)
    reader = GenerationReader(factory, active.generation_id)
    sample_records = await reader.list_records(limit=20)
    probe_ids = [r.record_id for r in sample_records]
    read_current: list[float] = []
    read_summary: list[float] = []
    read_lookup: list[float] = []
    read_page: list[float] = []
    for i in range(50):
        probe = probe_ids[i % len(probe_ids)]
        t0 = time.perf_counter()
        await resolve_current_generation(factory, "bench")
        read_current.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        await reader.summary()
        read_summary.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        await reader.get_record(probe)
        read_lookup.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        await reader.list_records(limit=20)
        read_page.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    verification = await verify_generation(factory, active.generation_id)
    verify_seconds = time.perf_counter() - t0

    # injected build fault: staged residue must not disturb the current
    await commit_service.commit(
        build_batch(20_000, 4, b"generation fault probe" * 8)
    )
    fault_snapshot = await resolve_snapshot(factory, "bench")
    staged_residue = 0
    prior_still_current = False
    try:
        await gen_service.build_and_activate(
            fault_snapshot, _inject_fault_at="gen-staged"
        )
    except InjectedFaultError:
        staged = await gen_service.list_generations(state="staged")
        staged_residue = len(staged)
        current = await resolve_current_generation(factory, "bench")
        prior_still_current = (
            current is not None and current.generation_id == active.generation_id
        )

    await engine.dispose()

    # restart view: a brand-new process recovers the current generation
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(
        engine2, class_=AsyncSession, expire_on_commit=False
    )
    t0 = time.perf_counter()
    restarted = await resolve_current_generation(factory2, "bench")
    restart_current_seconds = time.perf_counter() - t0
    await engine2.dispose()

    with closing(sqlite3.connect(db_path)) as conn:
        gen_rows = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload_json)), 0), "
            "COALESCE(SUM(LENGTH(payload_byte_hash)), 0) "
            "FROM kernel_generation_records WHERE generation_id = ?",
            (active.generation_id,),
        ).fetchone()
        kernel_rows = conn.execute(
            "SELECT COUNT(*) FROM kernel_records WHERE workspace_id = 'bench'"
        ).fetchone()[0]
    gen_record_rows, gen_payload_bytes, gen_hash_bytes = gen_rows
    gen_bytes_total = gen_payload_bytes + gen_hash_bytes + 220 * gen_record_rows

    return {
        "fixture": {
            "generation_cut": active.kernel_commit_id,
            "kernel_records_in_workspace": kernel_rows,
            "materialized_records": active.record_count,
            "materialized_edges": active.edge_count,
        },
        "snapshot_resolution": {
            "metadata_only": samples(meta_times),
            "inspectable_full_hash": samples(insp_times),
            "inspectable_completeness": insp_snapshot.completeness,
        },
        "lifecycle_seconds": {
            "build_stage_and_validate": round(build_seconds, 3),
            "atomic_activate": round(activate_seconds, 3),
            "deterministic_rebuild": round(rebuild_seconds, 3),
            "verify_generation": round(verify_seconds, 3),
            "restart_current_resolution": round(restart_current_seconds, 3),
        },
        "ready_read_ms": {
            "current_generation_resolution": samples(read_current),
            "manifest_summary": samples(read_summary),
            "record_lookup": samples(read_lookup),
            "record_page_20": samples(read_page),
        },
        "storage": {
            "generation_record_rows": gen_record_rows,
            "generation_payload_json_bytes": gen_payload_bytes,
            "estimated_bytes_per_materialized_record": (
                round(gen_bytes_total / gen_record_rows, 1) if gen_record_rows else 0
            ),
            "estimated_bytes_per_kernel_record": (
                round(gen_bytes_total / kernel_rows, 1) if kernel_rows else 0
            ),
        },
        "determinism": {
            "rebuild_digest_equal": rebuild_digest_equal,
            "content_digest": active.content_digest,
            "verify_ok": verification.ok,
        },
        "injected_build_fault": {
            "staged_residue_generations": staged_residue,
            "prior_generation_still_current": prior_still_current,
        },
        "restart_view": {
            "current_generation_recovered": restarted is not None
            and restarted.generation_id == active.generation_id,
        },
    }


async def _retention_section(db_dir: Path, url: str, db_path: Path) -> dict:
    """PR65B measurement block over the finished workload database.

    Scenario: the bench workspace's current generation is rebuilt at the
    head as metadata-only, an inspectable hold protects the older half
    of history, fresh unreachable payload commits plus one staged orphan
    provide reclaimable bytes, then a full mark/recheck/sweep pass runs
    under tracemalloc and a fresh engine performs restart reconciliation.
    """
    import tracemalloc

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalPayloadStore(db_dir / "payloads")
    commit_service = KernelCommitService(factory, payload_store=store)
    gen_service = GenerationService(factory)

    from app.kernel.replay import read_head

    head = await read_head(factory, "bench")
    objects_before = len(await store.list_objects())
    conn = sqlite3.connect(db_path)
    try:
        generations_before = conn.execute(
            "SELECT state, COUNT(*) FROM kernel_generations GROUP BY state"
        ).fetchall()
        registry_before = conn.execute(
            "SELECT COUNT(*) FROM kernel_payload_objects"
        ).fetchone()[0]
    finally:
        conn.close()

    # an inspectable hold keeps the older half of history's bytes alive
    hold_cut = max(head // 2, 1)
    hold = await declare_hold(
        factory,
        workspace_id="bench",
        root_kind=ROOT_KIND_SNAPSHOT_HOLD,
        kernel_commit_id=hold_cut,
        required_payload_state=PAYLOAD_REQUIREMENT_INSPECTABLE,
    )

    # the workspace drops to metadata-only serving: bytes beyond the
    # hold's cut become eligible
    meta_snapshot = await resolve_snapshot(
        factory, "bench", required_payload_state=PAYLOAD_REQUIREMENT_METADATA_ONLY
    )
    await gen_service.build_and_activate(meta_snapshot)

    # reclaimable input: fresh unreachable payload commits + one orphan
    junk = 10
    for i in range(junk):
        record = ObservationRecord(
            observer="gc-bench",
            derivation={"case": "retention", "i": i},
            payload_bytes=f"gc-reclaim-probe-{i}".encode() + b"x" * 512,
        )
        await commit_service.commit(
            KernelCommitBatch(workspace_id="bench", records=(record,))
        )
    await store.stage(b"gc-orphan-probe" + os.urandom(8))

    tracemalloc.start()
    t0 = time.perf_counter()
    plan = await plan_collection(factory, store)
    mark_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    report = await execute_collection(factory, store, plan)
    execute_seconds = time.perf_counter() - t0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    current = await resolve_current_generation(factory, "bench")
    assert current is not None
    verify_ok = (await verify_generation(factory, current.generation_id)).ok
    history_ok = (await verify_history(factory, "bench")).ok
    availability = await verify_payload_availability(factory, store, workspace_id="bench")
    objects_after = len(await store.list_objects())
    await engine.dispose()

    # restart view: a fresh engine reconciles any tombstone residue
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    store2 = LocalPayloadStore(db_dir / "payloads")
    t0 = time.perf_counter()
    restarted = await reconcile_retirements(factory2, store2)
    restart_reconcile_seconds = time.perf_counter() - t0
    await engine2.dispose()

    return {
        "fixture": {
            "head_before": head,
            "hold_cut": hold_cut,
            "generations_before": dict(generations_before),
            "registry_objects_before": registry_before,
            "objects_before": objects_before,
            "unreachable_commits_added": junk,
            "orphan_staged": 1,
        },
        "plan": plan.summary(),
        "pass": report.summary(),
        "outcomes": {
            "objects_after": objects_after,
            "bytes_reclaimed": report.bytes_reclaimed,
            "generations_retired": report.generations_retired,
            "rescued_count": report.rescued_count,
            "failed_count": len(report.failed_keys),
            "busy_retries": report.busy_retries,
            "expired_pins_purged": report.expired_pins_purged,
        },
        "integrity": {
            "hold_active": hold.active,
            "current_generation_ok": verify_ok,
            "history_ok": history_ok,
            "record_state_summary_after": availability.summary(),
            "restart_reconcile_swept": restarted.swept_deleted
            + restarted.already_absent,
        },
        "timing_seconds": {
            "mark": round(mark_seconds, 3),
            "recheck_tombstone_sweep": round(execute_seconds, 3),
            "restart_reconcile": round(restart_reconcile_seconds, 3),
            "tracemalloc_peak_kib": round(peak_bytes / 1024, 1),
        },
    }


async def _fencing_section(db_dir: Path, url: str, db_path: Path) -> dict:
    """PR66 measurement block over the finished workload database.

    Scenario: fresh outbox work is dispatched through the fencing
    boundary uncontended (claim -> accept -> complete), then a
    concurrent dispatch stress runs contending workers over distinct
    work items, then a stale worker's post-takeover rejection is timed,
    and a fresh engine resolves authority/publication truth.
    """
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)

    def _pct(timings: list[float]) -> dict:
        ordered = sorted(timings)
        return {
            "samples": len(ordered),
            "p50_ms": round(statistics.median(ordered) * 1000, 2),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
            "mean_ms": round(statistics.mean(ordered) * 1000, 2),
        }

    async def _make_work(tag: str) -> int:
        await service.commit(
            KernelCommitBatch(
                workspace_id="bench",
                records=(
                    ClaimAssertionRecord(
                        claim_key=f"fence-{tag}",
                        subject=f"doc:{tag}.pdf",
                        predicate="contains_table",
                        value=True,
                    ),
                ),
                outbox=(OutboxIntent(work_kind="materialize", payload={"tag": tag}),),
            )
        )
        from app.kernel.outbox import list_outbox

        rows = [r for r in await list_outbox(factory) if r.payload.get("tag") == tag]
        assert len(rows) == 1
        return rows[0].id

    # --- uncontended dispatch through the fencing boundary -----------
    # Drain any pending rows the earlier sections left behind so the
    # measured samples below are exactly this section's own work.
    drained = 0
    while True:
        residue = await fencing.claim_next(factory, owner_id="bench-drain")
        if residue is None:
            break
        await fencing.accept(
            factory,
            work_id=residue.work_id,
            fencing_token=residue.lease.fencing_token,
            result={"drained": True},
        )
        assert await fencing.complete_work(
            factory,
            work_id=residue.work_id,
            fencing_token=residue.lease.fencing_token,
        )
        drained += 1

    uncontended = 24
    claim_times: list[float] = []
    accept_times: list[float] = []
    complete_times: list[float] = []
    uncontended_ids = []
    for i in range(uncontended):
        work_id = await _make_work(f"uc-{i}")
        uncontended_ids.append(work_id)
        t0 = time.perf_counter()
        claimed = await fencing.claim_next(factory, owner_id="bench-solo")
        claim_times.append(time.perf_counter() - t0)
        assert claimed is not None and claimed.work_id == work_id
        t0 = time.perf_counter()
        await fencing.accept(
            factory,
            work_id=work_id,
            fencing_token=claimed.lease.fencing_token,
            result={"item": i, "pages": 4},
        )
        accept_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        assert await fencing.complete_work(
            factory, work_id=work_id, fencing_token=claimed.lease.fencing_token
        )
        complete_times.append(time.perf_counter() - t0)

    # --- concurrent dispatch stress: contending workers, distinct work -
    stress_items = 32
    stress_workers = 4
    stress_ids = {
        await _make_work(f"st-{i}"): i for i in range(stress_items)
    }
    e2e_times: list[float] = []

    async def _stress_worker(worker: int) -> None:
        while stress_ids:
            t0 = time.perf_counter()
            claimed = await fencing.claim_next(
                factory, owner_id=f"bench-worker-{worker}"
            )
            if claimed is None:
                # everything claimable is in flight elsewhere; yield
                await asyncio.sleep(0.005)
                continue
            stress_ids.pop(claimed.work_id, None)
            await fencing.accept(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
                result={"worker": worker},
            )
            assert await fencing.complete_work(
                factory,
                work_id=claimed.work_id,
                fencing_token=claimed.lease.fencing_token,
            )
            e2e_times.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    await asyncio.gather(*(_stress_worker(w) for w in range(stress_workers)))
    stress_seconds = time.perf_counter() - t0

    # --- stale-fence rejection after takeover --------------------------
    stale_id = await _make_work("stale-probe")
    stale_lease = await fencing.acquire(
        factory, work_id=stale_id, owner_id="bench-stale", lease_seconds=0.05
    )
    await asyncio.sleep(0.07)
    successor = await fencing.acquire(
        factory, work_id=stale_id, owner_id="bench-successor"
    )
    assert successor.fencing_token == stale_lease.fencing_token + 1
    t0 = time.perf_counter()
    stale_rejected = False
    try:
        await fencing.accept(
            factory,
            work_id=stale_id,
            fencing_token=stale_lease.fencing_token,
            result={"stale": True},
        )
    except StaleFenceError:
        stale_rejected = True
    stale_reject_seconds = time.perf_counter() - t0
    assert stale_rejected
    await fencing.accept(
        factory, work_id=stale_id, fencing_token=successor.fencing_token, result={}
    )

    await engine.dispose()

    # --- restart: fresh engine resolves durable authority ---------------
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    t0 = time.perf_counter()
    restarted_lease = await fencing.get_lease(factory2, work_id=uncontended_ids[0])
    restarted_publication = await fencing.get_publication(
        factory2, work_id=uncontended_ids[0]
    )
    restart_resolve_seconds = time.perf_counter() - t0
    await engine2.dispose()

    conn = sqlite3.connect(db_path)
    try:
        lease_rows = conn.execute("SELECT COUNT(*) FROM kernel_work_leases").fetchone()[0]
        publication_rows = conn.execute(
            "SELECT COUNT(*) FROM kernel_publications"
        ).fetchone()[0]
        outbox_done = conn.execute(
            "SELECT COUNT(*) FROM kernel_outbox WHERE state = 'done'"
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "fixture": {
            "residue_items_drained": drained,
            "uncontended_items": uncontended,
            "stress_items": stress_items,
            "stress_workers": stress_workers,
        },
        "uncontended_latency_ms": {
            "claim_next_incl_fence": _pct(claim_times),
            "accept_publication": _pct(accept_times),
            "complete_work": _pct(complete_times),
        },
        "concurrent_stress": {
            "items_per_worker_e2e_ms": _pct(e2e_times),
            "wall_seconds": round(stress_seconds, 3),
        },
        "stale_fence": {
            "rejected": stale_rejected,
            "reject_seconds": round(stale_reject_seconds, 4),
        },
        "rows_added_per_work": {
            "kernel_work_leases": 1,
            "kernel_publications": 1,
        },
        "durable_rows": {
            "kernel_work_leases": lease_rows,
            "kernel_publications": publication_rows,
            "outbox_done": outbox_done,
            "exactly_one_publication_per_accepted_work": (
                lease_rows == publication_rows
                == drained + uncontended + stress_items + 1
            ),
        },
        "restart": {
            "lease_resolved": restarted_lease is not None,
            "publication_resolved": restarted_publication is not None,
            "resolve_seconds": round(restart_resolve_seconds, 4),
        },
    }


async def run(
    db_dir: Path,
    *,
    commits: int,
    records_per_commit: int,
    concurrent_writers: int,
) -> dict:
    db_path = db_dir / "kernel-bench.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    started = time.perf_counter()
    await upgrade_database(url=url)
    migration_seconds = round(time.perf_counter() - started, 3)

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalPayloadStore(db_dir / "payloads")
    service = KernelCommitService(factory, payload_store=store)
    payload_bytes = b"x" * 512

    # --- PR64: staging latency distribution by payload size ----------
    staging_profile: dict[str, dict] = {}
    staging_samples = 20
    for label, size in (("1KiB", 1024), ("64KiB", 64 * 1024), ("1MiB", 1024 * 1024)):
        probe_store = LocalPayloadStore(db_dir / f"probe-{label}")
        sample = os.urandom(size)
        timings: list[float] = []
        for i in range(staging_samples):
            data = sample if i == 0 else sample + i.to_bytes(4, "big") + b"tail"
            t0 = time.perf_counter()
            await probe_store.stage(data)
            timings.append(time.perf_counter() - t0)
        staging_profile[label] = {
            "bytes": size,
            "samples": staging_samples,
            "p50_ms": round(statistics.median(timings) * 1000, 2),
            "p95_ms": round(sorted(timings)[int(len(timings) * 0.95) - 1] * 1000, 2),
            "mean_ms": round(statistics.mean(timings) * 1000, 2),
        }

    # --- workload: metadata-only commits vs payload-bearing commits ---
    latencies_meta: list[float] = []
    latencies_payload: list[float] = []
    sem = asyncio.Semaphore(concurrent_writers)

    async def one_commit(index: int, with_payload: bool) -> None:
        payload = None
        if with_payload:
            # Unique content per commit: staging, registry, and object
            # counts then reflect real payload-bearing work, not dedup.
            payload = payload_bytes + index.to_bytes(8, "big")
        batch = build_batch(index, records_per_commit, payload)
        if index == 0:
            batch.outbox = (OutboxIntent(work_kind="materialize", payload={"i": index}),)
        async with sem:
            t0 = time.perf_counter()
            await service.commit(batch)
            (latencies_payload if with_payload else latencies_meta).append(
                time.perf_counter() - t0
            )

    started = time.perf_counter()
    await asyncio.gather(
        *(one_commit(i, with_payload=i % 2 == 0) for i in range(commits))
    )
    workload_seconds = round(time.perf_counter() - started, 3)

    def _lat(list_: list[float]) -> dict:
        if not list_:
            return {"p50_ms": 0.0, "p95_ms": 0.0, "mean_ms": 0.0, "commits": 0}
        ordered = sorted(list_)
        return {
            "commits": len(ordered),
            "p50_ms": round(statistics.median(ordered) * 1000, 2),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
            "mean_ms": round(statistics.mean(ordered) * 1000, 2),
        }

    # --- PR64: availability scan + restart reconciliation cost --------
    t0 = time.perf_counter()
    availability = await verify_payload_availability(factory, store, workspace_id="bench")
    availability_scan_seconds = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    await reconcile_after_restart(factory, store, tmp_older_than_seconds=0)
    reconcile_seconds = round(time.perf_counter() - t0, 3)

    # --- PR64: injected pre-commit failure residue ---------------------
    orphans_before = len(availability.orphan_objects)
    failed = 0
    for i in range(5):
        try:
            await service.commit(
                # records_per_commit must be >= 4: build_batch's edge
                # references records[2].
                build_batch(10_000 + i, 4, b"orphan probe" * 64 + i.to_bytes(4, "big")),
                _inject_fault_at="pre-commit",
            )
        except Exception:
            failed += 1
    residue = await verify_payload_availability(factory, store, workspace_id="bench")
    orphans_after_failure = len(residue.orphan_objects) - orphans_before
    tmp_residue_after_failure = len(residue.tmp_residue)

    # --- PR63A replay/verification ------------------------------------
    t0 = time.perf_counter()
    replayed = await replay(factory, "bench")
    replay_seconds = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    verification = await verify_history(factory, "bench")
    verify_seconds = round(time.perf_counter() - t0, 3)
    await engine.dispose()

    # --- fresh-process restart view over the same durable files -------
    engine2 = create_async_engine(url, connect_args={"check_same_thread": False})
    factory2 = async_sessionmaker(engine2, class_=AsyncSession, expire_on_commit=False)
    store2 = LocalPayloadStore(db_dir / "payloads")
    t0 = time.perf_counter()
    restart_availability = await verify_payload_availability(
        factory2, store2, workspace_id="bench"
    )
    restart_scan_seconds = round(time.perf_counter() - t0, 3)
    await engine2.dispose()

    wal_path = db_path.with_name(db_path.name + "-wal")

    # --- PR65A: snapshot + materialized generation envelope ------------
    generation_report = await _generation_section(db_dir, url, db_path)

    # --- PR65B: retention + garbage-collection envelope -----------------
    retention_report = await _retention_section(db_dir, url, db_path)

    # --- PR66: fenced ownership + accepted-publication envelope --------
    fencing_report = await _fencing_section(db_dir, url, db_path)

    conn = sqlite3.connect(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        payload_registry_rows = conn.execute(
            "SELECT COUNT(*) FROM kernel_payload_objects"
        ).fetchone()[0]
        outbox_rows = conn.execute(
            "SELECT COUNT(*) FROM kernel_outbox WHERE state = 'pending'"
        ).fetchone()[0]
    finally:
        conn.close()

    object_files = sum(1 for _ in (db_dir / "payloads" / "objects").rglob("*") if _.is_file())

    return {
        "workload": {
            "commits": commits,
            "records_per_commit": records_per_commit,
            "edges_per_commit": 1,
            "payload_bytes_per_observation": len(payload_bytes),
            "concurrent_writers": concurrent_writers,
            "commit_mix": "even metadata-only / payload-bearing",
        },
        "totals": {
            "records": verification.checked_records,
            "edges": verification.checked_edges,
            "replay_digest": replayed.replay_digest,
            "verification_ok": verification.ok,
        },
        "storage": {
            "db_bytes": db_path.stat().st_size,
            "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            "journal_mode": journal_mode,
            "page_count": page_count,
            "payload_object_files": object_files,
            "payload_registry_rows": payload_registry_rows,
        },
        "latency_ms": {
            "metadata_commits": _lat(latencies_meta),
            "payload_commits": _lat(latencies_payload),
            "all_commits": _lat(latencies_meta + latencies_payload),
        },
        "payload_durability": {
            "staging_by_size": staging_profile,
            "bytes_logical": store.bytes_logical,
            "bytes_written": store.bytes_written,
            "bytes_read_back": store.bytes_read_back,
            "write_amplification": (
                round(store.bytes_written / store.bytes_logical, 3)
                if store.bytes_logical
                else 0.0
            ),
            "dedup_hits": store.dedup_hits,
            "stage_calls": store.stage_calls,
            "availability": {
                "payload_backed_complete": availability.payload_backed_complete,
                "record_state_summary": availability.summary(),
                "orphan_objects": len(availability.orphan_objects),
                "tmp_residue": len(availability.tmp_residue),
            },
            "injected_failures": {
                "pre_commit_failures": failed,
                "orphan_objects_created": orphans_after_failure,
                "tmp_files_left": tmp_residue_after_failure,
            },
            "pending_outbox_rows": outbox_rows,
        },
        "duration_seconds": {
            "migration_to_head": migration_seconds,
            "commit_workload": workload_seconds,
            "availability_scan": availability_scan_seconds,
            "reconcile_after_restart": reconcile_seconds,
            "restart_availability_scan": restart_scan_seconds,
            "full_replay": replay_seconds,
            "full_verification": verify_seconds,
        },
        "contention": {
            "busy_retries": service.busy_retries,
            "head_retries": service.head_retries,
        },
        "restart_view": {
            "payload_backed_complete": restart_availability.payload_backed_complete,
            "record_state_summary": restart_availability.summary(),
        },
        "generations": generation_report,
        "retention_gc": retention_report,
        "fencing_publication": fencing_report,
        "runtime": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "sqlite_library": sqlite3.sqlite_version,
            "sqlite_runtime": sqlite3.connect(":memory:").execute("select sqlite_version()").fetchone()[0],
            "journal_mode": journal_mode,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commits", type=int, default=100)
    parser.add_argument("--records", dest="records_per_commit", type=int, default=12)
    parser.add_argument("--concurrency", dest="concurrent_writers", type=int, default=4)
    parser.add_argument("--keep", action="store_true", help="keep the scratch database")
    args = parser.parse_args()

    kwargs = {
        "commits": args.commits,
        "records_per_commit": args.records_per_commit,
        "concurrent_writers": args.concurrent_writers,
    }
    if args.keep:
        db_dir = Path(tempfile.mkdtemp(prefix="kernel-bench-"))
        report = asyncio.run(run(db_dir, **kwargs))
        print(json.dumps(report, indent=2))
        print(f"scratch dir kept: {db_dir}", file=sys.stderr)
    else:
        with tempfile.TemporaryDirectory(prefix="kernel-bench-") as tmp:
            report = asyncio.run(run(Path(tmp), **kwargs))
            print(json.dumps(report, indent=2))
    return 0 if report["totals"]["verification_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
