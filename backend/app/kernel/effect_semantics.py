"""Per-destination external-effect semantics declaration (V3.2, inv 37).

Readiness invariant 37 requires every external effect destination to be
declared as exactly-once, at-least-once, at-most-once, or
reconciliation-required **based on real destination primitives** — not
on aspiration. The masterplan runtime contract (§11B.1) reserves
exactly-once language for effects backed by an actual transaction or
idempotency primitive and assigns everything else the honest weaker
contract.

This module is the single authoritative declaration table. Each entry
carries:

* a :class:`DestinationCapabilities` vector — boolean *facts* about the
  destination primitive's behavior, each of which is proven by an
  executable test in ``tests/test_kernel_effect_semantics.py`` against
  the real primitive (not a mock);
* the semantics *derived* from those facts by :func:`derive_semantics`
  — never hand-assigned, so the label cannot drift from the facts;
* the dotted import path of the primitive that owns the effect, which
  the tests resolve against real code so a renamed or removed primitive
  fails the declaration suite;
* an honest reconciliation note where effects can outlive their
  authority (orphaned bytes, partial sets).

Current destinations and their honest declarations:

``kernel.accepted_publication``
    ``fencing.accept`` linearizes acceptance in one transaction (fence
    check + publication insert + lease flip). Same-result redelivery
    converges onto the existing publication; a divergent result raises
    :class:`~app.kernel.errors.PublicationConflictError`; a stale
    fencing token is rejected before comparison. ⇒ **exactly_once**.

``filesystem.conversion_output``
    ``write_conversion_output`` writes per-file atomically (temp +
    fsync + rename) but has no cross-file transaction, and in the
    default collision-avoiding mode a redelivery writes a *new*
    ``-N``-suffixed set rather than overwriting. Accepted truth stays
    singular (the publication descriptor bounds which paths are truth),
    but interrupted or superseded writes leave orphaned files no
    subsystem sweeps. ⇒ **manual_reconciliation_required**.

``compatibility.conversion_job_row``
    The compatibility row is a derived projection of accepted kernel
    truth (guarded ``status not_in terminal`` UPDATE replay in
    ``_project_publication``/``_finalize_job``). Replay converges,
    terminal rows are never overwritten, and the row may only read
    ``completed`` after fenced acceptance. It holds no independent
    authority. ⇒ **exactly_once** as a derived effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AT_LEAST_ONCE",
    "AT_MOST_ONCE",
    "DESTINATIONS",
    "EXACTLY_ONCE",
    "RECONCILIATION_REQUIRED",
    "DestinationCapabilities",
    "DestinationDeclaration",
    "EffectSemantics",
    "declare_destination",
    "declared_destinations",
    "derive_semantics",
]


#: Public contract values (readiness invariant 37 wording).
EXACTLY_ONCE = "exactly_once"
AT_LEAST_ONCE = "at_least_once"
AT_MOST_ONCE = "at_most_once"
RECONCILIATION_REQUIRED = "manual_reconciliation_required"


class EffectSemantics(Enum):
    """The four declared external-effect contracts."""

    EXACTLY_ONCE = EXACTLY_ONCE
    AT_LEAST_ONCE = AT_LEAST_ONCE
    AT_MOST_ONCE = AT_MOST_ONCE
    RECONCILIATION_REQUIRED = RECONCILIATION_REQUIRED


@dataclass(frozen=True)
class DestinationCapabilities:
    """Facts about one destination primitive's effect behavior.

    Every field must be backed by an executable fact-test against the
    real primitive; the declaration suite fails if a fact and the code
    it describes drift apart.
    """

    #: The effect commits atomically with its authority decision inside
    #: one database transaction.
    linearizes_in_transaction: bool
    #: Delivering the same effect twice leaves exactly one durable
    #: effect (identity/idempotency primitive present).
    suppresses_duplicate_delivery: bool
    #: A divergent duplicate attempt is refused with the existing
    #: effect unchanged (conflict rejection primitive present).
    rejects_divergent_effect: bool
    #: The effect is a pure projection of accepted kernel truth and
    #: holds no independent authority of its own.
    derived_from_kernel_truth: bool
    #: Failed, interrupted, or superseded attempts can leave effects
    #: (orphaned/partial bytes) that require external reconciliation.
    requires_reconciliation: bool


@dataclass(frozen=True)
class DestinationDeclaration:
    """One destination's declared external-effect semantics."""

    destination: str
    semantics: EffectSemantics
    capabilities: DestinationCapabilities
    #: Dotted import path of the real primitive owning the effect.
    primitive: str
    #: Honest note on what reconciliation the destination requires
    #: (empty when nothing outlives the authority decision).
    reconciliation: str


def derive_semantics(capabilities: DestinationCapabilities) -> EffectSemantics:
    """Derive the declared semantics from capability facts.

    Rules, in authority order:

    1. Effects that can outlive their authority decision (orphaned or
       partial bytes with no automatic sweep) are declared
       ``manual_reconciliation_required`` regardless of other
       strengths — the orphan risk dominates the contract.
    2. A pure projection of accepted kernel truth that commits in one
       transaction and converges on replay is ``exactly_once`` as a
       derived effect.
    3. An authoritative effect linearized in one transaction with
       duplicate convergence and divergent rejection is ``exactly_once``.
    4. An atomic effect with no identity/deduplication primitive is
       ``at_most_once`` per delivery attempt: each attempt commits at
       most one effect and nothing converges redeliveries.
    5. Anything else (non-atomic effect without reconciliation needs)
       is ``at_least_once``: redelivery re-executes the effect and
       convergence is only observable, not guaranteed by the primitive.
    """
    c = capabilities
    if c.requires_reconciliation:
        return EffectSemantics.RECONCILIATION_REQUIRED
    if (
        c.derived_from_kernel_truth
        and c.linearizes_in_transaction
        and c.suppresses_duplicate_delivery
    ):
        return EffectSemantics.EXACTLY_ONCE
    if (
        c.linearizes_in_transaction
        and c.suppresses_duplicate_delivery
        and c.rejects_divergent_effect
    ):
        return EffectSemantics.EXACTLY_ONCE
    if c.linearizes_in_transaction and not c.suppresses_duplicate_delivery:
        return EffectSemantics.AT_MOST_ONCE
    return EffectSemantics.AT_LEAST_ONCE


_PUBLICATION_CAPABILITIES = DestinationCapabilities(
    linearizes_in_transaction=True,
    suppresses_duplicate_delivery=True,
    rejects_divergent_effect=True,
    derived_from_kernel_truth=False,
    requires_reconciliation=False,
)

_FILESYSTEM_CAPABILITIES = DestinationCapabilities(
    linearizes_in_transaction=False,
    suppresses_duplicate_delivery=False,
    rejects_divergent_effect=False,
    derived_from_kernel_truth=False,
    requires_reconciliation=True,
)

_PROJECTION_CAPABILITIES = DestinationCapabilities(
    linearizes_in_transaction=True,
    suppresses_duplicate_delivery=True,
    rejects_divergent_effect=True,
    derived_from_kernel_truth=True,
    requires_reconciliation=False,
)

DESTINATIONS: tuple[DestinationDeclaration, ...] = (
    DestinationDeclaration(
        destination="kernel.accepted_publication",
        semantics=derive_semantics(_PUBLICATION_CAPABILITIES),
        capabilities=_PUBLICATION_CAPABILITIES,
        primitive="app.kernel.fencing.accept",
        reconciliation="",
    ),
    DestinationDeclaration(
        destination="filesystem.conversion_output",
        semantics=derive_semantics(_FILESYSTEM_CAPABILITIES),
        capabilities=_FILESYSTEM_CAPABILITIES,
        primitive="app.services.output_writer.write_conversion_output",
        reconciliation=(
            "Per-file atomic rename only; no cross-file transaction. In the "
            "default collision-avoiding mode a redelivery writes a new -N "
            "suffixed set, so interrupted, superseded, or re-executed writes "
            "leave orphaned predecessor files. Accepted truth remains singular "
            "(the publication descriptor bounds which paths are truth), but "
            "orphan cleanup is an explicit reconciliation duty — no subsystem "
            "sweeps it automatically."
        ),
    ),
    DestinationDeclaration(
        destination="compatibility.conversion_job_row",
        semantics=derive_semantics(_PROJECTION_CAPABILITIES),
        capabilities=_PROJECTION_CAPABILITIES,
        primitive=(
            "app.services.kernel_runtime.KernelRuntimeCoordinator"
            "._project_publication"
        ),
        reconciliation="",
    ),
)


def declared_destinations() -> tuple[str, ...]:
    """Stable destination ids in declaration order."""
    return tuple(entry.destination for entry in DESTINATIONS)


def declare_destination(destination: str) -> DestinationDeclaration:
    """Look up the declaration for *destination* (KeyError if unknown)."""
    for entry in DESTINATIONS:
        if entry.destination == destination:
            return entry
    raise KeyError(
        f"unknown external-effect destination: {destination!r}; "
        f"declared destinations: {declared_destinations()!r}"
    )
