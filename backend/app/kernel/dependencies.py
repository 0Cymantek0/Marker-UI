"""Dependency completeness & conservative reverse invalidation (V3.2 PR73).

Derived state declares its inputs together with how *complete* that
dependency knowledge is. Every declaration input carries one of four
completeness semantics (master-plan amendment names preserved):

* ``exact_native``     — the source format gives the relation directly;
* ``exact_operator``   — an operator declares and tests its complete
  input set;
* ``conservative_scope`` — exact dependencies are unknown, so
  invalidation widens to a declared scope boundary instead of
  pretending to know a narrower graph;
* ``semantic_candidate`` — useful for recall/discovery only. It can
  never justify a narrower correctness invalidation and never adds a
  subject to the correctness set.

The invalidation contract (:func:`compute_invalidation`):

* a subject with exact knowledge is invalidated exactly when one of its
  exact inputs changed, and stays stale until every changed input is
  reconciled (``pending_inputs``);
* a changed conservative input invalidates the whole declared scope —
  widening, never guessing;
* a change that no exact knowledge anywhere covers is *unknown*: every
  conservative scope widens (``widened``/``uncovered_changes`` report
  why), because uncertainty must expand invalidation rather than
  disappear;
* semantic-candidate edges land in ``recall_candidates`` and nothing
  else.

Declarations are versioned records: bumping ``operator_version`` mints a
new identity, so a changed operator invalidates the old assumptions by
supersession instead of quietly reusing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from app.kernel.errors import KernelError
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import record_identity_hash

DEPENDENCY_SCHEMA_VERSION = "1.0.0"
RECORD_TYPE_DEPENDENCY_DECLARATION = "marker.kernel.dependency_declaration.v1"

COMPLETENESS_EXACT_NATIVE = "exact_native"
COMPLETENESS_EXACT_OPERATOR = "exact_operator"
COMPLETENESS_CONSERVATIVE_SCOPE = "conservative_scope"
COMPLETENESS_SEMANTIC_CANDIDATE = "semantic_candidate"
COMPLETENESS_LEVELS = frozenset(
    {
        COMPLETENESS_EXACT_NATIVE,
        COMPLETENESS_EXACT_OPERATOR,
        COMPLETENESS_CONSERVATIVE_SCOPE,
        COMPLETENESS_SEMANTIC_CANDIDATE,
    }
)

#: Completeness levels that prove a subject's full input set and can
#: therefore justify localized (narrow) correctness invalidation.
EXACT_COMPLETENESS_LEVELS = frozenset(
    {COMPLETENESS_EXACT_NATIVE, COMPLETENESS_EXACT_OPERATOR}
)

OPERATOR_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class DependencyInput:
    """One declared input edge with its completeness classification."""

    input_ref: str
    completeness: str

    def __post_init__(self) -> None:
        validate_record_ref(self.input_ref, field_name="input_ref")
        if self.completeness not in COMPLETENESS_LEVELS:
            raise KernelError(
                f"unknown completeness {self.completeness!r}; allowed: "
                f"{sorted(COMPLETENESS_LEVELS)}"
            )

    def canonical_value(self) -> dict[str, Any]:
        return {"input_ref": self.input_ref, "completeness": self.completeness}


@dataclass(kw_only=True)
class DependencyDeclarationRecord(KernelRecord):
    """One derived artifact's declared dependency knowledge.

    ``subject_ref`` names the derived artifact (a record id, or a stable
    node identity in the derived view). ``inputs`` is the declared input
    set with per-input completeness. ``scope_ref`` names the conservative
    boundary used when exact knowledge is absent — required whenever an
    input is ``conservative_scope``.
    """

    record_class: ClassVar[str] = "dependency_declaration"
    record_type: ClassVar[str] = RECORD_TYPE_DEPENDENCY_DECLARATION
    schema_version: ClassVar[str] = DEPENDENCY_SCHEMA_VERSION

    subject_ref: str
    inputs: tuple[DependencyInput, ...] = ()
    scope_ref: str | None = None
    operator: str
    operator_version: str

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.subject_ref, field_name="subject_ref")
        inputs = tuple(self.inputs)
        seen: set[str] = set()
        for item in inputs:
            if not isinstance(item, DependencyInput):
                raise KernelError(
                    f"inputs must be DependencyInput, got {type(item).__name__}"
                )
            if item.input_ref in seen:
                raise KernelError(
                    f"duplicate input {item.input_ref!r}; one declaration names "
                    "each input once with its completeness"
                )
            seen.add(item.input_ref)
        self.inputs = inputs
        if self.scope_ref is not None:
            validate_record_ref(self.scope_ref, field_name="scope_ref")
        if any(
            item.completeness == COMPLETENESS_CONSERVATIVE_SCOPE for item in inputs
        ) and self.scope_ref is None:
            raise KernelError(
                "a conservative_scope input requires scope_ref — the widening "
                "boundary must be declared, not implied"
            )
        for name, value in (("operator", self.operator), ("operator_version", self.operator_version)):
            if not isinstance(value, str) or not OPERATOR_ID_PATTERN.match(value):
                raise KernelError(
                    f"invalid {name}: {value!r} must match "
                    f"{OPERATOR_ID_PATTERN.pattern}"
                )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "subject_ref": self.subject_ref,
            "inputs": [
                item.canonical_value()
                for item in sorted(self.inputs, key=lambda i: i.input_ref)
            ],
            "scope_ref": self.scope_ref,
            "operator": self.operator,
            "operator_version": self.operator_version,
        }

    def declaration_id(self) -> str:
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=self.identity_payload(),
        )

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> DependencyDeclarationRecord:
        if not isinstance(payload, Mapping):
            raise KernelError(f"declaration payload must be a mapping, got {payload!r}")
        allowed = {
            "subject_ref",
            "inputs",
            "scope_ref",
            "operator",
            "operator_version",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown declaration payload fields {sorted(unknown)}")
        return cls(
            record_id=record_id,
            subject_ref=payload["subject_ref"],
            inputs=tuple(
                DependencyInput(**item) for item in payload.get("inputs") or []
            ),
            scope_ref=payload.get("scope_ref"),
            operator=payload["operator"],
            operator_version=payload["operator_version"],
        )


@dataclass(frozen=True)
class InvalidationResult:
    """The explainable outcome of reverse invalidation.

    ``invalidated`` is the correctness set: subjects that must be
    recomputed (or explicitly reconciled) before they may be reused.
    ``pending_inputs`` names, per invalidated subject, the changed
    inputs that keep it stale until reconciled — a multi-input subject
    does not become fresh just because one input was rebuilt.
    """

    invalidated: frozenset[str]
    reasons: Mapping[str, tuple[str, ...]]
    pending_inputs: Mapping[str, frozenset[str]]
    widened: bool
    widened_scopes: frozenset[str]
    recall_candidates: frozenset[str]
    uncovered_changes: frozenset[str]

    def explain(self) -> dict[str, Any]:
        """A serializable why: per-subject reasons, pending inputs, and
        whether the scope was exact or conservative."""
        return {
            "invalidated": sorted(self.invalidated),
            "reasons": {s: list(r) for s, r in sorted(self.reasons.items())},
            "pending_inputs": {
                s: sorted(i) for s, i in sorted(self.pending_inputs.items())
            },
            "widened": self.widened,
            "widened_scopes": sorted(self.widened_scopes),
            "recall_candidates": sorted(self.recall_candidates),
            "uncovered_changes": sorted(self.uncovered_changes),
        }


def compute_invalidation(
    changed_input_refs: Sequence[str],
    declarations: Sequence[DependencyDeclarationRecord],
) -> InvalidationResult:
    """Reverse-invalidated subjects for the given changed inputs.

    Pure and deterministic. See the module docstring for the contract;
    the result carries enough structure to explain *why* every subject
    was invalidated and whether each decision was exact or conservative.
    """
    changed = frozenset(changed_input_refs)
    reasons: dict[str, set[str]] = {}
    pending: dict[str, set[str]] = {}
    recall: set[str] = set()
    scopes_to_widen: set[str] = set()

    exact_cover: set[str] = set()
    for declaration in declarations:
        exact_changed = {
            item.input_ref
            for item in declaration.inputs
            if item.completeness in EXACT_COMPLETENESS_LEVELS
            and item.input_ref in changed
        }
        for item in declaration.inputs:
            if item.completeness in EXACT_COMPLETENESS_LEVELS:
                exact_cover.add(item.input_ref)
            elif item.completeness == COMPLETENESS_SEMANTIC_CANDIDATE:
                if item.input_ref in changed:
                    recall.add(declaration.subject_ref)
        if exact_changed:
            slot = reasons.setdefault(declaration.subject_ref, set())
            slot.add("exact")
            pending.setdefault(declaration.subject_ref, set()).update(exact_changed)
        for item in declaration.inputs:
            if (
                item.completeness == COMPLETENESS_CONSERVATIVE_SCOPE
                and item.input_ref in changed
            ):
                assert declaration.scope_ref is not None  # enforced at construction
                scopes_to_widen.add(declaration.scope_ref)

    uncovered = changed - exact_cover
    if uncovered:
        # Unknown change surface: every declared conservative boundary
        # widens. Uncertainty expands invalidation; it never disappears.
        scopes_to_widen.update(
            d.scope_ref for d in declarations if d.scope_ref is not None
        )

    widened_scopes = frozenset(scopes_to_widen)
    if widened_scopes:
        for declaration in declarations:
            if declaration.scope_ref in widened_scopes:
                slot = reasons.setdefault(declaration.subject_ref, set())
                slot.add("conservative_scope")

    return InvalidationResult(
        invalidated=frozenset(reasons),
        reasons={subject: tuple(sorted(because)) for subject, because in reasons.items()},
        pending_inputs={s: frozenset(i) for s, i in pending.items()},
        widened=bool(widened_scopes),
        widened_scopes=widened_scopes,
        recall_candidates=frozenset(recall),
        uncovered_changes=frozenset(uncovered),
    )
