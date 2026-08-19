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

import importlib
from typing import Any

# Exports resolve lazily (PEP 562). Importing the package root must stay
# dependency-light: the canonical conformance suite imports pure kernel
# modules (proofs, records, verification_risk) in a stdlib-only
# environment, and eager submodule imports here would drag the ORM and
# the database stack into that import graph.

_EXPORT_MODULES: dict[str, str] = {
    "AcceptOutcome": "app.kernel.fencing",
    "ClaimRequirement": "app.kernel.proofs",
    "ClaimedWork": "app.kernel.fencing",
    "CollectionPlan": "app.kernel.gc",
    "CollectionReport": "app.kernel.gc",
    "GenerationReader": "app.kernel.generations",
    "GenerationRef": "app.kernel.generations",
    "GenerationService": "app.kernel.generations",
    "KernelCommitBatch": "app.kernel.commit",
    "KernelCommitReceipt": "app.kernel.commit",
    "KernelCommitService": "app.kernel.commit",
    "KernelCursor": "app.kernel.models",
    "KernelError": "app.kernel.errors",
    "KernelQueryCursor": "app.kernel.models",
    "KernelSnapshot": "app.kernel.snapshots",
    "LocalPayloadStore": "app.kernel.payloads",
    "OutboxIntent": "app.kernel.outbox",
    "OutboxView": "app.kernel.outbox",
    "PayloadAvailabilityResult": "app.kernel.reconcile",
    "ProofSupportRecord": "app.kernel.proofs",
    "Publication": "app.kernel.fencing",
    "PublicationService": "app.kernel.publications",
    "PublicationSetRef": "app.kernel.publications",
    "ReaderPinView": "app.kernel.retention",
    "ReconcileReport": "app.kernel.reconcile",
    "ReplayResult": "app.kernel.replay",
    "RetentionHoldView": "app.kernel.retention",
    "StagedBlob": "app.kernel.payloads",
    "VerificationResult": "app.kernel.replay",
    "VerificationRiskError": "app.kernel.errors",
    "VerificationRiskGateError": "app.kernel.errors",
    "WorkLease": "app.kernel.fencing",
    "accept": "app.kernel.fencing",
    "acquire": "app.kernel.fencing",
    "acquire_reader_pin": "app.kernel.retention",
    "active_reader_pins": "app.kernel.retention",
    "check_batch_proof_integrity": "app.kernel.proofs",
    "check_batch_verification_risk": "app.kernel.verification_risk",
    "claim_next": "app.kernel.fencing",
    "collect": "app.kernel.gc",
    "complete_work": "app.kernel.fencing",
    "declare_hold": "app.kernel.retention",
    "default_commit_service": "app.kernel.commit",
    "default_generation_service": "app.kernel.generations",
    "evaluate_claim_requirements": "app.kernel.proofs",
    "get_lease": "app.kernel.fencing",
    "get_publication": "app.kernel.fencing",
    "open_current_generation": "app.kernel.generations",
    "open_pinned_generation": "app.kernel.generations",
    "plan_collection": "app.kernel.gc",
    "read_head": "app.kernel.replay",
    "reconcile": "app.kernel.reconcile",
    "reconcile_after_restart": "app.kernel.reconcile",
    "reconcile_retirements": "app.kernel.gc",
    "release": "app.kernel.fencing",
    "release_hold": "app.kernel.retention",
    "release_reader_pin": "app.kernel.retention",
    "renew_reader_pin": "app.kernel.retention",
    "replay": "app.kernel.replay",
    "resolve_current_generation": "app.kernel.generations",
    "resolve_published_set": "app.kernel.publications",
    "resolve_snapshot": "app.kernel.snapshots",
    "verify_history": "app.kernel.replay",
    "verify_payload_availability": "app.kernel.reconcile",
}


def __getattr__(name: str) -> Any:
    module_path = _EXPORT_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORT_MODULES))


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
    "KernelCursor",
    "KernelError",
    "KernelSnapshot",
    "KernelQueryCursor",
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
