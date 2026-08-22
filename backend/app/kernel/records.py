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
_RECORD_CLASS_SOURCE_IDENTITY = "source_identity"
_RECORD_CLASS_CONTENT_REVISION = "content_revision"
_RECORD_CLASS_ACCESS_POLICY_REVISION = "access_policy_revision"
_RECORD_CLASS_AUTHORIZATION_EPOCH = "authorization_epoch"
_RECORD_CLASS_SOURCE_OBSERVATION = "source_observation"
_RECORD_CLASS_SECURITY_DOMAIN = "security_domain"
_RECORD_CLASS_ACCESS_DENIAL = "access_denial"

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
    _RECORD_CLASS_SOURCE_IDENTITY: "marker.kernel.source_identity.v1",
    _RECORD_CLASS_CONTENT_REVISION: "marker.kernel.content_revision.v1",
    _RECORD_CLASS_ACCESS_POLICY_REVISION: "marker.kernel.access_policy_revision.v1",
    _RECORD_CLASS_AUTHORIZATION_EPOCH: "marker.kernel.authorization_epoch.v1",
    _RECORD_CLASS_SOURCE_OBSERVATION: "marker.kernel.source_observation.v1",
    _RECORD_CLASS_SECURITY_DOMAIN: "marker.kernel.security_domain.v1",
    _RECORD_CLASS_ACCESS_DENIAL: "marker.kernel.access_denial.v1",
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


#: PR74 assessment-outcome vocabulary (V3.2 §4.4). The named set is the
#: versioned contract later PR75 calibration/risk artifacts attach to;
#: unknown outcome strings remain committable as explicitly NON-
#: authority-bearing historical results (PR63 compatibility) — the
#: kernel never treats an outcome it does not know as authority.
CLAIM_OUTCOME_SOURCE_EXACT = "source_exact"
CLAIM_OUTCOME_VERIFIED = "verified"
CLAIM_OUTCOME_ACCEPTED_WITH_WARNING = "accepted_with_warning"
CLAIM_OUTCOME_UNCERTAIN = "uncertain"
CLAIM_OUTCOME_UNAVAILABLE = "unavailable"
CLAIM_OUTCOME_ABSTAINED = "abstained"
CLAIM_OUTCOME_FAILED = "failed"

CLAIM_ASSESSMENT_OUTCOMES = frozenset(
    {
        CLAIM_OUTCOME_SOURCE_EXACT,
        CLAIM_OUTCOME_VERIFIED,
        CLAIM_OUTCOME_ACCEPTED_WITH_WARNING,
        CLAIM_OUTCOME_UNCERTAIN,
        CLAIM_OUTCOME_UNAVAILABLE,
        CLAIM_OUTCOME_ABSTAINED,
        CLAIM_OUTCOME_FAILED,
    }
)

#: Outcomes that may only be committed together with a structurally
#: valid proof-support graph (see ``app/kernel/proofs.py``).
AUTHORITY_BEARING_OUTCOMES = frozenset(
    {CLAIM_OUTCOME_SOURCE_EXACT, CLAIM_OUTCOME_VERIFIED}
)


@dataclass(kw_only=True)
class ClaimAssertionRecord(KernelRecord):
    """Immutable assertion meaning + subject identity (stable claim).

    PR74 identity decision (explicit, fixture-pinned): ``claim_key`` IS
    part of semantic identity. It is the caller's stable external key
    that scopes the assertion's referent (its claim namespace); two
    assertions with different keys are two claims even when the
    subject/predicate/value triple coincides. Renaming the key mints a
    new claim rather than relabeling the old one, so no payload/identity
    split is needed and every PR63-era stored assertion keeps its
    identity. Changing evidence, policy, or assessment outcomes never
    touches this record.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_CLAIM_ASSERTION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_CLAIM_ASSERTION]

    claim_key: str
    subject: str
    predicate: str
    value: Any
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.claim_key, str) or not self.claim_key:
            raise KernelError(f"invalid claim_key: {self.claim_key!r}")
        for name in ("subject", "predicate"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise KernelError(f"invalid {name}: {value!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "claim_key": self.claim_key,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "qualifiers": dict(self.qualifiers),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> ClaimAssertionRecord:
        """Rematerialize a stored assertion payload (fail-closed)."""
        if not isinstance(payload, Mapping):
            raise KernelError(f"assertion payload must be a mapping, got {payload!r}")
        allowed = {"claim_key", "subject", "predicate", "value", "qualifiers"}
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown assertion payload fields {sorted(unknown)}")
        try:
            return cls(
                record_id=record_id,
                claim_key=payload["claim_key"],
                subject=payload["subject"],
                predicate=payload["predicate"],
                value=payload["value"],
                qualifiers=dict(payload.get("qualifiers") or {}),
            )
        except KeyError as exc:
            raise KernelError(f"assertion payload is missing {exc.args[0]!r}") from None


@dataclass(kw_only=True)
class ClaimAssessmentRecord(KernelRecord):
    """Append-only assessment of an assertion under a declared context.

    PR74 binds the context that makes the outcome meaningful into typed,
    identity-affecting fields:

    * ``policy_id``/``policy_revision`` — the verifier policy the
      outcome is relative to (there is no policy-free claim status);
    * ``evidence_refs`` — the unordered evidence set; at commit time it
      must agree exactly with the assessment's proof-support records;
    * ``snapshot_commit_id`` — the committed kernel head the assessment
      was computed against (the as-of cut); validated ``<= current
      head`` at commit so an assessment can never claim a future cut;
    * ``workflow_class`` — versioned workflow/risk class label (a
      PR75 hook; deliberately a plain string here).

    ``declared_context`` is retained from PR63 and stays identity-
    affecting for stored-record compatibility. Statistical sufficiency
    of the proof (calibration, risk bounds) is PR75 and is intentionally
    absent: an authority-bearing outcome requires a structurally valid
    proof, never a statistically sufficient one.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_CLAIM_ASSESSMENT
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_CLAIM_ASSESSMENT]

    assertion_ref: str
    outcome: str
    policy_id: str
    policy_revision: str
    evidence_refs: tuple[str, ...] = ()
    snapshot_commit_id: int = 0
    workflow_class: str = ""
    declared_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.assertion_ref, field_name="assertion_ref")
        if not isinstance(self.outcome, str) or not self.outcome:
            raise KernelError(f"invalid outcome: {self.outcome!r}")
        for name in ("policy_id", "policy_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise KernelError(f"invalid {name}: {value!r}")
        self.evidence_refs = tuple(
            validate_record_ref(ref, field_name="evidence_ref") for ref in self.evidence_refs
        )
        if (
            not isinstance(self.snapshot_commit_id, int)
            or isinstance(self.snapshot_commit_id, bool)
            or self.snapshot_commit_id < 0
        ):
            raise KernelError(f"invalid snapshot_commit_id: {self.snapshot_commit_id!r}")
        if not isinstance(self.workflow_class, str):
            raise KernelError(f"invalid workflow_class: {self.workflow_class!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "assertion_ref": self.assertion_ref,
            "outcome": self.outcome,
            "policy": {"policy_id": self.policy_id, "revision": self.policy_revision},
            "evidence_refs": sorted(self.evidence_refs),
            "snapshot_commit_id": self.snapshot_commit_id,
            "workflow_class": self.workflow_class,
            "declared_context": dict(self.declared_context),
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, record_id: str
    ) -> ClaimAssessmentRecord:
        """Rematerialize a stored assessment payload (fail-closed).

        Stored PR63 payloads predate ``snapshot_commit_id`` and
        ``workflow_class``; they rematerialize with the honest defaults
        (snapshot 0 = the empty cut, no workflow class). Historical
        outcome strings outside the PR74 vocabulary stay readable —
        append-only history is never rewritten, and commit-time
        authority rules only ever apply to newly committed records.
        """
        if not isinstance(payload, Mapping):
            raise KernelError(f"assessment payload must be a mapping, got {payload!r}")
        allowed = {
            "assertion_ref",
            "outcome",
            "policy",
            "evidence_refs",
            "snapshot_commit_id",
            "workflow_class",
            "declared_context",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown assessment payload fields {sorted(unknown)}")
        try:
            policy = payload["policy"]
            return cls(
                record_id=record_id,
                assertion_ref=payload["assertion_ref"],
                outcome=payload["outcome"],
                policy_id=policy["policy_id"],
                policy_revision=policy["revision"],
                evidence_refs=tuple(payload.get("evidence_refs") or ()),
                snapshot_commit_id=payload.get("snapshot_commit_id", 0),
                workflow_class=payload.get("workflow_class", ""),
                declared_context=dict(payload.get("declared_context") or {}),
            )
        except KeyError as exc:
            raise KernelError(f"assessment payload is missing {exc.args[0]!r}") from None


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


# ---------------------------------------------------------------------------
# Source truth records (V3.2 PR70/71 local slice: amendment 16B identities)
# ---------------------------------------------------------------------------

#: Master-plan source consistency classes (amendment 16B.2). An accepted
#: ContentRevision carries one of the first four; ``incoherent_rejected``
#: is only ever an observation outcome — a rejected acquisition must not
#: mint a content revision.
SOURCE_CONSISTENCY_NATIVE_ATOMIC = "native_atomic"
SOURCE_CONSISTENCY_VERSION_PINNED = "version_pinned"
SOURCE_CONSISTENCY_STABLE_HANDLE = "stable_handle"
SOURCE_CONSISTENCY_BEST_EFFORT = "best_effort_consistent"
SOURCE_CONSISTENCY_INCOHERENT_REJECTED = "incoherent_rejected"

SOURCE_CONSISTENCY_CLASSES = frozenset(
    {
        SOURCE_CONSISTENCY_NATIVE_ATOMIC,
        SOURCE_CONSISTENCY_VERSION_PINNED,
        SOURCE_CONSISTENCY_STABLE_HANDLE,
        SOURCE_CONSISTENCY_BEST_EFFORT,
    }
)

#: Logical source kinds for this slice. ``upload`` is an immutable
#: Marker-UI-owned upload occurrence; ``local_path`` is a permitted-root
#: path; ``url`` is a fetched remote origin (best-effort consistency);
#: ``connector`` is a remote provider item ingested through the connector
#: convergence core, whose logical key is provider-qualified identity
#: (never a path, URL, or content hash).
SOURCE_KIND_LOCAL_PATH = "local_path"
SOURCE_KIND_UPLOAD = "upload"
SOURCE_KIND_URL = "url"
SOURCE_KIND_CONNECTOR = "connector"
SOURCE_KINDS = frozenset(
    {
        SOURCE_KIND_LOCAL_PATH,
        SOURCE_KIND_UPLOAD,
        SOURCE_KIND_URL,
        SOURCE_KIND_CONNECTOR,
    }
)

_SOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/\\@-]{0,511}$")
_BLOB_KEY_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_SUFFIX_PATTERN = re.compile(r"^\.[a-z0-9]{1,10}$")


@dataclass(kw_only=True)
class SourceIdentityRecord(KernelRecord):
    """Logical provider/item identity — never a content hash.

    Identity is ``source_kind`` + ``source_key``: two logically distinct
    sources that happen to contain identical bytes commit two of these
    records (and may share one physical artifact), while one logical
    source whose bytes changed keeps this record and mints a new
    ContentRevision instead.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_SOURCE_IDENTITY
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_SOURCE_IDENTITY]

    source_kind: str
    source_key: str
    #: audit-only registration context; deliberately not identity
    registered_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.source_kind not in SOURCE_KINDS:
            raise KernelError(
                f"invalid source_kind: {self.source_kind!r}; allowed: {sorted(SOURCE_KINDS)}"
            )
        if not isinstance(self.source_key, str) or not _SOURCE_KEY_PATTERN.match(
            self.source_key
        ):
            raise KernelError(
                f"invalid source_key: {self.source_key!r} must match {_SOURCE_KEY_PATTERN.pattern}"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_key": self.source_key,
        }


@dataclass(kw_only=True)
class ContentRevisionRecord(KernelRecord):
    """Exact acquired bytes of one logical source, plus their proof anchor.

    The immutable bytes live in the content-addressed source store under
    ``blob_key``; this record is the durable claim that those bytes were
    acquired from ``source_ref`` with ``consistency_class`` evidence.
    Acquisition evidence itself (timestamps, handle stats) belongs to the
    SourceObservationRecord so re-acquiring identical bytes converges to
    this one revision instead of forking on volatile metadata.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_CONTENT_REVISION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_CONTENT_REVISION]

    source_ref: str
    blob_key: str
    byte_length: int
    media_type: str
    consistency_class: str
    #: declared suffix of the staged artifact (converter routing needs it)
    suffix: str

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.source_ref, field_name="source_ref")
        if not isinstance(self.blob_key, str) or not _BLOB_KEY_PATTERN.match(self.blob_key):
            raise KernelError(
                f"invalid blob_key: {self.blob_key!r} must match {_BLOB_KEY_PATTERN.pattern}"
            )
        if not isinstance(self.byte_length, int) or isinstance(self.byte_length, bool) or self.byte_length < 0:
            raise KernelError(f"invalid byte_length: {self.byte_length!r}")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise KernelError(f"invalid media_type: {self.media_type!r}")
        if self.consistency_class not in SOURCE_CONSISTENCY_CLASSES:
            raise KernelError(
                f"invalid consistency_class: {self.consistency_class!r}; accepted revisions "
                f"carry one of {sorted(SOURCE_CONSISTENCY_CLASSES)}"
            )
        if not isinstance(self.suffix, str) or not _SOURCE_SUFFIX_PATTERN.match(self.suffix):
            raise KernelError(
                f"invalid suffix: {self.suffix!r} must match {_SOURCE_SUFFIX_PATTERN.pattern}"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "blob_key": self.blob_key,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "consistency_class": self.consistency_class,
            "suffix": self.suffix,
        }


@dataclass(kw_only=True)
class AccessPolicyRevisionRecord(KernelRecord):
    """Source access-policy snapshot, separate from content identity.

    A policy-only change (permitted-root reconfiguration, restriction
    toggles) mints one of these and leaves every ContentRevisionRecord
    untouched. ``policy_facts`` carries only what the local profile
    actually observes; it never fabricates ACL/group knowledge.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_ACCESS_POLICY_REVISION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_ACCESS_POLICY_REVISION]

    source_ref: str
    policy_profile: str
    policy_facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.source_ref, field_name="source_ref")
        if not isinstance(self.policy_profile, str) or not self.policy_profile:
            raise KernelError(f"invalid policy_profile: {self.policy_profile!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "policy_profile": self.policy_profile,
            "policy_facts": dict(self.policy_facts),
        }


@dataclass(kw_only=True)
class AuthorizationEpochRecord(KernelRecord):
    """Workspace authorization-domain epoch (amendment 16B.1).

    Structurally distinct from content identity: advancing the epoch
    records that the effective local authorization domain changed, so
    later query/effect decisions can be invalidated independently of
    immutable historical source bytes. ``fingerprint`` is derived from
    ``domain_facts``; both participate in identity so the stored record
    is self-describing.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_AUTHORIZATION_EPOCH
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_AUTHORIZATION_EPOCH]

    epoch_number: int
    fingerprint: str
    domain_facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            not isinstance(self.epoch_number, int)
            or isinstance(self.epoch_number, bool)
            or self.epoch_number < 1
        ):
            raise KernelError(f"invalid epoch_number: {self.epoch_number!r}")
        if not isinstance(self.fingerprint, str) or not _BLOB_KEY_PATTERN.match(self.fingerprint):
            raise KernelError(
                f"invalid fingerprint: {self.fingerprint!r} must match {_BLOB_KEY_PATTERN.pattern}"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "epoch_number": self.epoch_number,
            "fingerprint": self.fingerprint,
            "domain_facts": dict(self.domain_facts),
        }


@dataclass(kw_only=True)
class SourceObservationRecord(KernelRecord):
    """One acquisition/poll event relating the source identities.

    Append-only audit history: carries the handle evidence (identity
    stats before/after, observed path, timestamps) and the outcome.
    Rejected incoherent acquisitions record ``outcome=rejected_incoherent``
    with ``content_revision_ref=None`` — the failure is inspectable
    without having minted a revision.

    Connector lifecycle outcomes (PR71B amendment 16B.7) also leave the
    content-revision reference empty: ``policy_updated`` records a
    policy/ACL-only transition, ``access_lost`` records deletion or
    loss-of-access (the live-deny consequence is the paired
    AccessDenialRecord), and ``restored`` records reappearance under the
    same provider identity.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_SOURCE_OBSERVATION
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_SOURCE_OBSERVATION]

    observer: str
    source_ref: str
    outcome: str
    content_revision_ref: str | None = None
    access_policy_ref: str | None = None
    authorization_epoch: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)

    #: acquisition outcomes (PR70/71) plus connector lifecycle outcomes
    SOURCE_OBSERVATION_OUTCOMES = frozenset(
        {
            "accepted",
            "rejected_incoherent",
            "downgraded",
            "policy_updated",
            "access_lost",
            "restored",
            "metadata_updated",
        }
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.source_ref, field_name="source_ref")
        if self.content_revision_ref is not None:
            validate_record_ref(
                self.content_revision_ref, field_name="content_revision_ref"
            )
        if self.access_policy_ref is not None:
            validate_record_ref(self.access_policy_ref, field_name="access_policy_ref")
        if self.outcome not in self.SOURCE_OBSERVATION_OUTCOMES:
            raise KernelError(
                f"invalid outcome: {self.outcome!r}; allowed: "
                f"{sorted(self.SOURCE_OBSERVATION_OUTCOMES)}"
            )
        if self.outcome == "accepted" and self.content_revision_ref is None:
            raise KernelError(
                "an accepted observation must reference the content revision it accepted"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "observer": self.observer,
            "source_ref": self.source_ref,
            "outcome": self.outcome,
            "content_revision_ref": self.content_revision_ref,
            "access_policy_ref": self.access_policy_ref,
            "authorization_epoch": self.authorization_epoch,
            "evidence": dict(self.evidence),
        }


#: Security-domain key grammar — intentionally aligned with the PR76
#: publication-profile grammar so a domain set can name its high-assurance
#: partition profile without a second encoding.
_DOMAIN_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

#: Live-deny targets. A denial is authoritative query-time state: it must
#: win over stale published/indexed material without waiting for any
#: background rebuild (PR78).
ACCESS_DENIAL_TARGET_DOMAIN = "domain"
ACCESS_DENIAL_TARGET_SOURCE = "source"
ACCESS_DENIAL_TARGET_RECORD = "record"
ACCESS_DENIAL_TARGET_KINDS = frozenset(
    {
        ACCESS_DENIAL_TARGET_DOMAIN,
        ACCESS_DENIAL_TARGET_SOURCE,
        ACCESS_DENIAL_TARGET_RECORD,
    }
)


@dataclass(kw_only=True)
class SecurityDomainRecord(KernelRecord):
    """Assignment of one logical source to a security domain (PR78).

    Security domains are the partition dimension of authorization-first
    retrieval: a domain assignment is *policy*, deliberately separate
    from content identity (a reassignment mints this record and leaves
    every ContentRevisionRecord untouched) and from the workspace
    AuthorizationEpochRecord (which tracks the local domain facts, not
    per-source membership). ``assignment_basis`` participates in
    identity so the stored record is self-describing, mirroring
    ``AuthorizationEpochRecord.domain_facts``.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_SECURITY_DOMAIN
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_SECURITY_DOMAIN]

    source_ref: str
    domain_key: str
    #: self-describing assignment context (operator, reason, ...)
    assignment_basis: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.source_ref, field_name="source_ref")
        if not isinstance(self.domain_key, str) or not _DOMAIN_KEY_PATTERN.match(
            self.domain_key
        ):
            raise KernelError(
                f"invalid domain_key: {self.domain_key!r} must match "
                f"{_DOMAIN_KEY_PATTERN.pattern}"
            )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "domain_key": self.domain_key,
            "assignment_basis": dict(self.assignment_basis),
        }


@dataclass(kw_only=True)
class AccessDenialRecord(KernelRecord):
    """One live-deny state event for a domain, source, or record (PR78).

    Denial state is an append-only event chain keyed to stable identity
    (domain key / source record id / record id), never to a display
    path. The latest event per target is authoritative: ``denied=True``
    refuses the target immediately — even if a pinned PublicationSet or
    its lexical generation still contains the material — and
    ``denied=False`` is the explicit re-authorization (lifting a deny is
    a new event, never a mutation of history). ``supersedes`` names the
    previous event for the same target so a deny→allow→deny cycle never
    collides with an earlier semantically identical record.
    ``denial_basis`` participates in identity so the stored record is
    self-describing, mirroring ``AuthorizationEpochRecord.domain_facts``.
    """

    record_class: ClassVar[str] = _RECORD_CLASS_ACCESS_DENIAL
    record_type: ClassVar[str] = RECORD_TYPES[_RECORD_CLASS_ACCESS_DENIAL]

    target_kind: str
    target_ref: str
    denied: bool
    #: previous AccessDenialRecord for the same target, or None
    supersedes: str | None = None
    #: self-describing denial context (operator, reason, ...)
    denial_basis: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.target_kind not in ACCESS_DENIAL_TARGET_KINDS:
            raise KernelError(
                f"invalid target_kind: {self.target_kind!r}; allowed: "
                f"{sorted(ACCESS_DENIAL_TARGET_KINDS)}"
            )
        if not isinstance(self.denied, bool):
            raise KernelError(f"invalid denied flag: {self.denied!r}")
        if self.target_kind == ACCESS_DENIAL_TARGET_DOMAIN:
            if not isinstance(self.target_ref, str) or not _DOMAIN_KEY_PATTERN.match(
                self.target_ref
            ):
                raise KernelError(
                    f"invalid domain target_ref: {self.target_ref!r} must match "
                    f"{_DOMAIN_KEY_PATTERN.pattern}"
                )
        else:
            validate_record_ref(self.target_ref, field_name="target_ref")
        if self.supersedes is not None:
            validate_record_ref(self.supersedes, field_name="supersedes")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "target_kind": self.target_kind,
            "target_ref": self.target_ref,
            "denied": self.denied,
            "supersedes": self.supersedes,
            "denial_basis": dict(self.denial_basis),
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
