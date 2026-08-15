"""Truth Kernel persistence models (V3.2 PR63A commit spine + PR64
payload durability and outbox).

Six tables establish the durable commit authority:

* ``kernel_commit_heads`` — one row per workspace/shard holding the current
  committed head. The row is the serialization point for the commit
  protocol: every commit transaction writes this row first to take the
  SQLite writer lock, and advances it last under a conditional update.
* ``kernel_commit_manifests`` — one immutable manifest per accepted commit,
  identified by ``(workspace_id, kernel_commit_id)``. Detects missing or
  mismatched batch contents via deterministic roots and a manifest
  identity hash.
* ``kernel_records`` — committed logical record metadata. One row per
  logical record; semantic identity hash and payload byte hash are stored
  separately. Append-only history: rows are never updated or deleted.
* ``kernel_record_edges`` — dependency/reference edges between records,
  either within one commit or pointing at earlier commits.
* ``kernel_payload_objects`` (PR64) — registry of durably published
  content-addressed payload objects. One row per blob key; inserted in
  the same transaction as the records referencing it, so a committed
  registry row always implies the object was published and verified
  before the database accepted the reference.
* ``kernel_outbox`` (PR64) — durable successor-work intent enqueued in
  the same transaction as its authorizing commit. Delivery is at-least-
  once; the dedupe key makes consumers idempotent.

Wall-clock timestamps on these tables are audit metadata only. Causal
order is ``kernel_commit_id``; nothing in this module may use timestamps
for ordering, membership, or identity.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Envelope semantics version for the commit spine as a whole. PR64 adds
# the payload registry and outbox tables: 1.1.0.
KERNEL_SCHEMA_VERSION = "1.1.0"

# Framing record_type used for commit manifest identity hashing.
MANIFEST_RECORD_TYPE = "marker.kernel.commit_manifest.v1"
MANIFEST_SCHEMA_VERSION = "1.0.0"

MAX_WORKSPACE_ID_LENGTH = 128
MAX_HASH_LENGTH = 80
MAX_RECORD_TYPE_LENGTH = 100
MAX_SCHEMA_VERSION_LENGTH = 32
MAX_RECORD_CLASS_LENGTH = 50
MAX_EDGE_KIND_LENGTH = 64
MAX_WORK_KIND_LENGTH = 64
MAX_OUTBOX_STATE_LENGTH = 16
MAX_STORE_PROFILE_LENGTH = 64
MAX_STORAGE_LOCATOR_LENGTH = 256


class KernelCommitHead(Base):
    """Current committed head for one workspace/shard chain.

    ``head_kernel_commit_id == 0`` is the initial empty state. The row is
    created lazily by the first commit transaction (insert-or-ignore) and
    is advanced only inside that commit's transaction.
    """

    __tablename__ = "kernel_commit_heads"

    workspace_id: Mapped[str] = mapped_column(String(MAX_WORKSPACE_ID_LENGTH), primary_key=True)
    head_kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )


class KernelCommitManifest(Base):
    """Immutable manifest describing one accepted kernel commit."""

    __tablename__ = "kernel_commit_manifests"

    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), primary_key=True
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    record_class_counts_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    record_identity_root: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    edge_identity_root: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    manifest_identity_hash: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    kernel_schema_version: Mapped[str] = mapped_column(
        String(MAX_SCHEMA_VERSION_LENGTH), nullable=False
    )
    canonicalization_profile: Mapped[str] = mapped_column(
        String(MAX_SCHEMA_VERSION_LENGTH), nullable=False
    )
    producer_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<KernelCommitManifest(workspace={self.workspace_id!r}, "
            f"commit={self.kernel_commit_id}, parent={self.parent_kernel_commit_id})>"
        )


class KernelRecord(Base):
    """One committed logical record (typed envelope, append-only).

    ``identity_hash`` is the semantic record identity computed with the
    PR61 canonical utilities over ``record_type``/``schema_version`` and
    the canonical payload stored in ``payload_json``. It is unique per
    workspace: re-committing a semantically identical record is rejected;
    supersession requires a new record. ``payload_byte_hash`` is the exact
    byte hash of referenced payload material and is deliberately separate
    from identity — identical bytes may back multiple distinct records.
    """

    __tablename__ = "kernel_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    record_class: Mapped[str] = mapped_column(
        String(MAX_RECORD_CLASS_LENGTH), index=True, nullable=False
    )
    record_type: Mapped[str] = mapped_column(String(MAX_RECORD_TYPE_LENGTH), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(MAX_SCHEMA_VERSION_LENGTH), nullable=False
    )
    identity_hash: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), index=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_byte_hash: Mapped[str | None] = mapped_column(String(MAX_HASH_LENGTH), nullable=True)
    payload_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<KernelRecord(id={self.id!r}, class={self.record_class!r}, "
            f"commit={self.kernel_commit_id})>"
        )


class KernelRecordEdge(Base):
    """Dependency/reference edge between committed records.

    Both endpoints must belong to the same workspace as the committing
    batch; the target must be visible at the commit (same commit or an
    earlier one). Edges are append-only history; ondelete RESTRICT keeps
    committed records from being silently removed underneath them.
    """

    __tablename__ = "kernel_record_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    edge_kind: Mapped[str] = mapped_column(String(MAX_EDGE_KIND_LENGTH), nullable=False)
    source_record_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("kernel_records.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    target_record_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("kernel_records.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<KernelRecordEdge({self.edge_kind!r}, {self.source_record_id!r} -> "
            f"{self.target_record_id!r}, commit={self.kernel_commit_id})>"
        )


class KernelPayloadObject(Base):
    """Registry row for one durably published content-addressed object.

    ``blob_key`` is the exact-byte hash (``sha256:<hex>``) shared with
    ``kernel_records.payload_byte_hash``. The row is inserted in the
    same transaction as the records that reference it: a visible row
    therefore guarantees the object was staged, verified, and only then
    accepted by the database. Rows are never updated; an unavailable or
    corrupt object is an availability fact reported by reconciliation,
    not something the registry may hide.
    """

    __tablename__ = "kernel_payload_objects"

    blob_key: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), primary_key=True)
    payload_length: Mapped[int] = mapped_column(Integer, nullable=False)
    store_profile: Mapped[str] = mapped_column(
        String(MAX_STORE_PROFILE_LENGTH), nullable=False
    )
    #: store-root-relative POSIX locator; hex-derived, validated on use.
    storage_locator: Mapped[str] = mapped_column(
        String(MAX_STORAGE_LOCATOR_LENGTH), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<KernelPayloadObject(blob_key={self.blob_key!r})>"


class KernelOutbox(Base):
    """Durable successor-work intent for one accepted commit.

    Rows are created only inside the commit transaction that authorizes
    them; a rolled-back commit leaves no visible work. Delivery is
    at-least-once (PR64): ``state`` moves pending -> in_flight -> done,
    and interrupted in-flight work is returned to pending on
    reconciliation. ``dedupe_key`` uniquely identifies one intent so
    commit retries cannot duplicate work; consumers must still be
    idempotent across redelivery.
    """

    __tablename__ = "kernel_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    work_kind: Mapped[str] = mapped_column(String(MAX_WORK_KIND_LENGTH), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(
        String(MAX_HASH_LENGTH), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(MAX_OUTBOX_STATE_LENGTH), index=True, nullable=False, default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KernelOutbox(id={self.id}, kind={self.work_kind!r}, "
            f"commit={self.kernel_commit_id}, state={self.state!r})>"
        )
