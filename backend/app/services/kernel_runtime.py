"""Kernel runtime coordinator (PR67B): the authority bridge for live conversions.

This module is the single place where a submitted conversion becomes
kernel-authorized work and where the existing PR66/PR67 contracts govern
execution:

* **authorize** — one ``KernelCommitBatch`` carries a legitimate
  ``NativeObjectRecord`` describing the conversion request plus one
  ``OutboxIntent`` (``conversion.execute``). The record and the intent
  commit atomically, so "authorized" and "executable" can never split.
  A deterministic record id makes re-authorization idempotent: the
  semantic-identity uniqueness of the kernel rejects the duplicate and
  the existing work item is resolved instead.
* **dispatch** — a loop claims work exclusively through
  ``scheduler.claim_fair`` (fair share, hard fan-out cap, fenced
  ownership, challenge seeding). No executor path may treat a job as
  owned without this claim.
* **liveness** — a per-claim renewal task extends the lease only when
  the real control loop produced fresh activity (progress/log/status
  evidence). A wedged converter stops producing evidence, stops
  renewing, and becomes takeover-eligible. There is no detached
  heartbeat that can outlive the work it claims to represent. The
  claim-to-start gap is bounded structurally instead: executor
  parallelism is aligned with ``max_in_flight`` so claimed work starts
  without a liveness-blind queue wait.
* **acceptance** — success is linearized by ``scheduler.accept_work``
  (PR66 fence + publication). The compatibility ``ConversionJob`` row
  may only read ``completed`` AFTER acceptance commits. The accepted
  payload is a bounded, canonical result descriptor over the resolved
  durable output — never an ephemeral PR68A ArtifactHandle pathname.
* **failure/cancellation** — terminal failure and cancellation are
  durably recorded as semantic events, stop outbox redelivery behind
  the current fence, and project to the compatibility row. A stale
  worker cannot renew, publish, or flip the row terminal-success.
* **recovery** — ``recover()`` reconciles dispatch state after a
  restart, completes lost acknowledgements behind accepted truth,
  projects accepted-but-unprojected publications, adopts legacy
  durable rows into kernel authority, and sweeps abandoned
  non-durable rows. Every crash boundary converges to pending,
  leased, cancelled/failed, or accepted-exactly-once.

The coordinator holds no scheduling truth of its own: the kernel
outbox/scheduler/fence/publication is the one authority. The
``ConversionJob`` row is a compatibility projection; the legacy
SQLite durable queue is dispatch-disabled in kernel mode and its rows
are adopted as migration input.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select, update

from app.database import async_session_factory
from app.kernel import events as kernel_events
from app.kernel import fencing, liveness, outbox as kernel_outbox
from app.kernel import scheduler
from app.kernel.commit import (
    KernelCommitBatch,
    KernelCommitService,
)
from app.kernel.errors import (
    DuplicateRecordIdentityError,
    KernelBusyError,
    KernelError,
    PublicationConflictError,
    StaleFenceError,
    WorkCancelledError,
    InvalidChallengeError,
)
from app.kernel.records import NativeObjectRecord, KernelEdge
from app.kernel.source_store import SourceArtifactStore, SourceStoreError
from app.models.job import ConversionJob
from app.services.source_acquisition import (
    SOURCE_CONFIG_KEY,
    AcquiredSourceRevision,
    SourceAcquisitionService,
)

logger = logging.getLogger(__name__)

#: Outbox work kind for executable conversion work.
WORK_KIND = "conversion.execute"
#: Scheduling resource class for conversions (single class; groups divide it).
RESOURCE_CLASS = "conversion"
#: Default scheduling group when a job declares none.
DEFAULT_GROUP = "default"
#: Compatibility marker written on rows owned by the kernel runtime.
QUEUE_MARKER = "kernel"

EVENT_WORK_FAILED = "work.failed"
EVENT_WORK_CANCELLED = "work.cancelled"
EVENT_WORK_RETRY = "work.retry"

#: Bounded error text stored in durable events.
_MAX_ERROR_CHARS = 500

TERMINAL_ROW_STATUSES = ("completed", "failed", "cancelled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime:
    """Normalize a lease/view timestamp (aware, naive, or iso string)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_id_for_job(job_id: str) -> str:
    return f"conversion-request.{job_id}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ActiveClaim:
    """Live execution context carried from ``claim_fair`` to finalization."""

    work_id: int
    job_id: str
    owner_id: str
    fencing_token: int
    challenge_nonce: str
    active_request_id: str
    max_retries: int = 0
    #: monotonically advancing activity counter fed by real control-loop
    #: evidence (progress ticks, worker logs/status). Liveness renewal
    #: presents this counter; a frozen loop freezes the counter.
    activity: int = 0
    renewed_through: int = 0
    cancelled: bool = False
    superseded: bool = False
    finalizing: bool = False
    finished: bool = False
    renew_task: Optional[asyncio.Task[None]] = None
    _activity_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_activity(self) -> None:
        with self._activity_lock:
            self.activity += 1

    @property
    def has_fresh_activity(self) -> bool:
        with self._activity_lock:
            return self.activity > self.renewed_through


class KernelRuntimeCoordinator:
    """Bridges the live conversion path onto the kernel runtime authority."""

    def __init__(
        self,
        task_manager: Any,
        *,
        session_factory: Callable[[], Any] | None = None,
        commit_service: KernelCommitService | None = None,
        source_store: SourceArtifactStore | None = None,
        source_cache_root: Path | None = None,
        workspace_id: str = "local",
        owner_id: str = "marker-runtime",
        lease_seconds: float = 900.0,
        renew_interval_seconds: float = 5.0,
        dispatch_poll_seconds: float = 0.25,
        watchdog_interval_seconds: float = 15.0,
        max_in_flight: int = 4,
    ) -> None:
        self._task_manager = task_manager
        self._session_factory_ref = session_factory
        self._commit_service_ref = commit_service
        self._source_store = source_store
        self._source_cache_root = source_cache_root
        self._source_service_ref: SourceAcquisitionService | None = None
        self.workspace_id = workspace_id
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self.dispatch_poll_seconds = dispatch_poll_seconds
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.max_in_flight = max(1, int(max_in_flight))

        self._active_by_job: dict[str, ActiveClaim] = {}
        self._active_by_work: dict[int, ActiveClaim] = {}
        self._conversion_service: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    @property
    def _sf(self) -> Any:
        if self._session_factory_ref is not None:
            return self._session_factory_ref
        return async_session_factory

    def _commit_service(self) -> KernelCommitService:
        if self._commit_service_ref is None:
            self._commit_service_ref = KernelCommitService(self._sf)
        return self._commit_service_ref

    def _source_service(self) -> SourceAcquisitionService:
        if self._source_service_ref is None:
            from app.kernel.source_store import build_source_store

            store = self._source_store or build_source_store()
            self._source_service_ref = SourceAcquisitionService(
                self._sf,
                self._commit_service(),
                store,
                workspace_id=self.workspace_id,
                cache_root=self._source_cache_root,
            )
        return self._source_service_ref

    def set_conversion_service(self, service: Any) -> None:
        self._conversion_service = service

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="kernel-dispatch"))
        self._tasks.append(asyncio.create_task(self._watchdog_loop(), name="kernel-watchdog"))

    def stop(self) -> None:
        self._started = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for claim in list(self._active_by_job.values()):
            self._end_claim(claim)

    # ------------------------------------------------------------------
    # authorization (Band 1)
    # ------------------------------------------------------------------

    async def authorize(self, job_id: str, config: dict[str, Any]) -> int:
        """Make *job_id* executable as exactly one kernel work item.

        Idempotent: the deterministic request record's semantic identity
        is unique per workspace, so a retried authorization either finds
        the existing work item or converges onto it after the loser of
        an insert race resolves the winner's row.

        Source truth (PR70 local slice): a config carrying a committed
        source-revision block is validated against the kernel + artifact
        store BEFORE the work item is authorized — the request record
        gains the revision references and a ``depends_on`` edge onto the
        ContentRevision, so authorized work can never point at an
        uncommitted or unavailable revision.
        """
        record_id = _record_id_for_job(job_id)
        existing = await self._resolve_work_id(record_id)
        if existing is not None:
            return existing

        acquired = await self._validated_source_revision(job_id, config)

        source_service = self._source_service()
        if acquired is None and not source_service.legacy_submit_fallback:
            # Industrial profiles never execute unowned paths: a
            # submission without committed source truth is rejected at
            # authorization, not silently run against a node-local or
            # external path whose provenance cannot be bound to a
            # revision. (Restart adoption calls authorize() per row and
            # logs this refusal instead of blocking boot.)
            raise KernelError(
                f"job {job_id!r}: the active source-artifact profile "
                f"({source_service.store_profile}) requires a committed "
                "source revision; path-trust submission is not available"
            )

        source_uri = str(config.get("source_url") or "") or (
            "file://" + str(config.get("local_filepath") or config.get("durable_filepath") or job_id)
        )
        locator = str(config.get("durable_filepath") or config.get("local_filepath") or "")
        if acquired is not None:
            # The execution source is the acquired revision's immutable
            # artifact, never the mutable external path; the locator is
            # the durable, credential-free address of those bytes under
            # the active profile.
            locator = source_service.execution_locator_for(acquired)
        properties: dict[str, Any] = {
            "job_id": str(job_id),
            "output_format": str(config.get("output_format") or "markdown"),
            "max_retries": int(config.get("max_retries") or 0),
        }
        edges: tuple[KernelEdge, ...] = ()
        if acquired is not None:
            properties["source_revision"] = acquired.to_config()
            edges = (
                KernelEdge(
                    edge_kind="depends_on",
                    source_ref=record_id,
                    target_ref=acquired.content_revision_id,
                ),
            )
        record = NativeObjectRecord(
            record_id=record_id,
            source_uri=source_uri,
            locator=locator or job_id,
            media_type=str(config.get("input_format") or "document"),
            extractor_name="marker-ui-runtime",
            extractor_version="pr83b3",
            properties=properties,
        )
        intent_payload = {"job_id": str(job_id)}
        batch = KernelCommitBatch(
            workspace_id=self.workspace_id,
            records=(record,),
            edges=edges,
            producer={"operation": "conversion.submit", "job_id": str(job_id)},
            outbox=(kernel_outbox.OutboxIntent(work_kind=WORK_KIND, payload=intent_payload),),
        )
        try:
            receipt = await self._commit_service().commit(batch)
            work_id = receipt.outbox_ids[0] if receipt.outbox_ids else None
        except (DuplicateRecordIdentityError, KernelError) as exc:
            # Lost an idempotency race (or replayed a submission): the
            # winner's record+intent already committed atomically, so the
            # canonical work item exists and must be reused, never duplicated.
            logger.info("authorize(%s) converged onto existing work: %s", job_id, exc)
            work_id = None
        if work_id is None:
            work_id = await self._resolve_work_id(record_id)
        if work_id is None:
            raise KernelError(
                f"authorization for job {job_id!r} neither committed nor resolved"
            )

        await scheduler.register_work(
            self._sf,
            work_id=work_id,
            resource_class=RESOURCE_CLASS,
            group_id=self._group_for_config(config),
        )
        await self._mark_row_kernel_owned(job_id, config)
        return work_id

    async def _validated_source_revision(
        self, job_id: str, config: dict[str, Any]
    ) -> AcquiredSourceRevision | None:
        """Resolve the config's source-revision block against truth.

        Returns None for legacy configs without a block (the pre-PR70
        direct-submission shape). Raises when a block exists but its
        revision is not committed in this workspace or its bytes are
        unavailable — a submission may then re-acquire, but work must
        not be authorized against fiction.
        """
        block = config.get(SOURCE_CONFIG_KEY)
        if not isinstance(block, dict):
            return None
        acquired = await self._source_service().resolve(block)
        if acquired is None:
            raise KernelError(
                f"job {job_id!r}: source revision block does not resolve to "
                "committed kernel truth with available bytes; re-acquire the source"
            )
        return acquired

    async def ensure_source_revision(
        self, job_id: str, filepath: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Normalize/acquire the source revision for one kernel submission.

        The single acquisition chokepoint for direct ``submit_conversion``
        callers (REST/agent/retry acquire earlier so their probe runs on
        the staged artifact). Rules:

        * a resolvable committed block is normalized in place;
        * a block whose bytes are gone is re-acquired from the current
          source — a NEW revision, never silent reuse of dead truth;
        * a config without a block acquires when the file exists and the
          local policy permits it; otherwise the submission proceeds in
          the legacy path-trust shape (recorded, never hidden);
        * whenever the block changes, the row's config_json is persisted
          so ``_launch`` (which reads the row, not this dict) sees it.
        """
        block = config.get(SOURCE_CONFIG_KEY)
        service = self._source_service()
        if isinstance(block, dict):
            acquired = await service.resolve(block)
            if acquired is not None:
                config[SOURCE_CONFIG_KEY] = acquired.to_config()
                return config

        path = Path(filepath or "")
        if not path.is_file():
            if not service.legacy_submit_fallback:
                # Industrial profile: a submission whose source file is
                # already gone cannot acquire and must not continue as
                # unowned path-trust work.
                raise KernelError(
                    f"job {job_id!r}: source file is missing and the active "
                    f"source-artifact profile ({service.store_profile}) "
                    "requires acquisition before submission"
                )
            return config  # launch-time terminal failure owns missing files
        try:
            from app.core.config import UPLOAD_DIR

            marker_owned = UPLOAD_DIR.resolve() in path.resolve().parents
            acquired = await service.acquire(
                path,
                source_kind="upload" if marker_owned else "local_path",
                suffix=path.suffix.lower(),
                job_id=job_id,
            )
            config[SOURCE_CONFIG_KEY] = acquired.to_config()
            if service.legacy_submit_fallback:
                # A node-local artifact path is durable executable state
                # for the local profile; shared profiles resolve bytes
                # from committed truth at launch instead.
                config["durable_filepath"] = str(await service.artifact_path_for(acquired))
            await self._persist_row_config(job_id, config)
        except Exception as exc:  # noqa: BLE001
            if not service.legacy_submit_fallback:
                # Industrial fail-closure: acquisition/policy failures
                # propagate — never a silent legacy-shaped submit.
                raise
            logger.info(
                "kernel submission for job %s runs without a source revision (%s: %s)",
                job_id,
                type(exc).__name__,
                exc,
            )
        return config

    async def _persist_row_config(self, job_id: str, config: dict[str, Any]) -> None:
        try:
            async with self._sf() as session:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id == job_id)
                    .values(config_json=json.dumps(config))
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - metadata must never block dispatch
            logger.exception("source-revision config persist failed for job %s", job_id)

    @staticmethod
    def _group_for_config(config: dict[str, Any]) -> str:
        group = str(config.get("scheduling_group") or DEFAULT_GROUP)
        return group if group.replace(".", "").replace("-", "").replace("_", "").isalnum() else DEFAULT_GROUP

    async def _resolve_work_id(self, record_id: str) -> int | None:
        from app.kernel.models import KernelOutbox, KernelRecord

        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KernelOutbox.id)
                    .join(
                        KernelRecord,
                        (KernelRecord.kernel_commit_id == KernelOutbox.kernel_commit_id)
                        & (KernelRecord.workspace_id == KernelOutbox.workspace_id),
                    )
                    .where(
                        KernelRecord.id == record_id,
                        KernelOutbox.work_kind == WORK_KIND,
                    )
                    .order_by(KernelOutbox.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return int(row) if row is not None else None

    async def resolve_work_for_job(self, job_id: str) -> int | None:
        return await self._resolve_work_id(_record_id_for_job(job_id))

    async def _mark_row_kernel_owned(self, job_id: str, config: dict[str, Any]) -> None:
        try:
            max_retries = int(config.get("max_retries") or 0)
        except (TypeError, ValueError):
            max_retries = 0
        try:
            async with self._sf() as session:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id == job_id)
                    .values(queue_backend=QUEUE_MARKER, max_retries=max(0, max_retries))
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - metadata must never block dispatch
            logger.exception("kernel marker write failed for job %s", job_id)

    async def ensure_group_policy(self, group_id: str = DEFAULT_GROUP) -> None:
        await scheduler.set_group_policy(
            self._sf,
            resource_class=RESOURCE_CLASS,
            group_id=group_id,
            policy=scheduler.GroupPolicy(max_in_flight=self.max_in_flight),
        )

    # ------------------------------------------------------------------
    # dispatch (Band 2)
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        await self.ensure_group_policy()
        while self._started:
            try:
                claimed = await scheduler.claim_fair(
                    self._sf,
                    owner_id=self.owner_id,
                    resource_class=RESOURCE_CLASS,
                    workspace_id=self.workspace_id,
                    lease_seconds=self.lease_seconds,
                )
                if claimed is None:
                    await asyncio.sleep(self.dispatch_poll_seconds)
                    continue
                await self._launch(claimed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop is the product's heartbeat
                logger.exception("kernel dispatch iteration failed")
                await asyncio.sleep(self.dispatch_poll_seconds)

    async def _launch(self, claimed: scheduler.ClaimFairResult) -> None:
        job_id = str((claimed.payload or {}).get("job_id") or "")
        if not job_id:
            # Not our work shape (foreign payload); vacate the claim honestly.
            await fencing.release(
                self._sf,
                work_id=claimed.work_id,
                owner_id=self.owner_id,
                fencing_token=claimed.lease.fencing_token,
            )
            await kernel_outbox.release(self._sf, claimed.work_id)
            return

        config: dict[str, Any] = {}
        row_status: str | None = None
        async with self._sf() as session:
            row = await session.get(ConversionJob, job_id)
            if row is not None:
                row_status = row.status
                try:
                    parsed = json.loads(row.config_json) if row.config_json else {}
                    config = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    config = {}
        if row_status is None or row_status in TERMINAL_ROW_STATUSES:
            # The compatibility row ended (cancel/fail raced the claim, or
            # the row vanished): vacate and stop redelivery.
            await fencing.release(
                self._sf,
                work_id=claimed.work_id,
                owner_id=self.owner_id,
                fencing_token=claimed.lease.fencing_token,
            )
            await kernel_outbox.ack(self._sf, claimed.work_id)
            logger.info("kernel work %d vacated: row %s is %s", claimed.work_id, job_id, row_status)
            return

        # Source resolution: a job with a committed source revision
        # executes against the revision's immutable bytes ONLY — the
        # local profile consumes the owned artifact path directly, a
        # shared profile consumes a verified node-local materialization
        # of the durable object. A missing/truncated/corrupt source
        # terminal-fails honestly — falling back to the external path
        # here would reopen the exact validate-A-parse-B hole source
        # truth exists to close.
        acquired = AcquiredSourceRevision.from_config(config.get(SOURCE_CONFIG_KEY) or {})
        if acquired is not None:
            try:
                consumable = Path(
                    await self._source_service().consumable_path_for(acquired)
                )
                if not consumable.is_file() or consumable.stat().st_size != acquired.byte_length:
                    raise FileNotFoundError(
                        f"source for {acquired.blob_key}{acquired.suffix} is "
                        "missing or truncated"
                    )
            except (SourceStoreError, OSError) as exc:
                await self._terminal_fail(
                    claimed.work_id,
                    job_id,
                    f"acquired source revision unavailable: {exc}",
                    attempts=0,
                )
                return
            filepath = str(consumable)
        else:
            filepath = str(
                config.get("durable_filepath") or config.get("local_filepath") or ""
            )
            if not filepath or not Path(filepath).is_file():
                await self._terminal_fail(
                    claimed.work_id,
                    job_id,
                    f"conversion source not found: {filepath}",
                    attempts=0,
                )
                return

        claim = ActiveClaim(
            work_id=claimed.work_id,
            job_id=job_id,
            owner_id=self.owner_id,
            fencing_token=claimed.lease.fencing_token,
            challenge_nonce=claimed.challenge_nonce,
            active_request_id=f"exec:{job_id}:{claimed.lease.fencing_token}",
            max_retries=int(config.get("max_retries") or 0),
        )
        self._active_by_job[job_id] = claim
        self._active_by_work[claimed.work_id] = claim
        manager = self._task_manager
        if manager is not None:
            manager._kernel_claims[job_id] = claim

        # Compatibility projection of the claim: the row is "processing"
        # only because a fenced execution generation now exists.
        async with self._sf() as session:
            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status.in_(["pending", "processing"]))
                .values(status="processing", started_at=_utcnow())
            )
            await session.commit()

        claim.renew_task = asyncio.create_task(
            self._renewal_loop(claim), name=f"kernel-renew-{job_id[:8]}"
        )
        if manager is not None:
            service = self._conversion_service
            try:
                manager.submit_job(job_id, filepath, config, service, claim=claim)
            except Exception:  # noqa: BLE001 - executor failure is a work failure
                logger.exception("executor launch failed for job %s", job_id)
                await self.fail_execution(claim, "executor launch failed")

    # ------------------------------------------------------------------
    # evidence-backed liveness (Outcome C)
    # ------------------------------------------------------------------

    async def _renewal_loop(self, claim: ActiveClaim) -> None:
        while self._started and not claim.finished and not claim.finalizing:
            await asyncio.sleep(self.renew_interval_seconds)
            if claim.finished or claim.finalizing or claim.cancelled or claim.superseded:
                break
            if not claim.has_fresh_activity:
                # No real control-loop evidence since the last renewal:
                # renewing anyway would be a heartbeat lie. Let the lease
                # lapse; the watchdog makes the work takeover-eligible.
                # (Claim-to-start latency is bounded structurally: the
                # runtime aligns executor parallelism with max_in_flight
                # so claimed work starts without a queue wait.)
                continue
            try:
                outcome = await liveness.renew_lease(
                    self._sf,
                    work_id=claim.work_id,
                    owner_id=claim.owner_id,
                    fencing_token=claim.fencing_token,
                    challenge_nonce=claim.challenge_nonce,
                    progress=claim.activity,
                    active_request_id=claim.active_request_id,
                    extend_seconds=self.lease_seconds,
                )
            except WorkCancelledError:
                claim.cancelled = True
                break
            except (StaleFenceError, InvalidChallengeError):
                # Superseded by takeover or vacated by cancellation: this
                # generation must stop speaking for the work.
                claim.superseded = True
                break
            except KernelBusyError:
                # Transient writer contention: retry on the next tick.
                # Killing the renewal loop here would lapse a healthy
                # lease and trigger a needless takeover.
                continue
            except KernelError:  # noqa: BLE001 - liveness must never kill execution
                logger.exception("liveness renewal failed for work %d", claim.work_id)
                break
            claim.challenge_nonce = outcome.next_challenge_nonce
            claim.renewed_through = claim.activity
            percent = 0
            manager = self._task_manager
            if manager is not None:
                try:
                    percent = int(manager._progress.get(claim.job_id, 0) or 0)
                except Exception:  # noqa: BLE001
                    percent = 0
            try:
                await kernel_events.append_progress(
                    self._sf,
                    workspace_id=self.workspace_id,
                    work_id=claim.work_id,
                    counter=claim.activity,
                    payload={"job_id": claim.job_id, "percent": percent},
                )
            except KernelError:  # noqa: BLE001 - progress projection is best effort
                logger.exception("durable progress write failed for work %d", claim.work_id)

    # ------------------------------------------------------------------
    # terminal publication (Outcome D/E)
    # ------------------------------------------------------------------

    async def accept_result(self, claim: ActiveClaim, descriptor: dict[str, Any]) -> str:
        """Cross the fenced accepted-publication boundary for *claim*.

        Returns one of ``projected`` (this generation's acceptance is the
        durable truth; caller may project the compatibility row),
        ``already`` (converged retry of the same accepted result),
        ``superseded`` (stale fence; nothing may be projected),
        ``conflict`` (a different result is already accepted), or
        ``cancelled`` (durable cancellation observed first).
        """
        live = await liveness.get_liveness(self._sf, claim.work_id)
        if live is not None and live.cancelled_at is not None:
            claim.cancelled = True
            self._end_claim(claim)
            return "cancelled"
        try:
            outcome, _appended = await scheduler.accept_work(
                self._sf,
                work_id=claim.work_id,
                fencing_token=claim.fencing_token,
                result=descriptor,
            )
        except StaleFenceError:
            claim.superseded = True
            self._end_claim(claim)
            logger.info(
                "late result from stale fence rejected for work %d (job %s)",
                claim.work_id,
                claim.job_id,
            )
            return "superseded"
        except PublicationConflictError:
            self._end_claim(claim)
            logger.error(
                "divergent result rejected for work %d (job %s): accepted "
                "publication wins, conflicting attempt surfaced",
                claim.work_id,
                claim.job_id,
            )
            return "conflict"
        accepted_now = not outcome.already_accepted
        # Acknowledge the outbox delivery strictly behind accepted truth
        # and the current fence.
        await fencing.complete_work(
            self._sf,
            work_id=claim.work_id,
            fencing_token=claim.fencing_token,
        )
        self._end_claim(claim)
        return "projected" if accepted_now else "already"

    async def fail_execution(self, claim: ActiveClaim, error_message: str) -> str:
        """Terminate a failed execution honestly.

        Returns ``retried`` (work returned to pending inside the retry
        budget), ``failed`` (terminal failure projected), or
        ``superseded`` (this generation lost authority; no projection).
        """
        if claim.superseded or claim.cancelled:
            self._end_claim(claim)
            return "superseded"
        # Authority re-check: a takeover/cancel may have advanced the fence
        # while this executor ran. A stale generation must not consume the
        # successor's retry budget or terminal-fail its live work.
        lease = await fencing.get_lease(self._sf, claim.work_id)
        if (
            lease is None
            or lease.fencing_token != claim.fencing_token
            or lease.state != "leased"
        ):
            claim.superseded = True
            self._end_claim(claim)
            logger.info(
                "stale generation failure rejected for work %d (job %s)",
                claim.work_id,
                claim.job_id,
            )
            return "superseded"
        message = str(error_message or "conversion failed")[:_MAX_ERROR_CHARS]
        view = await self._outbox_view(claim.work_id)
        attempts = int(view.attempts) if view is not None else 0
        if attempts < claim.max_retries:
            await fencing.release(
                self._sf,
                work_id=claim.work_id,
                owner_id=claim.owner_id,
                fencing_token=claim.fencing_token,
            )
            await kernel_outbox.release(self._sf, claim.work_id)
            await self._append_event(
                EVENT_WORK_RETRY,
                {
                    "work_id": claim.work_id,
                    "job_id": claim.job_id,
                    "attempts": attempts + 1,
                    "error": message,
                },
            )
            async with self._sf() as session:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id == claim.job_id)
                    .where(ConversionJob.status.not_in(TERMINAL_ROW_STATUSES))
                    .values(status="pending")
                )
                await session.commit()
            self._end_claim(claim)
            return "retried"
        await self._terminal_fail(claim.work_id, claim.job_id, message, attempts, claim=claim)
        return "failed"

    async def _terminal_fail(
        self,
        work_id: int,
        job_id: str,
        message: str,
        attempts: int,
        *,
        claim: ActiveClaim | None = None,
    ) -> None:
        bounded = str(message or "conversion failed")[:_MAX_ERROR_CHARS]
        lease = await fencing.get_lease(self._sf, work_id)
        if lease is not None and lease.state == "leased":
            try:
                await fencing.release(
                    self._sf,
                    work_id=work_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                )
            except KernelError:  # noqa: BLE001 - release is best effort here
                logger.exception("lease release failed while failing work %d", work_id)
        elif claim is not None and lease is None:
            try:
                await fencing.release(
                    self._sf,
                    work_id=work_id,
                    owner_id=claim.owner_id,
                    fencing_token=claim.fencing_token,
                )
            except KernelError:  # noqa: BLE001
                logger.exception("lease release failed while failing work %d", work_id)
        await self._append_event(
            EVENT_WORK_FAILED,
            {"work_id": work_id, "job_id": job_id, "error": bounded, "attempts": int(attempts)},
        )
        await self._ack_work(work_id)
        async with self._sf() as session:
            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status.not_in(TERMINAL_ROW_STATUSES))
                .values(
                    status="failed",
                    error_message=bounded,
                    completed_at=_utcnow(),
                )
            )
            await session.commit()
        if claim is not None:
            self._end_claim(claim)

    async def _ack_work(self, work_id: int) -> None:
        # ``ack`` only moves in_flight -> done; a pending row (e.g. cancel
        # of unclaimed work claimed just now) must be delivery-claimed first.
        if not await kernel_outbox.ack(self._sf, work_id):
            if await kernel_outbox.claim(self._sf, work_id) is not None:
                await kernel_outbox.ack(self._sf, work_id)

    # ------------------------------------------------------------------
    # cancellation
    # ------------------------------------------------------------------

    async def prepare_cancel(self, job_id: str) -> bool:
        """Durably cancel the kernel work for *job_id* (if any).

        Returns True when kernel work existed and its cancellation was
        recorded behind the current fence. The compatibility row is the
        caller's projection; this method never touches it.
        """
        claim = self._active_by_job.get(job_id)
        if claim is not None:
            claim.cancelled = True
            try:
                await liveness.report_cancellation(
                    self._sf,
                    work_id=claim.work_id,
                    owner_id=claim.owner_id,
                    fencing_token=claim.fencing_token,
                    reason="cancelled by user",
                )
            except KernelError:  # noqa: BLE001 - already cancelled/superseded
                logger.info("cancel observation rejected for work %d", claim.work_id)
            await self._finish_cancel(claim.work_id, job_id, claim)
            return True

        work_id = await self.resolve_work_for_job(job_id)
        if work_id is None:
            return False
        # Terminal kernel state cannot be cancelled: an accepted
        # publication (or already-done delivery) is durable truth the
        # caller must not overwrite with a cancelled row.
        view = await self._outbox_view(work_id)
        if view is not None and view.state == "done":
            return False
        existing_lease = await fencing.get_lease(self._sf, work_id)
        if existing_lease is not None and existing_lease.state == "accepted":
            return False
        # Claim the delivery first so a racing dispatcher cannot take the
        # work between our claim and the ack below.
        await kernel_outbox.claim(self._sf, work_id)
        lease = await fencing.acquire(
            self._sf, work_id=work_id, owner_id=self.owner_id, lease_seconds=self.lease_seconds
        )
        if lease is None:
            # Leased elsewhere (stale/foreign owner): the row projection
            # below plus the launch-time terminal guard will converge this
            # work when its fence lapses or its row check trips.
            logger.info("cancel of job %s found work %d leased elsewhere", job_id, work_id)
            return True
        try:
            await liveness.report_cancellation(
                self._sf,
                work_id=work_id,
                owner_id=self.owner_id,
                fencing_token=lease.fencing_token,
                reason="cancelled by user",
            )
        except KernelError:  # noqa: BLE001
            logger.info("cancel observation rejected for work %d", work_id)
        await self._finish_cancel(work_id, job_id, fencing_token=lease.fencing_token)
        return True

    async def _finish_cancel(
        self,
        work_id: int,
        job_id: str,
        claim: ActiveClaim | None = None,
        *,
        fencing_token: int | None = None,
    ) -> None:
        token = fencing_token if fencing_token is not None else (claim.fencing_token if claim else None)
        owner = claim.owner_id if claim is not None else self.owner_id
        if token is not None:
            try:
                await fencing.release(
                    self._sf, work_id=work_id, owner_id=owner, fencing_token=token
                )
            except KernelError:  # noqa: BLE001 - already vacated/accepted
                logger.info("cancel release rejected for work %d", work_id)
        await self._append_event(
            EVENT_WORK_CANCELLED,
            {"work_id": work_id, "job_id": job_id, "reason": "cancelled by user"},
        )
        await self._ack_work(work_id)
        if claim is not None:
            self._end_claim(claim)

    # ------------------------------------------------------------------
    # watchdog: lease-lapse takeover + lost-ack repair
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self.watchdog_interval_seconds)
            try:
                await self._watchdog_pass()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - watchdog must survive anything
                logger.exception("kernel watchdog pass failed")

    async def _watchdog_pass(self) -> None:
        views = await kernel_outbox.list_outbox(
            self._sf, workspace_id=self.workspace_id, state="in_flight"
        )
        for view in views:
            work_id = view.id
            lease = await fencing.get_lease(self._sf, work_id)
            if lease is not None and lease.state == "accepted":
                # Accepted publication whose acknowledgement was lost.
                if not await fencing.complete_work(
                    self._sf, work_id=work_id, fencing_token=lease.fencing_token
                ):
                    logger.warning("lost-ack repair failed for work %d", work_id)
                continue
            if lease is None:
                # The fence is gone, but the in_flight snapshot this
                # pass iterates may predate a concurrent release by the
                # live owner (the fail/retry path releases the fence
                # before the outbox row). Re-read the CURRENT durable
                # outbox state: only genuinely stranded in_flight work
                # is requeued here — never work the owner already
                # returned to pending or completed.
                fresh = await self._outbox_view(work_id)
                if fresh is not None and fresh.state != "in_flight":
                    continue
                await kernel_outbox.release(self._sf, work_id)
                continue
            if lease.state == "leased" and lease.lease_expires_at is not None:
                if _as_utc(lease.lease_expires_at) > _utcnow():
                    continue  # healthy current generation
                # Lapsed lease: vacate the dead generation so the next
                # claim is a takeover with a fresh fence. Any in-process
                # bookkeeping for that generation is stale — end it and
                # kill its worker so late terminal code has no path in.
                stale = self._active_by_work.get(work_id)
                if stale is not None and stale.fencing_token == lease.fencing_token:
                    stale.superseded = True
                    pid = None
                    manager = self._task_manager
                    if manager is not None:
                        pid = getattr(manager, "_pids", {}).get(stale.job_id)
                    self._end_claim(stale)
                    if pid is not None and manager is not None:
                        try:
                            manager._kill_pid(pid)
                        except Exception:  # noqa: BLE001 - best effort
                            logger.warning("failed killing zombie worker pid %s", pid)
                await fencing.release(
                    self._sf,
                    work_id=work_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                )
            # state released (or just lapsed): requeue or terminal-fail.
            await self._requeue_or_fail(work_id, view.payload)

    async def _requeue_or_fail(self, work_id: int, payload: dict[str, Any]) -> None:
        job_id = str((payload or {}).get("job_id") or "")
        if not job_id:
            await kernel_outbox.release(self._sf, work_id)
            return
        # Decisions must come from CURRENT durable state, never from the
        # caller's snapshot: the owner's fail/retry/cancel paths release
        # the fence BEFORE the outbox row, so a watchdog iterating a
        # stale in_flight list can reach this point for work the owner
        # is already returning to pending (or finishing). Re-read the
        # outbox: only genuinely stranded in_flight work is requeued or
        # terminal-failed here.
        view = await self._outbox_view(work_id)
        if view is None or view.state != "in_flight":
            return
        max_retries = 0
        async with self._sf() as session:
            row = await session.get(ConversionJob, job_id)
            if row is not None and row.status in TERMINAL_ROW_STATUSES:
                await self._ack_work(work_id)
                return
            if row is not None:
                max_retries = int(row.max_retries or 0)
        attempts = int(view.attempts)
        # A lapsed lease is a crash, not an executed failure: one lapse
        # retry is always allowed (legacy recovery parity), then the
        # explicit retry budget governs.
        budget = max(1, max_retries)
        if attempts < budget:
            await kernel_outbox.release(self._sf, work_id)
            await self._append_event(
                EVENT_WORK_RETRY,
                {"work_id": work_id, "job_id": job_id, "attempts": attempts + 1, "error": "lease lapsed"},
            )
            async with self._sf() as session:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id == job_id)
                    .where(ConversionJob.status.not_in(TERMINAL_ROW_STATUSES))
                    .values(status="pending")
                )
                await session.commit()
        else:
            await self._terminal_fail(
                work_id,
                job_id,
                "conversion worker lease expired (crash or hang); retry budget exhausted",
                attempts,
            )

    # ------------------------------------------------------------------
    # restart recovery (Band 3 + crash matrix)
    # ------------------------------------------------------------------

    async def recover(self) -> dict[str, Any]:
        """Converge kernel + compatibility state after a process restart."""
        report: dict[str, Any] = {
            "events_repaired": [],
            "acked_after_accept": [],
            "projected_completed": [],
            "projected_terminal": [],
            "adopted": [],
            "swept": [],
            "requeued": [],
        }
        report["events_repaired"] = (
            await scheduler.reconcile_dispatch(self._sf, workspace_id=self.workspace_id)
        )["events_repaired"]

        views = await kernel_outbox.list_outbox(self._sf, workspace_id=self.workspace_id)
        for view in views:
            work_id = view.id
            job_id = str(view.payload.get("job_id") or "")
            if not job_id:
                continue
            # Publication and terminal-event projection apply to every
            # state: a crash between the ack and the row write (or between
            # acceptance and projection) leaves a done/in_flight row whose
            # compatibility projection must still converge.
            publication = await fencing.get_publication(self._sf, work_id=work_id)
            if publication is not None:
                # Accepted truth exists: finish the ack and project the row.
                lease = await fencing.get_lease(self._sf, work_id)
                token = lease.fencing_token if lease is not None else None
                if view.state == "in_flight" and token is not None:
                    if await fencing.complete_work(self._sf, work_id=work_id, fencing_token=token):
                        report["acked_after_accept"].append(work_id)
                if await self._project_publication(job_id, publication):
                    report["projected_completed"].append(job_id)
                continue
            terminal_event = await self._latest_terminal_event(work_id)
            if terminal_event is not None:
                event_type, payload = terminal_event
                status = "failed" if event_type == EVENT_WORK_FAILED else "cancelled"
                if await self._project_terminal_event(job_id, status, payload):
                    report["projected_terminal"].append(job_id)
                await self._ack_work(work_id)
                continue
            if view.state == "done":
                # Done with no publication and no terminal event: nothing
                # durable describes an outcome — leave the row alone (the
                # row itself is the only state; adoption/sweep own it).
                continue
            if view.state == "in_flight":
                lease = await fencing.get_lease(self._sf, work_id)
                healthy = (
                    lease is not None
                    and lease.state == "leased"
                    and lease.lease_expires_at is not None
                    and _as_utc(lease.lease_expires_at) > _utcnow()
                )
                if not healthy:
                    if lease is not None and lease.state == "leased":
                        await fencing.release(
                            self._sf,
                            work_id=work_id,
                            owner_id=lease.owner_id,
                            fencing_token=lease.fencing_token,
                        )
                    await self._requeue_or_fail(work_id, view.payload)
                    report["requeued"].append(work_id)
                # An unexpired lease cannot survive its owner process, but
                # the watchdog owns its lapse either way — no invented state.
            # pending: the dispatch loop owns it from here.

        # Adopt compatibility rows that predate or lost their authorization.
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(ConversionJob.id, ConversionJob.config_json, ConversionJob.max_retries)
                    .where(ConversionJob.status.in_(["pending", "processing"]))
                    .where(ConversionJob.queue_backend.is_not(None))
                )
            ).all()
        for job_id, config_json, _max_retries in rows:
            work_id = await self.resolve_work_for_job(job_id)
            if work_id is not None:
                continue
            config: dict[str, Any] = {}
            try:
                parsed = json.loads(config_json) if config_json else {}
                config = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                config = {}
            try:
                await self.authorize(job_id, config)
                report["adopted"].append(job_id)
            except Exception:  # noqa: BLE001 - one bad row must not block boot
                logger.exception("kernel adoption failed for job %s", job_id)

        # Sweep abandoned non-durable rows (pre-kernel in-memory jobs).
        async with self._sf() as session:
            swept = (
                await session.execute(
                    select(ConversionJob.id)
                    .where(ConversionJob.status.in_(["pending", "processing"]))
                    .where(ConversionJob.queue_backend.is_(None))
                )
            ).scalars().all()
            if swept:
                await session.execute(
                    update(ConversionJob)
                    .where(ConversionJob.id.in_(list(swept)))
                    .values(
                        status="failed",
                        error_message="Interrupted by server restart",
                        completed_at=_utcnow(),
                    )
                )
                await session.commit()
                report["swept"] = list(swept)
        return report

    async def _project_publication(self, job_id: str, publication: fencing.Publication) -> bool:
        """Project accepted truth onto a non-terminal compatibility row."""
        async with self._sf() as session:
            row = await session.get(ConversionJob, job_id)
            if row is None or row.status in TERMINAL_ROW_STATUSES:
                return False
        descriptor = publication.result or {}
        result_path = str(descriptor.get("result_path") or "")
        result_text: str | None = None
        if result_path and Path(result_path).is_file():
            try:
                # Off the runtime loop: the accepted primary output can
                # be large, and this coroutine shares the loop with
                # dispatch/renewal/watchdog during recovery.
                result_text = await asyncio.to_thread(
                    Path(result_path).read_text,
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                result_text = None
        output_format = str(descriptor.get("output_format") or row.output_format or "markdown")
        async with self._sf() as session:
            await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status.not_in(TERMINAL_ROW_STATUSES))
                .values(
                    status="completed",
                    output_format=output_format,
                    result_text=result_text,
                    result_path=result_path or None,
                    progress=100,
                    completed_at=_utcnow(),
                )
            )
            await session.commit()
        logger.info(
            "job %s projected completed from publication %s",
            job_id,
            publication.publication_id,
        )
        return True

    async def _project_terminal_event(
        self, job_id: str, status: str, payload: dict[str, Any]
    ) -> bool:
        values: dict[str, Any] = {
            "status": status,
            "completed_at": _utcnow(),
        }
        if status == "failed":
            values["error_message"] = str(payload.get("error") or "conversion failed")[
                :_MAX_ERROR_CHARS
            ]
        async with self._sf() as session:
            result = await session.execute(
                update(ConversionJob)
                .where(ConversionJob.id == job_id)
                .where(ConversionJob.status.not_in(TERMINAL_ROW_STATUSES))
                .values(**values)
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def _latest_terminal_event(
        self, work_id: int
    ) -> tuple[str, dict[str, Any]] | None:
        events = await kernel_events.replay(
            self._sf, workspace_id=self.workspace_id, stream="work"
        )
        found: tuple[str, dict[str, Any]] | None = None
        for event in events:
            if event.event_type not in (EVENT_WORK_FAILED, EVENT_WORK_CANCELLED):
                continue
            try:
                payload = dict(event.payload or {})
            except Exception:  # noqa: BLE001
                payload = {}
            if payload.get("work_id") == work_id:
                found = (event.event_type, payload)
        return found

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _outbox_view(self, work_id: int) -> kernel_outbox.OutboxView | None:
        views = await kernel_outbox.list_outbox(self._sf, workspace_id=self.workspace_id)
        for view in views:
            if view.id == work_id:
                return view
        return None

    async def _append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        await kernel_events.append(
            self._sf,
            workspace_id=self.workspace_id,
            stream="work",
            event_type=event_type,
            payload=payload,
        )

    def _end_claim(self, claim: ActiveClaim) -> None:
        claim.finished = True
        if claim.renew_task is not None and not claim.renew_task.done():
            claim.renew_task.cancel()
        claim.renew_task = None
        if self._active_by_job.get(claim.job_id) is claim:
            self._active_by_job.pop(claim.job_id, None)
        if self._active_by_work.get(claim.work_id) is claim:
            self._active_by_work.pop(claim.work_id, None)
        manager = self._task_manager
        if manager is not None and getattr(manager, "_kernel_claims", {}).get(claim.job_id) is claim:
            manager._kernel_claims.pop(claim.job_id, None)


# ----------------------------------------------------------------------
# accepted-result descriptor (bounded, canonical, transport-independent)
# ----------------------------------------------------------------------


def build_result_descriptor(
    *,
    job_id: str,
    output_format: str,
    result_text: str,
    formats_json: str | None,
    result_metadata_json: str | None,
    final_path: Path,
    manifest_path: Path,
    asset_count: int,
    formats: list[str],
) -> dict[str, Any]:
    """Build the canonical accepted-publication payload for a conversion.

    The descriptor is bounded (hashes and lengths only — never document
    bodies), survives process transport, and describes the *resolved*
    durable output. PR68A ArtifactHandle envelopes are always resolved
    to real bytes before this runs, so no ephemeral handle pathname can
    become accepted truth. ``result_path`` is included because restart
    recovery must be able to project the compatibility row (re-reading
    the primary output file) after a crash between acceptance and row
    projection.
    """
    text_bytes = result_text.encode("utf-8", errors="replace")
    descriptor: dict[str, Any] = {
        "kind": "conversion.result",
        "schema": 1,
        "job_id": str(job_id),
        "output_format": str(output_format),
        "result_path": str(final_path),
        "result_text": {"bytes": len(text_bytes), "sha256": _sha256_bytes(text_bytes)},
        "result_file": {"name": final_path.name, "bytes": 0, "sha256": ""},
        "formats": [str(f) for f in formats],
        "assets_count": int(asset_count),
        "manifest_file": manifest_path.name,
    }
    try:
        file_bytes = final_path.read_bytes()
        descriptor["result_file"] = {
            "name": final_path.name,
            "bytes": len(file_bytes),
            "sha256": _sha256_bytes(file_bytes),
        }
    except OSError:
        pass
    if formats_json:
        encoded = formats_json.encode("utf-8")
        descriptor["formats_json"] = {"bytes": len(encoded), "sha256": _sha256_bytes(encoded)}
    if result_metadata_json:
        encoded = result_metadata_json.encode("utf-8")
        descriptor["result_metadata_json"] = {
            "bytes": len(encoded),
            "sha256": _sha256_bytes(encoded),
        }
    return descriptor
