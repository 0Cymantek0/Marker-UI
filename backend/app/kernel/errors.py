"""Truth Kernel error types (V3.2 PR63A).

Errors are part of the kernel's observable contract: every rejection at
the kernel boundary carries the violated invariant so callers and tests
can distinguish validation failures, contention, and integrity faults.
"""

from __future__ import annotations


class KernelError(Exception):
    """Base error for Truth Kernel commit/replay failures."""


class InvalidWorkspaceIdError(KernelError):
    """Workspace id does not match the kernel workspace id grammar."""


class EmptyBatchError(KernelError):
    """Batch contained neither records nor edges."""


class BatchTooLargeError(KernelError):
    """Batch exceeded the configured record bound."""


class DuplicateRecordIdError(KernelError):
    """Two records in one batch declared the same record id."""


class DuplicateRecordIdentityError(KernelError):
    """A record semantic identity already exists in this workspace."""


class InvalidRecordPayloadError(KernelError):
    """Record payload failed canonical value validation at the boundary."""


class UnknownRecordReferenceError(KernelError):
    """An edge referenced a record id that is not visible to the commit."""


class CrossWorkspaceReferenceError(KernelError):
    """An edge crossed workspace boundaries."""


class HeadMovedError(KernelError):
    """The workspace head advanced concurrently; the batch must be retried."""


class KernelBusyError(KernelError):
    """SQLite writer contention persisted beyond the retry budget."""


class InjectedFaultError(KernelError):
    """Deterministic test fault injected at a commit-protocol phase."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(f"injected fault at kernel commit phase {phase!r}")


class PayloadStageError(KernelError):
    """Durable payload staging failed; no reference may treat it as available."""


class InvalidOutboxIntentError(KernelError):
    """Outbox intent failed validation at the kernel boundary."""


# --- PR65A: snapshot + materialized generation ---------------------------


class InvalidSnapshotCutError(KernelError):
    """The requested kernel cut does not exist (negative, future, or unknown)."""


class SnapshotIntegrityError(KernelError):
    """Committed metadata at the cut is incoherent; the cut is not resolvable."""


class SnapshotRequirementError(KernelError):
    """The requested payload completeness cannot be honestly verified
    (e.g. an inspectable/replayable requirement without a payload store)."""


class UnknownGenerationError(KernelError):
    """No generation exists with the requested identity."""


class GenerationStateError(KernelError):
    """A generation lifecycle transition was requested from a state that
    does not permit it (e.g. activating a generation that is not validated)."""


class GenerationIntegrityError(KernelError):
    """Materialized generation content failed integrity verification;
    it may not be treated as valid current state."""


# --- PR65B: retention roots, reader pins, GC ------------------------------


class RetentionContractError(KernelError):
    """A retention hold or reader pin violated the declaration contract
    (unknown kind, missing target, invalid lease, unknown id)."""


class UnknownRetentionRootError(KernelError):
    """No retention root exists with the requested identity."""


class UnknownReaderPinError(KernelError):
    """No active reader pin exists with the requested identity
    (never acquired, already released, or the lease expired)."""


# --- PR66: fenced work ownership + accepted publication ------------------


class UnknownWorkError(KernelError):
    """No outbox work item exists with the requested identity."""


class UnknownWorkLeaseError(KernelError):
    """No durable ownership exists for the requested work item
    (work was never acquired through the fencing boundary)."""


class InvalidOwnerIdError(KernelError):
    """Worker owner id does not match the kernel owner id grammar."""


class InvalidWorkLeaseError(KernelError):
    """A lease parameter violated the fencing contract (e.g. a
    non-positive lease duration)."""


class InvalidWorkResultError(KernelError):
    """Work result failed canonical value validation at the boundary."""


class StaleFenceError(KernelError):
    """The submitted fencing token is no longer the current authority
    for the work item: a successor moved ownership forward, the owner
    vacated, or the work already reached an accepted publication under
    another generation. The attempt must not create accepted state."""

    def __init__(self, *, submitted_token: int, current_token: int) -> None:
        self.submitted_token = submitted_token
        self.current_token = current_token
        super().__init__(
            f"stale fence: submitted token {submitted_token} is not the "
            f"current fencing token {current_token}"
        )


class PublicationConflictError(KernelError):
    """A different result is already the accepted publication for this
    work identity; the conflicting submission was rejected without
    changing accepted state."""

    def __init__(self, *, existing_result_hash: str, submitted_result_hash: str) -> None:
        self.existing_result_hash = existing_result_hash
        self.submitted_result_hash = submitted_result_hash
        super().__init__(
            "publication conflict: accepted result "
            f"{existing_result_hash} differs from submitted "
            f"{submitted_result_hash}"
        )


# --- PR67A: fair scheduling, challenge liveness, semantic events ----------


class InvalidGroupPolicyError(KernelError):
    """A scheduling group policy value violated the fair-share contract
    (non-positive weight or fan-out window, non-positive age boost)."""


class InvalidChallengeError(KernelError):
    """Lease renewal did not present the current challenge evidence: the
    nonce was never issued for this lease, or it was superseded by a
    later renewal that rotated it. A renewal path that cannot show the
    live control loop's current nonce is rejected without touching the
    fence."""


class ProgressNotAdvancingError(KernelError):
    """Liveness evidence reported a progress counter that does not
    strictly advance the durable high-water mark (replay or stale
    snapshot, not a responsive control loop)."""


class RequestNotActiveError(KernelError):
    """The active request/stage the renewal claims to serve is not known
    active for the lease (never registered, expired, or unbound)."""


class TopologyMismatchError(KernelError):
    """Renewal evidence carries a topology generation that disagrees
    with the generation the fence was issued under."""

    def __init__(self, *, submitted_generation: int | None, current_generation: int | None) -> None:
        self.submitted_generation = submitted_generation
        self.current_generation = current_generation
        super().__init__(
            "topology generation mismatch: submitted "
            f"{submitted_generation} vs fenced {current_generation}"
        )


class WorkCancelledError(KernelError):
    """Cancellation was durably observed for this work item; liveness
    evidence can no longer extend the lease."""


class InvalidEventError(KernelError):
    """A semantic event or progress update failed validation at the
    kernel boundary (grammar, payload, or stream scope)."""
