"""Durable cursor-row operations for PR79A."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.continuation import (
    CURSOR_REPLAY_CONSUMED,
    CURSOR_REPLAY_FRESH,
    CURSOR_STATUS_ACTIVE,
)
from app.context_runtime.continuation_state import canonical, utc
from app.context_runtime.cursor import new_cursor_handle, new_cursor_nonce
from app.context_runtime.contract import QueryRequest, normalized_query
from app.context_runtime.packets import representation_semantics
from app.kernel.models import KernelQueryCursor


class CursorStore:
    """Small persistence boundary with conditional nonce transitions."""

    def __init__(self, session_factory: async_sessionmaker, clock):
        self.session_factory = session_factory
        self.clock = clock

    async def load(self, handle: str) -> KernelQueryCursor | None:
        async with self.session_factory() as session:
            return await session.get(KernelQueryCursor, handle)

    async def reclaim(
        self,
        *,
        claim_before: datetime,
    ) -> tuple[int, tuple[str, ...]]:
        """Delete expired/terminal/stale-claim rows and return their pin ids.

        Conditional selection and deletion share one transaction. Cursor
        tokens are opaque capabilities, not audit records; keeping dead state
        forever would turn abandoned pagination into unbounded database growth.
        """

        now = utc(self.clock())
        reclaimable = or_(
            KernelQueryCursor.expires_at <= now,
            KernelQueryCursor.status != CURSOR_STATUS_ACTIVE,
            and_(
                KernelQueryCursor.status == CURSOR_STATUS_ACTIVE,
                KernelQueryCursor.replay_state == CURSOR_REPLAY_CONSUMED,
                KernelQueryCursor.updated_at <= claim_before,
            ),
        )
        async with self.session_factory() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(
                            KernelQueryCursor.handle,
                            KernelQueryCursor.pin_id,
                        ).where(reclaimable)
                    )
                ).all()
                await session.execute(delete(KernelQueryCursor).where(reclaimable))
        return len(rows), tuple(pin_id for _handle, pin_id in rows if pin_id)

    async def insert(
        self,
        *,
        request: QueryRequest,
        publication: Mapping[str, Any],
        authorization: Mapping[str, Any],
        keyset: Mapping[str, Any],
        cumulative_budget: Mapping[str, Any],
        expires_at: datetime,
        pin_id: str,
        principal_id: str | None = None,
    ) -> tuple[str, str]:
        handle = new_cursor_handle()
        nonce = new_cursor_nonce()
        now = utc(self.clock())
        row = KernelQueryCursor(
            handle=handle,
            workspace_id=request.workspace_id,
            query_json=canonical(normalized_query(request)),
            snapshot_json=canonical(
                {
                    "snapshot_id": publication.get("snapshot_id"),
                    "materialized_generation_id": publication.get(
                        "materialized_generation_id"
                    ),
                }
            ),
            publication_json=canonical(dict(publication)),
            authorization_json=canonical(dict(authorization)),
            representation_json=canonical(representation_semantics()),
            keyset_json=canonical(dict(keyset)),
            cumulative_budget_json=canonical(dict(cumulative_budget)),
            page_count=int(cumulative_budget["pages"]),
            expires_at=expires_at,
            pin_id=pin_id,
            principal_id=principal_id,
            status=CURSOR_STATUS_ACTIVE,
            nonce=nonce,
            replay_state=CURSOR_REPLAY_FRESH,
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(row)
        return handle, nonce

    async def claim(self, handle: str, nonce: str) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelQueryCursor)
                    .where(
                        KernelQueryCursor.handle == handle,
                        KernelQueryCursor.status == CURSOR_STATUS_ACTIVE,
                        KernelQueryCursor.replay_state == CURSOR_REPLAY_FRESH,
                        KernelQueryCursor.nonce == nonce,
                    )
                    .values(
                        replay_state=CURSOR_REPLAY_CONSUMED,
                        updated_at=utc(self.clock()),
                    )
                )
                return result.rowcount == 1

    async def rotate(
        self,
        *,
        handle: str,
        old_nonce: str,
        keyset: Mapping[str, Any],
        cumulative_budget: Mapping[str, Any],
        pin_id: str,
        expires_at: datetime,
    ) -> str | None:
        new_nonce = new_cursor_nonce()
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelQueryCursor)
                    .where(
                        KernelQueryCursor.handle == handle,
                        KernelQueryCursor.status == CURSOR_STATUS_ACTIVE,
                        KernelQueryCursor.replay_state == CURSOR_REPLAY_CONSUMED,
                        KernelQueryCursor.nonce == old_nonce,
                    )
                    .values(
                        keyset_json=canonical(dict(keyset)),
                        cumulative_budget_json=canonical(dict(cumulative_budget)),
                        page_count=int(cumulative_budget["pages"]),
                        expires_at=expires_at,
                        pin_id=pin_id,
                        nonce=new_nonce,
                        replay_state=CURSOR_REPLAY_FRESH,
                        updated_at=utc(self.clock()),
                    )
                )
                if result.rowcount != 1:
                    return None
        return new_nonce

    async def terminalize_claimed(self, handle: str, status: str) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelQueryCursor)
                    .where(
                        KernelQueryCursor.handle == handle,
                        KernelQueryCursor.status == CURSOR_STATUS_ACTIVE,
                        KernelQueryCursor.replay_state == CURSOR_REPLAY_CONSUMED,
                    )
                    .values(
                        status=status,
                        replay_state=CURSOR_REPLAY_CONSUMED,
                        pin_id=None,
                        updated_at=utc(self.clock()),
                    )
                )
                return result.rowcount == 1

    async def terminalize_unclaimed(
        self, handle: str, nonce: str, status: str
    ) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(KernelQueryCursor)
                    .where(
                        KernelQueryCursor.handle == handle,
                        KernelQueryCursor.status == CURSOR_STATUS_ACTIVE,
                        KernelQueryCursor.replay_state == CURSOR_REPLAY_FRESH,
                        KernelQueryCursor.nonce == nonce,
                    )
                    .values(
                        status=status,
                        replay_state=CURSOR_REPLAY_CONSUMED,
                        pin_id=None,
                        updated_at=utc(self.clock()),
                    )
                )
                return result.rowcount == 1
