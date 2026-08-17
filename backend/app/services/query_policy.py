"""Durable write surface for query-time authorization policy (PR78).

The PR77 query core treats authorization as trusted server-side state.
This service is the operator-facing way to *change* that state through
the kernel commit spine, so every domain assignment and every live
deny/lift event is durable, append-only, and auditable — never a
caller-supplied request field. Reads happen in
``app.context_runtime.authorization`` (the trusted resolver); this
module only commits.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.models import KernelRecord
from app.kernel.records import (
    ACCESS_DENIAL_TARGET_DOMAIN,
    ACCESS_DENIAL_TARGET_KINDS,
    ACCESS_DENIAL_TARGET_RECORD,
    ACCESS_DENIAL_TARGET_SOURCE,
    AccessDenialRecord,
    SecurityDomainRecord,
)
from app.utils.canonical import payload_byte_hash

__all__ = ["QueryPolicyService"]

#: Producer tag recorded on every policy commit (audit trail).
_POLICY_PRODUCER = "marker.query_policy.v1"


class QueryPolicyService:
    """Commits security-domain assignments and live-deny events.

    One call is one atomic kernel commit: the effect becomes visible to
    the PR78 authorization resolver the moment the commit's transaction
    commits — independent of any publication/index rebuild (a deny must
    outrun background reindexing, never wait for it).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        commit_service: KernelCommitService,
        *,
        workspace_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._commits = commit_service
        self.workspace_id = workspace_id

    # -- domain assignment --------------------------------------------------

    async def assign_source_domain(
        self,
        source_ref: str,
        domain_key: str,
        *,
        basis: Mapping[str, Any] | None = None,
    ) -> str:
        """Assign (or reassign) one source to a security domain.

        A reassignment is a new policy record; content revisions and
        published generations are untouched. Returns the record id.
        """
        record = SecurityDomainRecord(
            record_id=self._record_id("assign", f"{source_ref}|{domain_key}"),
            source_ref=source_ref,
            domain_key=domain_key,
            assignment_basis=dict(basis or {}),
        )
        await self._commit_one(record)
        return record.record_id

    # -- live deny / lift ---------------------------------------------------

    async def deny_domain(
        self, domain_key: str, *, basis: Mapping[str, Any] | None = None
    ) -> str:
        return await self.set_denial(
            ACCESS_DENIAL_TARGET_DOMAIN, domain_key, denied=True, basis=basis
        )

    async def deny_source(
        self, source_ref: str, *, basis: Mapping[str, Any] | None = None
    ) -> str:
        return await self.set_denial(
            ACCESS_DENIAL_TARGET_SOURCE, source_ref, denied=True, basis=basis
        )

    async def deny_record(
        self, record_id: str, *, basis: Mapping[str, Any] | None = None
    ) -> str:
        return await self.set_denial(
            ACCESS_DENIAL_TARGET_RECORD, record_id, denied=True, basis=basis
        )

    async def allow_domain(
        self, domain_key: str, *, basis: Mapping[str, Any] | None = None
    ) -> str:
        """Explicit re-authorization of a domain (lift the live deny)."""
        return await self.set_denial(
            ACCESS_DENIAL_TARGET_DOMAIN, domain_key, denied=False, basis=basis
        )

    async def allow_source(
        self, source_ref: str, *, basis: Mapping[str, Any] | None = None
    ) -> str:
        return await self.set_denial(
            ACCESS_DENIAL_TARGET_SOURCE, source_ref, denied=False, basis=basis
        )

    async def allow_record(
        self, record_id: str, *, basis: Mapping[str, Any] | None = None
    ) -> str:
        return await self.set_denial(
            ACCESS_DENIAL_TARGET_RECORD, record_id, denied=False, basis=basis
        )

    async def set_denial(
        self,
        target_kind: str,
        target_ref: str,
        *,
        denied: bool,
        basis: Mapping[str, Any] | None = None,
    ) -> str:
        """Commit one deny/lift event for a target, chained onto the
        target's previous event so history stays append-only."""
        if target_kind not in ACCESS_DENIAL_TARGET_KINDS:
            raise ValueError(
                f"invalid target_kind {target_kind!r}; allowed: "
                f"{sorted(ACCESS_DENIAL_TARGET_KINDS)}"
            )
        previous = await self._latest_denial_event(target_kind, target_ref)
        record = AccessDenialRecord(
            record_id=self._record_id(
                "deny" if denied else "allow",
                f"{target_kind}|{target_ref}|{previous or ''}",
            ),
            target_kind=target_kind,
            target_ref=target_ref,
            denied=denied,
            supersedes=previous,
            denial_basis=dict(basis or {}),
        )
        await self._commit_one(record)
        return record.record_id

    # -- internals -----------------------------------------------------------

    def _record_id(self, prefix: str, distinct: str) -> str:
        digest = payload_byte_hash(distinct.encode("utf-8"))[:24]
        return f"{prefix}.{digest.removeprefix('sha256:')[:20]}"

    async def _commit_one(self, record: Any) -> None:
        await self._commits.commit(
            KernelCommitBatch(
                workspace_id=self.workspace_id,
                records=(record,),
                producer={"service": _POLICY_PRODUCER},
            )
        )

    async def _latest_denial_event(
        self, target_kind: str, target_ref: str
    ) -> str | None:
        """Record id of the newest committed denial event for the target
        (by causal commit order), or None."""
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            KernelRecord.id,
                            KernelRecord.kernel_commit_id,
                            KernelRecord.payload_json,
                        )
                        .where(
                            KernelRecord.workspace_id == self.workspace_id,
                            KernelRecord.record_class == "access_denial",
                        )
                        .order_by(
                            KernelRecord.kernel_commit_id.desc(),
                            KernelRecord.id.desc(),
                        )
                    )
                )
                .all()
            )
        for record_id, _commit_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except Exception:
                continue
            if (
                payload.get("target_kind") == target_kind
                and payload.get("target_ref") == target_ref
            ):
                return record_id
        return None
