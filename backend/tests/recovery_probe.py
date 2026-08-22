"""PR83C1 OS-process failover probe (run as ``python -m tests.recovery_probe``).

Every mode drives the *real* kernel authorities — commit service,
scheduler, fencing, liveness-shaped claims, publications, source
acquisition/materialization — against the real PostgreSQL + S3 services
named by the environment. The test parent spawns this module as actual
OS processes and kills them at adversarial points; nothing here fakes a
process boundary.

Output protocol (one JSON line per event, stdout only):
``SETUP:{...}`` after fixture build; ``CLAIM:{...}`` once work is held;
``RENEWED:{...}``/``TERMINAL:{...}`` for pre-fault lease renewal and
terminal completion (PR83C2); ``COMMITS:{...}`` for timed durability-
class commit probes; ``MILESTONE:<name> <epoch>`` recovery milestones;
``RECOVERED:{...}`` full-takeover summary; ``STALE_REJECTED``/
``STALE_ACCEPTED:{...}`` for late stale-owner completions. Any honest
failure exits non-zero with the error on stderr — the parent asserts
both directions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _emit(line: str) -> None:
    print(line, flush=True)


def _milestone(name: str) -> None:
    _emit(f"MILESTONE:{name} {time.time():.6f}")


async def _build_engine_and_stores():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    url = os.environ["PROBE_DB_URL"]
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.kernel.object_store import S3PayloadStore, S3StoreConfig
    from app.kernel.source_object_store import S3SourceStore

    endpoint = os.environ["MARKER_SOURCE_S3_ENDPOINT"]
    access = os.environ["MARKER_SOURCE_S3_ACCESS_KEY"]
    secret = os.environ["MARKER_SOURCE_S3_SECRET_KEY"]
    payload_store = S3PayloadStore(
        S3StoreConfig(
            endpoint_url=endpoint,
            bucket=os.environ["PROBE_PAYLOAD_BUCKET"],
            access_key_id=access,
            secret_access_key=secret,
            prefix="kernel-payloads",
        )
    )
    source_store = S3SourceStore.build_default(
        endpoint_url=endpoint,
        bucket=os.environ["PROBE_SOURCE_BUCKET"],
        access_key_id=access,
        secret_access_key=secret,
        region=os.environ.get("MARKER_SOURCE_S3_REGION", "us-east-1"),
        prefix="kernel-sources",
    )
    return engine, factory, payload_store, source_store


async def mode_setup() -> None:
    """Build the industrial fixture in this process (process A's world)."""
    from tests.recovery_drills import populate_workspace

    engine, factory, payload_store, source_store = await _build_engine_and_stores()
    workspace = os.environ["PROBE_WORKSPACE"]
    sources_dir = Path(os.environ["PROBE_SOURCES_DIR"])
    try:
        state = await populate_workspace(
            factory, payload_store, source_store, workspace, sources_dir
        )
        _emit(
            "SETUP:"
            + json.dumps(
                {
                    "workspace": workspace,
                    "query_expectation": state.query_expectation,
                    "source_blocks": state.source_blocks,
                    "work_id": state.work_id,
                }
            )
        )
    finally:
        await engine.dispose()


async def mode_hold() -> None:
    """Claim the registered work through the real scheduler and hold it.

    Mirrors the runtime dispatch path (``claim_fair``): outbox flips to
    in_flight, a fenced lease is acquired, and the liveness challenge is
    seeded. The process then parks in "execution" — the parent kills it
    here, without any graceful cleanup.
    """
    from app.kernel.scheduler import claim_fair

    engine, factory, _, source_store = await _build_engine_and_stores()
    try:
        owner = os.environ["PROBE_OWNER"]
        lease_seconds = float(os.environ.get("PROBE_LEASE_SECONDS", "1.5"))
        deadline = time.monotonic() + 60.0
        claimed = None
        while claimed is None and time.monotonic() < deadline:
            claimed = await claim_fair(
                factory,
                owner_id=owner,
                resource_class="conversion",
                workspace_id=os.environ["PROBE_WORKSPACE"],
                lease_seconds=lease_seconds,
            )
            if claimed is None:
                await asyncio.sleep(0.2)
        assert claimed is not None, "probe never claimed the work"
        _emit(
            "CLAIM:"
            + json.dumps(
                {
                    "work_id": claimed.work_id,
                    "fencing_token": claimed.lease.fencing_token,
                    "owner": owner,
                    "lease_expires_at": claimed.lease.lease_expires_at,
                }
            )
        )
        # "execute": park while holding the claim; the parent SIGKILLs
        await asyncio.sleep(120)
    finally:
        await engine.dispose()
        close = getattr(source_store, "close", None)
        if close is not None:
            await close()


async def mode_recover() -> None:
    """Replacement process B: recover everything from shared truth only.

    Node-local roots are fresh and empty; A's directories are gone. The
    milestones emitted here are the RTO component clocks — the parent
    anchors them against the kill time. Recovery is complete only when
    a NEW commit lands under B's authority (write-ready), never at
    "port open".
    """
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.fencing import acquire, complete_work
    from app.kernel.publications import open_published_reader
    from app.kernel.records import ObservationRecord
    from app.kernel.scheduler import accept_work, claim_fair
    from app.kernel.snapshots import (
        PAYLOAD_REQUIREMENT_REPLAYABLE,
        resolve_snapshot,
    )
    from app.services.source_acquisition import SourceAcquisitionService

    engine, factory, payload_store, source_store = await _build_engine_and_stores()
    workspace = os.environ["PROBE_WORKSPACE"]
    work_id = int(os.environ["PROBE_WORK_ID"])
    block = json.loads(os.environ["PROBE_BLOCK"])
    query = json.loads(os.environ["PROBE_QUERY"])
    owner = os.environ["PROBE_OWNER"]
    result = json.loads(os.environ.get("PROBE_RESULT", '{"status":"completed"}'))
    kill_epoch = float(os.environ.get("PROBE_KILL_EPOCH", "0"))
    try:
        _milestone("boot")

        # 1. semantic truth: the committed cut is complete and replayable
        snapshot = await resolve_snapshot(
            factory,
            workspace,
            required_payload_state=PAYLOAD_REQUIREMENT_REPLAYABLE,
            payload_store=payload_store,
        )
        assert snapshot.completeness == "complete", snapshot.completeness
        _milestone("semantic_ready")

        # 2. source continuity: materialize committed bytes from the
        #    shared store into THIS node's empty cache
        acquisition = SourceAcquisitionService(
            factory,
            KernelCommitService(factory),
            source_store,
            workspace_id=workspace,
            cache_root=Path(os.environ["MARKER_SOURCE_CACHE_ROOT"]),
        )
        revision = await acquisition.resolve(block)
        assert revision is not None, "committed source revision must resolve"
        consumable = await acquisition.consumable_path_for(revision)
        digest = hashlib.sha256(Path(consumable).read_bytes()).hexdigest()
        assert digest == revision.blob_key.removeprefix("sha256:"), (
            "materialized source bytes must hash to the committed identity"
        )
        _milestone("source_ready")

        # 3. query continuity: the published set serves deterministically
        reader = await open_published_reader(factory, workspace)
        assert reader is not None, "published set must resolve after failover"
        try:
            hits = await reader.search(query["text"], query["mode"])
            assert [h.record_id for h in hits] == query["expected_record_ids"]
        finally:
            await reader.close()
        _milestone("query_ready")

        # 4. work takeover: pending work is claimed through the real
        #    dispatch path (claim_fair: outbox -> in_flight + fresh
        #    fence + seeded challenge); work a dead owner left
        #    in-flight is taken over through the fence once its lease
        #    lapses. Both paths end in exactly one accepted publication.
        from sqlalchemy import text as sa_text

        async with factory() as session:
            outbox_state = await session.scalar(
                sa_text("SELECT state FROM kernel_outbox WHERE id = :id"),
                {"id": work_id},
            )
        lease = None
        deadline = time.monotonic() + 45.0
        if outbox_state == "pending":
            while lease is None and time.monotonic() < deadline:
                claimed = await claim_fair(
                    factory,
                    owner_id=owner,
                    resource_class="conversion",
                    workspace_id=workspace,
                    lease_seconds=30.0,
                )
                if claimed is not None and claimed.work_id == work_id:
                    lease = claimed.lease
                else:
                    # not yet claimable (or another item appeared): keep polling
                    await asyncio.sleep(0.25)
        else:
            while lease is None and time.monotonic() < deadline:
                lease = await acquire(
                    factory, work_id=work_id, owner_id=owner, lease_seconds=30.0
                )
                if lease is None:
                    await asyncio.sleep(0.25)
        assert lease is not None, (
            "replacement never acquired the work (claim pending or takeover "
            "of the lapsed lease)"
        )
        outcome, _appended = await accept_work(
            factory, work_id=work_id, fencing_token=lease.fencing_token, result=result
        )
        completed = await complete_work(
            factory, work_id=work_id, fencing_token=lease.fencing_token
        )
        assert completed, "replacement must ack the outbox behind its fence"
        _milestone("work_ready")

        # 5. post-recovery write under the new authority
        receipt = await KernelCommitService(factory, payload_store=payload_store).commit(
            KernelCommitBatch(
                workspace_id=workspace,
                records=(
                    ObservationRecord(
                        observer=f"drill.{owner}",
                        derivation={"step": "post-failover-write"},
                        payload_bytes=b"POST-FAILOVER-TRUTH-" + b"w" * 32,
                    ),
                ),
            )
        )
        assert receipt.kernel_commit_id > snapshot.kernel_commit_id
        _milestone("write_ready")

        _emit(
            "RECOVERED:"
            + json.dumps(
                {
                    "work_id": work_id,
                    "fencing_token": lease.fencing_token,
                    "owner": owner,
                    "recovered_cut": snapshot.kernel_commit_id,
                    "new_commit": receipt.kernel_commit_id,
                    "kill_epoch": kill_epoch,
                    "write_ready_epoch": time.time(),
                }
            )
        )
    finally:
        await engine.dispose()
        close = getattr(source_store, "close", None)
        if close is not None:
            await close()


async def mode_stale() -> None:
    """A dead owner's late completion attempt, replayed by a fresh process.

    Uses the OLD owner's fencing token after the replacement took over:
    the authority must reject it before any result comparison happens.
    """
    from app.kernel.errors import StaleFenceError
    from app.kernel.scheduler import accept_work

    engine, factory, _payload, _source = await _build_engine_and_stores()
    try:
        work_id = int(os.environ["PROBE_WORK_ID"])
        token = int(os.environ["PROBE_TOKEN"])
        result = json.loads(os.environ.get("PROBE_RESULT", '{"status":"completed"}'))
        try:
            outcome, _appended = await accept_work(
                factory, work_id=work_id, fencing_token=token, result=result
            )
        except StaleFenceError:
            _emit("STALE_REJECTED")
            return
        _emit("STALE_ACCEPTED:" + json.dumps({"work_id": work_id, "token": token}))
        sys.exit(3)  # a stale acceptance is an unrecoverable proof failure
    finally:
        await engine.dispose()


async def mode_oracle() -> None:
    """Run the recovery oracle (components incl. ownership) here.

    Used by the failover drill to prove full application-recovery on the
    SAME authorities process B used, after takeover: cut, closures,
    publication, ownership, all green simultaneously.
    """
    from app.kernel.recovery import RecoveryPointManifest, verify_recovery

    engine, factory, payload_store, source_store = await _build_engine_and_stores()
    try:
        manifest = RecoveryPointManifest.from_mapping(
            json.loads(os.environ["PROBE_MANIFEST"])
        )
        query = json.loads(os.environ["PROBE_QUERY"])
        report = await verify_recovery(
            factory,
            database_url=os.environ["PROBE_DB_URL"],
            workspace_id=os.environ["PROBE_WORKSPACE"],
            manifest=manifest,
            payload_store=payload_store,
            source_store=source_store,
            expected_query=query,
        )
        _emit("ORACLE:" + json.dumps(report.as_dict()))
        if not report.ready:
            sys.exit(4)
    finally:
        await engine.dispose()


async def mode_terminal() -> None:
    """Pre-fault terminal completion on the PRIMARY authority (PR83C2).

    Renew the lease held by the parking owner (renewal keeps the fence
    token), accept + complete the work under it (the terminal commit),
    and register one more pending work item so the post-promotion
    replacement has fresh work to claim through the real dispatch path.
    Every acknowledgement here is the commit return on the primary.
    """
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.fencing import acquire as acquire_lease
    from app.kernel.fencing import complete_work
    from app.kernel.outbox import OutboxIntent
    from app.kernel.records import KernelEdge, NativeObjectRecord
    from app.kernel.scheduler import accept_work, register_work
    from app.services.source_acquisition import SourceAcquisitionService

    engine, factory, payload_store, source_store = await _build_engine_and_stores()
    workspace = os.environ["PROBE_WORKSPACE"]
    work_id = int(os.environ["PROBE_WORK_ID"])
    owner = os.environ["PROBE_OWNER"]
    block = json.loads(os.environ["PROBE_BLOCK"])
    result = json.loads(os.environ.get("PROBE_RESULT", '{"status":"completed"}'))
    try:
        # 1. lease renewal under the same owner: acknowledged, token kept
        lease = None
        deadline = time.monotonic() + 30.0
        while lease is None and time.monotonic() < deadline:
            lease = await acquire_lease(
                factory, work_id=work_id, owner_id=owner, lease_seconds=30.0
            )
            if lease is None:
                await asyncio.sleep(0.25)
        assert lease is not None, "terminal probe never renewed the lease"
        _emit("RENEWED:" + json.dumps({"work_id": work_id, "fencing_token": lease.fencing_token, "owner": owner}))

        # 2. terminal transition: accept + complete under the renewed fence
        outcome, _appended = await accept_work(
            factory, work_id=work_id, fencing_token=lease.fencing_token, result=result
        )
        completed = await complete_work(
            factory, work_id=work_id, fencing_token=lease.fencing_token
        )
        assert completed, "terminal probe must ack the outbox behind its fence"

        # 3. one more pending work item for the post-promotion replacement
        acquisition = SourceAcquisitionService(
            factory,
            KernelCommitService(factory),
            source_store,
            workspace_id=workspace,
            cache_root=Path(os.environ["MARKER_SOURCE_CACHE_ROOT"]),
        )
        revision = await acquisition.resolve(block)
        assert revision is not None, "terminal probe source revision must resolve"
        record = NativeObjectRecord(
            record_id="conversion-request.drill-job-b",
            source_uri=revision.source_id,
            locator=revision.blob_key,
            media_type=revision.media_type,
            extractor_name="failover-drill",
            extractor_version="1",
        )
        receipt = await KernelCommitService(factory, payload_store=payload_store).commit(
            KernelCommitBatch(
                workspace_id=workspace,
                records=(record,),
                edges=(
                    KernelEdge(
                        edge_kind="depends_on",
                        source_ref=record.record_id,
                        target_ref=revision.content_revision_id,
                    ),
                ),
                outbox=(OutboxIntent(work_kind="conversion.execute", payload={"job_id": "drill-job-b"}),),
            )
        )
        work_id_b = receipt.outbox_ids[0]
        await register_work(factory, work_id=work_id_b, resource_class="conversion")

        _emit(
            "TERMINAL:"
            + json.dumps(
                {
                    "work_id": work_id,
                    "fencing_token": lease.fencing_token,
                    "owner": owner,
                    "already_accepted": bool(getattr(outcome, "already_accepted", False)),
                    "completed": bool(completed),
                    "work_id_b": work_id_b,
                }
            )
        )
    finally:
        await engine.dispose()
        close = getattr(source_store, "close", None)
        if close is not None:
            await close()


async def mode_commit_probe() -> None:
    """Timed durability-class commits with honest blocking observation.

    Used against a synchronous primary: when the required standby is
    unavailable, a commit in the declared durable class must never
    acknowledge success — the probe records the blocked observation and
    exits 0 (an honest measurement, not a failure); the parent decides
    what the observation must prove.
    """
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.records import ObservationRecord

    engine, factory, payload_store, _source = await _build_engine_and_stores()
    workspace = os.environ["PROBE_WORKSPACE"]
    requested = int(os.environ.get("PROBE_COMMITS", "8"))
    timeout = float(os.environ.get("PROBE_COMMIT_TIMEOUT", "30"))
    label = os.environ.get("PROBE_COMMIT_LABEL", "commit-probe")
    service = KernelCommitService(factory, payload_store=payload_store)
    latencies_ms: list[float] = []
    try:
        for index in range(requested):
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    service.commit(
                        KernelCommitBatch(
                            workspace_id=workspace,
                            records=(
                                ObservationRecord(
                                    observer=f"drill.{label}",
                                    derivation={"step": label, "index": index},
                                    payload_bytes=f"FAILOVER-COMMIT-{label}-".encode()
                                    + bytes([48 + (index % 10)]) * 24,
                                ),
                            ),
                        )
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                _emit(
                    "COMMITS:"
                    + json.dumps(
                        {
                            "label": label,
                            "requested": requested,
                            "completed": len(latencies_ms),
                            "blocked": True,
                            "blocked_epoch": time.time(),
                            "blocked_after_seconds": time.monotonic() - started,
                            "latencies_ms": latencies_ms,
                        }
                    )
                )
                return
            latencies_ms.append(round((time.monotonic() - started) * 1000.0, 3))
        _emit(
            "COMMITS:"
            + json.dumps(
                {
                    "label": label,
                    "requested": requested,
                    "completed": len(latencies_ms),
                    "blocked": False,
                    "latencies_ms": latencies_ms,
                }
            )
        )
    finally:
        try:
            await asyncio.wait_for(engine.dispose(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


MODES = {
    "setup": mode_setup,
    "hold": mode_hold,
    "recover": mode_recover,
    "stale": mode_stale,
    "oracle": mode_oracle,
    "terminal": mode_terminal,
    "commit_probe": mode_commit_probe,
}


def main() -> None:
    mode = os.environ.get("PROBE_MODE")
    handler = MODES.get(mode)
    if handler is None:
        raise SystemExit(f"unknown PROBE_MODE {mode!r}; expected one of {sorted(MODES)}")
    asyncio.run(handler())


if __name__ == "__main__":
    main()
