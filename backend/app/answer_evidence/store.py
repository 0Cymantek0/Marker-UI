"""Durable row operations for the answer-evidence boundary (PR85).

Small persistence boundary in the CursorStore style: every state
transition is one transaction with conditional semantics, so retries and
races converge instead of creating contradictory truth. Rows are
append-only — this layer offers no update or delete path for
disclosures, traces, or assessments.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.models import (
    KernelAnswerSupportAssessment,
    KernelAnswerTrace,
    KernelAnswerTraceDisclosure,
    KernelContextDisclosure,
)

__all__ = ["AnswerEvidenceStore"]


class AnswerEvidenceStore:
    """Row-level operations; no domain validation lives here."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    # -- disclosures --------------------------------------------------

    async def insert_disclosure(
        self,
        *,
        disclosure_id: str,
        workspace_id: str,
        principal_id: str | None,
        packet_id: str,
        packet_json: str,
        delivery_status: str,
    ) -> KernelContextDisclosure:
        row = KernelContextDisclosure(
            disclosure_id=disclosure_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            packet_id=packet_id,
            packet_json=packet_json,
            delivery_status=delivery_status,
        )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(row)
        return row

    async def load_disclosures(
        self, *, workspace_id: str, disclosure_ids: Sequence[str]
    ) -> dict[str, KernelContextDisclosure]:
        """Load tenant-scoped disclosures by id; order is not implied."""

        if not disclosure_ids:
            return {}
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KernelContextDisclosure).where(
                            KernelContextDisclosure.workspace_id == workspace_id,
                            KernelContextDisclosure.disclosure_id.in_(
                                list(disclosure_ids)
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {row.disclosure_id: row for row in rows}

    # -- traces ---------------------------------------------------------

    async def load_trace(
        self,
        *,
        workspace_id: str,
        trace_id: str | None = None,
        answer_ref: str | None = None,
    ) -> KernelAnswerTrace | None:
        if (trace_id is None) == (answer_ref is None):
            raise ValueError("provide exactly one of trace_id or answer_ref")
        async with self.session_factory() as session:
            statement = select(KernelAnswerTrace).where(
                KernelAnswerTrace.workspace_id == workspace_id
            )
            if trace_id is not None:
                statement = statement.where(KernelAnswerTrace.trace_id == trace_id)
            else:
                statement = statement.where(
                    KernelAnswerTrace.answer_ref == answer_ref
                )
            return (await session.execute(statement)).scalar_one_or_none()

    async def insert_trace(
        self,
        *,
        trace_id: str,
        workspace_id: str,
        principal_id: str | None,
        answer_ref: str,
        answer_digest: str,
        answer_content: str,
        context_fingerprint: str,
        ordered_disclosure_ids: Sequence[str],
    ) -> None:
        """Insert the trace and its full ordered link set atomically.

        Raises :class:`IntegrityError` when a concurrent commit already
        claimed ``(workspace_id, answer_ref)``; the caller resolves the
        collision idempotently or as an explicit conflict.
        """

        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    KernelAnswerTrace(
                        trace_id=trace_id,
                        workspace_id=workspace_id,
                        principal_id=principal_id,
                        answer_ref=answer_ref,
                        answer_digest=answer_digest,
                        answer_content=answer_content,
                        context_fingerprint=context_fingerprint,
                    )
                )
                for position, disclosure_id in enumerate(ordered_disclosure_ids):
                    session.add(
                        KernelAnswerTraceDisclosure(
                            trace_id=trace_id,
                            position=position,
                            workspace_id=workspace_id,
                            disclosure_id=disclosure_id,
                        )
                    )

    async def load_trace_links(
        self, trace_id: str
    ) -> list[KernelAnswerTraceDisclosure]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KernelAnswerTraceDisclosure)
                        .where(KernelAnswerTraceDisclosure.trace_id == trace_id)
                        .order_by(KernelAnswerTraceDisclosure.position)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    # -- assessments ----------------------------------------------------

    async def load_assessment_by_key(
        self, *, trace_id: str, assessment_key: str
    ) -> KernelAnswerSupportAssessment | None:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(KernelAnswerSupportAssessment).where(
                        KernelAnswerSupportAssessment.trace_id == trace_id,
                        KernelAnswerSupportAssessment.assessment_key
                        == assessment_key,
                    )
                )
            ).scalar_one_or_none()

    async def next_assessment_seq(self, trace_id: str) -> int:
        async with self.session_factory() as session:
            current = (
                await session.execute(
                    select(func.max(KernelAnswerSupportAssessment.seq)).where(
                        KernelAnswerSupportAssessment.trace_id == trace_id
                    )
                )
            ).scalar_one_or_none()
        return (current or 0) + 1

    async def insert_assessment(
        self, row: KernelAnswerSupportAssessment
    ) -> None:
        """Append one assessment; ``IntegrityError`` signals a key or
        sequence race for the caller to resolve deterministically."""

        async with self.session_factory() as session:
            async with session.begin():
                session.add(row)

    async def load_assessments(
        self, *, workspace_id: str, trace_id: str
    ) -> list[KernelAnswerSupportAssessment]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KernelAnswerSupportAssessment)
                        .where(
                            KernelAnswerSupportAssessment.workspace_id
                            == workspace_id,
                            KernelAnswerSupportAssessment.trace_id == trace_id,
                        )
                        .order_by(KernelAnswerSupportAssessment.seq)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def load_assessment(
        self, *, workspace_id: str, assessment_id: str
    ) -> KernelAnswerSupportAssessment | None:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(KernelAnswerSupportAssessment).where(
                        KernelAnswerSupportAssessment.workspace_id == workspace_id,
                        KernelAnswerSupportAssessment.assessment_id == assessment_id,
                    )
                )
            ).scalar_one_or_none()
