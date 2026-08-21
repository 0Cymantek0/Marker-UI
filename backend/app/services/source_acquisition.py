"""Stable local source acquisition service (V3.2 PR70/71 local slice).

The single application seam that turns a document ingress path into
committed source truth:

    permitted/marker-owned source
      -> LocalSourceStore.stage_from_path  (coherent, single-open, hashed)
      -> SourceIdentity / ContentRevision / AccessPolicyRevision /
         AuthorizationEpoch / SourceObservation in one kernel commit
      -> AcquiredSourceRevision handed to submission + probe + execution

Design invariants (plan §6, §12):

* acquisition commits its records *before* any work is authorized
  against them — an authorized job can never reference an uncommitted
  revision, because the config block only exists after the commit
  returned;
* convergence is identity-driven, not exception-driven in the common
  case: candidate records are identity-hashed locally, already-committed
  identities are resolved and reused (their record ids feed edges and
  the returned config), and only genuinely new records enter the batch.
  A concurrent duplicate insert still converges through one bounded
  retry after ``DuplicateRecordIdentityError``;
* rejected/incoherent acquisitions commit an observation with
  ``outcome=rejected_incoherent`` and never mint a ContentRevision;
* ``resolve()`` re-validates a config block against committed kernel
  records and the artifact store, so restarts/retries reuse owned bytes
  without re-trusting an external path that may have changed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import select

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import DuplicateRecordIdentityError, KernelError
from app.kernel.models import KernelRecord
from app.kernel.records import (
    SOURCE_CONSISTENCY_BEST_EFFORT,
    SOURCE_CONSISTENCY_STABLE_HANDLE,
    SOURCE_KIND_LOCAL_PATH,
    SOURCE_KIND_UPLOAD,
    SOURCE_KIND_URL,
    AccessPolicyRevisionRecord,
    AuthorizationEpochRecord,
    ContentRevisionRecord,
    KernelEdge,
    KernelRecord as RecordInput,
    SourceIdentityRecord,
    SourceObservationRecord,
)
from app.kernel.source_object_store import S3_SOURCE_STORE_PROFILE
from app.kernel.source_store import (
    LOCAL_SOURCE_STORE_PROFILE,
    IncoherentSourceError,
    SourceArtifactStore,
    SourceStoreError,
)
from app.services.source_materialization import VerifiedSourceMaterializer
from app.services.policy import (
    _is_under,
    unrestricted_local_paths_enabled,
    workspace_roots,
)
from app.utils.canonical import (
    canonical_json_bytes,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "AcquiredSourceRevision",
    "SOURCE_CONFIG_KEY",
    "SOURCE_STORE_PROFILES",
    "SourceAcquisitionService",
    "default_source_acquisition_service",
    "set_default_source_acquisition_service",
]

#: config_json key carrying the committed source-revision block.
SOURCE_CONFIG_KEY = "source_revision"

_OBSERVER = "marker-ui-source-acquisition"
_POLICY_PROFILE = "local_v1"

_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".epub": "application/epub+zip",
    ".html": "text/html",
    ".md": "text/markdown",
    ".txt": "text/plain",
}

#: Consistency class by source kind for this slice: local paths and
#: marker-owned uploads/downloads stage through a single-open verified
#: handle (``stable_handle``); a generic URL origin has no provider
#: version validators, so its acquisition is honestly best-effort even
#: though the staged bytes are immutable once owned.
_KIND_CONSISTENCY: dict[str, str] = {
    SOURCE_KIND_LOCAL_PATH: SOURCE_CONSISTENCY_STABLE_HANDLE,
    SOURCE_KIND_UPLOAD: SOURCE_CONSISTENCY_STABLE_HANDLE,
    SOURCE_KIND_URL: SOURCE_CONSISTENCY_BEST_EFFORT,
}


def media_type_for_suffix(suffix: str) -> str:
    return _MEDIA_TYPES.get(suffix.lower(), "application/octet-stream")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Config blocks may carry one of these declared store profiles. A
#: block committed before PR83B3 carries no ``store_profile`` and is
#: interpreted as the local profile — the only topology that existed
#: when it was committed; it is never silently reinterpreted as
#: guaranteed-available in the shared topology.
SOURCE_STORE_PROFILES = frozenset({LOCAL_SOURCE_STORE_PROFILE, S3_SOURCE_STORE_PROFILE})


@dataclass(frozen=True)
class AcquiredSourceRevision:
    """Committed source truth handed to submission/probe/execution."""

    source_id: str
    content_revision_id: str
    access_policy_id: str
    authorization_epoch: int
    blob_key: str
    byte_length: int
    consistency_class: str
    media_type: str
    suffix: str
    kernel_commit_id: int
    store_profile: str = LOCAL_SOURCE_STORE_PROFILE

    def to_config(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "content_revision_id": self.content_revision_id,
            "access_policy_id": self.access_policy_id,
            "authorization_epoch": self.authorization_epoch,
            "blob_key": self.blob_key,
            "byte_length": self.byte_length,
            "consistency_class": self.consistency_class,
            "media_type": self.media_type,
            "suffix": self.suffix,
            "kernel_commit_id": self.kernel_commit_id,
            "store_profile": self.store_profile,
        }

    @classmethod
    def from_config(cls, block: Mapping[str, Any]) -> "AcquiredSourceRevision | None":
        """Strictly validate a config block's shape (no kernel lookup)."""
        try:
            store_profile = str(
                block.get("store_profile") or LOCAL_SOURCE_STORE_PROFILE
            )
            if store_profile not in SOURCE_STORE_PROFILES:
                return None
            return cls(
                source_id=str(block["source_id"]),
                content_revision_id=str(block["content_revision_id"]),
                access_policy_id=str(block["access_policy_id"]),
                authorization_epoch=int(block["authorization_epoch"]),
                blob_key=str(block["blob_key"]),
                byte_length=int(block["byte_length"]),
                consistency_class=str(block["consistency_class"]),
                media_type=str(block["media_type"]),
                suffix=str(block["suffix"]),
                kernel_commit_id=int(block.get("kernel_commit_id") or 0),
                store_profile=store_profile,
            )
        except (KeyError, TypeError, ValueError):
            return None


class SourceAcquisitionService:
    """Acquire local/marker-owned sources into committed kernel truth."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        commit_service: KernelCommitService,
        store: SourceArtifactStore,
        *,
        workspace_id: str = "local",
        cache_root: Path | None = None,
    ) -> None:
        self._sf = session_factory
        self._commit_service = commit_service
        self.store = store
        self.workspace_id = workspace_id
        self._cache_root = cache_root
        self._materializer: VerifiedSourceMaterializer | None = None

    @property
    def store_profile(self) -> str:
        """Active physical source-artifact topology identity."""
        return self.store.profile

    @property
    def legacy_submit_fallback(self) -> bool:
        """Whether submissions without a source revision may proceed.

        True only for the node-local profile: its historical
        path-trust shape is a documented compatibility contract. An
        industrial (shared) profile must never execute unowned paths —
        acquisition failures propagate and the submission fails
        honestly.
        """
        return self.store.profile == LOCAL_SOURCE_STORE_PROFILE

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------

    async def acquire(
        self,
        path: Path,
        *,
        source_kind: str,
        suffix: str,
        job_id: str = "",
        source_key_override: str | None = None,
        media_type: str | None = None,
        hooks: Mapping[str, Callable[[], None]] | None = None,
    ) -> AcquiredSourceRevision:
        """Acquire *path* coherently and commit its source truth.

        Raises :class:`IncoherentSourceError` when the source mutated
        during acquisition (a rejected observation is committed first),
        and :class:`SourceStoreError` for unusable inputs.
        """
        resolved = Path(os.path.realpath(path))
        if not resolved.is_file():
            raise SourceStoreError(f"source is not a regular file: {resolved}")
        suffix = suffix.lower()
        media_type = media_type or media_type_for_suffix(suffix)

        policy_facts = self._check_and_capture_policy(source_kind, resolved)
        source_key = source_key_override or self._derive_source_key(source_kind, resolved, job_id)

        try:
            staged = await self.store.stage_from_path(resolved, suffix=suffix, hooks=hooks)
        except IncoherentSourceError as exc:
            await self._record_rejection(
                source_kind=source_kind,
                source_key=source_key,
                suffix=suffix,
                media_type=media_type,
                reason=str(exc),
                resolved=str(resolved),
                job_id=job_id,
            )
            raise

        consistency = _KIND_CONSISTENCY[source_kind]
        identity = SourceIdentityRecord(
            record_id="source." + payload_byte_hash(source_key.encode("utf-8"))[:24],
            source_kind=source_kind,
            source_key=source_key,
            registered_context={"first_observed_path": str(resolved)},
        )
        epoch_record, epoch_number = await self._current_or_next_epoch()
        policy = AccessPolicyRevisionRecord(
            record_id="access."
            + payload_byte_hash(
                f"{source_key}|{canonical_json_bytes(to_json_ready(policy_facts))}".encode("utf-8")
            )[:24],
            source_ref=identity.record_id,
            policy_profile=_POLICY_PROFILE,
            policy_facts=policy_facts,
        )
        revision = ContentRevisionRecord(
            record_id="content."
            + payload_byte_hash(f"{source_key}|{staged.blob_key}|{consistency}".encode("utf-8"))[:24],
            source_ref=identity.record_id,
            blob_key=staged.blob_key,
            byte_length=staged.byte_length,
            media_type=media_type,
            consistency_class=consistency,
            suffix=suffix,
        )
        observation = SourceObservationRecord(
            record_id="obs."
            + payload_byte_hash(f"{source_key}|{staged.blob_key}|{_now_iso()}".encode("utf-8"))[:24],
            observer=_OBSERVER,
            source_ref=identity.record_id,
            outcome="accepted",
            content_revision_ref=revision.record_id,
            access_policy_ref=policy.record_id,
            authorization_epoch=epoch_number,
            evidence={
                "observed_at": _now_iso(),
                "observed_path": str(resolved),
                "acquired_via": "single-open-streamed-stage",
                # stat evidence serialized as strings: platform identity
                # fields (device/mtime_ns) exceed the canonical safe-int
                # range and are audit text, not computed identity input
                "handle_pre_stat": {k: str(v) for k, v in staged.pre_stat.items()},
                "handle_post_stat": {k: str(v) for k, v in staged.post_stat.items()},
                "artifact_already_present": staged.already_present,
                "job_id": job_id,
            },
        )

        result = await self._commit_converging(
            records=[identity, revision, policy, observation]
            + ([epoch_record] if epoch_record is not None else []),
            edges=[
                KernelEdge(
                    edge_kind="derived_from",
                    source_ref=revision.record_id,
                    target_ref=identity.record_id,
                ),
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
            ],
            producer={
                "operation": "source.acquire",
                "source_kind": source_kind,
                "job_id": job_id,
                "blob_key": staged.blob_key,
            },
        )

        return AcquiredSourceRevision(
            source_id=result["ids"][identity.record_id],
            content_revision_id=result["ids"][revision.record_id],
            access_policy_id=result["ids"][policy.record_id],
            authorization_epoch=epoch_number,
            blob_key=staged.blob_key,
            byte_length=staged.byte_length,
            consistency_class=consistency,
            media_type=media_type,
            suffix=suffix,
            kernel_commit_id=result["commit_id"],
            store_profile=self.store.profile,
        )

    # ------------------------------------------------------------------
    # resolution (restart / retry / duplicate submission)
    # ------------------------------------------------------------------

    async def resolve(
        self, block: Mapping[str, Any]
    ) -> AcquiredSourceRevision | None:
        """Validate a config block against committed truth + owned bytes.

        Returns None when the block is malformed, its revision is not
        committed in this workspace, the committed record disagrees with
        the block, the block belongs to a different store profile than
        the active one, or the artifact bytes are no longer available
        under the active profile. Callers must re-acquire (fresh
        revision) rather than fall back to an external path or another
        topology.
        """
        candidate = AcquiredSourceRevision.from_config(block)
        if candidate is None:
            return None
        if candidate.store_profile != self.store.profile:
            # The revision's bytes are durable in the profile that
            # committed them; this runtime's topology cannot vouch for
            # them. Honest unresolvability — never a cross-profile
            # fallback (a legacy block without a profile is local-only
            # by construction).
            return None

        async with self._sf() as session:
            row = await session.get(KernelRecord, candidate.content_revision_id)
        if row is None or row.workspace_id != self.workspace_id:
            return None
        if row.record_class != "content_revision":
            return None
        try:
            committed = json.loads(row.payload_json)
        except (TypeError, ValueError):
            return None
        if any(
            committed.get(key) != value
            for key, value in (
                ("blob_key", candidate.blob_key),
                ("byte_length", candidate.byte_length),
                ("media_type", candidate.media_type),
                ("consistency_class", candidate.consistency_class),
                ("suffix", candidate.suffix),
            )
        ):
            return None

        if not await self.store.artifact_exists(candidate.blob_key, candidate.suffix):
            return None
        length = await self.store.available_length(candidate.blob_key, candidate.suffix)
        if length is None or length != candidate.byte_length:
            return None
        return candidate

    async def artifact_path_for(self, revision: AcquiredSourceRevision) -> Path:
        """Local-profile artifact path (the immutable owned file)."""
        return self.store.artifact_path(revision.blob_key, revision.suffix)

    def execution_locator_for(self, revision: AcquiredSourceRevision) -> str:
        """Durable, credential-free locator for the revision's bytes.

        Local profile keeps the historical artifact-path string for
        compatibility; shared profiles report their object-store
        locator so authorized records self-describe their topology.
        """
        if self.store.profile == LOCAL_SOURCE_STORE_PROFILE:
            return str(self.store.artifact_path(revision.blob_key, revision.suffix))
        return self.store.locator_for(revision.blob_key, revision.suffix)

    async def consumable_path_for(self, revision: AcquiredSourceRevision) -> Path:
        """Converter-facing local path holding exactly the revision's bytes.

        Local profile: the immutable owned artifact itself (no copy).
        Shared profile: a verified node-local materialization — every
        use is content-verified against the committed identity, and a
        corrupt or stale working copy is rebuilt from durable shared
        truth. Raises SourceStoreError when the shared bytes are
        missing or fail verification: unavailable truth, never a
        fallback authority.
        """
        if self.store.profile == LOCAL_SOURCE_STORE_PROFILE:
            return await self.artifact_path_for(revision)
        if self._materializer is None:
            from app.core.config import SOURCE_CACHE_ROOT

            self._materializer = VerifiedSourceMaterializer(
                self.store, self._cache_root or SOURCE_CACHE_ROOT
            )
        return await self._materializer.path_for(
            revision.blob_key,
            revision.suffix,
            expected_length=revision.byte_length,
        )

    # ------------------------------------------------------------------
    # policy / epoch
    # ------------------------------------------------------------------

    def _check_and_capture_policy(self, source_kind: str, resolved: Path) -> dict[str, Any]:
        """Capture the local profile's real access facts, honestly.

        For local paths the permitted-root policy is enforced against
        the *resolved* path (symlinks cannot re-point it afterwards).
        Marker-owned kinds (upload/url) record that ownership instead of
        fabricating root/ACL knowledge.
        """
        if source_kind != SOURCE_KIND_LOCAL_PATH:
            return {
                "basis": "marker_owned",
                "declared_acl_knowledge": "none",
            }
        from app.errors import InputNotAllowedError

        roots = workspace_roots()
        matched = ""
        for root in roots:
            if _is_under(resolved, root):
                matched = os.path.normcase(str(root))
                break
        unrestricted = unrestricted_local_paths_enabled()
        facts: dict[str, Any] = {
            "basis": "workspace_roots",
            "permitted_root": matched,
            "unrestricted": unrestricted,
            "roots_configured": len(roots),
            "declared_acl_knowledge": "none",
        }
        if not roots and not unrestricted:
            raise InputNotAllowedError(
                "Local input acquisition requires MARKER_WORKSPACE_ROOTS or "
                "MARKER_ALLOW_UNRESTRICTED_LOCAL_PATHS=true.",
                details={"path": str(resolved)},
            )
        if roots and not matched and not unrestricted:
            raise InputNotAllowedError(
                f"Local input path is outside MARKER_WORKSPACE_ROOTS: {resolved}",
                details={"path": str(resolved)},
            )
        return facts

    @staticmethod
    def _derive_source_key(source_kind: str, resolved: Path, job_id: str) -> str:
        if source_kind == SOURCE_KIND_UPLOAD:
            return f"upload:{job_id or 'unknown'}"
        if source_kind == SOURCE_KIND_LOCAL_PATH:
            return f"local:{os.path.normcase(str(resolved))}"
        # URL origins carry no stable provider identity in this slice;
        # the caller must supply the URL as the logical key.
        raise SourceStoreError(
            f"source_kind {source_kind!r} requires source_key_override"
        )

    async def _current_or_next_epoch(self) -> tuple[AuthorizationEpochRecord | None, int]:
        """Latest committed epoch; a new record when the domain changed.

        Returns ``(new_epoch_record_or_None, effective_epoch_number)``.
        """
        facts = self._domain_facts()
        fingerprint = payload_byte_hash(canonical_json_bytes(to_json_ready(facts)))
        latest = await self._latest_epoch_record()
        if latest is not None:
            if latest["fingerprint"] == fingerprint:
                return None, latest["epoch_number"]
            number = latest["epoch_number"] + 1
        else:
            number = 1
        record = AuthorizationEpochRecord(
            record_id=f"epoch.{number}.{fingerprint.removeprefix('sha256:')[:24]}",
            epoch_number=number,
            fingerprint=fingerprint,
            domain_facts=facts,
        )
        return record, number

    def _domain_facts(self) -> dict[str, Any]:
        roots = sorted(os.path.normcase(str(root)) for root in workspace_roots())
        return {
            "profile": _POLICY_PROFILE,
            "roots": roots,
            "unrestricted": unrestricted_local_paths_enabled(),
        }

    async def _latest_epoch_record(self) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KernelRecord.id, KernelRecord.payload_json)
                    .where(
                        KernelRecord.workspace_id == self.workspace_id,
                        KernelRecord.record_class == "authorization_epoch",
                    )
                    .order_by(KernelRecord.kernel_commit_id.desc(), KernelRecord.id.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return {
                "epoch_number": int(payload["epoch_number"]),
                "fingerprint": str(payload["fingerprint"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # convergent commit
    # ------------------------------------------------------------------

    def _identity_hash(self, record: RecordInput) -> str:
        return record_identity_hash(
            record_type=record.record_type,
            schema_version=record.schema_version,
            payload=to_json_ready(record.identity_payload()),
        )

    async def _resolve_existing_ids(
        self, records: list[RecordInput]
    ) -> dict[str, str]:
        """Map candidate record_id -> committed record_id for duplicates."""
        hashes = [self._identity_hash(record) for record in records]
        found: dict[str, str] = {}
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(KernelRecord.identity_hash, KernelRecord.id).where(
                        KernelRecord.workspace_id == self.workspace_id,
                        KernelRecord.identity_hash.in_(hashes),
                    )
                )
            ).all()
        by_hash = {row.identity_hash: row.id for row in rows}
        for record, digest in zip(records, hashes):
            committed = by_hash.get(digest)
            if committed is not None:
                found[record.record_id] = committed
        return found

    async def _commit_converging(
        self,
        *,
        records: list[RecordInput],
        edges: list[KernelEdge],
        producer: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit only genuinely-new records; converge onto committed ones.

        Returns ``{"ids": {candidate_record_id: committed_record_id},
        "commit_id": int}``. One bounded retry handles the race where a
        concurrent acquisition committed an identical record between the
        resolution query and the insert.
        """
        existing = await self._resolve_existing_ids(records)
        commit_id = 0
        for attempt in range(2):
            new_records = [r for r in records if r.record_id not in existing]
            resolved_edges = [
                KernelEdge(
                    edge_kind=edge.edge_kind,
                    source_ref=existing.get(edge.source_ref, edge.source_ref),
                    target_ref=existing.get(edge.target_ref, edge.target_ref),
                )
                for edge in edges
                # An edge whose source record is already committed was
                # committed with it; re-adding would duplicate lineage.
                if edge.source_ref not in existing
            ]
            try:
                receipt = await self._commit_service.commit(
                    KernelCommitBatch(
                        workspace_id=self.workspace_id,
                        records=tuple(new_records),
                        edges=tuple(resolved_edges),
                        producer=producer,
                    )
                )
            except DuplicateRecordIdentityError:
                if attempt == 0:
                    existing = await self._resolve_existing_ids(records)
                    continue
                raise
            commit_id = receipt.kernel_commit_id
            break
        ids = {r.record_id: existing.get(r.record_id, r.record_id) for r in records}
        return {"ids": ids, "commit_id": commit_id}

    async def _record_rejection(
        self,
        *,
        source_kind: str,
        source_key: str,
        suffix: str,
        media_type: str,
        reason: str,
        resolved: str,
        job_id: str,
    ) -> None:
        """Commit a rejected_incoherent observation (audit-only truth)."""
        identity = SourceIdentityRecord(
            record_id="source." + payload_byte_hash(source_key.encode("utf-8"))[:24],
            source_kind=source_kind,
            source_key=source_key,
        )
        observation = SourceObservationRecord(
            record_id="obs." + payload_byte_hash(f"{source_key}|{_now_iso()}".encode("utf-8"))[:24],
            observer=_OBSERVER,
            source_ref=identity.record_id,
            outcome="rejected_incoherent",
            evidence={
                "observed_at": _now_iso(),
                "observed_path": resolved,
                "reason": reason[:500],
                "job_id": job_id,
                "declared_suffix": suffix,
                "declared_media_type": media_type,
            },
        )
        try:
            await self._commit_converging(
                records=[identity, observation],
                edges=[
                    KernelEdge(
                        edge_kind="observes",
                        source_ref=observation.record_id,
                        target_ref=identity.record_id,
                    )
                ],
                producer={
                    "operation": "source.acquire.rejected",
                    "source_kind": source_kind,
                    "job_id": job_id,
                },
            )
        except KernelError:  # noqa: BLE001 - audit must not mask the rejection
            pass


# ----------------------------------------------------------------------
# process-wide default (production ingress + runtime share one service
# over one database and one artifact store; tests swap it explicitly)
# ----------------------------------------------------------------------

_default_service: SourceAcquisitionService | None = None


def default_source_acquisition_service() -> SourceAcquisitionService:
    """Process-wide service bound to the production engine.

    Commits fail closed until the kernel schema is verified ready; the
    artifact store is the one selected by ``MARKER_SOURCE_STORE_PROFILE``
    (``local`` roots at ``MARKER_SOURCE_STORE_ROOT``, default
    ``<data>/source_store``; ``s3`` is the PR83B3 industrial profile).
    Shares the kernel runtime's workspace so revisions acquired at
    ingress resolve inside authorization.
    """
    global _default_service
    if _default_service is None:
        from app.core.config import (
            KERNEL_RUNTIME_WORKSPACE,
            SOURCE_CACHE_ROOT,
        )
        from app.database import async_session_factory
        from app.kernel.commit import default_commit_service
        from app.kernel.source_store import build_source_store

        _default_service = SourceAcquisitionService(
            async_session_factory,
            default_commit_service(),
            build_source_store(),
            workspace_id=KERNEL_RUNTIME_WORKSPACE,
            cache_root=SOURCE_CACHE_ROOT,
        )
    return _default_service


def set_default_source_acquisition_service(
    service: SourceAcquisitionService | None,
) -> None:
    """Test seam: bind/restore the process-wide service."""
    global _default_service
    _default_service = service
