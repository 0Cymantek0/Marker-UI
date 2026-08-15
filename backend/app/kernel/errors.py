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
