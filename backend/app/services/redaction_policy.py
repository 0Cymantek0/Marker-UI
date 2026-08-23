"""Durable write surface for redaction policy (PR89).

The serving path treats redaction as trusted server-side state (see
``app.context_runtime.redaction``). This service is the operator-facing
way to *change* that state through the kernel commit spine, so every
profile revision is durable, append-only, and auditable — never a
caller-supplied request field. One call is one atomic kernel commit:
the new rules become effective for every release decision the moment
the commit's transaction commits, independent of any publication or
index rebuild (redaction must outrun background reindexing, never wait
for it).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.models import KernelRecord
from app.kernel.records import (
    RedactionProfileRecord,
    normalize_redaction_rules,
)
from app.utils.canonical import payload_byte_hash

__all__ = ["RedactionPolicyService"]

#: Producer tag recorded on every policy commit (audit trail).
_POLICY_PRODUCER = "marker.redaction_policy.v1"


class RedactionPolicyService:
    """Commits revisions of named redaction profiles for one workspace."""

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

    async def define_profile(
        self,
        profile_id: str,
        rules: Any,
        *,
        basis: Mapping[str, Any] | None = None,
    ) -> str:
        """Commit one revision of a named redaction profile.

        The new revision supersedes the profile's previous one; an empty
        rule list is a valid *relaxation* revision (nothing redacted).
        Returns the record id.
        """
        normalized = normalize_redaction_rules(rules)
        previous = await self._latest_profile_record(profile_id)
        record = RedactionProfileRecord(
            record_id=self._record_id(
                "redact", f"{profile_id}|{previous or ''}|{len(normalized)}"
            ),
            profile_id=profile_id,
            rules=tuple(normalized),
            supersedes=previous,
            redaction_basis=dict(basis or {}),
        )
        await self._commit_one(record)
        return record.record_id

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

    async def _latest_profile_record(self, profile_id: str) -> str | None:
        """Record id of the newest committed revision for the profile
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
                            KernelRecord.record_class == "redaction_profile",
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
            if payload.get("profile_id") == profile_id:
                return record_id
        return None
