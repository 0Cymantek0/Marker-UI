"""Conservative two-phase local garbage collection (V3.2 PR65B).

The kernel database is the only authority for what must survive. This
collector never deletes committed truth — records, edges, manifests,
and payload registry rows are permanent metadata — it retires exactly
three things:

* **derived generation state** — superseded/failed materialized rows
  (and stale never-activated staging residue) that no current pointer,
  active hold, or unexpired reader pin protects;
* **publication serving state (PR76)** — superseded/failed (or stale
  staged/validated) publication sets that no publication head and no
  unexpired publication pin protects, plus lexical generations no
  surviving set row references (dropping their runtime-managed physical
  tables transactionally — FTS5 virtual tables on SQLite, tsvector+GIN
  tables on PostgreSQL, both named by the generation manifest);
* **physical payload bytes** — content-addressed objects whose hashes
  no live retention root requires, plus pre-commit orphan objects the
  registry never accepted.

Lifecycle (mark → recheck → tombstone → sweep), linearization honesty:

1. **Mark** (:func:`plan_collection`) builds a read-only plan at the
   current root set. Pure reads; nothing durable. A plan is evidence,
   never authorization.
2. **Recheck + tombstone** (:func:`execute_collection`) opens ONE write
   transaction, recomputes the live closure from freshly read roots,
   and inserts tombstones for whatever is still unreachable. That
   transaction's commit is **the deletion linearization point**: every
   root, pin, or hold committed before it is honored; every one
   committed after it is a post-decision root that sees honest
   ``retired``/degraded availability and may heal by re-staging exact
   bytes through the normal publish path. On SQLite the transaction
   opens write-first so the single writer lock serializes it against
   every root/pin writer; on PostgreSQL the transaction instead takes
   ``pg_advisory_xact_lock`` on ``PAYLOAD_DECISION_LOCK_SCOPE``, the
   same scope retention roots, reader pins, generation activation, and
   payload-carrying commits acquire — root-creating transactions and
   deletion decisions therefore linearize on both profiles.
3. **Sweep** retires one object per short write transaction. Each sweep
   transaction write-first claims the tombstone row, re-checks it is
   still pending/failed (a concurrent commit's rescue may have deleted
   it), deletes the bytes through the store's maintenance capability,
   then records the outcome. Already-absent objects converge
   idempotently; deletion failures are recorded as retryable
   ``failed`` state, never as success.
4. **Restart reconciliation** (:func:`reconcile_retirements`) resumes
   pending/failed tombstones with no process memory: pending + object
   present → delete; pending + object absent → deleted; failed →
   retry.

Safety boundaries this module refuses to cross:

* reachability is computed over the **whole physical dedup domain** —
  all workspaces' roots union — because blob keys are shared store-wide
  (a workspace-scoped orphan list is a known reporting quirk of
  reconciliation and is deliberately not reused here);
* unknown/ambiguous dependency knowledge retains bytes;
* ``staged``/``validated`` generations are only collectible once their
  staging age exceeds a grace threshold, so in-flight builds never race
  the collector;
* current generations are structurally never candidates;
* lexical retirement is proof-closed on both profiles: a generation
  referenced by any surviving publication set row, protected by an
  unexpired pin, or still inside the staging grace is rescued, and the
  physical artifact named by its manifest is dropped in the same
  transaction as its locator and manifest rows (PR83B2 gave
  PostgreSQL generations real physical artifacts, replacing the former
  fail-closed boundary).

Store-neutral since PR83B1 WS6: physical accounting (orphan age/size)
and deletion run through ``PayloadMaintenanceStore`` — the local
filesystem and S3-compatible profiles prove the same semantics.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.dialects import (
    advisory_xact_lock,
    dialect_insert,
    run_with_contention_retry,
)
from app.kernel.errors import (
    InjectedFaultError,
    KernelError,
    PayloadStageError,
)
from app.kernel.generations import (
    GENERATION_STATE_FAILED,
    GENERATION_STATE_STAGED,
    GENERATION_STATE_SUPERSEDED,
    GENERATION_STATE_VALIDATED,
)
from app.kernel.models import (
    KernelGeneration,
    KernelGenerationEdge,
    KernelGenerationHead,
    KernelGenerationRecord,
    KernelLexicalGeneration,
    KernelLexicalRow,
    KernelPayloadObject,
    KernelPayloadRetirement,
    KernelPublicationHead,
    KernelPublicationPin,
    KernelPublicationSet,
    KernelReaderPin,
    KernelRecord,
    KernelRetentionRoot,
)
from app.kernel.payloads import (
    PAYLOAD_DECISION_LOCK_SCOPE,
    PayloadMaintenanceStore,
)
from app.kernel.publications import (
    LEXICAL_STATE_FAILED,
    LEXICAL_STATE_STAGED,
    LEXICAL_STATE_VALIDATED,
    PUBLICATION_STATE_FAILED,
    PUBLICATION_STATE_STAGED,
    PUBLICATION_STATE_SUPERSEDED,
    PUBLICATION_STATE_VALIDATED,
)
from app.kernel.retention import ROOT_STATE_ACTIVE
from app.kernel.snapshots import PAYLOAD_REQUIREMENT_METADATA_ONLY

__all__ = [
    "DEFAULT_STALE_STAGING_SECONDS",
    "GC_FAULT_PHASES",
    "GC_PAUSE_PHASES",
    "GenerationCandidate",
    "CollectionPlan",
    "CollectionReport",
    "PHASE_GC_AFTER_GENERATIONS",
    "PHASE_GC_AFTER_MARK",
    "PHASE_GC_AFTER_RECHECK",
    "PHASE_GC_AFTER_SWEEP",
    "PHASE_GC_AFTER_UNLINK",
    "PHASE_GC_BEFORE_UNLINK",
    "RETIRE_REASON_ORPHAN",
    "RETIRE_REASON_UNREACHABLE",
    "RETIRE_STATE_DELETED",
    "RETIRE_STATE_FAILED",
    "RETIRE_STATE_PENDING",
    "collect",
    "execute_collection",
    "plan_collection",
    "reconcile_retirements",
]

#: Tombstone lifecycle. ``pending`` = authorized, bytes present;
#: ``deleted`` = bytes absent by decision; ``failed`` = unlink errored
#: (retryable, never a false success).
RETIRE_STATE_PENDING = "pending"
RETIRE_STATE_DELETED = "deleted"
RETIRE_STATE_FAILED = "failed"

#: Why a candidate was eligible at decision time.
RETIRE_REASON_UNREACHABLE = "unreachable"
RETIRE_REASON_ORPHAN = "orphan"

#: Deterministic fault-injection phases (test-only parameters).
PHASE_GC_AFTER_MARK = "gc-after-mark"
PHASE_GC_AFTER_RECHECK = "gc-after-recheck"
PHASE_GC_AFTER_GENERATIONS = "gc-after-generations"
PHASE_GC_BEFORE_UNLINK = "gc-before-unlink"
PHASE_GC_AFTER_UNLINK = "gc-after-unlink"
PHASE_GC_AFTER_SWEEP = "gc-after-sweep"

GC_FAULT_PHASES = frozenset(
    {
        PHASE_GC_AFTER_MARK,
        PHASE_GC_AFTER_RECHECK,
        PHASE_GC_AFTER_GENERATIONS,
        PHASE_GC_BEFORE_UNLINK,
        PHASE_GC_AFTER_UNLINK,
        PHASE_GC_AFTER_SWEEP,
    }
)

#: Deterministic barrier hooks (test-only parameter ``_test_pause``).
#: Unlike faults, a pause does not fail the collector: it suspends the
#: recheck transaction at a named point so tests can force true overlap
#: with a concurrent root/pin/commit writer and then release it.
PHASE_GC_PAUSE_BEFORE_LOCK = "recheck-before-lock"
PHASE_GC_PAUSE_AFTER_ROOTS = "recheck-after-roots"
GC_PAUSE_PHASES = frozenset({PHASE_GC_PAUSE_BEFORE_LOCK, PHASE_GC_PAUSE_AFTER_ROOTS})

#: ``staged``/``validated`` generations younger than this are retained:
#: they may be an in-flight or resumable build. Crash residue older
#: than the threshold is collectible through the normal recheck path.
DEFAULT_STALE_STAGING_SECONDS = 3600.0


class _GcRetry:
    """The shared contention budget with GC-level retry observability.

    Delegates to :func:`app.kernel.dialects.run_with_contention_retry`
    (one vocabulary, one bounded envelope — SQLite busy text and
    PostgreSQL 40001/40P01/55P03 alike) and counts the retries the
    collection report surfaces.
    """

    def __init__(self, operation_name: str = "gc operation") -> None:
        self.busy_retries = 0
        self._operation_name = operation_name

    async def run(self, operation: Callable[[], Any]) -> Any:
        def _count(_n: int, _exc: BaseException) -> None:
            self.busy_retries += 1

        return await run_with_contention_retry(
            operation, on_retry=_count, operation_name=self._operation_name
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# plan (mark phase)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationCandidate:
    """One derived generation the plan believes is collectible."""

    generation_id: str
    workspace_id: str
    state: str
    kernel_commit_id: int
    reason: str


@dataclass(frozen=True)
class CollectionPlan:
    """Read-only mark-phase result; evidence, never authorization."""

    planned_at: datetime
    workspace_scope: str | None
    root_cuts: tuple[tuple[str, int, str], ...]
    live_blob_keys: frozenset[str]
    registry_total: int
    retained_objects: int
    retained_bytes: int
    candidate_registry_keys: tuple[str, ...]
    candidate_registry_bytes: int
    candidate_orphan_keys: tuple[str, ...]
    candidate_orphan_bytes: int
    eligible_generations: tuple[GenerationCandidate, ...]
    retained_generations: int
    conservative_notes: tuple[str, ...] = field(default_factory=tuple)
    eligible_publication_sets: tuple[str, ...] = field(default_factory=tuple)
    eligible_lexical_generations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def roots(self) -> int:
        return len(self.root_cuts)

    def summary(self) -> dict[str, Any]:
        return {
            "planned_at": self.planned_at.isoformat(),
            "workspace_scope": self.workspace_scope,
            "roots": len(self.root_cuts),
            "live_objects": len(self.live_blob_keys),
            "registry_objects": self.registry_total,
            "retained_objects": self.retained_objects,
            "retained_bytes": self.retained_bytes,
            "candidate_registry_objects": len(self.candidate_registry_keys),
            "candidate_registry_bytes": self.candidate_registry_bytes,
            "candidate_orphan_objects": len(self.candidate_orphan_keys),
            "candidate_orphan_bytes": self.candidate_orphan_bytes,
            "eligible_generations": len(self.eligible_generations),
            "retained_generations": self.retained_generations,
            "conservative_notes": list(self.conservative_notes),
        }


async def _live_blob_keys(session: Any) -> set[str]:
    """Union of payload hashes required by every live root closure.

    Runs inside the caller's transaction/session: the recheck path must
    compute liveness from exactly the state its own transaction sees.
    Roots declaring ``metadata_only`` contribute nothing — their
    inspectability class does not require bytes.
    """
    root_cuts = await load_active_root_cuts_from(session)
    live: set[str] = set()
    seen: set[tuple[str, int]] = set()
    for workspace_id, cut, required in root_cuts:
        if required == PAYLOAD_REQUIREMENT_METADATA_ONLY:
            continue
        if (workspace_id, cut) in seen:  # several roots, one closure query
            continue
        seen.add((workspace_id, cut))
        rows = (
            await session.execute(
                select(KernelRecord.payload_byte_hash)
                .where(
                    KernelRecord.workspace_id == workspace_id,
                    KernelRecord.kernel_commit_id <= cut,
                    KernelRecord.payload_byte_hash.is_not(None),
                )
                .distinct()
            )
        ).all()
        live.update(row[0] for row in rows)
    return live


async def load_active_root_cuts_from(session: Any) -> tuple[tuple[str, int, str], ...]:
    """Session-bound twin of ``retention.load_active_root_cuts``.

    The recheck transaction must read roots through its own session so
    the liveness decision and the tombstone writes linearize together.
    PR76 adds publication roots: the head-current published set and any
    pinned published set contribute their materialized member's
    (workspace, cut, required payload class) closure.
    """
    now = _utcnow()
    current_ids = {
        row[0]
        for row in (
            await session.execute(select(KernelGenerationHead.current_generation_id))
        ).all()
    }
    pinned_ids = {
        row[0]
        for row in (
            await session.execute(
                select(KernelReaderPin.generation_id).where(
                    KernelReaderPin.expires_at > now
                )
            )
        ).all()
    }
    protected = current_ids | pinned_ids
    live: set[tuple[str, int, str]] = set()
    if protected:
        gen_rows = (
            await session.execute(
                select(
                    KernelGeneration.workspace_id,
                    KernelGeneration.kernel_commit_id,
                    KernelGeneration.required_payload_state,
                ).where(KernelGeneration.generation_id.in_(protected))
            )
        ).all()
        live.update((row[0], row[1], row[2]) for row in gen_rows)
    hold_rows = (
        await session.execute(
            select(
                KernelRetentionRoot.workspace_id,
                KernelRetentionRoot.kernel_commit_id,
                KernelRetentionRoot.required_payload_state,
                KernelRetentionRoot.expires_at,
            ).where(KernelRetentionRoot.state == ROOT_STATE_ACTIVE)
        )
    ).all()
    for workspace_id, cut, required, expires_at in hold_rows:
        expires = _as_utc(expires_at)
        if expires is not None and expires <= now:
            continue
        live.add((workspace_id, cut, required))
    live_set_ids = await _publication_live_set_ids(session)
    if live_set_ids:
        member_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(KernelPublicationSet.materialized_generation_id).where(
                        KernelPublicationSet.publication_set_id.in_(live_set_ids)
                    )
                )
            ).all()
        }
        if member_ids:
            pub_gen_rows = (
                await session.execute(
                    select(
                        KernelGeneration.workspace_id,
                        KernelGeneration.kernel_commit_id,
                        KernelGeneration.required_payload_state,
                    ).where(KernelGeneration.generation_id.in_(member_ids))
                )
            ).all()
            live.update((row[0], row[1], row[2]) for row in pub_gen_rows)
    return tuple(sorted(live))


def _live_publication_set_ids_subquery():
    """Set ids protected right now: head-current or pinned by an
    unexpired publication pin (usable inside retirement transactions)."""
    from sqlalchemy import select as _select

    return _select(KernelPublicationHead.current_publication_set_id).union(
        _select(KernelPublicationPin.publication_set_id).where(
            KernelPublicationPin.expires_at > _utcnow()
        )
    )


async def _publication_live_set_ids(session: Any) -> set[str]:
    rows = (
        await session.execute(_live_publication_set_ids_subquery())
    ).all()
    return {row[0] for row in rows if row[0] is not None}


async def plan_collection(
    session_factory: async_sessionmaker,
    store: PayloadMaintenanceStore,
    *,
    workspace_id: str | None = None,
    stale_staging_seconds: float = DEFAULT_STALE_STAGING_SECONDS,
    orphan_min_age_seconds: float = 0.0,
) -> CollectionPlan:
    """Mark phase: a deterministic, read-only collection plan.

    Reachability is always computed store-wide (all workspaces' roots
    union) because blob keys are deduplicated across workspaces; the
    optional ``workspace_id`` only scopes which workspaces' derived
    *generations* may be retired. Nothing here writes or deletes.
    """
    async with session_factory() as session:
        root_cuts = await load_active_root_cuts_from(session)
        live = await _live_blob_keys(session)

        registry_rows = (
            await session.execute(
                select(KernelPayloadObject.blob_key, KernelPayloadObject.payload_length)
            )
        ).all()
        current_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(KernelGenerationHead.current_generation_id)
                )
            ).all()
        }
        pinned_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(KernelReaderPin.generation_id).where(
                        KernelReaderPin.expires_at > _utcnow()
                    )
                )
            ).all()
        }
        held_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(KernelRetentionRoot.target_generation_id).where(
                        KernelRetentionRoot.state == ROOT_STATE_ACTIVE,
                        KernelRetentionRoot.target_generation_id.is_not(None),
                    )
                )
            ).all()
        }
        gen_stmt = select(
            KernelGeneration.generation_id,
            KernelGeneration.workspace_id,
            KernelGeneration.state,
            KernelGeneration.kernel_commit_id,
            KernelGeneration.created_at,
        ).order_by(KernelGeneration.created_at.asc())
        if workspace_id is not None:
            gen_stmt = gen_stmt.where(KernelGeneration.workspace_id == workspace_id)
        generation_rows = (await session.execute(gen_stmt)).all()

        live_set_ids = await _publication_live_set_ids(session)
        set_stmt = select(
            KernelPublicationSet.publication_set_id,
            KernelPublicationSet.workspace_id,
            KernelPublicationSet.state,
            KernelPublicationSet.created_at,
            KernelPublicationSet.lexical_generation_id,
        ).order_by(KernelPublicationSet.created_at.asc())
        if workspace_id is not None:
            set_stmt = set_stmt.where(KernelPublicationSet.workspace_id == workspace_id)
        publication_set_rows = (await session.execute(set_stmt)).all()

        lexical_stmt = select(
            KernelLexicalGeneration.lexical_generation_id,
            KernelLexicalGeneration.state,
            KernelLexicalGeneration.created_at,
        ).order_by(KernelLexicalGeneration.created_at.asc())
        if workspace_id is not None:
            lexical_stmt = lexical_stmt.where(
                KernelLexicalGeneration.workspace_id == workspace_id
            )
        lexical_generation_rows = (await session.execute(lexical_stmt)).all()

    registry_keys = {row[0] for row in registry_rows}
    registry_lengths = {row[0]: row[1] for row in registry_rows}
    candidates = sorted(key for key in registry_keys if key not in live)
    retained_objects = len(registry_keys) - len(candidates)
    retained_bytes = sum(
        length for key, length in registry_lengths.items() if key in live
    )

    physical = await store.list_objects()
    now_ts = _utcnow().timestamp()
    orphan_candidates: list[str] = []
    orphan_bytes = 0
    for key in physical:
        if key in registry_keys:
            continue
        stat = await store.stat_object(key)
        if stat is None:
            continue  # vanished mid-scan; the next pass will see truth
        if orphan_min_age_seconds > 0:
            age = now_ts - stat.last_modified_epoch
            if age < orphan_min_age_seconds:
                continue
        orphan_candidates.append(key)
        orphan_bytes += stat.length

    stale_horizon = _utcnow() - timedelta(seconds=stale_staging_seconds)
    eligible: list[GenerationCandidate] = []
    retained_generations = 0
    fresh_staging = 0
    for generation_id, ws, state, cut, created_at in generation_rows:
        if (
            generation_id in current_ids
            or generation_id in pinned_ids
            or generation_id in held_ids
        ):
            retained_generations += 1
            continue
        if state in (GENERATION_STATE_SUPERSEDED, GENERATION_STATE_FAILED):
            eligible.append(
                GenerationCandidate(
                    generation_id=generation_id,
                    workspace_id=ws,
                    state=state,
                    kernel_commit_id=cut,
                    reason=f"state={state}",
                )
            )
            continue
        if state in (GENERATION_STATE_STAGED, GENERATION_STATE_VALIDATED):
            created = _as_utc(created_at)
            if created is not None and created >= stale_horizon:
                fresh_staging += 1
                retained_generations += 1
                continue
            eligible.append(
                GenerationCandidate(
                    generation_id=generation_id,
                    workspace_id=ws,
                    state=state,
                    kernel_commit_id=cut,
                    reason=f"stale-{state}",
                )
            )
            continue
        retained_generations += 1  # active but not current: guarded residue

    # PR76 serving state: superseded/failed (or stale staged/validated)
    # publication sets that no head and no unexpired pin protects are
    # eligible; a lexical generation is eligible only when no surviving
    # set row still references it. Both decisions are re-checked inside
    # the retirement transactions before anything is deleted.
    eligible_sets: list[str] = []
    for set_id, ws, state, created_at, lexical_id in publication_set_rows:
        if set_id in live_set_ids:
            continue
        if state in (PUBLICATION_STATE_SUPERSEDED, PUBLICATION_STATE_FAILED):
            eligible_sets.append(set_id)
            continue
        if state in (PUBLICATION_STATE_STAGED, PUBLICATION_STATE_VALIDATED):
            created = _as_utc(created_at)
            if created is not None and created >= stale_horizon:
                continue  # fresh staging: in-flight or resumable candidate
            eligible_sets.append(set_id)
        # published but not live (no head, no pin): guarded residue
    eligible_set_ids = set(eligible_sets)
    protecting_lexical_ids = {
        lexical_id
        for set_id, _ws, state, _created, lexical_id in publication_set_rows
        if set_id not in eligible_set_ids and lexical_id is not None
    }
    eligible_lexical: list[str] = []
    for lexical_id, state, created_at in lexical_generation_rows:
        if lexical_id in protecting_lexical_ids:
            continue
        if state in (LEXICAL_STATE_FAILED, LEXICAL_STATE_VALIDATED):
            # failed: rejected build. validated: either never staged into
            # a set, or its protecting set is itself eligible and retires
            # first in the same pass — either way nothing live names it.
            # (Only STAGED residue keeps the in-flight grace: a lexical
            # generation in state validated can never regress, so the
            # build→stage window is the sole race, and a racing builder
            # converges by rebuilding the same deterministic identity.)
            eligible_lexical.append(lexical_id)
            continue
        if state == LEXICAL_STATE_STAGED:
            created = _as_utc(created_at)
            if created is not None and created >= stale_horizon:
                continue
            eligible_lexical.append(lexical_id)

    notes: list[str] = []
    if fresh_staging:
        notes.append(
            f"retained {fresh_staging} staged/validated generations younger than "
            f"{stale_staging_seconds}s (possible in-flight or resumable builds)"
        )
    notes.append("kernel records/edges/manifests and registry rows are never deleted")

    return CollectionPlan(
        planned_at=_utcnow(),
        workspace_scope=workspace_id,
        root_cuts=root_cuts,
        live_blob_keys=frozenset(live),
        registry_total=len(registry_keys),
        retained_objects=retained_objects,
        retained_bytes=retained_bytes,
        candidate_registry_keys=tuple(candidates),
        candidate_registry_bytes=sum(registry_lengths[k] for k in candidates),
        candidate_orphan_keys=tuple(orphan_candidates),
        candidate_orphan_bytes=orphan_bytes,
        eligible_generations=tuple(eligible),
        retained_generations=retained_generations,
        conservative_notes=tuple(notes),
        eligible_publication_sets=tuple(eligible_sets),
        eligible_lexical_generations=tuple(eligible_lexical),
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionReport:
    """Measured outcome of one collection (or reconciliation) pass."""

    dry_run: bool
    workspace_scope: str | None
    roots_at_recheck: int
    active_pins_at_recheck: int
    objects_considered: int
    retained_objects: int
    retained_bytes: int
    eligible_registry_objects: int
    eligible_registry_bytes: int
    orphan_objects: int
    rescued_count: int
    rescued_keys: tuple[str, ...]
    tombstoned: int
    swept_deleted: int
    already_absent: int
    failed_keys: tuple[str, ...]
    bytes_reclaimed: int
    generations_eligible: int
    generations_retired: int
    generations_rescued: tuple[str, ...]
    expired_pins_purged: int
    busy_retries: int
    publication_sets_eligible: int = 0
    publication_sets_retired: int = 0
    publication_sets_rescued: tuple[str, ...] = ()
    lexical_generations_eligible: int = 0
    lexical_generations_retired: int = 0
    lexical_generations_rescued: tuple[str, ...] = ()
    expired_publication_pins_purged: int = 0
    duration_seconds: dict[str, float] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "workspace_scope": self.workspace_scope,
            "roots_at_recheck": self.roots_at_recheck,
            "active_pins_at_recheck": self.active_pins_at_recheck,
            "objects_considered": self.objects_considered,
            "retained_objects": self.retained_objects,
            "retained_bytes": self.retained_bytes,
            "eligible_registry_objects": self.eligible_registry_objects,
            "eligible_registry_bytes": self.eligible_registry_bytes,
            "orphan_objects": self.orphan_objects,
            "rescued_count": self.rescued_count,
            "tombstoned": self.tombstoned,
            "swept_deleted": self.swept_deleted,
            "already_absent": self.already_absent,
            "failed_count": len(self.failed_keys),
            "bytes_reclaimed": self.bytes_reclaimed,
            "generations_eligible": self.generations_eligible,
            "generations_retired": self.generations_retired,
            "generations_rescued_count": len(self.generations_rescued),
            "expired_pins_purged": self.expired_pins_purged,
            "publication_sets_eligible": self.publication_sets_eligible,
            "publication_sets_retired": self.publication_sets_retired,
            "publication_sets_rescued_count": len(self.publication_sets_rescued),
            "lexical_generations_eligible": self.lexical_generations_eligible,
            "lexical_generations_retired": self.lexical_generations_retired,
            "lexical_generations_rescued_count": len(
                self.lexical_generations_rescued
            ),
            "expired_publication_pins_purged": self.expired_publication_pins_purged,
            "busy_retries": self.busy_retries,
            "duration_seconds": self.duration_seconds,
        }


# ---------------------------------------------------------------------------
# execute (recheck + tombstone + generation retirement + sweep)
# ---------------------------------------------------------------------------


@dataclass
class _ExecutionState:
    roots: int = 0
    pins: int = 0
    rescued: list[str] = field(default_factory=list)
    still_registry: list[str] = field(default_factory=list)
    tombstoned_keys: list[str] = field(default_factory=list)
    orphan_still: list[str] = field(default_factory=list)
    orphan_rescued: list[str] = field(default_factory=list)
    registry_lengths: dict[str, int] = field(default_factory=dict)
    retained_objects: int = 0
    retained_bytes: int = 0
    pins_purged: int = 0
    pub_pins_purged: int = 0


def _check_fault(phase: str | None, expected: str) -> None:
    if phase == expected:
        raise InjectedFaultError(phase)


def _validate_fault_phase(phase: str | None) -> None:
    if phase is not None and phase not in GC_FAULT_PHASES:
        raise KernelError(f"unknown fault phase {phase!r}")


def _validate_pause_hook(pause: Callable[[str], Awaitable[None]] | None) -> None:
    if pause is not None and not asyncio.iscoroutinefunction(pause):
        raise KernelError("_test_pause must be an async callable")


async def _maybe_pause(
    pause: Callable[[str], Awaitable[None]] | None, phase: str
) -> None:
    if pause is not None:
        await pause(phase)


async def execute_collection(
    session_factory: async_sessionmaker,
    store: PayloadMaintenanceStore,
    plan: CollectionPlan,
    *,
    dry_run: bool = False,
    stale_staging_seconds: float = DEFAULT_STALE_STAGING_SECONDS,
    _inject_fault_at: str | None = None,
    _test_pause: Callable[[str], Awaitable[None]] | None = None,
) -> CollectionReport:
    """Recheck the plan against fresh authority; retire what survives.

    The recheck + tombstone transaction is the deletion linearization
    point. ``dry_run`` performs the recheck diff and reports what would
    happen without writing anything. ``_test_pause`` is a deterministic
    barrier hook for concurrency tests: an async callable receiving
    ``GC_PAUSE_PHASES`` names, suspending the recheck transaction so a
    concurrent root/pin/commit writer can be forced into a true overlap
    before the decision commits.
    """
    _validate_fault_phase(_inject_fault_at)
    _validate_pause_hook(_test_pause)
    retry = _GcRetry("gc recheck")
    t0 = time.perf_counter()
    state = _ExecutionState()

    # ---- recheck + tombstone (ONE transaction; serialized) -----------
    candidate_keys = sorted(set(plan.candidate_registry_keys))

    async def recheck_transaction() -> None:
        async with session_factory() as session:
            async with session.begin():
                await _maybe_pause(_test_pause, PHASE_GC_PAUSE_BEFORE_LOCK)
                if not dry_run:
                    # Linearization serialization: on SQLite the expired-
                    # pin purge below is write-first and takes the single
                    # writer lock before any read; on PostgreSQL the
                    # advisory transaction lock serializes this decision
                    # with every root/pin/head writer that acquires the
                    # same scope (retention, generation activation,
                    # payload-carrying commits). Either way no root, pin,
                    # or hold can commit between the liveness reads and
                    # the tombstone inserts below.
                    await advisory_xact_lock(session, *PAYLOAD_DECISION_LOCK_SCOPE)
                    purge = await session.execute(
                        delete(KernelReaderPin).where(
                            KernelReaderPin.expires_at <= _utcnow()
                        )
                    )
                    state.pins_purged = purge.rowcount or 0
                    pub_purge = await session.execute(
                        delete(KernelPublicationPin).where(
                            KernelPublicationPin.expires_at <= _utcnow()
                        )
                    )
                    state.pub_pins_purged = pub_purge.rowcount or 0

                root_cuts = await load_active_root_cuts_from(session)
                live = await _live_blob_keys(session)
                state.roots = len(root_cuts)
                state.pins = (
                    await session.execute(
                        select(func.count())
                        .select_from(KernelReaderPin)
                        .where(KernelReaderPin.expires_at > _utcnow())
                    )
                ).scalar() or 0

                registry_rows = (
                    await session.execute(
                        select(
                            KernelPayloadObject.blob_key,
                            KernelPayloadObject.payload_length,
                        )
                    )
                ).all()
                state.registry_lengths = {row[0]: row[1] for row in registry_rows}
                registry_keys = set(state.registry_lengths)
                state.retained_objects = len(registry_keys & live)
                state.retained_bytes = sum(
                    state.registry_lengths[key]
                    for key in registry_keys & live
                )

                still = [key for key in candidate_keys if key not in live]
                state.rescued = [key for key in candidate_keys if key in live]
                state.still_registry = still

                orphan_still: list[str] = []
                orphan_rescued: list[str] = []
                for key in plan.candidate_orphan_keys:
                    if key in registry_keys:
                        # A commit adopted the staged bytes: the registry
                        # now owns the object; it survives or is judged
                        # as an unreachable registry object instead.
                        orphan_rescued.append(key)
                    else:
                        orphan_still.append(key)
                state.orphan_still = orphan_still
                state.orphan_rescued = orphan_rescued

                await _maybe_pause(_test_pause, PHASE_GC_PAUSE_AFTER_ROOTS)
                if not dry_run:
                    for key in still:
                        await session.execute(
                            dialect_insert(session.bind, KernelPayloadRetirement)
                            .values(
                                blob_key=key,
                                state=RETIRE_STATE_PENDING,
                                reason=RETIRE_REASON_UNREACHABLE,
                                decided_at=_utcnow(),
                                attempts=0,
                            )
                            .on_conflict_do_nothing(
                                index_elements=[KernelPayloadRetirement.blob_key]
                            )
                        )
                    for key in orphan_still:
                        await session.execute(
                            dialect_insert(session.bind, KernelPayloadRetirement)
                            .values(
                                blob_key=key,
                                state=RETIRE_STATE_PENDING,
                                reason=RETIRE_REASON_ORPHAN,
                                decided_at=_utcnow(),
                                attempts=0,
                            )
                            .on_conflict_do_nothing(
                                index_elements=[KernelPayloadRetirement.blob_key]
                            )
                        )
                    state.tombstoned_keys = still + orphan_still

    await retry.run(recheck_transaction)
    recheck_seconds = time.perf_counter() - t0
    _check_fault(_inject_fault_at, PHASE_GC_AFTER_RECHECK)

    # ---- derived generation retirement -------------------------------
    t1 = time.perf_counter()
    retired: list[str] = []
    gen_rescued: list[str] = []
    if not dry_run:
        for candidate in plan.eligible_generations:
            rescued = await _retire_generation(
                session_factory, candidate, retry, stale_staging_seconds
            )
            if rescued:
                gen_rescued.append(candidate.generation_id)
            else:
                retired.append(candidate.generation_id)
    generations_seconds = time.perf_counter() - t1
    if retired:
        _check_fault(_inject_fault_at, PHASE_GC_AFTER_GENERATIONS)

    # ---- publication serving state retirement -------------------------
    # Sets retire before lexical generations so a retired set's member
    # reference disappears in the same pass; each retirement re-checks
    # every protection inside its own write-first transaction.
    t1b = time.perf_counter()
    sets_retired: list[str] = []
    sets_rescued: list[str] = []
    lexical_retired: list[str] = []
    lexical_rescued: list[str] = []
    if not dry_run:
        for set_id in plan.eligible_publication_sets:
            rescued = await _retire_publication_set(
                session_factory, set_id, retry, stale_staging_seconds
            )
            (sets_rescued if rescued else sets_retired).append(set_id)
        for lexical_id in plan.eligible_lexical_generations:
            rescued = await _retire_lexical_generation(
                session_factory, lexical_id, retry, stale_staging_seconds
            )
            (lexical_rescued if rescued else lexical_retired).append(lexical_id)
    publications_seconds = time.perf_counter() - t1b

    # ---- sweep --------------------------------------------------------
    t2 = time.perf_counter()
    swept_deleted = 0
    already_absent = 0
    failed_keys: list[str] = []
    bytes_reclaimed = 0
    if not dry_run:
        registry_keys = set(state.registry_lengths)
        outcomes = await _sweep_keys(
            session_factory,
            store,
            state.tombstoned_keys,
            retry,
            registry_keys=registry_keys,
            _inject_fault_at=_inject_fault_at,
        )
        swept_deleted = outcomes.deleted
        already_absent = outcomes.already_absent
        failed_keys = outcomes.failed
        # bytes reclaimed: registry-known lengths of deleted unreachable
        # objects, plus on-disk size of deleted orphans.
        for key, did_delete in outcomes.deleted_keys.items():
            if not did_delete:
                continue
            if key in registry_keys:
                bytes_reclaimed += state.registry_lengths[key]
            else:
                bytes_reclaimed += outcomes.orphan_deleted_bytes.get(key, 0)
    sweep_seconds = time.perf_counter() - t2
    if swept_deleted or already_absent:
        _check_fault(_inject_fault_at, PHASE_GC_AFTER_SWEEP)

    return CollectionReport(
        dry_run=dry_run,
        workspace_scope=plan.workspace_scope,
        roots_at_recheck=state.roots,
        active_pins_at_recheck=state.pins,
        objects_considered=len(state.registry_lengths),
        retained_objects=state.retained_objects,
        retained_bytes=state.retained_bytes,
        eligible_registry_objects=len(state.still_registry),
        eligible_registry_bytes=sum(
            state.registry_lengths.get(key, 0) for key in state.still_registry
        ),
        orphan_objects=len(state.orphan_still),
        rescued_count=len(state.rescued) + len(state.orphan_rescued),
        rescued_keys=tuple(state.rescued + state.orphan_rescued),
        tombstoned=len(state.tombstoned_keys),
        swept_deleted=swept_deleted,
        already_absent=already_absent,
        failed_keys=tuple(failed_keys),
        bytes_reclaimed=bytes_reclaimed,
        generations_eligible=len(plan.eligible_generations),
        generations_retired=len(retired),
        generations_rescued=tuple(gen_rescued),
        expired_pins_purged=state.pins_purged,
        publication_sets_eligible=len(plan.eligible_publication_sets),
        publication_sets_retired=len(sets_retired),
        publication_sets_rescued=tuple(sets_rescued),
        lexical_generations_eligible=len(plan.eligible_lexical_generations),
        lexical_generations_retired=len(lexical_retired),
        lexical_generations_rescued=tuple(lexical_rescued),
        expired_publication_pins_purged=state.pub_pins_purged,
        busy_retries=retry.busy_retries,
        duration_seconds={
            "recheck_tombstone": round(recheck_seconds, 4),
            "generations": round(generations_seconds, 4),
            "publications": round(publications_seconds, 4),
            "sweep": round(sweep_seconds, 4),
        },
    )


async def _retire_generation(
    session_factory: async_sessionmaker,
    candidate: GenerationCandidate,
    retry: _GcRetry,
    stale_staging_seconds: float,
) -> bool:
    """Delete one generation's derived rows; True when rescued instead.

    The transaction joins the payload-decision advisory scope (SQLite:
    already serialized by the writer lock it takes with the row touch),
    then re-verifies every protection inside the same transaction —
    current pointer, active pin, active hold, and state freshness for
    staged/validated residue. Any protection commits the rescue; the
    deletes are all-or-nothing with the checks.
    """

    async def retire_tx() -> bool:
        async with session_factory() as session:
            async with session.begin():
                await advisory_xact_lock(session, *PAYLOAD_DECISION_LOCK_SCOPE)
                # Write-first: row touch acquires the writer lock so the
                # protection reads below cannot interleave with a pin or
                # hold declared concurrently.
                touched = await session.execute(
                    update(KernelGeneration)
                    .where(KernelGeneration.generation_id == candidate.generation_id)
                    .values(state=KernelGeneration.state)  # no-op value write
                )
                if touched.rowcount != 1:
                    return True  # already gone; nothing to retire

                current = await session.scalar(
                    select(KernelGenerationHead.current_generation_id).where(
                        KernelGenerationHead.workspace_id == candidate.workspace_id
                    )
                )
                if current == candidate.generation_id:
                    return True
                pinned = await session.scalar(
                    select(func.count())
                    .select_from(KernelReaderPin)
                    .where(
                        KernelReaderPin.generation_id == candidate.generation_id,
                        KernelReaderPin.expires_at > _utcnow(),
                    )
                )
                if (pinned or 0) > 0:
                    return True
                held = await session.scalar(
                    select(func.count())
                    .select_from(KernelRetentionRoot)
                    .where(
                        KernelRetentionRoot.target_generation_id
                        == candidate.generation_id,
                        KernelRetentionRoot.state == ROOT_STATE_ACTIVE,
                    )
                )
                if (held or 0) > 0:
                    return True
                # PR76: a live publication set (head-current or pinned)
                # names this generation as its materialized member.
                protecting_set = await session.scalar(
                    select(func.count())
                    .select_from(KernelPublicationSet)
                    .where(
                        KernelPublicationSet.materialized_generation_id
                        == candidate.generation_id,
                        KernelPublicationSet.publication_set_id.in_(
                            _live_publication_set_ids_subquery()
                        ),
                    )
                )
                if (protecting_set or 0) > 0:
                    return True

                row = await session.get(KernelGeneration, candidate.generation_id)
                assert row is not None
                if row.state in (GENERATION_STATE_SUPERSEDED, GENERATION_STATE_FAILED):
                    pass  # unconditionally eligible
                elif row.state in (GENERATION_STATE_STAGED, GENERATION_STATE_VALIDATED):
                    created = _as_utc(row.created_at)
                    horizon = _utcnow() - timedelta(seconds=stale_staging_seconds)
                    if created is None or created >= horizon:
                        return True  # fresh staging: in-flight build
                else:
                    return True  # active or unknown state: retain

                await session.execute(
                    delete(KernelGenerationRecord).where(
                        KernelGenerationRecord.generation_id == candidate.generation_id
                    )
                )
                await session.execute(
                    delete(KernelGenerationEdge).where(
                        KernelGenerationEdge.generation_id == candidate.generation_id
                    )
                )
                await session.execute(
                    delete(KernelGeneration).where(
                        KernelGeneration.generation_id == candidate.generation_id
                    )
                )
                return False

    return await retry.run(retire_tx)


async def _retire_publication_set(
    session_factory: async_sessionmaker,
    publication_set_id: str,
    retry: _GcRetry,
    stale_staging_seconds: float,
) -> bool:
    """Delete one publication set row; True when rescued instead.

    Same posture as generation retirement: join the payload-decision
    advisory scope, write-first row touch under the writer lock, then
    re-verify every protection inside the same transaction — head
    currency, unexpired publication pin, and state freshness for
    staged/validated candidates. Expired leftover pins of a retiring
    set are deleted with it.
    """

    async def retire_tx() -> bool:
        async with session_factory() as session:
            async with session.begin():
                await advisory_xact_lock(session, *PAYLOAD_DECISION_LOCK_SCOPE)
                touched = await session.execute(
                    update(KernelPublicationSet)
                    .where(
                        KernelPublicationSet.publication_set_id == publication_set_id
                    )
                    .values(state=KernelPublicationSet.state)  # no-op value write
                )
                if touched.rowcount != 1:
                    return True  # already gone; nothing to retire

                head_named = await session.scalar(
                    select(func.count())
                    .select_from(KernelPublicationHead)
                    .where(
                        KernelPublicationHead.current_publication_set_id
                        == publication_set_id
                    )
                )
                if (head_named or 0) > 0:
                    return True
                pinned = await session.scalar(
                    select(func.count())
                    .select_from(KernelPublicationPin)
                    .where(
                        KernelPublicationPin.publication_set_id == publication_set_id,
                        KernelPublicationPin.expires_at > _utcnow(),
                    )
                )
                if (pinned or 0) > 0:
                    return True

                row = await session.get(KernelPublicationSet, publication_set_id)
                assert row is not None
                if row.state in (PUBLICATION_STATE_SUPERSEDED, PUBLICATION_STATE_FAILED):
                    pass  # unconditionally eligible
                elif row.state in (
                    PUBLICATION_STATE_STAGED,
                    PUBLICATION_STATE_VALIDATED,
                ):
                    created = _as_utc(row.created_at)
                    horizon = _utcnow() - timedelta(seconds=stale_staging_seconds)
                    if created is None or created >= horizon:
                        return True  # fresh staging: in-flight candidate
                else:
                    return True  # published but unpinned: guarded residue

                await session.execute(
                    delete(KernelPublicationPin).where(
                        KernelPublicationPin.publication_set_id == publication_set_id
                    )
                )
                await session.execute(
                    delete(KernelPublicationSet).where(
                        KernelPublicationSet.publication_set_id == publication_set_id
                    )
                )
                return False

    return await retry.run(retire_tx)


async def _retire_lexical_generation(
    session_factory: async_sessionmaker,
    lexical_generation_id: str,
    retry: _GcRetry,
    stale_staging_seconds: float,
) -> bool:
    """Delete one lexical generation and its physical table; True if rescued.

    Store- and backend-neutral since PR83B2: the physical artifact is
    named by the generation manifest (FTS5 virtual table on SQLite,
    tsvector+GIN table on PostgreSQL) and ``DROP TABLE`` is portable,
    so retirement runs identically on both profiles.

    A lexical generation survives while ANY publication set row still
    references it (sets retire first, releasing their references);
    staged/validated residue obeys the same staleness grace as
    generations. The physical table is dropped in the same transaction
    as its manifest and locator rows.
    """

    async def retire_tx() -> bool:
        async with session_factory() as session:
            async with session.begin():
                touched = await session.execute(
                    update(KernelLexicalGeneration)
                    .where(
                        KernelLexicalGeneration.lexical_generation_id
                        == lexical_generation_id
                    )
                    .values(state=KernelLexicalGeneration.state)  # no-op value write
                )
                if touched.rowcount != 1:
                    return True  # already gone; nothing to retire

                referenced = await session.scalar(
                    select(func.count())
                    .select_from(KernelPublicationSet)
                    .where(
                        KernelPublicationSet.lexical_generation_id
                        == lexical_generation_id
                    )
                )
                if (referenced or 0) > 0:
                    return True

                row = await session.get(KernelLexicalGeneration, lexical_generation_id)
                assert row is not None
                if row.state in (LEXICAL_STATE_FAILED, LEXICAL_STATE_VALIDATED):
                    pass  # unreferenced and terminal-or-validated: eligible
                elif row.state == LEXICAL_STATE_STAGED:
                    created = _as_utc(row.created_at)
                    horizon = _utcnow() - timedelta(seconds=stale_staging_seconds)
                    if created is None or created >= horizon:
                        return True  # fresh staging: in-flight build
                else:
                    return True  # unknown state: retain

                await session.execute(
                    delete(KernelLexicalRow).where(
                        KernelLexicalRow.lexical_generation_id
                        == lexical_generation_id
                    )
                )
                await session.execute(
                    text(f'DROP TABLE IF EXISTS "{row.fts_table}"')
                )
                await session.execute(
                    delete(KernelLexicalGeneration).where(
                        KernelLexicalGeneration.lexical_generation_id
                        == lexical_generation_id
                    )
                )
                return False

    return await retry.run(retire_tx)


@dataclass
class _SweepOutcomes:
    deleted: int = 0
    already_absent: int = 0
    failed: list[str] = field(default_factory=list)
    deleted_keys: dict[str, bool] = field(default_factory=dict)
    rescued_keys: dict[str, bool] = field(default_factory=dict)
    orphan_deleted_bytes: dict[str, int] = field(default_factory=dict)


async def _sweep_keys(
    session_factory: async_sessionmaker,
    store: PayloadMaintenanceStore,
    keys: Sequence[str],
    retry: _GcRetry,
    *,
    registry_keys: set[str] | None = None,
    _inject_fault_at: str | None = None,
) -> _SweepOutcomes:
    """Retire tombstoned objects one short transaction per object.

    Each transaction write-first claims the tombstone (bumping
    ``attempts``), so a concurrent commit's tombstone rescue — which
    deletes the row — either wins before this transaction starts (the
    sweep then sees no row and rescues) or waits for it. The physical
    deletion through the store's maintenance capability and its outcome
    recording therefore cannot interleave with a commit adopting the
    same bytes.
    """
    outcomes = _SweepOutcomes()
    known = registry_keys if registry_keys is not None else set()
    for key in keys:

        async def sweep_tx() -> None:
            async with session_factory() as session:
                async with session.begin():
                    claimed = await session.execute(
                        update(KernelPayloadRetirement)
                        .where(
                            KernelPayloadRetirement.blob_key == key,
                            KernelPayloadRetirement.state.in_(
                                (RETIRE_STATE_PENDING, RETIRE_STATE_FAILED)
                            ),
                        )
                        .values(attempts=KernelPayloadRetirement.attempts + 1)
                    )
                    if (claimed.rowcount or 0) != 1:
                        row = await session.get(KernelPayloadRetirement, key)
                        if row is None or row.state == RETIRE_STATE_DELETED:
                            outcomes.rescued_keys[key] = True
                        return

                    _check_fault(_inject_fault_at, PHASE_GC_BEFORE_UNLINK)
                    orphan_size = 0
                    if key not in known:
                        stat = await store.stat_object(key)
                        orphan_size = stat.length if stat is not None else 0
                    try:
                        result = await store.delete_object(key)
                    except PayloadStageError as exc:
                        row = await session.get(KernelPayloadRetirement, key)
                        assert row is not None
                        row.state = RETIRE_STATE_FAILED
                        row.last_error = str(exc)
                        session.add(row)
                        outcomes.failed.append(key)
                        return
                    _check_fault(_inject_fault_at, PHASE_GC_AFTER_UNLINK)

                    row = await session.get(KernelPayloadRetirement, key)
                    assert row is not None
                    row.state = RETIRE_STATE_DELETED
                    row.swept_at = _utcnow()
                    row.last_error = None
                    session.add(row)
                    if result.deleted:
                        outcomes.deleted += 1
                        outcomes.deleted_keys[key] = True
                        if key not in known:
                            outcomes.orphan_deleted_bytes[key] = orphan_size
                    elif not result.existed:
                        outcomes.already_absent += 1
                        outcomes.deleted_keys[key] = False

        await retry.run(sweep_tx)
    return outcomes


# ---------------------------------------------------------------------------
# restart reconciliation + one-shot collect
# ---------------------------------------------------------------------------


async def reconcile_retirements(
    session_factory: async_sessionmaker,
    store: PayloadMaintenanceStore,
    *,
    _inject_fault_at: str | None = None,
) -> CollectionReport:
    """Resume unfinished tombstones from durable state alone.

    After any crash: ``pending`` + object present → delete; ``pending``
    + object absent → record deleted (idempotent convergence); ``failed``
    → retry. Never resurrects bytes and never fabricates availability.
    """
    _validate_fault_phase(_inject_fault_at)
    retry = _GcRetry("gc reconcile")
    t0 = time.perf_counter()
    async with session_factory() as session:
        keys = [
            row[0]
            for row in (
                await session.execute(
                    select(KernelPayloadRetirement.blob_key).where(
                        KernelPayloadRetirement.state.in_(
                            (RETIRE_STATE_PENDING, RETIRE_STATE_FAILED)
                        )
                    )
                )
            ).all()
        ]
        registry_rows = (
            await session.execute(
                select(KernelPayloadObject.blob_key, KernelPayloadObject.payload_length)
            )
        ).all()
    registry_keys = {row[0] for row in registry_rows}
    registry_lengths = {row[0]: row[1] for row in registry_rows}
    outcomes = await _sweep_keys(
        session_factory,
        store,
        keys,
        retry,
        registry_keys=registry_keys,
        _inject_fault_at=_inject_fault_at,
    )
    bytes_reclaimed = 0
    for key, did_delete in outcomes.deleted_keys.items():
        if not did_delete:
            continue
        if key in registry_lengths:
            bytes_reclaimed += registry_lengths[key]
        else:
            bytes_reclaimed += outcomes.orphan_deleted_bytes.get(key, 0)
    return CollectionReport(
        dry_run=False,
        workspace_scope=None,
        roots_at_recheck=0,
        active_pins_at_recheck=0,
        objects_considered=len(registry_keys),
        retained_objects=0,
        retained_bytes=0,
        eligible_registry_objects=0,
        eligible_registry_bytes=0,
        orphan_objects=0,
        rescued_count=len(outcomes.rescued_keys),
        rescued_keys=tuple(outcomes.rescued_keys),
        tombstoned=0,
        swept_deleted=outcomes.deleted,
        already_absent=outcomes.already_absent,
        failed_keys=tuple(outcomes.failed),
        bytes_reclaimed=bytes_reclaimed,
        generations_eligible=0,
        generations_retired=0,
        generations_rescued=(),
        expired_pins_purged=0,
        busy_retries=retry.busy_retries,
        duration_seconds={"reconcile_sweep": round(time.perf_counter() - t0, 4)},
    )


async def collect(
    session_factory: async_sessionmaker,
    store: PayloadMaintenanceStore,
    *,
    workspace_id: str | None = None,
    grace_seconds: float = 0.0,
    stale_staging_seconds: float = DEFAULT_STALE_STAGING_SECONDS,
    orphan_min_age_seconds: float = 0.0,
    dry_run: bool = False,
    _inject_fault_at: str | None = None,
) -> CollectionReport:
    """One-shot conservative collection: plan, grace, recheck, sweep.

    ``grace_seconds`` sleeps between mark and execute — the 4C.10 grace
    interval letting uncommitted writers and in-flight pin/hold
    declarations become visible before the deletion decision. It is a
    politeness window, not the safety mechanism: safety comes from the
    recheck transaction and the commit-side tombstone rescue.
    """
    plan = await plan_collection(
        session_factory,
        store,
        workspace_id=workspace_id,
        stale_staging_seconds=stale_staging_seconds,
        orphan_min_age_seconds=orphan_min_age_seconds,
    )
    _check_fault(_inject_fault_at, PHASE_GC_AFTER_MARK)
    if grace_seconds > 0:
        await asyncio.sleep(grace_seconds)
    return await execute_collection(
        session_factory,
        store,
        plan,
        dry_run=dry_run,
        stale_staging_seconds=stale_staging_seconds,
        _inject_fault_at=_inject_fault_at,
    )
