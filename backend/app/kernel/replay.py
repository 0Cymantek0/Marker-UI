"""Truth Kernel replay and integrity inspection (V3.2 PR63A).

Read-only surface over committed kernel history:

* read the current workspace head;
* enumerate manifests in ``kernel_commit_id`` order (never timestamp
  order);
* enumerate records/edges of a commit or committed range;
* replay a metadata-only range into a deterministic logical view with a
  replay digest (two replays of the same range must be byte-identical);
* verify committed history: recompute record identities, manifest roots
  and manifest identities, check the parent chain, contiguity, counts,
  and edge visibility, and report violations by workspace/commit with
  the violated expectation named.

This is a correctness/replay tool for the authoritative log-like
metadata — it is NOT the PR65 materialized Twin read path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import validate_workspace_id
from app.kernel.manifest import (
    compute_edge_root,
    compute_manifest_identity_hash,
    compute_record_root,
    edge_root_entry,
    manifest_identity_payload,
    record_root_entry,
)
from app.kernel.models import (
    KernelCommitHead,
    KernelCommitManifest,
    KernelRecord,
    KernelRecordEdge,
)
from app.utils.canonical import (
    canonical_json_str,
    payload_byte_hash,
    record_identity_hash,
    to_json_ready,
)

__all__ = [
    "CommitView",
    "EdgeView",
    "ManifestView",
    "RecordView",
    "ReplayResult",
    "VerificationResult",
    "list_manifests",
    "read_head",
    "replay",
    "verify_history",
]


@dataclass(frozen=True)
class ManifestView:
    workspace_id: str
    kernel_commit_id: int
    parent_kernel_commit_id: int
    record_count: int
    edge_count: int
    record_class_counts: dict[str, int]
    record_identity_root: str
    edge_identity_root: str
    manifest_identity_hash: str
    kernel_schema_version: str
    canonicalization_profile: str
    producer: dict
    created_at: str | None


@dataclass(frozen=True)
class RecordView:
    record_id: str
    workspace_id: str
    kernel_commit_id: int
    record_class: str
    record_type: str
    schema_version: str
    identity_hash: str
    payload: dict
    payload_json: str
    payload_byte_hash: str | None
    payload_length: int | None


@dataclass(frozen=True)
class EdgeView:
    edge_id: str
    workspace_id: str
    kernel_commit_id: int
    edge_kind: str
    source_record_id: str
    target_record_id: str


@dataclass(frozen=True)
class CommitView:
    manifest: ManifestView
    records: tuple[RecordView, ...]
    edges: tuple[EdgeView, ...]


@dataclass(frozen=True)
class ReplayResult:
    workspace_id: str
    from_commit: int
    to_commit: int
    commits: tuple[CommitView, ...]
    replay_digest: str


@dataclass(frozen=True)
class VerificationResult:
    workspace_id: str
    ok: bool
    problems: tuple[str, ...]
    checked_commits: int
    checked_records: int
    checked_edges: int
    head_kernel_commit_id: int


async def read_head(session_factory: async_sessionmaker, workspace_id: str) -> int:
    """Current committed head; ``0`` means the initial empty state."""
    validate_workspace_id(workspace_id)
    async with session_factory() as session:
        head = await session.scalar(
            select(KernelCommitHead.head_kernel_commit_id).where(
                KernelCommitHead.workspace_id == workspace_id
            )
        )
    return head or 0


async def list_manifests(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    to_commit: int | None = None,
) -> list[ManifestView]:
    """Manifests in causal order (optionally bounded at ``to_commit``)."""
    validate_workspace_id(workspace_id)
    stmt = select(KernelCommitManifest).where(
        KernelCommitManifest.workspace_id == workspace_id
    )
    if to_commit is not None:
        stmt = stmt.where(KernelCommitManifest.kernel_commit_id <= to_commit)
    stmt = stmt.order_by(KernelCommitManifest.kernel_commit_id.asc())
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [_manifest_view(row) for row in rows]


def _manifest_view(row: KernelCommitManifest) -> ManifestView:
    return ManifestView(
        workspace_id=row.workspace_id,
        kernel_commit_id=row.kernel_commit_id,
        parent_kernel_commit_id=row.parent_kernel_commit_id,
        record_count=row.record_count,
        edge_count=row.edge_count,
        record_class_counts=json.loads(row.record_class_counts_json),
        record_identity_root=row.record_identity_root,
        edge_identity_root=row.edge_identity_root,
        manifest_identity_hash=row.manifest_identity_hash,
        kernel_schema_version=row.kernel_schema_version,
        canonicalization_profile=row.canonicalization_profile,
        producer=json.loads(row.producer_json),
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _record_view(row: KernelRecord) -> RecordView:
    return RecordView(
        record_id=row.id,
        workspace_id=row.workspace_id,
        kernel_commit_id=row.kernel_commit_id,
        record_class=row.record_class,
        record_type=row.record_type,
        schema_version=row.schema_version,
        identity_hash=row.identity_hash,
        payload=json.loads(row.payload_json),
        payload_json=row.payload_json,
        payload_byte_hash=row.payload_byte_hash,
        payload_length=row.payload_length,
    )


def _edge_view(row: KernelRecordEdge) -> EdgeView:
    return EdgeView(
        edge_id=row.id,
        workspace_id=row.workspace_id,
        kernel_commit_id=row.kernel_commit_id,
        edge_kind=row.edge_kind,
        source_record_id=row.source_record_id,
        target_record_id=row.target_record_id,
    )


async def replay(
    session_factory: async_sessionmaker,
    workspace_id: str,
    *,
    to_commit: int | None = None,
) -> ReplayResult:
    """Deterministically replay committed metadata in commit order.

    Membership and order derive exclusively from ``kernel_commit_id``.
    The replay digest is the canonical byte hash of the logical view, so
    replaying the same range twice must produce identical digests.
    """
    validate_workspace_id(workspace_id)
    async with session_factory() as session:
        manifest_rows = (
            (
                await session.execute(
                    select(KernelCommitManifest)
                    .where(KernelCommitManifest.workspace_id == workspace_id)
                    .order_by(KernelCommitManifest.kernel_commit_id.asc())
                )
            )
            .scalars()
            .all()
        )
        if to_commit is not None:
            manifest_rows = [
                row for row in manifest_rows if row.kernel_commit_id <= to_commit
            ]
        record_rows = (
            (
                await session.execute(
                    select(KernelRecord)
                    .where(KernelRecord.workspace_id == workspace_id)
                    .order_by(
                        KernelRecord.kernel_commit_id.asc(),
                        KernelRecord.identity_hash.asc(),
                        KernelRecord.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        edge_rows = (
            (
                await session.execute(
                    select(KernelRecordEdge)
                    .where(KernelRecordEdge.workspace_id == workspace_id)
                    .order_by(
                        KernelRecordEdge.kernel_commit_id.asc(),
                        KernelRecordEdge.source_record_id.asc(),
                        KernelRecordEdge.target_record_id.asc(),
                        KernelRecordEdge.edge_kind.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

    records_by_commit: dict[int, list[RecordView]] = {}
    for row in record_rows:
        records_by_commit.setdefault(row.kernel_commit_id, []).append(_record_view(row))
    edges_by_commit: dict[int, list[EdgeView]] = {}
    for row in edge_rows:
        edges_by_commit.setdefault(row.kernel_commit_id, []).append(_edge_view(row))

    commits = tuple(
        CommitView(
            manifest=_manifest_view(row),
            records=tuple(records_by_commit.get(row.kernel_commit_id, [])),
            edges=tuple(edges_by_commit.get(row.kernel_commit_id, [])),
        )
        for row in manifest_rows
    )

    digest_view = [
        {
            "manifest": {
                "kernel_commit_id": view.manifest.kernel_commit_id,
                "parent_kernel_commit_id": view.manifest.parent_kernel_commit_id,
                "record_count": view.manifest.record_count,
                "edge_count": view.manifest.edge_count,
                "record_class_counts": view.manifest.record_class_counts,
                "record_identity_root": view.manifest.record_identity_root,
                "edge_identity_root": view.manifest.edge_identity_root,
                "manifest_identity_hash": view.manifest.manifest_identity_hash,
                "kernel_schema_version": view.manifest.kernel_schema_version,
                "canonicalization_profile": view.manifest.canonicalization_profile,
            },
            "records": [
                {
                    "record_id": record.record_id,
                    "record_class": record.record_class,
                    "record_type": record.record_type,
                    "schema_version": record.schema_version,
                    "identity_hash": record.identity_hash,
                    "payload": record.payload,
                    "payload_byte_hash": record.payload_byte_hash,
                    "payload_length": record.payload_length,
                }
                for record in view.records
            ],
            "edges": [
                {
                    "edge_id": edge.edge_id,
                    "edge_kind": edge.edge_kind,
                    "source": edge.source_record_id,
                    "target": edge.target_record_id,
                }
                for edge in view.edges
            ],
        }
        for view in commits
    ]
    digest = payload_byte_hash(canonical_json_str(digest_view).encode("utf-8"))

    return ReplayResult(
        workspace_id=workspace_id,
        from_commit=commits[0].manifest.kernel_commit_id if commits else 0,
        to_commit=commits[-1].manifest.kernel_commit_id if commits else 0,
        commits=commits,
        replay_digest=digest,
    )


async def verify_history(
    session_factory: async_sessionmaker, workspace_id: str
) -> VerificationResult:
    """Recompute and cross-check committed history; report named violations."""
    validate_workspace_id(workspace_id)
    problems: list[str] = []

    def problem(commit: int, message: str) -> None:
        problems.append(f"[kernel] workspace={workspace_id!r} commit={commit}: {message}")

    head = await read_head(session_factory, workspace_id)
    result = await replay(session_factory, workspace_id)

    # --- chain: contiguity, parent linkage, head agreement -------------
    expected_commit = 1
    previous_id = 0
    for view in result.commits:
        manifest = view.manifest
        cid = manifest.kernel_commit_id
        if cid != expected_commit:
            problem(cid, f"commit id {cid} breaks contiguity; expected {expected_commit}")
            expected_commit = cid
        if manifest.parent_kernel_commit_id != previous_id:
            problem(
                cid,
                f"parent {manifest.parent_kernel_commit_id} does not name the "
                f"immediately preceding committed head {previous_id}",
            )
        if manifest.workspace_id != workspace_id:
            problem(cid, f"manifest workspace {manifest.workspace_id!r} mismatch")
        previous_id = cid
        expected_commit += 1

    if previous_id != head:
        problem(head, f"head {head} does not match last committed manifest {previous_id}")

    # --- per-commit content: counts, identities, roots, manifest hash --
    checked_records = 0
    checked_edges = 0
    for view in result.commits:
        manifest = view.manifest
        cid = manifest.kernel_commit_id

        if len(view.records) != manifest.record_count:
            problem(
                cid,
                f"record count mismatch: manifest declares {manifest.record_count}, "
                f"found {len(view.records)}",
            )
        if len(view.edges) != manifest.edge_count:
            problem(
                cid,
                f"edge count mismatch: manifest declares {manifest.edge_count}, "
                f"found {len(view.edges)}",
            )

        class_counts: dict[str, int] = {}
        record_entries: list[str] = []
        for record in view.records:
            checked_records += 1
            if record.kernel_commit_id != cid:
                problem(cid, f"record {record.record_id!r} assigned to wrong commit")
            class_counts[record.record_class] = class_counts.get(record.record_class, 0) + 1
            try:
                recomputed = record_identity_hash(
                    record_type=record.record_type,
                    schema_version=record.schema_version,
                    payload=to_json_ready(record.payload),
                )
            except Exception as exc:  # canonical value rejection is a violation
                problem(
                    cid,
                    f"record {record.record_id!r} payload rejected by canonical "
                    f"value rules: {exc}",
                )
                continue
            if recomputed != record.identity_hash:
                problem(
                    cid,
                    f"record {record.record_id!r} identity hash mismatch: stored "
                    f"{record.identity_hash} != recomputed {recomputed}",
                )
            canonical_form = canonical_json_str(to_json_ready(record.payload))
            if canonical_form != record.payload_json:
                problem(
                    cid,
                    f"record {record.record_id!r} payload_json is not canonical form",
                )
            record_entries.append(
                record_root_entry(record.identity_hash, record.payload_byte_hash)
            )

        if class_counts != manifest.record_class_counts:
            problem(
                cid,
                f"record class counts mismatch: manifest declares "
                f"{manifest.record_class_counts}, found {class_counts}",
            )

        recomputed_record_root = compute_record_root(record_entries)
        if recomputed_record_root != manifest.record_identity_root:
            problem(
                cid,
                f"record identity root mismatch: manifest declares "
                f"{manifest.record_identity_root}, recomputed {recomputed_record_root}",
            )

        edge_entries = []
        for edge in view.edges:
            checked_edges += 1
            if edge.kernel_commit_id != cid:
                problem(cid, f"edge {edge.edge_id!r} assigned to wrong commit")
            edge_entries.append(
                edge_root_entry(edge.source_record_id, edge.target_record_id, edge.edge_kind)
            )
        recomputed_edge_root = compute_edge_root(edge_entries)
        if recomputed_edge_root != manifest.edge_identity_root:
            problem(
                cid,
                f"edge identity root mismatch: manifest declares "
                f"{manifest.edge_identity_root}, recomputed {recomputed_edge_root}",
            )

        manifest_payload = manifest_identity_payload(
            workspace_id=manifest.workspace_id,
            kernel_commit_id=manifest.kernel_commit_id,
            parent_kernel_commit_id=manifest.parent_kernel_commit_id,
            record_count=manifest.record_count,
            edge_count=manifest.edge_count,
            record_class_counts=manifest.record_class_counts,
            record_identity_root=manifest.record_identity_root,
            edge_identity_root=manifest.edge_identity_root,
            kernel_schema_version=manifest.kernel_schema_version,
            canonicalization_profile=manifest.canonicalization_profile,
        )
        recomputed_manifest_hash = compute_manifest_identity_hash(manifest_payload)
        if recomputed_manifest_hash != manifest.manifest_identity_hash:
            problem(
                cid,
                f"manifest identity hash mismatch: stored "
                f"{manifest.manifest_identity_hash} != recomputed "
                f"{recomputed_manifest_hash}",
            )

    # --- edge visibility: endpoints exist in-workspace, not from future -
    record_commit = {record.record_id: record.kernel_commit_id for view in result.commits
                     for record in view.records}
    for view in result.commits:
        for edge in view.edges:
            for role, ref in (
                ("source", edge.source_record_id),
                ("target", edge.target_record_id),
            ):
                if ref not in record_commit:
                    problem(
                        edge.kernel_commit_id,
                        f"edge {edge.edge_id!r} {role} {ref!r} not present in "
                        "committed history",
                    )
                elif record_commit[ref] > edge.kernel_commit_id:
                    problem(
                        edge.kernel_commit_id,
                        f"edge {edge.edge_id!r} {role} {ref!r} references a record "
                        f"from a later commit ({record_commit[ref]})",
                    )

    # --- orphans: rows whose creating commit has no manifest ------------
    manifest_ids = {view.manifest.kernel_commit_id for view in result.commits}
    record_stmt = select(KernelRecord.kernel_commit_id, KernelRecord.id).where(
        KernelRecord.workspace_id == workspace_id
    )
    edge_stmt = select(KernelRecordEdge.kernel_commit_id, KernelRecordEdge.id).where(
        KernelRecordEdge.workspace_id == workspace_id
    )
    if manifest_ids:
        record_stmt = record_stmt.where(
            KernelRecord.kernel_commit_id.notin_(manifest_ids)
        )
        edge_stmt = edge_stmt.where(
            KernelRecordEdge.kernel_commit_id.notin_(manifest_ids)
        )
    async with session_factory() as session:
        orphan_records = (await session.execute(record_stmt)).all()
        orphan_edges = (await session.execute(edge_stmt)).all()
    for commit_id, record_id in orphan_records:
        problem(commit_id, f"record {record_id!r} belongs to a commit without a manifest")
    for commit_id, edge_id in orphan_edges:
        problem(commit_id, f"edge {edge_id!r} belongs to a commit without a manifest")

    return VerificationResult(
        workspace_id=workspace_id,
        ok=not problems,
        problems=tuple(problems),
        checked_commits=len(result.commits),
        checked_records=checked_records,
        checked_edges=checked_edges,
        head_kernel_commit_id=head,
    )
