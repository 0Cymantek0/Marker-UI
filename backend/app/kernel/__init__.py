"""Local Truth Kernel — commit spine (V3.2 PR63A) with durable payload
staging, availability truth, the transactional outbox (PR64),
snapshot-pinned immutable materialized generations (PR65A), and the
retention/GC contract (PR65B).

Public surface:

* :mod:`app.kernel.records` — typed record inputs (NativeObject,
  NativeFact, ClaimAssertion, ClaimAssessment, Observation, Decision)
  and dependency edges;
* :mod:`app.kernel.commit` — :class:`KernelCommitService`, the single
  transactional commit authority, plus :func:`default_commit_service`;
* :mod:`app.kernel.payloads` — :class:`LocalPayloadStore`, the
  content-addressed immutable blob store behind payload references;
* :mod:`app.kernel.outbox` — at-least-once durable successor-work
  surface (claim/ack/release/restart reset);
* :mod:`app.kernel.reconcile` — payload availability classification and
  conservative repair/restart reconciliation;
* :mod:`app.kernel.replay` — head reads, causal-range replay, and
  database-chain integrity verification (correctness tooling, NOT the
  materialized serving path);
* :mod:`app.kernel.snapshots` (PR65A) — :func:`resolve_snapshot`, the
  committed-cut snapshot contract with honest completeness classes;
* :mod:`app.kernel.generations` (PR65A) — :class:`GenerationService`
  (build → validate → activate) and :class:`GenerationReader`, the
  bounded generation-pinned read path (optionally under a durable GC
  pin since PR65B);
* :mod:`app.kernel.retention` (PR65B) — declared retention holds and
  bounded reader pins: the attachment contract every current or future
  retention producer uses;
* :mod:`app.kernel.gc` (PR65B) — conservative two-phase collection
  (plan → recheck → tombstone → sweep) and restart reconciliation;
* :mod:`app.kernel.errors` — boundary error contract;
* :mod:`app.kernel.fencing` (PR66) — fenced work ownership and the
  exactly-once accepted-publication boundary on top of the outbox
  (acquire/release/takeover leases, monotonic fencing tokens,
  acceptance linearization, fenced acknowledgement, and a deliberately
  minimal claim-next dispatch seam);
* :mod:`app.kernel.models` — ORM tables owned by Alembic revisions
  ``20260815_0004`` (commit spine), ``20260815_0005`` (payload
  registry + outbox), ``20260815_0006`` (materialized generations),
  ``20260815_0007`` (retention roots, reader pins, GC tombstones), and
  ``20260816_0008`` (work leases + accepted publications).

What this slice guarantees and deliberately does not guarantee is
documented in ``docs/reference/truth-kernel.md``.
"""

from __future__ import annotations

from app.kernel.commit import (
    KernelCommitBatch,
    KernelCommitReceipt,
    KernelCommitService,
    default_commit_service,
)
from app.kernel.errors import KernelError
from app.kernel.errors import VerificationRiskError, VerificationRiskGateError
from app.kernel.fencing import (
    AcceptOutcome,
    ClaimedWork,
    Publication,
    WorkLease,
    accept,
    acquire,
    claim_next,
    complete_work,
    get_lease,
    get_publication,
    release,
)
from app.kernel.gc import (
    CollectionPlan,
    CollectionReport,
    collect,
    plan_collection,
    reconcile_retirements,
)
from app.kernel.generations import (
    GenerationReader,
    GenerationRef,
    GenerationService,
    default_generation_service,
    open_current_generation,
    open_pinned_generation,
    resolve_current_generation,
)
from app.kernel.publications import (
    PublicationService,
    PublicationSetRef,
    resolve_published_set,
)
from app.kernel.outbox import OutboxIntent, OutboxView
from app.kernel.payloads import LocalPayloadStore, StagedBlob
from app.kernel.proofs import (
    ClaimRequirement,
    ProofSupportRecord,
    check_batch_proof_integrity,
    evaluate_claim_requirements,
)
from app.kernel.verification_risk import check_batch_verification_risk
from app.kernel.reconcile import (
    PayloadAvailabilityResult,
    ReconcileReport,
    reconcile,
    reconcile_after_restart,
    verify_payload_availability,
)
from app.kernel.replay import (
    ReplayResult,
    VerificationResult,
    read_head,
    replay,
    verify_history,
)
from app.kernel.retention import (
    ReaderPinView,
    RetentionHoldView,
    acquire_reader_pin,
    active_reader_pins,
    declare_hold,
    release_hold,
    release_reader_pin,
    renew_reader_pin,
)
from app.kernel.snapshots import KernelSnapshot, resolve_snapshot

__all__ = [
    "AcceptOutcome",
    "ClaimRequirement",
    "ClaimedWork",
    "CollectionPlan",
    "CollectionReport",
    "GenerationReader",
    "GenerationRef",
    "GenerationService",
    "KernelCommitBatch",
    "KernelCommitReceipt",
    "KernelCommitService",
    "KernelError",
    "KernelSnapshot",
    "LocalPayloadStore",
    "OutboxIntent",
    "OutboxView",
    "PayloadAvailabilityResult",
    "ProofSupportRecord",
    "Publication",
    "PublicationService",
    "PublicationSetRef",
    "ReaderPinView",
    "resolve_published_set",
    "ReconcileReport",
    "ReplayResult",
    "RetentionHoldView",
    "StagedBlob",
    "VerificationResult",
    "VerificationRiskGateError",
    "VerificationRiskError",
    "WorkLease",
    "accept",
    "acquire",
    "acquire_reader_pin",
    "active_reader_pins",
    "check_batch_proof_integrity",
    "check_batch_verification_risk",
    "claim_next",
    "collect",
    "complete_work",
    "default_commit_service",
    "default_generation_service",
    "declare_hold",
    "evaluate_claim_requirements",
    "get_lease",
    "get_publication",
    "open_current_generation",
    "open_pinned_generation",
    "plan_collection",
    "read_head",
    "reconcile",
    "reconcile_after_restart",
    "reconcile_retirements",
    "release",
    "release_hold",
    "release_reader_pin",
    "renew_reader_pin",
    "replay",
    "resolve_current_generation",
    "resolve_snapshot",
    "verify_history",
    "verify_payload_availability",
]
