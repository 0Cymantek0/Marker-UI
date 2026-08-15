"""Local Truth Kernel — commit spine (V3.2 PR63A) with durable payload
staging, availability truth, the transactional outbox (PR64), and
snapshot-pinned immutable materialized generations (PR65A).

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
  bounded generation-pinned read path;
* :mod:`app.kernel.errors` — boundary error contract;
* :mod:`app.kernel.models` — ORM tables owned by Alembic revisions
  ``20260815_0004`` (commit spine), ``20260815_0005`` (payload
  registry + outbox), and ``20260815_0006`` (materialized generations).

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
from app.kernel.generations import (
    GenerationReader,
    GenerationRef,
    GenerationService,
    default_generation_service,
    open_current_generation,
    resolve_current_generation,
)
from app.kernel.outbox import OutboxIntent, OutboxView
from app.kernel.payloads import LocalPayloadStore, StagedBlob
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
from app.kernel.snapshots import KernelSnapshot, resolve_snapshot

__all__ = [
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
    "ReconcileReport",
    "ReplayResult",
    "StagedBlob",
    "VerificationResult",
    "default_commit_service",
    "default_generation_service",
    "open_current_generation",
    "read_head",
    "reconcile",
    "reconcile_after_restart",
    "replay",
    "resolve_current_generation",
    "resolve_snapshot",
    "verify_history",
    "verify_payload_availability",
]
