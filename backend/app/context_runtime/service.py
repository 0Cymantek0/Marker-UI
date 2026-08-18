"""Snapshot-safe, authorization-aware continuation service (PR79A)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Mapping

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.authorization import (
    ASSURANCE_HIGH,
    resolve_effective_authorization,
)
from app.context_runtime.continuation import (
    CURSOR_REPLAY_FRESH,
    CURSOR_STATUS_ACTIVE,
    CURSOR_STATUS_EXHAUSTED,
    CURSOR_STATUS_EXPIRED,
    CURSOR_STATUS_REVOKED,
    ContinuationOutcome,
    OUTCOME_COMPLETE,
    OUTCOME_EXECUTION_FAILURE,
    OUTCOME_INVALIDATED,
    OUTCOME_LOOP_LIMIT,
    OUTCOME_PARTIAL,
    OUTCOME_POLICY_FAIL_CLOSED,
    OUTCOME_STALE,
    parse_cursor_state_json,
)
from app.context_runtime.continuation_paging import ContinuationPager
from app.context_runtime.continuation_state import (
    coerce_request,
    initial_budget,
    initial_keyset,
    publication_matches,
    utc,
    validate_budget,
    validate_keyset,
)
from app.context_runtime.contract import QueryRequest, normalized_query
from app.context_runtime.cursor import (
    CursorCodec,
    CursorCodecError,
    CursorExpiredError,
    validate_cursor_expiry,
)
from app.context_runtime.errors import QueryAuthorizationError, QueryContractError
from app.context_runtime.executor import unpublished_packet
from app.context_runtime.packets import EvidencePacket
from app.context_runtime.continuation_store import CursorStore
from app.kernel.publications import (
    PublicationReader,
    acquire_publication_pin,
    active_publication_pins,
    open_pinned_publication,
    open_published_reader,
    release_publication_pin,
)
from app.kernel.errors import PublicationIntegrityError, UnknownPublicationSetError

__all__ = ["ContinuationService"]

CONTINUATION_DEFAULT_TTL_SECONDS = 60.0
CONTINUATION_DEFAULT_PIN_LEASE_SECONDS = 120.0
CONTINUATION_DEFAULT_PAGE_SIZE = 10
CONTINUATION_MAX_PAGE_SIZE = 100
CONTINUATION_DEFAULT_MAX_PAGES = 32
CONTINUATION_DEFAULT_CLAIM_TIMEOUT_SECONDS = 60.0

_INVALID = "cursor_invalid"
_EXPIRED = "cursor_expired"
_AUTH_CHANGED = "authorization_changed"
_PIN_UNAVAILABLE = "pinned_state_unavailable"
_POLICY = "policy_fail_closed"
_EXECUTION = "execution_failure"


def _page_size(value: int | None) -> int:
    selected = CONTINUATION_DEFAULT_PAGE_SIZE if value is None else value
    if (
        isinstance(selected, bool)
        or not isinstance(selected, int)
        or selected < 1
        or selected > CONTINUATION_MAX_PAGE_SIZE
    ):
        raise QueryContractError(
            f"page_size must be an integer from 1 to {CONTINUATION_MAX_PAGE_SIZE}"
        )
    return selected


def _outcome(
    status: str,
    *,
    packet: EvidencePacket | None = None,
    budget: Mapping[str, Any] | None = None,
    reason: str | None = None,
    error_code: str | None = None,
    next_cursor: str | None = None,
) -> ContinuationOutcome:
    result: dict[str, Any] = {}
    if budget is not None:
        result["cumulative_budget"] = {
            key: value for key, value in budget.items() if key != "emitted_keys"
        }
    if packet is not None:
        result["packet"] = packet
    return ContinuationOutcome(
        status=status,
        packet=packet,
        result=result or None,
        next_cursor=next_cursor,
        reason=reason,
        error_code=error_code,
    )


def _structured_failures(method):
    """Keep operational/cleanup faults inside the structured result contract."""

    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except QueryContractError:
            raise
        except Exception:
            return _outcome(
                OUTCOME_EXECUTION_FAILURE,
                reason=_EXECUTION,
                error_code=_EXECUTION,
            )

    return wrapped


class ContinuationService:
    """Run fresh pages and consume one-time continuation capabilities."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        cursor_codec: CursorCodec,
        ttl_seconds: float = CONTINUATION_DEFAULT_TTL_SECONDS,
        pin_lease_seconds: float = CONTINUATION_DEFAULT_PIN_LEASE_SECONDS,
        max_chain_pages: int = CONTINUATION_DEFAULT_MAX_PAGES,
        claim_timeout_seconds: float = CONTINUATION_DEFAULT_CLAIM_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0 or pin_lease_seconds <= 0 or claim_timeout_seconds <= 0:
            raise ValueError("cursor and pin leases must be positive")
        if pin_lease_seconds < ttl_seconds:
            raise ValueError("pin lease must cover the full cursor lifetime")
        if isinstance(max_chain_pages, bool) or max_chain_pages < 1:
            raise ValueError("max_chain_pages must be positive")
        self.session_factory = session_factory
        self.cursor_codec = cursor_codec
        self.ttl_seconds = float(ttl_seconds)
        # A continuation must never retain its snapshot beyond its own
        # capability lifetime. Later pages further clamp the replacement pin
        # to the cursor's remaining lifetime.
        self.pin_lease_seconds = float(ttl_seconds)
        self.claim_timeout_seconds = float(claim_timeout_seconds)
        self.max_chain_pages = int(max_chain_pages)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = CursorStore(session_factory, self.clock)
        self.pager = ContinuationPager()

    @_structured_failures
    async def fresh_query(
        self,
        request: QueryRequest | Mapping[str, Any],
        *,
        page_size: int | None = None,
    ) -> ContinuationOutcome:
        parsed = coerce_request(request)
        size = _page_size(page_size)
        await self._sweep()
        reader: PublicationReader | None = None
        cursor_pin_id: str | None = None
        cursor_persisted = False
        try:
            auth = await resolve_effective_authorization(
                self.session_factory,
                parsed.workspace_id,
                assurance=parsed.assurance,
            )
            profile = (
                auth.partition_profile()
                if parsed.assurance == ASSURANCE_HIGH
                else parsed.profile
            )
            reader = await open_published_reader(
                self.session_factory,
                parsed.workspace_id,
                profile=profile,
                pin_lease_seconds=self.pin_lease_seconds,
            )
            if reader is None:
                if parsed.assurance == ASSURANCE_HIGH:
                    raise QueryAuthorizationError("high-assurance partition unavailable")
                budget = initial_budget()
                budget["pages"] = 1
                return _outcome(
                    OUTCOME_COMPLETE,
                    packet=await unpublished_packet(parsed, auth),
                    budget=budget,
                )
            run = await self.pager.run_async(
                reader,
                parsed,
                auth,
                initial_keyset(parsed),
                initial_budget(),
                size,
            )
            run.cumulative_budget["pages"] += 1
            latest = await resolve_effective_authorization(
                self.session_factory,
                parsed.workspace_id,
                assurance=parsed.assurance,
            )
            if latest.identity_view() != auth.identity_view():
                return _outcome(
                    OUTCOME_INVALIDATED,
                    reason=_AUTH_CHANGED,
                    error_code=_AUTH_CHANGED,
                )
            if not run.more_work:
                return _outcome(
                    OUTCOME_COMPLETE,
                    packet=run.packet,
                    budget=run.cumulative_budget,
                )
            budget_stop = self.pager.budget_stop_reason(
                parsed, run.cumulative_budget
            )
            if budget_stop is not None:
                return _outcome(
                    OUTCOME_COMPLETE,
                    packet=run.packet,
                    budget=run.cumulative_budget,
                    reason="continuation budget exhausted",
                    error_code=budget_stop,
                )
            if run.cumulative_budget["pages"] >= self.max_chain_pages:
                return _outcome(
                    OUTCOME_LOOP_LIMIT,
                    packet=run.packet,
                    budget=run.cumulative_budget,
                    reason="continuation chain limit reached",
                    error_code="loop_limit",
                )
            expires_at = utc(self.clock()) + timedelta(seconds=self.ttl_seconds)
            cursor_pin = await acquire_publication_pin(
                self.session_factory,
                reader.publication_set_id,
                lease_seconds=self.pin_lease_seconds,
                expires_at=expires_at,
            )
            cursor_pin_id = cursor_pin.pin_id
            handle, nonce = await self.store.insert(
                request=parsed,
                publication=reader.explain(),
                authorization=auth.identity_view(),
                keyset=run.keyset,
                cumulative_budget=run.cumulative_budget,
                expires_at=expires_at,
                pin_id=cursor_pin_id,
            )
            cursor_persisted = True
            return _outcome(
                OUTCOME_PARTIAL,
                packet=run.packet,
                budget=run.cumulative_budget,
                reason="more authorized work remains",
                error_code="continuation_available",
                next_cursor=self.cursor_codec.encode(handle, nonce),
            )
        except QueryAuthorizationError:
            return _outcome(
                OUTCOME_POLICY_FAIL_CLOSED,
                reason=_POLICY,
                error_code=_POLICY,
            )
        except Exception:
            return _outcome(
                OUTCOME_EXECUTION_FAILURE,
                reason=_EXECUTION,
                error_code=_EXECUTION,
            )
        finally:
            if reader is not None:
                await reader.close()
            if cursor_pin_id is not None and not cursor_persisted:
                await self._release_pin(cursor_pin_id)

    @_structured_failures
    async def continue_query(
        self,
        token: str,
        *,
        workspace_id: str,
        request: QueryRequest | Mapping[str, Any] | None = None,
        page_size: int | None = None,
    ) -> ContinuationOutcome:
        size = _page_size(page_size)
        try:
            envelope = self.cursor_codec.decode(token)
        except CursorExpiredError:
            return _outcome(OUTCOME_STALE, reason=_EXPIRED, error_code=_EXPIRED)
        except CursorCodecError:
            return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code=_INVALID)

        row = await self.store.load(envelope.handle)
        if row is None:
            return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code=_INVALID)
        if row.workspace_id != workspace_id:
            return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code=_INVALID)
        if row.status != CURSOR_STATUS_ACTIVE:
            await self._cleanup_terminal(row)
            return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code="cursor_replayed")
        if row.replay_state != CURSOR_REPLAY_FRESH:
            # Another request owns this nonce. Never revoke its in-flight
            # claim: doing so would let a replay race invalidate legitimate
            # progress. A crashed claim remains bounded by cursor/pin expiry.
            await self._sweep()
            return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code="cursor_replayed")
        if utc(row.expires_at) <= utc(self.clock()):
            await self._finish_unclaimed(row, CURSOR_STATUS_EXPIRED)
            return _outcome(OUTCOME_STALE, reason=_EXPIRED, error_code=_EXPIRED)
        await self._sweep()

        try:
            query = coerce_request(parse_cursor_state_json(row.query_json))
            publication = self._required_object(row.publication_json)
            snapshot = self._required_object(row.snapshot_json)
            authorization = self._required_object(row.authorization_json)
            keyset = validate_keyset(
                self._required_object(row.keyset_json), query
            )
            budget = validate_budget(self._required_object(row.cumulative_budget_json))
            if request is not None:
                supplied = coerce_request(request)
                if normalized_query(supplied) != normalized_query(query):
                    return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code=_INVALID)
                if supplied.workspace_id != workspace_id:
                    return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code=_INVALID)
                query = supplied
            if snapshot != {
                "snapshot_id": publication.get("snapshot_id"),
                "materialized_generation_id": publication.get(
                    "materialized_generation_id"
                ),
            }:
                raise ValueError("cursor snapshot binding is inconsistent")
            validate_cursor_expiry(row.expires_at, now=utc(self.clock()))
        except CursorExpiredError:
            await self._finish_unclaimed(row, CURSOR_STATUS_EXPIRED)
            return _outcome(OUTCOME_STALE, reason=_EXPIRED, error_code=_EXPIRED)
        except Exception:
            await self._finish_unclaimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_STALE, reason=_PIN_UNAVAILABLE, error_code="cursor_state_invalid")

        try:
            auth = await resolve_effective_authorization(
                self.session_factory,
                workspace_id,
                assurance=query.assurance,
            )
        except QueryAuthorizationError:
            await self._finish_unclaimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_POLICY_FAIL_CLOSED, reason=_POLICY, error_code=_POLICY)
        except Exception:
            await self._finish_unclaimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_EXECUTION_FAILURE, reason=_EXECUTION, error_code=_EXECUTION)
        if auth.identity_view() != authorization:
            await self._finish_unclaimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_INVALIDATED, reason=_AUTH_CHANGED, error_code=_AUTH_CHANGED)
        if row.page_count >= self.max_chain_pages:
            await self._finish_unclaimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_LOOP_LIMIT, reason="continuation chain limit reached", error_code="loop_limit")

        pins = await active_publication_pins(
            self.session_factory,
            publication_set_id=publication.get("publication_set_id"),
        )
        if row.pin_id is None or row.pin_id not in {pin.pin_id for pin in pins}:
            await self._finish_unclaimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_STALE, reason=_PIN_UNAVAILABLE, error_code=_PIN_UNAVAILABLE)
        if not await self.store.claim(row.handle, envelope.nonce):
            return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code="cursor_replayed")

        reader: PublicationReader | None = None
        try:
            reader = await open_pinned_publication(
                self.session_factory,
                publication["publication_set_id"],
                lease_seconds=min(
                    self.pin_lease_seconds,
                    max(
                        0.001,
                        (utc(row.expires_at) - utc(self.clock())).total_seconds(),
                    ),
                ),
            )
            if not publication_matches(publication, reader.explain()):
                await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
                return _outcome(OUTCOME_STALE, reason=_PIN_UNAVAILABLE, error_code=_PIN_UNAVAILABLE)
            run = await self.pager.run_async(
                reader, query, auth, keyset, budget, size
            )
            run.cumulative_budget["pages"] += 1
            latest = await resolve_effective_authorization(
                self.session_factory,
                workspace_id,
                assurance=query.assurance,
            )
            if latest.identity_view() != auth.identity_view():
                await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
                return _outcome(OUTCOME_INVALIDATED, reason=_AUTH_CHANGED, error_code=_AUTH_CHANGED)
            if not run.more_work:
                await self._finish_claimed(row, CURSOR_STATUS_EXHAUSTED)
                return _outcome(OUTCOME_COMPLETE, packet=run.packet, budget=run.cumulative_budget)
            budget_stop = self.pager.budget_stop_reason(
                query, run.cumulative_budget
            )
            if budget_stop is not None:
                await self._finish_claimed(row, CURSOR_STATUS_EXHAUSTED)
                return _outcome(
                    OUTCOME_COMPLETE,
                    packet=run.packet,
                    budget=run.cumulative_budget,
                    reason="continuation budget exhausted",
                    error_code=budget_stop,
                )
            if run.cumulative_budget["pages"] >= self.max_chain_pages:
                await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
                return _outcome(
                    OUTCOME_LOOP_LIMIT,
                    packet=run.packet,
                    budget=run.cumulative_budget,
                    reason="continuation chain limit reached",
                    error_code="loop_limit",
                )
            new_nonce = await self.store.rotate(
                handle=row.handle,
                old_nonce=envelope.nonce,
                keyset=run.keyset,
                cumulative_budget=run.cumulative_budget,
                pin_id=row.pin_id,
                expires_at=row.expires_at,
            )
            if new_nonce is None:
                await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
                return _outcome(OUTCOME_INVALIDATED, reason=_INVALID, error_code="cursor_state_changed")
            return _outcome(
                OUTCOME_PARTIAL,
                packet=run.packet,
                budget=run.cumulative_budget,
                reason="more authorized work remains",
                error_code="continuation_available",
                next_cursor=self.cursor_codec.encode(row.handle, new_nonce),
            )
        except QueryAuthorizationError:
            await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_POLICY_FAIL_CLOSED, reason=_POLICY, error_code=_POLICY)
        except (PublicationIntegrityError, UnknownPublicationSetError):
            await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(OUTCOME_STALE, reason=_PIN_UNAVAILABLE, error_code=_PIN_UNAVAILABLE)
        except Exception:
            await self._finish_claimed(row, CURSOR_STATUS_REVOKED)
            return _outcome(
                OUTCOME_EXECUTION_FAILURE,
                reason=_EXECUTION,
                error_code=_EXECUTION,
            )
        finally:
            if reader is not None:
                await reader.close()

    @staticmethod
    def _required_object(value: str | None) -> dict[str, Any]:
        if value is None:
            raise ValueError("cursor state is absent")
        parsed = parse_cursor_state_json(value)
        if not isinstance(parsed, dict):
            raise ValueError("cursor state is not an object")
        return parsed

    async def _finish_claimed(self, row: Any, status: str) -> None:
        if await self.store.terminalize_claimed(row.handle, status):
            await self._release_pin(row.pin_id)

    async def _finish_unclaimed(self, row: Any, status: str) -> None:
        if await self.store.terminalize_unclaimed(row.handle, row.nonce, status):
            await self._release_pin(row.pin_id)

    async def _cleanup_terminal(self, row: Any) -> None:
        if row.status != CURSOR_STATUS_ACTIVE and row.pin_id is not None:
            await self._release_pin(row.pin_id)

    async def reclaim_expired_cursors(self) -> int:
        """Reclaim expired, terminal, and abandoned claimed cursor rows."""

        return await self._sweep()

    async def _sweep(self) -> int:
        claim_before = utc(self.clock()) - timedelta(
            seconds=self.claim_timeout_seconds
        )
        reclaimed, pin_ids = await self.store.reclaim(claim_before=claim_before)
        for pin_id in pin_ids:
            await self._release_pin(pin_id)
        return reclaimed

    async def _release_pin(self, pin_id: str | None) -> None:
        if not pin_id:
            return
        try:
            await release_publication_pin(self.session_factory, pin_id)
        except Exception:
            return
