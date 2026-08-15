"""Typed kernel record inputs (V3.2 PR63A minimum entity semantics).

These envelopes are the submitter-facing shape of the record classes PR63
must establish: NativeObject, NativeFact, ClaimAssertion, ClaimAssessment,
Observation, and a storage-envelope Decision. They are deliberately
metadata-only — payload byte hashes may be attached, but durable payload
staging/availability belongs to PR64.

Identity rules:

* semantic identity is computed by the commit service from
  ``identity_payload()`` through the PR61 canonical utilities
  (``record_identity_hash``), never from ``repr``/ad-hoc JSON;
* ``record_id`` is a caller-visible event id, NOT part of semantic
  identity — semantically identical records cannot be committed twice to
  one workspace (supersession requires a new record);
* floats, sets, datetimes, bytes, and arbitrary objects are rejected at
  the kernel boundary when the payload is canonicalized;
* unordered reference sets are sorted so member order never changes
  identity;
* raw Unicode strings are preserved exactly — no NFC/NFKC folding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping
from uuid import uuid4

from app.kernel.errors import KernelError

RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")

_RECORD_CLASS_NATIVE_OBJECT = "native_object"
_RECORD_CLASS_NATIVE_FACT = "native_fact"
_RECORD_CLASS_CLAIM_ASSERTION = "claim_assertion"
_RECORD_CLASS_CLAIM_ASSESSMENT = "claim_assessment"
_RECORD_CLASS_OBSERVATION = "observation"
_RECORD_CLASS_DECISION = "decision"

#: Framing record_type per record class. Together with the per-class
#: schema_version this is the identity domain separator consumed by
#: ``app.utils.canonical.framing``.
RECORD_TYPES: dict[str, str] = {
    _RECORD_CLASS_NATIVE_OBJECT: "marker.kernel.native_object.v1",
    _RECORD_CLASS_NATIVE_FACT: "marker.kernel.native_fact.v1",
    _RECORD_CLASS_CLAIM_ASSERTION: "marker.kernel.claim_assertion.v1",
    _RECORD_CLASS_CLAIM_ASSESSMENT: "marker.kernel.claim_assessment.v1",
    _RECORD_CLASS_OBSERVATION: "marker.kernel.observation.v1",
    _RECORD_CLASS_DECISION: "marker.kernel.decision.v1",
}


def validate_record_ref(value: str, *, field_name: str = "record_id") -> str:
    """Validate a record id / record reference at the kernel boundary."""
    if not isinstance(value, str) or not RECORD_ID_PATTERN.match(value):
        raise KernelError(
            f"invalid {field_name}: {value!r} must match {RECORD_ID_PATTERN.pattern}"
        )
    return value


@dataclass(kw_only=True)
class KernelRecord:
    """Base envelope; use the concrete classes below."""

    record_id: str = field(default_factory=lambda: str(uuid4()))

    record_class: ClassVar[str] = ""
    record_type: ClassVar[str] = ""
    schema_version: ClassVar[str] = "1.0.0"

    def identity_payload(self) -> dict[str, Any]:
        raise NotImplementedError

    def __post_init__(self) -> None:
        validate_record_ref(self.record_id)


@dataclass(kw_only=True)
class NativeObjectRecord(KernelRecord):
    """Source-relative object identity/locator with extractor lineage."""

    record_class: ClassVar[str] = _RECORD_CLASS_NATIVE_OBJECT
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_NATIVE_OBJECT]

    source_uri: str
    locator: str
    media_type: str
    extractor_name: str
    extractor_version: str
    properties: Mapping[str, Any] = field(default_factory=dict)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "locator": self.locator,
            "media_type": self.media_type,
            "extractor_lineage": {
                "name": self.extractor_name,
                "version": self.extractor_version,
            },
            "properties": dict(self.properties),
        }


@dataclass(kw_only=True)
class NativeFactRecord(KernelRecord):
    """Exact property/representation derived from a native object/source."""

    record_class: ClassVar[str] = _RECORD_CLASS_NATIVE_FACT
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_NATIVE_FACT]

    native_object_ref: str
    property_name: str
    raw_representation: str
    typed_interpretation: Any
    extractor_name: str
    extractor_version: str
    anchor: Any = None  # canonical geometry value (e.g. CanonicalBox) or None

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.native_object_ref, field_name="native_object_ref")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "native_object_ref": self.native_object_ref,
            "property_name": self.property_name,
            "raw_representation": self.raw_representation,
            "typed_interpretation": self.typed_interpretation,
            "extractor_lineage": {
                "name": self.extractor_name,
                "version": self.extractor_version,
            },
            "anchor": self.anchor,
        }


@dataclass(kw_only=True)
class ClaimAssertionRecord(KernelRecord):
    """Immutable assertion meaning + subject identity (stable claim)."""

    record_class: ClassVar[str] = _RECORD_CLASS_CLAIM_ASSERTION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_CLAIM_ASSERTION]

    claim_key: str
    subject: str
    predicate: str
    value: Any
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "claim_key": self.claim_key,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "qualifiers": dict(self.qualifiers),
        }


@dataclass(kw_only=True)
class ClaimAssessmentRecord(KernelRecord):
    """Append-only assessment of an assertion under a declared context.

    Stores the policy/evidence/snapshot context fields it knows; full
    verifier policy resolution is PR74/75 and is intentionally absent.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_CLAIM_ASSESSMENT
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_CLAIM_ASSESSMENT]

    assertion_ref: str
    outcome: str
    policy_id: str
    policy_revision: str
    evidence_refs: tuple[str, ...] = ()
    declared_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.assertion_ref, field_name="assertion_ref")
        self.evidence_refs = tuple(
            validate_record_ref(ref, field_name="evidence_ref") for ref in self.evidence_refs
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "assertion_ref": self.assertion_ref,
            "outcome": self.outcome,
            "policy": {"policy_id": self.policy_id, "revision": self.policy_revision},
            "evidence_refs": sorted(self.evidence_refs),
            "declared_context": dict(self.declared_context),
        }


@dataclass(kw_only=True)
class ObservationRecord(KernelRecord):
    """Evidence/witness record demonstrating record-vs-payload identity split.

    ``derivation`` describes how the observation was produced; it is part
    of semantic identity, so two observations with identical payload bytes
    but different derivations are two distinct evidence records (the
    payload byte hash is stored separately and never deduplicates
    evidence).
    """

    record_class: ClassVar[str] = _RECORD_CLASS_OBSERVATION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_OBSERVATION]

    observer: str
    derivation: Mapping[str, Any]
    summary: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    #: exact payload material (metadata-only in PR63A); hashed at commit
    payload_bytes: bytes | None = None
    #: externally staged payload hash (PR64 will own durable staging)
    declared_payload_hash: str | None = None

    def identity_payload(self) -> dict[str, Any]:
        return {
            "observer": self.observer,
            "derivation": dict(self.derivation),
            "summary": self.summary,
            "context": dict(self.context),
        }


@dataclass(kw_only=True)
class DecisionRecord(KernelRecord):
    """Storage-envelope decision proving multi-record authoritative mutation.

    No PR74 verification authorization is implied or evaluated.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_DECISION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_DECISION]

    decision_key: str
    outcome: str
    rationale: str
    input_refs: tuple[str, ...] = ()
    authority_rule: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.input_refs = tuple(
            validate_record_ref(ref, field_name="input_ref") for ref in self.input_refs
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "decision_key": self.decision_key,
            "outcome": self.outcome,
            "rationale": self.rationale,
            "input_refs": sorted(self.input_refs),
            "authority_rule": self.authority_rule,
        }


#: Allowed dependency-edge kinds for this slice. Generic enough for
#: record-to-record lineage without pulling in PR74 proof semantics.
EDGE_KIND_DEPENDS_ON = "depends_on"
EDGE_KIND_DERIVED_FROM = "derived_from"
EDGE_KIND_ASSESSES = "assesses"
EDGE_KIND_EVIDENCE_FOR = "evidence_for"
EDGE_KIND_OBSERVES = "observes"

ALLOWED_EDGE_KINDS = frozenset(
    {
        EDGE_KIND_DEPENDS_ON,
        EDGE_KIND_DERIVED_FROM,
        EDGE_KIND_ASSESSES,
        EDGE_KIND_EVIDENCE_FOR,
        EDGE_KIND_OBSERVES,
    }
)

EDGE_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(kw_only=True)
class KernelEdge:
    """Dependency/reference edge between records of one workspace."""

    edge_id: str = field(default_factory=lambda: str(uuid4()))
    edge_kind: str
    source_ref: str
    target_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.edge_kind, str) or not EDGE_KIND_PATTERN.match(self.edge_kind):
            raise KernelError(
                f"invalid edge_kind: {self.edge_kind!r} must match {EDGE_KIND_PATTERN.pattern}"
            )
        if self.edge_kind not in ALLOWED_EDGE_KINDS:
            raise KernelError(
                f"unknown edge_kind {self.edge_kind!r}; allowed: {sorted(ALLOWED_EDGE_KINDS)}"
            )
        validate_record_ref(self.source_ref, field_name="source_ref")
        validate_record_ref(self.target_ref, field_name="target_ref")
        if self.source_ref == self.target_ref:
            raise KernelError("self-referential edge rejected")
        validate_record_ref(self.edge_id, field_name="edge_id")
