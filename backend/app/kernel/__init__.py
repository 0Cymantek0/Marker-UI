"""Local Truth Kernel — commit spine (V3.2 PR63A).

Public surface:

* :mod:`app.kernel.records` — typed record inputs (NativeObject,
  NativeFact, ClaimAssertion, ClaimAssessment, Observation, Decision)
  and dependency edges;
* :mod:`app.kernel.commit` — :class:`KernelCommitService`, the single
  transactional commit authority, plus :func:`default_commit_service`;
* :mod:`app.kernel.replay` — head reads, causal-range replay, and
  integrity verification;
* :mod:`app.kernel.errors` — boundary error contract;
* :mod:`app.kernel.models` — ORM tables owned by Alembic revision
  ``20260815_0004``.

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
from app.kernel.replay import (
    ReplayResult,
    VerificationResult,
    read_head,
    replay,
    verify_history,
)

__all__ = [
    "KernelCommitBatch",
    "KernelCommitReceipt",
    "KernelCommitService",
    "KernelError",
    "ReplayResult",
    "VerificationResult",
    "default_commit_service",
    "read_head",
    "replay",
    "verify_history",
]
