"""Truth Kernel persistence models (V3.2 PR63A commit spine + PR64
payload durability and outbox + PR65A materialized generations + PR65B
retention roots, reader pins, and payload retirement tombstones).

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

Four PR65A tables hold the materialized read model (revision
``20260815_0006``). They are **derived, rebuildable state — never a
second truth authority**: every row is copied from the committed kernel
cut named by its generation and can be discarded and rebuilt at any time:

* ``kernel_generations`` — one immutable manifest row per materialized
  generation: the pinned snapshot identity, materializer/schema/config
  identity, lifecycle state (staged/validated/active/superseded/failed),
  and the deterministic content digest of the materialized view.
* ``kernel_generation_records`` — committed record metadata materialized
  into the generation, bounded to the snapshot cut.
* ``kernel_generation_edges`` — dependency edges materialized into the
  generation, bounded to the snapshot cut.
* ``kernel_generation_heads`` — one row per workspace naming the current
  accepted read generation. The single atomic pointer switch happens on
  this row; readers resolve it once and pin the named generation.

Three PR65B tables hold the retention contract (revision
``20260815_0007``). They describe what current policy *requires*; they
never rewrite committed truth:

* ``kernel_retention_roots`` — declared durable retention holds. Any
  future subsystem (jobs, reviews, exports, legal holds, PublicationSets)
  attaches retention by declaring a root naming a cut and a required
  payload class; the intrinsic current-generation roots are read from
  ``kernel_generation_heads`` and are not stored here.
* ``kernel_reader_pins`` — bounded wall-clock read leases over one
  generation. An unexpired pin is an active root: collection may not
  retire the pinned generation or payload bytes its class requires. A
  crashed reader's pin lapses when its lease expires — safety across
  restart comes from durable rows, never process memory.
* ``kernel_payload_retirements`` — durable GC tombstones. Presence of a
  row means the database has authorized physical retirement of that
  blob; ``state`` tracks pending/deleted/failed so crash recovery can
  converge idempotently. The ``kernel_payload_objects`` registry row is
  deliberately kept: retired bytes remain an honest availability fact,
  never a fabricated "available".

Two PR66 tables hold the fenced work authority (revision
``20260816_0008``). They answer one question durably — *which
ownership generation may turn an executed result into accepted state*:

* ``kernel_work_leases`` — one row per outbox work item holding the
  current fenced ownership: a monotonically increasing
  ``fencing_token``, the current ``owner_id``, a wall-clock
  ``lease_expires_at`` (takeover eligibility only — never proof of
  authority), and a lifecycle ``state`` (leased/released/accepted).
  Every ownership transition (acquire, takeover, vacate) advances the
  token inside one conditional transaction, so an older token is stale
  forever, even after restart.
* ``kernel_publications`` — the exactly-once accepted result for one
  work identity. The unique ``(workspace_id, work_id)`` scope is the
  database-enforced "at most one accepted publication" guarantee; the
  deterministic ``publication_id`` plus ``result_hash`` make retrying
  the same logical acceptance converge idempotently while a materially
  different result is rejected as a conflict.

Five PR67A tables hold fair scheduling, challenge liveness, and durable
semantic events (revision ``20260816_0009``). None of them is an
ownership or publication authority — that remains PR66; they add policy
metadata, liveness evidence, and the authoritative semantic log:

* ``kernel_scheduling_entries`` — per-work scheduling metadata (resource
  class, group, deadline). Derived policy data keyed to the outbox row;
  the outbox state remains the only work truth.
* ``kernel_scheduling_groups`` — per-(resource class, group) policy and
  non-authoritative service bookkeeping (weight, fan-out window, age
  boost, served count). Fairness accounting may be rebuilt; it never
  decides authority.
* ``kernel_liveness`` — per-work challenge evidence for lease renewal:
  the rotating challenge nonce, monotonic progress high-water mark,
  active request binding, topology generation, request deadline, and
  cancellation observation. Renewal without matching this evidence is
  rejected; a wedged worker stops renewing and becomes takeover-eligible
  through the PR66 lease-expiry path.
* ``kernel_events`` — append-only durable semantic events with an
  authoritative per-(workspace, stream) ``semantic_sequence`` assigned
  inside the append transaction. Replay order is the sequence, never
  wall-clock timestamps.
* ``kernel_progress`` — coalescible progress snapshots: exactly one row
  per (workspace, work), updated in place. Progress floods never
  amplify into per-tick durable rows, and losing them never loses
  semantic truth.

Wall-clock timestamps on these tables are audit metadata only. Causal
order is ``kernel_commit_id``; nothing in this module may use timestamps
for ordering, membership, or identity. Lease expiry on reader pins is
the one deliberate wall-clock grace mechanism (mirroring the outbox's
claimed-at timestamps), not a causal claim. Work-lease expiry follows
the same rule: it permits takeover, it never authorizes an older token
to accept.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
MAX_GENERATION_STATE_LENGTH = 16
MAX_COMPLETENESS_LENGTH = 16
MAX_PAYLOAD_REQUIREMENT_LENGTH = 24
MAX_ROOT_KIND_LENGTH = 50
MAX_ROOT_STATE_LENGTH = 16
MAX_RETIRE_STATE_LENGTH = 16
MAX_RETIRE_REASON_LENGTH = 64
MAX_OWNER_ID_LENGTH = 64
MAX_LEASE_STATE_LENGTH = 16
MAX_RESOURCE_CLASS_LENGTH = 32
MAX_GROUP_ID_LENGTH = 192
MAX_CHALLENGE_NONCE_LENGTH = 64
MAX_EVENT_STREAM_LENGTH = 64
MAX_EVENT_TYPE_LENGTH = 100
MAX_EVENT_DURABILITY_LENGTH = 16
MAX_ACTIVE_REQUEST_ID_LENGTH = 192


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


class KernelGeneration(Base):
    """Immutable manifest row for one materialized generation (PR65A).

    ``generation_id`` is deterministic: the same pinned snapshot plus the
    same declared materializer/schema/config identity always derive the
    same id, so a rebuild of the same declared inputs either matches the
    stored ``content_digest`` exactly (idempotent reuse) or fails closed
    as an integrity violation. Rows are never mutated except for the
    lifecycle ``state``/timestamp columns; materialized content is
    append-only and immutable once staged.
    """

    __tablename__ = "kernel_generations"

    generation_id: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    materializer_id: Mapped[str] = mapped_column(
        String(MAX_RECORD_TYPE_LENGTH), nullable=False
    )
    materializer_version: Mapped[str] = mapped_column(
        String(MAX_SCHEMA_VERSION_LENGTH), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(
        String(MAX_SCHEMA_VERSION_LENGTH), nullable=False
    )
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(
        String(MAX_GENERATION_STATE_LENGTH), index=True, nullable=False
    )
    content_digest: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    commit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False)
    record_class_counts_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    required_payload_state: Mapped[str] = mapped_column(
        String(MAX_PAYLOAD_REQUIREMENT_LENGTH), nullable=False
    )
    completeness: Mapped[str] = mapped_column(
        String(MAX_COMPLETENESS_LENGTH), nullable=False
    )
    payload_state_counts_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KernelGeneration(id={self.generation_id!r}, cut={self.kernel_commit_id}, "
            f"state={self.state!r})>"
        )


class KernelGenerationRecord(Base):
    """Committed record metadata materialized into one generation.

    Derived state: copied verbatim from ``kernel_records`` for every
    record whose commit is ``<=`` the generation's pinned cut. Never
    mutated after staging; discardable and rebuildable at any time.
    """

    __tablename__ = "kernel_generation_records"

    generation_id: Mapped[str] = mapped_column(
        String(MAX_HASH_LENGTH), primary_key=True
    )
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), nullable=False
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    record_class: Mapped[str] = mapped_column(String(MAX_RECORD_CLASS_LENGTH), nullable=False)
    record_type: Mapped[str] = mapped_column(String(MAX_RECORD_TYPE_LENGTH), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(MAX_SCHEMA_VERSION_LENGTH), nullable=False
    )
    identity_hash: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_byte_hash: Mapped[str | None] = mapped_column(String(MAX_HASH_LENGTH), nullable=True)
    payload_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KernelGenerationRecord(generation={self.generation_id!r}, "
            f"record={self.record_id!r})>"
        )


class KernelGenerationEdge(Base):
    """Dependency edge materialized into one generation (derived state)."""

    __tablename__ = "kernel_generation_edges"

    generation_id: Mapped[str] = mapped_column(
        String(MAX_HASH_LENGTH), primary_key=True
    )
    edge_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), nullable=False
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_kind: Mapped[str] = mapped_column(String(MAX_EDGE_KIND_LENGTH), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_record_id: Mapped[str] = mapped_column(String(36), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<KernelGenerationEdge(generation={self.generation_id!r}, "
            f"edge={self.edge_id!r})>"
        )


class KernelGenerationHead(Base):
    """Current accepted read generation for one workspace (PR65A).

    One row per workspace; the single atomic current-generation switch is
    the transactional update of this row. Readers resolve it once and pin
    the named generation for their whole request.
    """

    __tablename__ = "kernel_generation_heads"

    workspace_id: Mapped[str] = mapped_column(String(MAX_WORKSPACE_ID_LENGTH), primary_key=True)
    current_generation_id: Mapped[str] = mapped_column(
        String(MAX_HASH_LENGTH), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )


class KernelRetentionRoot(Base):
    """One declared durable retention hold (PR65B).

    ``root_id`` is deterministic over (workspace, kind, target, cut,
    required class, producer context), so re-declaring the same hold is
    idempotent. A root is *active* when ``state == 'active'`` and its
    optional ``expires_at`` has not passed; expiry or release only stops
    protecting data — it never deletes anything by itself. Collection
    must treat an active root's cut and required payload class as live.
    """

    __tablename__ = "kernel_retention_roots"

    root_id: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    root_kind: Mapped[str] = mapped_column(String(MAX_ROOT_KIND_LENGTH), nullable=False)
    #: generation hold: the named generation (and its cut/class) is live.
    #: snapshot hold: the row's own cut/class is live without a
    #: materialized generation. Kinds are open-ended for future PRs.
    target_generation_id: Mapped[str | None] = mapped_column(
        String(MAX_HASH_LENGTH), nullable=True
    )
    kernel_commit_id: Mapped[int] = mapped_column(Integer, nullable=False)
    required_payload_state: Mapped[str] = mapped_column(
        String(MAX_PAYLOAD_REQUIREMENT_LENGTH), nullable=False
    )
    producer_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(
        String(MAX_ROOT_STATE_LENGTH), index=True, nullable=False, default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KernelRetentionRoot(id={self.root_id!r}, kind={self.root_kind!r}, "
            f"workspace={self.workspace_id!r}, cut={self.kernel_commit_id})>"
        )


class KernelReaderPin(Base):
    """Bounded wall-clock read lease over one generation (PR65B).

    Acquired before a reader relies on a superseded generation staying
    readable; released (row deleted) when the reader finishes. A pin
    whose lease has expired is inert — crash-orphaned pins therefore
    lapse on their own and expired rows are purged by collection.
    """

    __tablename__ = "kernel_reader_pins"

    pin_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation_id: Mapped[str] = mapped_column(
        String(MAX_HASH_LENGTH), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(), index=True, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<KernelReaderPin(id={self.pin_id!r}, generation={self.generation_id!r})>"
        )


class KernelPayloadRetirement(Base):
    """Durable GC tombstone for one payload object (PR65B).

    A row exists only after collection re-validated, inside the
    authorization transaction, that no live root requires the blob.
    ``state``: ``pending`` (authorized, bytes not yet unlinked),
    ``deleted`` (bytes absent by our decision), ``failed`` (unlink
    attempted and errored — retryable, never a false success). The
    ``kernel_payload_objects`` registry row is intentionally NOT
    deleted: historical identity, length, and locator remain
    interpretable, and availability classification reports the bytes as
    ``retired`` rather than pretending they were never referenced.
    """

    __tablename__ = "kernel_payload_retirements"

    blob_key: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), primary_key=True)
    state: Mapped[str] = mapped_column(
        String(MAX_RETIRE_STATE_LENGTH), index=True, nullable=False
    )
    reason: Mapped[str] = mapped_column(
        String(MAX_RETIRE_REASON_LENGTH), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=lambda: datetime.now(timezone.utc),
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    swept_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KernelPayloadRetirement(blob_key={self.blob_key!r}, "
            f"state={self.state!r}, attempts={self.attempts})>"
        )


class KernelWorkLease(Base):
    """Current fenced ownership for one outbox work item (PR66).

    ``fencing_token`` is the durable authority generation: it starts at
    1 when the work is first acquired and is advanced by exactly one
    inside every ownership transition transaction (first acquire,
    takeover, vacate). An acceptance submitted with a token smaller
    than the stored one is stale forever — wall-clock lease expiry may
    *permit* takeover, but never revives or re-authorizes an old token.
    """

    __tablename__ = "kernel_work_leases"

    work_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    work_kind: Mapped[str] = mapped_column(String(MAX_WORK_KIND_LENGTH), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(MAX_OWNER_ID_LENGTH), nullable=False)
    state: Mapped[str] = mapped_column(
        String(MAX_LEASE_STATE_LENGTH), nullable=False, default="leased"
    )
    #: takeover eligibility only (see class docstring); never authority.
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<KernelWorkLease(work={self.work_id}, token={self.fencing_token}, "
            f"owner={self.owner_id!r}, state={self.state!r})>"
        )


class KernelPublication(Base):
    """The one accepted publication for one work identity (PR66).

    Inserted only inside the acceptance transaction that first verified
    the submitting fence is current. ``publication_id`` is the
    deterministic framed hash over (workspace, work, result hash), and
    the unique ``(workspace_id, work_id)`` scope is what makes "exactly
    one accepted publication" a database-enforced fact rather than a
    convention. Rows are immutable once accepted.
    """

    __tablename__ = "kernel_publications"

    publication_id: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), index=True, nullable=False
    )
    work_id: Mapped[int] = mapped_column(Integer, nullable=False)
    work_kind: Mapped[str] = mapped_column(String(MAX_WORK_KIND_LENGTH), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_hash: Mapped[str] = mapped_column(String(MAX_HASH_LENGTH), nullable=False)
    #: fencing token (and owner) that legitimately accepted this result.
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(MAX_OWNER_ID_LENGTH), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "work_id", name="uq_kernel_publications_scope"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<KernelPublication(work={self.work_id}, "
            f"result={self.result_hash!r}, token={self.fencing_token})>"
        )


class KernelSchedulingEntry(Base):
    """Per-work scheduling metadata (PR67A).

    Policy data keyed 1:1 to an outbox work item. The outbox row remains
    the only work truth: an entry neither claims the work is runnable nor
    records ownership — it tells the fair scheduler which resource class,
    scheduling group, and deadline pressure this work belongs to. Rows
    are created at ``register_work`` time or lazily with workspace
    defaults by the first dispatch that meets an unregistered pending
    item.
    """

    __tablename__ = "kernel_scheduling_entries"

    work_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), nullable=False
    )
    resource_class: Mapped[str] = mapped_column(
        String(MAX_RESOURCE_CLASS_LENGTH), nullable=False
    )
    group_id: Mapped[str] = mapped_column(String(MAX_GROUP_ID_LENGTH), nullable=False)
    #: deadline pressure input; expiry does not cancel or reorder truth.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        # group-window and per-class candidate scans
        Index("ix_kernel_sched_entries_class_group", "resource_class", "group_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<KernelSchedulingEntry(work={self.work_id}, "
            f"class={self.resource_class!r}, group={self.group_id!r})>"
        )


class KernelSchedulingGroup(Base):
    """Fair-share policy and service bookkeeping for one scheduling
    group inside a resource class (PR67A).

    ``served_count`` is deliberately *non-authoritative accounting*: it
    feeds the weighted-fair ordering (virtual finish ≈ served / weight)
    and may drift transiently under concurrent dispatchers without
    affecting ownership, acceptance, or acknowledgement. Weights shape
    long-run service shares; the age boost keeps an old eligible group
    from being perpetually displaced; ``max_in_flight`` bounds how many
    items of one group may be simultaneously outstanding (fan-out
    backpressure that never occupies a slot while waiting).
    """

    __tablename__ = "kernel_scheduling_groups"

    resource_class: Mapped[str] = mapped_column(
        String(MAX_RESOURCE_CLASS_LENGTH), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        String(MAX_GROUP_ID_LENGTH), primary_key=True
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    max_in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    age_boost_after_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=30.0
    )
    age_boost_factor: Mapped[float] = mapped_column(Float, nullable=False, default=4.0)
    served_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<KernelSchedulingGroup(class={self.resource_class!r}, "
            f"group={self.group_id!r}, served={self.served_count}, "
            f"weight={self.weight})>"
        )


class KernelLiveness(Base):
    """Challenge evidence backing lease renewal for one work item
    (PR67A).

    Seeded in the same transaction that records a fair claim. Renewal
    must present the *current* ``challenge_nonce`` (rotated on every
    successful renewal, handed only to the responder), a progress
    counter strictly advancing ``progress_high_water``, and the active
    request identity the control loop is serving. A detached timer that
    merely knows the owner id cannot satisfy this contract: its nonce
    goes stale at the first rotation and its progress cannot advance.
    """

    __tablename__ = "kernel_liveness"

    work_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    challenge_nonce: Mapped[str] = mapped_column(
        String(MAX_CHALLENGE_NONCE_LENGTH), nullable=False
    )
    progress_high_water: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_request_id: Mapped[str] = mapped_column(
        String(MAX_ACTIVE_REQUEST_ID_LENGTH), nullable=False, default=""
    )
    topology_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: external request the control loop is waiting on; liveness ends
    #: with it. Audit/eligibility input — never authority.
    request_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    renew_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<KernelLiveness(work={self.work_id}, "
            f"renews={self.renew_count}, cancelled={self.cancelled_at is not None})>"
        )


class KernelEvent(Base):
    """One durable semantic event in the authoritative per-scope
    sequence (PR67A).

    ``semantic_sequence`` is allocated inside the append transaction
    (writer-serialized ``MAX+1``), so within ``(workspace_id, stream)``
    the sequence cannot fork or regress, and replay never depends on
    wall-clock timestamps. ``created_at`` is audit metadata only. Rows
    are append-only; coalescible progress never lands in this table.
    """

    __tablename__ = "kernel_events"

    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), primary_key=True
    )
    stream: Mapped[str] = mapped_column(
        String(MAX_EVENT_STREAM_LENGTH), primary_key=True
    )
    semantic_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(
        String(MAX_EVENT_TYPE_LENGTH), nullable=False
    )
    durability: Mapped[str] = mapped_column(
        String(MAX_EVENT_DURABILITY_LENGTH), nullable=False, default="durable"
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<KernelEvent(ws={self.workspace_id!r}, stream={self.stream!r}, "
            f"seq={self.semantic_sequence}, type={self.event_type!r})>"
        )


class KernelProgress(Base):
    """Coalescible progress snapshot for one work item (PR67A).

    Exactly one row per (workspace, work), updated in place: a progress
    flood converges to the latest snapshot instead of one durable row
    per tick. Progress is best-effort by design — dropping or lagging it
    never loses durable semantic events, which live in ``kernel_events``.
    """

    __tablename__ = "kernel_progress"

    workspace_id: Mapped[str] = mapped_column(
        String(MAX_WORKSPACE_ID_LENGTH), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<KernelProgress(work={self.work_id}, counter={self.counter})>"
        )
