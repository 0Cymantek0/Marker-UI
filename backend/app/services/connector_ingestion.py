"""Connector convergence core (V3.2 PR71B, amendment 16B.7).

The application seam that turns a remote provider's unreliable
incremental change stream into the existing local source truth:

    provider adapter (hints: duplicate, out-of-order, gapped, reset)
      -> durable inbox receipt + classification (dedupe authority)
      -> SourceIdentity / ContentRevision / AccessPolicyRevision /
         AccessDenial / SourceObservation candidates
      -> ONE kernel commit: records + edges + outbox intent + inbox
         rows + conditional checkpoint advancement, atomically
      -> reconciliation when incremental state becomes untrustworthy

Every correctness-critical decision is durable (database constraints and
the kernel writer lock, never in-process memory), so process restarts
replay safely and concurrent workers converge through the same
authority. The service holds no mutable connector state of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select

from app.kernel.connector_state import (
    CONNECTOR_APPLIED_APPLIED,
    CONNECTOR_APPLIED_DEFERRED,
    CONNECTOR_APPLIED_DUPLICATE,
    CONNECTOR_APPLIED_STALE,
    CONNECTOR_EVENT_CONTENT_CHANGED,
    CONNECTOR_EVENT_MOVED,
    CONNECTOR_EVENT_POLICY_CHANGED,
    CONNECTOR_EVENT_REMOVED,
    CONNECTOR_EVENT_RESTORED,
    CONNECTOR_STREAM_CONSUMING,
    CONNECTOR_STREAM_RECONCILIATION_REQUIRED,
    ConnectorCursorAdvancement,
    ConnectorEffects,
    ConnectorInboxEntry,
)
from app.kernel.errors import (
    ConnectorStreamStateError,
    DuplicateConnectorEventError,
    KernelError,
)
from app.kernel.models import (
    KernelConnectorInbox,
    KernelConnectorStream,
    KernelRecord,
)
from app.kernel.outbox import OutboxIntent
from app.kernel.records import (
    ACCESS_DENIAL_TARGET_SOURCE,
    SOURCE_CONSISTENCY_BEST_EFFORT,
    SOURCE_CONSISTENCY_VERSION_PINNED,
    AccessDenialRecord,
    KernelEdge,
    KernelRecord as RecordInput,
    SourceObservationRecord,
)
from app.services.connector_adapter import (
    ORDERING_NONE,
    ChangePage,
    ConnectorAdapter,
    InvalidStreamSignal,
    ItemSnapshot,
    ProviderChange,
)
from app.services.source_acquisition import SourceAcquisitionService
from app.utils.canonical import payload_byte_hash

__all__ = [
    "WORK_KIND_SOURCE_INVALIDATED",
    "ConnectorStreamView",
    "EventOutcome",
    "ApplyResult",
    "ReconcileResult",
    "ConnectorIngestionService",
]

#: Outbox work kind for downstream invalidation authorized by a
#: connector source transition (payload addresses the logical source
#: key — stable across record-id convergence — so consumers resolve
#: identity themselves).
WORK_KIND_SOURCE_INVALIDATED = "source.invalidated"

_OBSERVER = "marker-ui-connector-ingestion"


@dataclass(frozen=True)
class ConnectorStreamView:
    """Durable connector stream state snapshot."""

    stream_id: str
    cursor_token: str
    cursor_seq: int | None
    state: str
    reconciliation_reason: str | None
    applied_kernel_commit_id: int


@dataclass(frozen=True)
class EventOutcome:
    """Classification of one provider event by the application unit."""

    event_id: str
    item_id: str | None
    applied_state: str
    note: str = ""


@dataclass(frozen=True)
class ApplyResult:
    """Result of one poll/deliver application round."""

    outcomes: tuple[EventOutcome, ...]
    kernel_commit_id: int
    stream: ConnectorStreamView


@dataclass(frozen=True)
class ReconcileResult:
    """Result of one (possibly partial) reconciliation drive."""

    completed: bool
    pages_applied: int
    kernel_commit_ids: tuple[int, ...]
    stream: ConnectorStreamView


class ConnectorIngestionService:
    """Converge provider change streams into committed source truth."""

    def __init__(
        self,
        session_factory,
        acquisition: SourceAcquisitionService,
    ) -> None:
        self._sf = session_factory
        self._acq = acquisition
        self.workspace_id = acquisition.workspace_id

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    async def stream_view(self, stream_id: str) -> ConnectorStreamView | None:
        row = await self._stream_row(stream_id)
        if row is None:
            return None
        return _view_of(row)

    async def inbox_rows(self, stream_id: str) -> list[KernelConnectorInbox]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(KernelConnectorInbox)
                    .where(KernelConnectorInbox.stream_id == stream_id)
                    .order_by(KernelConnectorInbox.id.asc())
                )
            ).scalars().all()
        return list(rows)

    # ------------------------------------------------------------------
    # polling (change-feed consumption)
    # ------------------------------------------------------------------

    async def poll(
        self,
        stream_id: str,
        adapter: ConnectorAdapter,
        *,
        _inject_fault_at: str | None = None,
    ) -> ApplyResult:
        """Fetch and durably apply one round of incremental changes.

        The checkpoint advances only when the provider page is complete;
        an invalid/expired/reset signal or a detected sequence gap parks
        the stream in ``reconciliation_required`` instead (events in the
        failing page are recorded as deferred, never applied).
        """
        row = await self._stream_row(stream_id)
        if row is not None and row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED:
            raise ConnectorStreamStateError(
                f"stream {stream_id!r} awaits reconciliation; poll refused — "
                f"run reconcile() (reason: {row.reconciliation_reason!r})"
            )

        try:
            page = await adapter.fetch_changes(row.cursor_token if row else None)
        except InvalidStreamSignal as exc:
            return await self._enter_reconciliation(
                stream_id, row, exc.reason, exc.detail, changes=(), fault_at=_inject_fault_at
            )

        if page.invalid_reason is not None:
            return await self._enter_reconciliation(
                stream_id,
                row,
                page.invalid_reason,
                page.invalid_detail,
                changes=page.changes,
                fault_at=_inject_fault_at,
            )

        if row is not None and row.cursor_seq is not None and page.page_seq is not None:
            if page.page_seq > row.cursor_seq + 1:
                return await self._enter_reconciliation(
                    stream_id,
                    row,
                    "gap_detected",
                    f"page seq {page.page_seq} follows checkpoint seq {row.cursor_seq}",
                    changes=page.changes,
                    fault_at=_inject_fault_at,
                )

        cursor: ConnectorCursorAdvancement | None = None
        if page.complete and page.next_cursor is not None:
            new_seq = _page_seq(page, row)
            cursor = ConnectorCursorAdvancement(
                expected_cursor_token=row.cursor_token if row else None,
                new_cursor_token=page.next_cursor,
                new_state=CONNECTOR_STREAM_CONSUMING,
                new_cursor_seq=new_seq,
            )
        return await self._apply_unit(
            stream_id,
            adapter,
            page.changes,
            cursor=cursor,
            producer_op="connector.poll",
            fault_at=_inject_fault_at,
        )

    # ------------------------------------------------------------------
    # single-event delivery (webhook/push style — never owns the cursor)
    # ------------------------------------------------------------------

    async def deliver(
        self,
        stream_id: str,
        adapter: ConnectorAdapter,
        change: ProviderChange,
        *,
        _inject_fault_at: str | None = None,
    ) -> ApplyResult:
        """Apply one pushed provider event without moving the checkpoint.

        Notifications are hints: a push never claims feed progress. If
        the stream does not exist yet it is initialized with the empty
        checkpoint (no provider progress consumed) in the same commit.
        """
        row = await self._stream_row(stream_id)
        if row is not None and row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED:
            # The event is recorded as deferred evidence: inspectable,
            # never applied against untrusted incremental state.
            return await self._enter_reconciliation(
                stream_id,
                row,
                None,  # keep the existing reason
                f"event {change.event_id} deferred",
                changes=(change,),
                fault_at=_inject_fault_at,
            )

        cursor: ConnectorCursorAdvancement | None = None
        if row is None:
            cursor = ConnectorCursorAdvancement(
                expected_cursor_token=None,
                new_cursor_token="",
                new_state=CONNECTOR_STREAM_CONSUMING,
            )
        return await self._apply_unit(
            stream_id,
            adapter,
            (change,),
            cursor=cursor,
            producer_op="connector.deliver",
            fault_at=_inject_fault_at,
        )

    # ------------------------------------------------------------------
    # reconciliation (P5/P6: restartable, idempotent)
    # ------------------------------------------------------------------

    async def reconcile(
        self,
        stream_id: str,
        adapter: ConnectorAdapter,
        *,
        page_limit: int | None = None,
        _inject_fault_at: str | None = None,
    ) -> ReconcileResult:
        """Drive the adapter's authoritative scan to durable completion.

        Each scan page is applied through the same atomic unit; the
        checkpoint moves tentatively page-wise (crash mid-scan restarts
        from the last durably-applied page) and only the page carrying
        ``final=True`` completes the reconciliation, installing the
        provider's fresh checkpoint. ``page_limit`` bounds work per
        call — the remaining pages are a later continuation, never a
        skipped interval.
        """
        row = await self._stream_row(stream_id)
        resume = (
            row.cursor_token
            if row is not None
            and row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
            and row.cursor_token
            else None
        )

        commit_ids: list[int] = []
        pages = 0
        while True:
            scan = await adapter.full_scan(resume)
            pages += 1
            row = await self._stream_row(stream_id)

            if scan.final:
                fresh = (
                    scan.fresh_cursor
                    if scan.fresh_cursor is not None
                    else (scan.resume_token or (row.cursor_token if row else ""))
                )
                was_broken = (
                    row is not None
                    and row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
                )
                cursor = ConnectorCursorAdvancement(
                    expected_cursor_token=row.cursor_token if row else None,
                    new_cursor_token=fresh,
                    new_state=CONNECTOR_STREAM_CONSUMING,
                    completes_reconciliation=was_broken,
                )
                result = await self._apply_unit(
                    stream_id,
                    adapter,
                    scan.changes,
                    cursor=cursor,
                    producer_op="connector.reconcile.final",
                    fault_at=_inject_fault_at,
                )
                if result.kernel_commit_id:
                    commit_ids.append(result.kernel_commit_id)
                return ReconcileResult(
                    completed=True,
                    pages_applied=pages,
                    kernel_commit_ids=tuple(commit_ids),
                    stream=result.stream,
                )

            cursor = ConnectorCursorAdvancement(
                expected_cursor_token=row.cursor_token if row else None,
                new_cursor_token=scan.resume_token or "",
                new_state=(
                    row.state
                    if row is not None
                    and row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
                    else CONNECTOR_STREAM_CONSUMING
                ),
                reconciliation_reason=(
                    row.reconciliation_reason
                    if row is not None
                    and row.state == CONNECTOR_STREAM_RECONCILIATION_REQUIRED
                    else None
                ),
            )
            result = await self._apply_unit(
                stream_id,
                adapter,
                scan.changes,
                cursor=cursor,
                producer_op="connector.reconcile.page",
                fault_at=_inject_fault_at,
            )
            if result.kernel_commit_id:
                commit_ids.append(result.kernel_commit_id)
            resume = scan.resume_token

            if page_limit is not None and pages >= page_limit:
                return ReconcileResult(
                    completed=False,
                    pages_applied=pages,
                    kernel_commit_ids=tuple(commit_ids),
                    stream=result.stream,
                )

    # ------------------------------------------------------------------
    # the atomic application unit
    # ------------------------------------------------------------------

    async def _apply_unit(
        self,
        stream_id: str,
        adapter: ConnectorAdapter,
        changes: Sequence[ProviderChange],
        *,
        cursor: ConnectorCursorAdvancement | None,
        producer_op: str,
        fault_at: str | None = None,
    ) -> ApplyResult:
        """One atomic decision over a batch of provider changes.

        Stages bytes, mints candidates, and commits records + edges +
        outbox + inbox + checkpoint through one kernel transaction. A
        concurrent duplicate delivery surfaces as
        ``DuplicateConnectorEventError`` at the commit check and is
        converged here (outcomes re-classified as duplicates) rather
        than retried.
        """
        known = await self._known_event_ids(
            stream_id, [c.event_id for c in changes]
        )
        fresh = [c for c in changes if c.event_id not in known]
        outcomes = [
            EventOutcome(
                event_id=c.event_id,
                item_id=c.item_id,
                applied_state=CONNECTOR_APPLIED_DUPLICATE,
                note="provider event already durably recorded",
            )
            for c in changes
            if c.event_id in known
        ]

        records: list[RecordInput] = []
        edges: list[KernelEdge] = []
        outbox: list[OutboxIntent] = []
        entries: list[ConnectorInboxEntry] = []

        for change in fresh:
            outcome = await self._apply_one(
                stream_id, adapter, change, records, edges, outbox
            )
            entries.append(
                ConnectorInboxEntry(
                    provider_event_id=change.event_id,
                    event_kind=change.kind,
                    applied_state=outcome.applied_state,
                    provider_item_id=change.item_id,
                    provider_revision=change.revision,
                    provider_seq=change.seq,
                    result={
                        "note": outcome.note,
                        "source_key": self._source_key(adapter, change.item_id),
                    },
                )
            )
            outcomes.append(outcome)

        if not entries:
            # Nothing new: commit only when the checkpoint/state actually
            # moves. Re-acknowledging an unchanged checkpoint would mint
            # empty commits on every duplicate redelivery.
            row = await self._stream_row(stream_id)
            if cursor is None or (
                row is not None
                and row.cursor_token == cursor.new_cursor_token
                and row.cursor_seq == cursor.new_cursor_seq
                and row.state == cursor.new_state
            ):
                view = _view_of(row) if row else _empty_view(stream_id)
                return ApplyResult(tuple(outcomes), 0, view)

        effects = ConnectorEffects(
            workspace_id=self.workspace_id,
            stream_id=stream_id,
            inbox=tuple(entries),
            cursor=cursor,
        )
        try:
            result = await self._acq.commit_converging(
                records=records,
                edges=edges,
                producer={
                    "operation": producer_op,
                    "stream_id": stream_id,
                    "provider": adapter.provider_name,
                    "events": [e.provider_event_id for e in entries],
                },
                outbox=tuple(outbox),
                connector=effects,
                _inject_fault_at=fault_at,
            )
        except DuplicateConnectorEventError:
            # A concurrent worker committed some of these events first.
            # The database refused our batch atomically; converge by
            # re-classifying against the now-durable inbox.
            return await self._reclassify_after_race(stream_id, changes)

        row = await self._stream_row(stream_id)
        view = _view_of(row) if row else _empty_view(stream_id)
        return ApplyResult(tuple(outcomes), result["commit_id"], view)

    async def _apply_one(
        self,
        stream_id: str,
        adapter: ConnectorAdapter,
        change: ProviderChange,
        records: list[RecordInput],
        edges: list[KernelEdge],
        outbox: list[OutboxIntent],
    ) -> EventOutcome:
        """Classify and mint candidates for one provider change.

        Appends to the shared candidate lists; returns the inbox
        classification. Stale events add no truth records.
        """
        source_key = self._source_key(adapter, change.item_id)

        # P2: arrival order is not causal order. A comparable provider
        # sequence older than (or equal to) the newest durably-applied
        # state for this item must never regress truth.
        if change.seq is not None:
            last = await self._last_applied_seq(stream_id, change.item_id)
            if last is not None and change.seq <= last:
                return EventOutcome(
                    event_id=change.event_id,
                    item_id=change.item_id,
                    applied_state=CONNECTOR_APPLIED_STALE,
                    note=f"provider seq {change.seq} not newer than applied {last}",
                )

        # T6: providers that cannot prove revision order are resolved
        # through authoritative current truth, never arrival order.
        if change.ordering == ORDERING_NONE and change.kind in (
            CONNECTOR_EVENT_CONTENT_CHANGED,
            CONNECTOR_EVENT_POLICY_CHANGED,
        ):
            snapshot = await adapter.fetch_item(change.item_id)
            if snapshot is None or not snapshot.present:
                return await self._mint_access_loss(
                    source_key, change, records, edges, outbox,
                    note="authoritative query reports item absent",
                )
            return await self._apply_snapshot(
                source_key, change, snapshot, records, edges, outbox
            )

        if change.kind == CONNECTOR_EVENT_CONTENT_CHANGED:
            minted, mint_edges, _ = await self._acq.mint_connector_revision(
                source_key=source_key,
                data=change.content or b"",
                suffix=change.suffix,
                consistency_class=(
                    SOURCE_CONSISTENCY_VERSION_PINNED
                    if change.revision
                    else SOURCE_CONSISTENCY_BEST_EFFORT
                ),
                provider_event_id=change.event_id,
                media_type=change.media_type,
                policy_facts=change.policy_facts or None,
                evidence={
                    "provider_revision": change.revision,
                    "provider_seq": change.seq,
                    "change_kind": change.kind,
                },
            )
            records.extend(minted)
            edges.extend(mint_edges)
            outbox.append(self._invalidation_intent(source_key, change))
            return EventOutcome(
                event_id=change.event_id,
                item_id=change.item_id,
                applied_state=CONNECTOR_APPLIED_APPLIED,
                note="content revision minted",
            )

        identity = self._acq.connector_identity_record(source_key)
        records.append(identity)

        if change.kind == CONNECTOR_EVENT_POLICY_CHANGED:
            policy = self._acq.connector_policy_record(
                source_key, dict(change.policy_facts)
            )
            observation = self._lifecycle_observation(
                source_key, change, outcome="policy_updated", policy_ref=policy.record_id
            )
            records.extend((policy, observation))
            edges.extend(
                [
                    KernelEdge(
                        edge_kind="derived_from",
                        source_ref=policy.record_id,
                        target_ref=identity.record_id,
                    ),
                    KernelEdge(
                        edge_kind="observes",
                        source_ref=observation.record_id,
                        target_ref=identity.record_id,
                    ),
                ]
            )
            await self._mint_denial_transition(
                source_key, change, bool(change.policy_facts.get("denied")),
                records, edges, outbox,
            )
            return EventOutcome(
                event_id=change.event_id,
                item_id=change.item_id,
                applied_state=CONNECTOR_APPLIED_APPLIED,
                note="policy transition recorded",
            )

        if change.kind == CONNECTOR_EVENT_REMOVED:
            return await self._mint_access_loss(
                source_key, change, records, edges, outbox, note=None
            )

        if change.kind == CONNECTOR_EVENT_RESTORED:
            if change.content is not None:
                minted, mint_edges, _ = await self._acq.mint_connector_revision(
                    source_key=source_key,
                    data=change.content,
                    suffix=change.suffix,
                    consistency_class=(
                        SOURCE_CONSISTENCY_VERSION_PINNED
                        if change.revision
                        else SOURCE_CONSISTENCY_BEST_EFFORT
                    ),
                    provider_event_id=change.event_id,
                    media_type=change.media_type,
                    policy_facts=change.policy_facts or None,
                    evidence={
                        "provider_revision": change.revision,
                        "provider_seq": change.seq,
                        "change_kind": change.kind,
                    },
                )
                records.extend(minted)
                edges.extend(mint_edges)
            observation = self._lifecycle_observation(
                source_key, change, outcome="restored"
            )
            records.append(observation)
            edges.append(
                KernelEdge(
                    edge_kind="observes",
                    source_ref=observation.record_id,
                    target_ref=identity.record_id,
                )
            )
            await self._mint_denial_transition(
                source_key, change, False, records, edges, outbox
            )
            return EventOutcome(
                event_id=change.event_id,
                item_id=change.item_id,
                applied_state=CONNECTOR_APPLIED_APPLIED,
                note="restore recorded; access denial lifted",
            )

        # CONNECTOR_EVENT_MOVED: stable provider identity survives the
        # location change; no content/policy transition is implied.
        observation = self._lifecycle_observation(
            source_key, change, outcome="metadata_updated"
        )
        records.append(observation)
        edges.append(
            KernelEdge(
                edge_kind="observes",
                source_ref=observation.record_id,
                target_ref=identity.record_id,
            )
        )
        return EventOutcome(
            event_id=change.event_id,
            item_id=change.item_id,
            applied_state=CONNECTOR_APPLIED_APPLIED,
            note=f"location metadata recorded: {change.new_location}",
        )

    async def _apply_snapshot(
        self,
        source_key: str,
        change: ProviderChange,
        snapshot: ItemSnapshot,
        records: list[RecordInput],
        edges: list[KernelEdge],
        outbox: list[OutboxIntent],
    ) -> EventOutcome:
        """Apply authoritative current truth for an ordering-free provider."""
        if snapshot.content is not None:
            minted, mint_edges, _ = await self._acq.mint_connector_revision(
                source_key=source_key,
                data=snapshot.content,
                suffix=snapshot.suffix,
                consistency_class=(
                    SOURCE_CONSISTENCY_VERSION_PINNED
                    if snapshot.revision
                    else SOURCE_CONSISTENCY_BEST_EFFORT
                ),
                provider_event_id=change.event_id,
                media_type=snapshot.media_type,
                policy_facts=snapshot.policy_facts or None,
                evidence={
                    "resolved_via": "authoritative_item_query",
                    "notification_ordering": "none",
                },
            )
            records.extend(minted)
            edges.extend(mint_edges)
            outbox.append(self._invalidation_intent(source_key, change))
            return EventOutcome(
                event_id=change.event_id,
                item_id=change.item_id,
                applied_state=CONNECTOR_APPLIED_APPLIED,
                note="applied authoritative snapshot",
            )
        # Present without content: policy/metadata-only truth.
        policy = self._acq.connector_policy_record(
            source_key, dict(snapshot.policy_facts)
        )
        observation = self._lifecycle_observation(
            source_key, change, outcome="policy_updated", policy_ref=policy.record_id
        )
        identity = self._acq.connector_identity_record(source_key)
        records.extend((identity, policy, observation))
        edges.extend(
            [
                KernelEdge(
                    edge_kind="derived_from",
                    source_ref=policy.record_id,
                    target_ref=identity.record_id,
                ),
                KernelEdge(
                    edge_kind="observes",
                    source_ref=observation.record_id,
                    target_ref=identity.record_id,
                ),
            ]
        )
        return EventOutcome(
            event_id=change.event_id,
            item_id=change.item_id,
            applied_state=CONNECTOR_APPLIED_APPLIED,
            note="applied authoritative policy snapshot",
        )

    async def _mint_access_loss(
        self,
        source_key: str,
        change: ProviderChange,
        records: list[RecordInput],
        edges: list[KernelEdge],
        outbox: list[OutboxIntent],
        *,
        note: str | None,
    ) -> EventOutcome:
        """Removal / loss-of-access: live deny now, history preserved."""
        identity = self._acq.connector_identity_record(source_key)
        observation = self._lifecycle_observation(
            source_key, change, outcome="access_lost"
        )
        records.extend((identity, observation))
        edges.append(
            KernelEdge(
                edge_kind="observes",
                source_ref=observation.record_id,
                target_ref=identity.record_id,
            )
        )
        await self._mint_denial_transition(
            source_key, change, True, records, edges, outbox
        )
        return EventOutcome(
            event_id=change.event_id,
            item_id=change.item_id,
            applied_state=CONNECTOR_APPLIED_APPLIED,
            note=note or "access loss recorded; live deny active",
        )

    async def _mint_denial_transition(
        self,
        source_key: str,
        change: ProviderChange,
        denied: bool,
        records: list[RecordInput],
        edges: list[KernelEdge],
        outbox: list[OutboxIntent],
    ) -> None:
        """Mint the live-deny transition only when state changes.

        Deny state is an append-only event chain (PR78): lifting a deny
        is a new event, never a mutation of history. A transition that
        matches current state mints nothing (an explicit no-op is not a
        state event), and the invalidation intent fires exactly when
        live access semantics actually change.
        """
        identity = self._acq.connector_identity_record(source_key)
        latest = await self._latest_denial(identity.record_id)
        if latest is not None and latest[1] == denied:
            return
        denial = AccessDenialRecord(
            record_id="cdeny."
            + payload_byte_hash(
                f"{identity.record_id}|{denied}|{latest[0] if latest else ''}|{change.event_id}".encode()
            )[:24],
            target_kind=ACCESS_DENIAL_TARGET_SOURCE,
            target_ref=identity.record_id,
            denied=denied,
            supersedes=latest[0] if latest else None,
            denial_basis={
                "provider_event_id": change.event_id,
                "removal_kind": change.policy_facts.get("removal_kind", "deleted"),
            },
        )
        records.append(denial)
        edges.append(
            KernelEdge(
                edge_kind="derived_from",
                source_ref=denial.record_id,
                target_ref=identity.record_id,
            )
        )
        outbox.append(self._invalidation_intent(source_key, change))

    def _lifecycle_observation(
        self,
        source_key: str,
        change: ProviderChange,
        *,
        outcome: str,
        policy_ref: str | None = None,
    ) -> SourceObservationRecord:
        identity = self._acq.connector_identity_record(source_key)
        return SourceObservationRecord(
            record_id="cobs."
            + payload_byte_hash(
                f"{source_key}|{change.event_id}|{outcome}".encode("utf-8")
            )[:24],
            observer=_OBSERVER,
            source_ref=identity.record_id,
            outcome=outcome,
            access_policy_ref=policy_ref,
            evidence={
                "provider_event_id": change.event_id,
                "provider_revision": change.revision,
                "provider_seq": change.seq,
                "change_kind": change.kind,
                "new_location": change.new_location,
                "removal_kind": change.policy_facts.get("removal_kind"),
            },
        )

    def _invalidation_intent(
        self, source_key: str, change: ProviderChange
    ) -> OutboxIntent:
        return OutboxIntent(
            work_kind=WORK_KIND_SOURCE_INVALIDATED,
            payload={
                "source_key": source_key,
                "item_id": change.item_id,
                "change_kind": change.kind,
                "provider_event_id": change.event_id,
            },
        )

    # ------------------------------------------------------------------
    # reconciliation transition
    # ------------------------------------------------------------------

    async def _enter_reconciliation(
        self,
        stream_id: str,
        row: KernelConnectorStream | None,
        reason: str | None,
        detail: str,
        *,
        changes: Sequence[ProviderChange],
        fault_at: str | None,
    ) -> ApplyResult:
        """Park the stream in reconciliation_required, events deferred."""
        resolved_reason = reason or (
            row.reconciliation_reason if row is not None else "unknown"
        )
        known = await self._known_event_ids(
            stream_id, [c.event_id for c in changes]
        )
        entries = [
            ConnectorInboxEntry(
                provider_event_id=c.event_id,
                event_kind=c.kind,
                applied_state=CONNECTOR_APPLIED_DEFERRED,
                provider_item_id=c.item_id,
                provider_revision=c.revision,
                provider_seq=c.seq,
                result={"deferred_reason": resolved_reason},
            )
            for c in changes
            if c.event_id not in known
        ]
        cursor = ConnectorCursorAdvancement(
            expected_cursor_token=row.cursor_token if row else None,
            new_cursor_token=row.cursor_token if row else "",
            new_state=CONNECTOR_STREAM_RECONCILIATION_REQUIRED,
            reconciliation_reason=resolved_reason,
        )
        effects = ConnectorEffects(
            workspace_id=self.workspace_id,
            stream_id=stream_id,
            inbox=tuple(entries),
            cursor=cursor,
        )
        await self._acq.commit_converging(
            records=[],
            edges=[],
            producer={
                "operation": "connector.reconciliation_required",
                "stream_id": stream_id,
                "reason": resolved_reason,
                "detail": detail[:500],
            },
            outbox=(),
            connector=effects,
            _inject_fault_at=fault_at,
        )
        outcomes = [
            EventOutcome(
                event_id=c.event_id,
                item_id=c.item_id,
                applied_state=CONNECTOR_APPLIED_DEFERRED,
                note=resolved_reason,
            )
            for c in changes
        ]
        fresh_row = await self._stream_row(stream_id)
        view = _view_of(fresh_row) if fresh_row else _empty_view(stream_id)
        return ApplyResult(tuple(outcomes), 0, view)

    async def _reclassify_after_race(
        self,
        stream_id: str,
        changes: Sequence[ProviderChange],
    ) -> ApplyResult:
        """Converge after a concurrent worker won the duplicate race."""
        known = await self._known_event_ids(
            stream_id, [c.event_id for c in changes]
        )
        outcomes = [
            EventOutcome(
                event_id=c.event_id,
                item_id=c.item_id,
                applied_state=(
                    CONNECTOR_APPLIED_DUPLICATE
                    if c.event_id in known
                    else CONNECTOR_APPLIED_STALE
                ),
                note="converged after concurrent application",
            )
            for c in changes
        ]
        row = await self._stream_row(stream_id)
        view = _view_of(row) if row else _empty_view(stream_id)
        return ApplyResult(tuple(outcomes), 0, view)

    # ------------------------------------------------------------------
    # durable reads
    # ------------------------------------------------------------------

    def _source_key(self, adapter: ConnectorAdapter, item_id: str) -> str:
        return self._acq.connector_source_key(
            adapter.provider_name, getattr(adapter, "account", "default"), item_id
        )

    async def _stream_row(self, stream_id: str) -> KernelConnectorStream | None:
        async with self._sf() as session:
            return await session.get(KernelConnectorStream, stream_id)

    async def _known_event_ids(
        self, stream_id: str, event_ids: Iterable[str]
    ) -> set[str]:
        ids = list(event_ids)
        if not ids:
            return set()
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(KernelConnectorInbox.provider_event_id).where(
                        KernelConnectorInbox.stream_id == stream_id,
                        KernelConnectorInbox.provider_event_id.in_(ids),
                    )
                )
            ).scalars().all()
        return set(rows)

    async def _last_applied_seq(
        self, stream_id: str, item_id: str
    ) -> int | None:
        """Newest provider sequence durably applied for one item.

        The inbox IS the per-item revision evidence: applied receipts
        carry the provider's comparable sequence, so stale refusal reads
        durable application history, not process memory.
        """
        async with self._sf() as session:
            value = await session.scalar(
                select(func.max(KernelConnectorInbox.provider_seq)).where(
                    KernelConnectorInbox.stream_id == stream_id,
                    KernelConnectorInbox.provider_item_id == item_id,
                    KernelConnectorInbox.applied_state == CONNECTOR_APPLIED_APPLIED,
                )
            )
        return value

    async def _latest_denial(
        self, source_identity_id: str
    ) -> tuple[str, bool] | None:
        """Latest committed AccessDenialRecord for one source target.

        Bounded scan over the newest denial records (same pattern as the
        epoch lookup): identity convergence keeps per-target chains
        short, and a miss simply means no denial exists yet.
        """
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(KernelRecord.id, KernelRecord.payload_json)
                    .where(
                        KernelRecord.workspace_id == self.workspace_id,
                        KernelRecord.record_class == "access_denial",
                    )
                    .order_by(
                        KernelRecord.kernel_commit_id.desc(), KernelRecord.id.desc()
                    )
                    .limit(200)
                )
            ).all()
        for row_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("target_kind") != ACCESS_DENIAL_TARGET_SOURCE:
                continue
            if payload.get("target_ref") != source_identity_id:
                continue
            return row_id, bool(payload.get("denied"))
        return None


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _view_of(row: KernelConnectorStream) -> ConnectorStreamView:
    return ConnectorStreamView(
        stream_id=row.stream_id,
        cursor_token=row.cursor_token,
        cursor_seq=row.cursor_seq,
        state=row.state,
        reconciliation_reason=row.reconciliation_reason,
        applied_kernel_commit_id=row.applied_kernel_commit_id,
    )


def _empty_view(stream_id: str) -> ConnectorStreamView:
    return ConnectorStreamView(
        stream_id=stream_id,
        cursor_token="",
        cursor_seq=None,
        state=CONNECTOR_STREAM_CONSUMING,
        reconciliation_reason=None,
        applied_kernel_commit_id=0,
    )


def _page_seq(page: ChangePage, row: KernelConnectorStream | None) -> int | None:
    if page.page_seq is not None:
        return page.page_seq
    seqs = [c.seq for c in page.changes if c.seq is not None]
    if seqs:
        return max(seqs)
    return row.cursor_seq if row is not None else None
