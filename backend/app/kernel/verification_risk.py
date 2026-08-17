"""Immutable dependency/risk evidence contracts (PR75).

This module stays deliberately small.  It records dependency disclosure and
empirical risk evidence as immutable kernel inputs, then applies one pure,
conservative policy check.  Model agreement is never an authority shortcut.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, ClassVar

from app.kernel.errors import KernelError, VerificationRiskGateError
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import (
    CanonicalSet,
    CanonicalValueError,
    DecimalValue,
    canonical_json_bytes,
    record_identity_hash,
    to_json_ready,
)


VERIFICATION_RISK_SCHEMA_VERSION = "1.0.0"
RECORD_TYPE_DEPENDENCY_DISCLOSURE = "marker.kernel.dependency_disclosure.v1"
RECORD_TYPE_VERIFICATION_RISK_EVIDENCE = "marker.kernel.verification_risk_evidence.v1"

DISCLOSURE_COMPLETE = "complete"
DISCLOSURE_PARTIAL = "partial"
DISCLOSURE_UNKNOWN = "unknown"
DISCLOSURE_QUALITIES = frozenset(
    {DISCLOSURE_COMPLETE, DISCLOSURE_PARTIAL, DISCLOSURE_UNKNOWN}
)

DEPENDENCY_INDEPENDENT = "independent_looking"
DEPENDENCY_CORRELATED = "correlated"
DEPENDENCY_UNKNOWN = "unknown"
DEPENDENCY_STATUSES = frozenset(
    {DEPENDENCY_INDEPENDENT, DEPENDENCY_CORRELATED, DEPENDENCY_UNKNOWN}
)

SHIFT_MATCHED = "matched"
SHIFT_SHIFTED = "shifted"
SHIFT_UNKNOWN = "unknown"
SHIFT_STATES = frozenset({SHIFT_MATCHED, SHIFT_SHIFTED, SHIFT_UNKNOWN})

EVIDENCE_MODEL = "model"
EVIDENCE_SOURCE_NATIVE = "source_native"
EVIDENCE_DETERMINISTIC = "deterministic"
EVIDENCE_HUMAN_REVIEWED = "human_reviewed"
EVIDENCE_MIXED = "mixed"
EVIDENCE_KINDS = frozenset(
    {
        EVIDENCE_MODEL,
        EVIDENCE_SOURCE_NATIVE,
        EVIDENCE_DETERMINISTIC,
        EVIDENCE_HUMAN_REVIEWED,
        EVIDENCE_MIXED,
    }
)

AUTHORITY_SOURCE_NATIVE = "source_native"
AUTHORITY_DETERMINISTIC = "deterministic"
AUTHORITY_HUMAN_REVIEWED = "human_reviewed"
AUTHORITY_EMPIRICALLY_VALIDATED_MODEL = "empirically_validated_model"
AUTHORITY_CLASSES = frozenset(
    {
        AUTHORITY_SOURCE_NATIVE,
        AUTHORITY_DETERMINISTIC,
        AUTHORITY_HUMAN_REVIEWED,
        AUTHORITY_EMPIRICALLY_VALIDATED_MODEL,
    }
)
HIGH_RISK_NON_MODEL_AUTHORITY_CLASSES = frozenset(
    {AUTHORITY_SOURCE_NATIVE, AUTHORITY_DETERMINISTIC, AUTHORITY_HUMAN_REVIEWED}
)

OUTCOME_VERIFIED = "verified"
OUTCOME_ACCEPTED_WITH_WARNING = "accepted_with_warning"
OUTCOME_UNCERTAIN = "uncertain"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_ABSTAINED = "abstained"

# PR75's first authoritative integration is intentionally narrow.  Only this
# exact workflow activates the commit-time risk gate; all other workflows keep
# PR74's structural proof contract unchanged.
HIGH_RISK_SOURCE_NATIVE_WORKFLOW = "high_risk.source_native.v1"
HIGH_RISK_SOURCE_NATIVE_POLICY_ID = "marker.high_risk.source_native"
HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION = "1"
HIGH_RISK_SOURCE_NATIVE_RISK_BOUND = "0.05"
HIGH_RISK_SOURCE_NATIVE_MIN_SAMPLES = 50

REASON_SUFFICIENT = "sufficient"
REASON_INSUFFICIENT = "insufficient_evidence"
REASON_EXPIRED = "evidence_expired"
REASON_SHIFT = "distribution_shift"
REASON_UNKNOWN_OR_CORRELATED = "unknown_or_correlated_dependency"
REASON_RISK_BOUND = "risk_bound_failure"
REASON_MODEL_ONLY_HIGH_RISK = "model_only_high_risk_consensus"
REASON_CLAIM_AUTHORITY = "claim_authority_class_mismatch"
REASON_SCOPE = "evaluation_scope_mismatch"


def _reject_float(value: Any, path: str = "value") -> None:
    if isinstance(value, float):
        raise KernelError(f"{path} contains float; use DecimalValue or integer")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_float(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_float(item, f"{path}[{index}]")
    elif isinstance(value, (set, frozenset)):
        raise KernelError(f"{path} plain set is not canonical; use an ordered semantic set")


def _freeze(value: Any, path: str = "value") -> Any:
    """Freeze nested metadata while rejecting non-canonical identity values."""
    _reject_float(value, path)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise KernelError(f"{path} mapping keys must be strings")
        return MappingProxyType({key: _freeze(item, f"{path}.{key}") for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{path}[]") for item in value)
    if isinstance(value, CanonicalSet):
        return tuple(_freeze(item, f"{path}[]") for item in value.items)
    try:
        to_json_ready(value)
    except CanonicalValueError as exc:
        raise KernelError(f"{path} is not canonical: {exc}") from None
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _ordered_strings(
    value: Iterable[str] | CanonicalSet | None,
    *,
    field_name: str,
    refs: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or isinstance(value, Mapping):
        raise KernelError(f"{field_name} must be a sequence/set of strings")
    if isinstance(value, CanonicalSet):
        items = list(value.items)
    else:
        try:
            items = list(value)
        except TypeError:
            raise KernelError(f"{field_name} must be iterable") from None
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item:
            raise KernelError(f"{field_name} members must be non-empty strings")
        if refs:
            validate_record_ref(item, field_name=f"{field_name} member")
        result.append(item)
    if len(result) != len(set(result)):
        raise KernelError(f"{field_name} contains duplicate members")
    return tuple(sorted(result, key=lambda item: canonical_json_bytes(item)))


def _optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise KernelError(f"invalid {field_name}: {value!r}")
    return value


def _number(value: Any, *, field_name: str, probability: bool = False) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise KernelError(f"{field_name} must not be bool/float; use DecimalValue or integer")
    if isinstance(value, DecimalValue):
        decimal = Decimal(value.text)
    elif isinstance(value, Decimal):
        decimal = value
    elif isinstance(value, int):
        decimal = Decimal(value)
    elif isinstance(value, str):
        try:
            DecimalValue(value)
            decimal = Decimal(value)
        except (InvalidOperation, CanonicalValueError):
            raise KernelError(f"invalid canonical number for {field_name}: {value!r}") from None
    else:
        raise KernelError(f"invalid number for {field_name}: {value!r}")
    if not decimal.is_finite():
        raise KernelError(f"{field_name} must be finite")
    if probability and not (Decimal("0") <= decimal <= Decimal("1")):
        raise KernelError(f"{field_name} probability must be in [0, 1]")
    return value


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, DecimalValue):
        return Decimal(value.text)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise KernelError(f"unsupported numeric value {value!r}")


def _identity_ok(payload: Mapping[str, Any]) -> None:
    try:
        to_json_ready(dict(payload))
    except CanonicalValueError as exc:
        raise KernelError(f"identity payload is not canonical: {exc}") from None


class _ImmutableRecord:
    """Dataclass-compatible sealing mixin for non-frozen KernelRecord base."""

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError(f"{type(self).__name__} is immutable")
        object.__delattr__(self, name)

    def _seal(self) -> None:
        object.__setattr__(self, "_sealed", True)


@dataclass(kw_only=True)
class DependencyDisclosureRecord(_ImmutableRecord, KernelRecord):
    """Immutable, versioned disclosure of known witness dependencies."""

    record_class: ClassVar[str] = "dependency_disclosure"
    record_type: ClassVar[str] = RECORD_TYPE_DEPENDENCY_DISCLOSURE
    schema_version: ClassVar[str] = VERIFICATION_RISK_SCHEMA_VERSION

    witness_ref: str = ""
    disclosure_quality: str = DISCLOSURE_UNKNOWN
    architecture_family: str | None = None
    base_model_family: str | None = None
    training_sources: tuple[str, ...] = ()
    teacher_lineage: tuple[str, ...] = ()
    shared_dependency_refs: tuple[str, ...] = ()
    renderer_profile: str | None = None
    layout_profile: str | None = None
    detector_profile: str | None = None
    preprocessor_profile: str | None = None
    postprocessor_profile: str | None = None
    prompt_template: str | None = None
    runtime_profile: str | None = None
    quantization_profile: str | None = None
    profile_version: str = VERIFICATION_RISK_SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def training_source_refs(self) -> tuple[str, ...]:
        return self.training_sources

    @property
    def teacher_lineage_refs(self) -> tuple[str, ...]:
        return self.teacher_lineage

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_record_ref(self.witness_ref, field_name="witness_ref")
        if self.disclosure_quality not in DISCLOSURE_QUALITIES:
            raise KernelError(f"invalid disclosure_quality: {self.disclosure_quality!r}")
        for name in (
            "architecture_family",
            "base_model_family",
            "renderer_profile",
            "layout_profile",
            "detector_profile",
            "preprocessor_profile",
            "postprocessor_profile",
            "prompt_template",
            "runtime_profile",
            "quantization_profile",
            "profile_version",
        ):
            _optional_text(getattr(self, name), field_name=name)
        object.__setattr__(self, "training_sources", _ordered_strings(self.training_sources, field_name="training_sources"))
        object.__setattr__(self, "teacher_lineage", _ordered_strings(self.teacher_lineage, field_name="teacher_lineage"))
        object.__setattr__(self, "shared_dependency_refs", _ordered_strings(self.shared_dependency_refs, field_name="shared_dependency_refs", refs=True))
        object.__setattr__(self, "metadata", _freeze(dict(self.metadata), "metadata"))
        _identity_ok(self.identity_payload())
        self._seal()

    def identity_payload(self) -> dict[str, Any]:
        return {
            "witness_ref": self.witness_ref,
            "disclosure_quality": self.disclosure_quality,
            "architecture_family": self.architecture_family,
            "base_model_family": self.base_model_family,
            "training_sources": list(self.training_sources),
            "teacher_lineage": list(self.teacher_lineage),
            "shared_dependency_refs": list(self.shared_dependency_refs),
            "renderer_profile": self.renderer_profile,
            "layout_profile": self.layout_profile,
            "detector_profile": self.detector_profile,
            "preprocessor_profile": self.preprocessor_profile,
            "postprocessor_profile": self.postprocessor_profile,
            "prompt_template": self.prompt_template,
            "runtime_profile": self.runtime_profile,
            "quantization_profile": self.quantization_profile,
            "profile_version": self.profile_version,
            "metadata": _thaw(self.metadata),
        }

    def identity_hash(self) -> str:
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=self.identity_payload(),
        )

    disclosure_id = identity_hash

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, record_id: str) -> "DependencyDisclosureRecord":
        if not isinstance(payload, Mapping):
            raise KernelError(f"disclosure payload must be a mapping, got {payload!r}")
        allowed = {
            "witness_ref", "disclosure_quality", "architecture_family", "base_model_family",
            "training_sources", "teacher_lineage", "shared_dependency_refs", "renderer_profile",
            "layout_profile", "detector_profile", "preprocessor_profile", "postprocessor_profile",
            "prompt_template", "runtime_profile", "quantization_profile", "profile_version", "metadata",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown disclosure payload fields {sorted(unknown)}")
        missing = {"witness_ref", "disclosure_quality"} - set(payload)
        if missing:
            raise KernelError(f"disclosure payload is missing {sorted(missing)}")
        try:
            return cls(record_id=record_id, **dict(payload))
        except TypeError as exc:
            raise KernelError(f"invalid disclosure payload: {exc}") from None


@dataclass(kw_only=True)
class VerificationRiskEvidenceRecord(_ImmutableRecord, KernelRecord):
    """Immutable empirical risk artifact scoped to one claim/workflow slice."""

    record_class: ClassVar[str] = "verification_risk_evidence"
    record_type: ClassVar[str] = RECORD_TYPE_VERIFICATION_RISK_EVIDENCE
    schema_version: ClassVar[str] = VERIFICATION_RISK_SCHEMA_VERSION

    policy_id: str = ""
    policy_revision: str = ""
    workflow_class: str = ""
    claim_authority_class: str = ""
    evaluation_slice_id: str = ""
    witness_refs: tuple[str, ...] = ()
    disclosure_refs: tuple[str, ...] = ()
    sample_count: int = 0
    risk_upper_bound: Any = None
    risk_estimate: Any = None
    joint_error_rate: Any = None
    disagreement_rate: Any = None
    marginal_error_rates: Mapping[str, Any] = field(default_factory=dict)
    joint_error_rates: Mapping[str, Any] = field(default_factory=dict)
    evaluated_at: str = ""
    expires_at: str | None = None
    shift_status: str = SHIFT_MATCHED
    dependency_status: str = DEPENDENCY_UNKNOWN
    evidence_kind: str = EVIDENCE_MODEL
    model_only: bool = True
    consensus: bool = False
    method_id: str = ""
    method_version: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def slice_id(self) -> str:
        return self.evaluation_slice_id

    @property
    def authority_class(self) -> str:
        return self.claim_authority_class

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in (
            "policy_id",
            "policy_revision",
            "workflow_class",
            "claim_authority_class",
            "evaluation_slice_id",
            "evaluated_at",
            "method_id",
            "method_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise KernelError(f"invalid {name}: {value!r}")
        if self.claim_authority_class not in AUTHORITY_CLASSES:
            raise KernelError(
                f"invalid claim_authority_class: {self.claim_authority_class!r}"
            )
        object.__setattr__(self, "witness_refs", _ordered_strings(self.witness_refs, field_name="witness_refs", refs=True))
        object.__setattr__(self, "disclosure_refs", _ordered_strings(self.disclosure_refs, field_name="disclosure_refs", refs=True))
        if not isinstance(self.sample_count, int) or isinstance(self.sample_count, bool) or self.sample_count < 0:
            raise KernelError(f"invalid sample_count: {self.sample_count!r}")
        for name in ("risk_upper_bound", "risk_estimate", "joint_error_rate", "disagreement_rate"):
            object.__setattr__(self, name, _number(getattr(self, name), field_name=name, probability=True))
        evaluated_at = _parse_time(self.evaluated_at)
        if self.expires_at is not None:
            if not isinstance(self.expires_at, str) or not self.expires_at:
                raise KernelError(f"invalid expires_at: {self.expires_at!r}")
            if _parse_time(self.expires_at) < evaluated_at:
                raise KernelError("expires_at cannot precede evaluated_at")
        if self.shift_status not in SHIFT_STATES:
            raise KernelError(f"invalid shift_status: {self.shift_status!r}")
        if self.dependency_status not in DEPENDENCY_STATUSES:
            raise KernelError(f"invalid dependency_status: {self.dependency_status!r}")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise KernelError(f"invalid evidence_kind: {self.evidence_kind!r}")
        for name in ("model_only", "consensus"):
            if not isinstance(getattr(self, name), bool):
                raise KernelError(f"invalid {name}: {getattr(self, name)!r}")
        if self.evidence_kind == EVIDENCE_MODEL:
            if not self.witness_refs:
                raise KernelError("model risk evidence requires witness_refs")
            if not self.disclosure_refs:
                raise KernelError("model risk evidence requires disclosure_refs")
        object.__setattr__(self, "marginal_error_rates", _freeze(dict(self.marginal_error_rates), "marginal_error_rates"))
        object.__setattr__(self, "joint_error_rates", _freeze(dict(self.joint_error_rates), "joint_error_rates"))
        object.__setattr__(self, "metrics", _freeze(dict(self.metrics), "metrics"))
        object.__setattr__(self, "metadata", _freeze(dict(self.metadata), "metadata"))
        _identity_ok(self.identity_payload())
        self._seal()

    def identity_payload(self) -> dict[str, Any]:
        return {
            "policy": {
                "policy_id": self.policy_id,
                "revision": self.policy_revision,
            },
            "workflow_class": self.workflow_class,
            "claim_authority_class": self.claim_authority_class,
            "evaluation_slice_id": self.evaluation_slice_id,
            "witness_refs": list(self.witness_refs),
            "disclosure_refs": list(self.disclosure_refs),
            "sample_count": self.sample_count,
            "risk_upper_bound": self.risk_upper_bound,
            "risk_estimate": self.risk_estimate,
            "joint_error_rate": self.joint_error_rate,
            "disagreement_rate": self.disagreement_rate,
            "marginal_error_rates": _thaw(self.marginal_error_rates),
            "joint_error_rates": _thaw(self.joint_error_rates),
            "evaluated_at": self.evaluated_at,
            "expires_at": self.expires_at,
            "shift_status": self.shift_status,
            "dependency_status": self.dependency_status,
            "evidence_kind": self.evidence_kind,
            "model_only": self.model_only,
            "consensus": self.consensus,
            "method_id": self.method_id,
            "method_version": self.method_version,
            "metrics": _thaw(self.metrics),
            "metadata": _thaw(self.metadata),
        }

    def identity_hash(self) -> str:
        return record_identity_hash(
            record_type=self.record_type,
            schema_version=self.schema_version,
            payload=self.identity_payload(),
        )

    evidence_id = identity_hash

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, record_id: str) -> "VerificationRiskEvidenceRecord":
        if not isinstance(payload, Mapping):
            raise KernelError(f"risk evidence payload must be a mapping, got {payload!r}")
        allowed = {
            "policy", "workflow_class", "claim_authority_class", "evaluation_slice_id", "witness_refs", "disclosure_refs",
            "sample_count", "risk_upper_bound", "risk_estimate", "joint_error_rate", "disagreement_rate",
            "marginal_error_rates", "joint_error_rates", "evaluated_at", "expires_at", "shift_status",
            "dependency_status", "evidence_kind", "model_only", "consensus", "method_id", "method_version",
            "metrics", "metadata",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise KernelError(f"unknown risk evidence payload fields {sorted(unknown)}")
        required = {
            "policy", "workflow_class", "claim_authority_class",
            "evaluation_slice_id", "evaluated_at", "method_id", "method_version",
        } - set(payload)
        if required:
            raise KernelError(f"risk evidence payload is missing {sorted(required)}")
        try:
            values = dict(payload)
            policy = values.pop("policy")
            if not isinstance(policy, Mapping):
                raise KernelError("risk evidence policy must be a mapping")
            unknown_policy = set(policy) - {"policy_id", "revision"}
            if unknown_policy:
                raise KernelError(
                    f"unknown risk evidence policy fields {sorted(unknown_policy)}"
                )
            return cls(
                record_id=record_id,
                policy_id=policy["policy_id"],
                policy_revision=policy["revision"],
                **values,
            )
        except KeyError as exc:
            raise KernelError(
                f"risk evidence policy is missing {exc.args[0]!r}"
            ) from None
        except TypeError as exc:
            raise KernelError(f"invalid risk evidence payload: {exc}") from None


@dataclass(frozen=True)
class VerificationRiskPolicy:
    """Versioned, claim-relative policy configuration."""

    policy_id: str = ""
    policy_revision: str = ""
    workflow_class: str = ""
    evaluation_slice_id: str = ""
    claim_authority_class: str = ""
    risk_bound: Any = None
    min_sample_count: int = 1
    high_risk: bool = True
    require_independent_witnesses: bool = True
    allow_shifted: bool = False

    def __post_init__(self) -> None:
        for name in ("policy_id", "policy_revision", "workflow_class", "evaluation_slice_id", "claim_authority_class"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise KernelError(f"invalid policy {name}")
        if self.claim_authority_class not in AUTHORITY_CLASSES:
            raise KernelError(
                f"invalid policy claim_authority_class: {self.claim_authority_class!r}"
            )
        _number(self.risk_bound, field_name="risk_bound", probability=True)
        if not isinstance(self.min_sample_count, int) or isinstance(self.min_sample_count, bool) or self.min_sample_count < 1:
            raise KernelError("min_sample_count must be positive integer")
        for name in ("high_risk", "require_independent_witnesses", "allow_shifted"):
            if not isinstance(getattr(self, name), bool):
                raise KernelError(f"policy {name} must be bool")


@dataclass(frozen=True)
class VerificationRiskDecision:
    outcome: str
    reason_code: str
    reason: str
    authority_granted: bool
    dependency_status: str
    policy_id: str
    policy_revision: str
    claim_authority_class: str
    sample_count: int
    risk_upper_bound: Any = None

    @property
    def status(self) -> str:
        return self.outcome

    @property
    def is_authoritative(self) -> bool:
        return self.authority_granted


def classify_dependency_status(
    disclosures: Sequence[DependencyDisclosureRecord],
    witness_refs: Sequence[str],
) -> str:
    """Classify known dependence conservatively; never prove independence."""
    refs = tuple(witness_refs)
    if not refs or len(refs) != len(set(refs)):
        return DEPENDENCY_UNKNOWN
    by_ref = {item.witness_ref: item for item in disclosures}
    if len(by_ref) != len(disclosures) or any(ref not in by_ref for ref in refs):
        return DEPENDENCY_UNKNOWN
    if any(item.disclosure_quality != DISCLOSURE_COMPLETE for item in by_ref.values()):
        return DEPENDENCY_UNKNOWN
    profiles = [by_ref[ref] for ref in refs]
    for left_index, left in enumerate(profiles):
        for right in profiles[left_index + 1 :]:
            if left.shared_dependency_refs and right.shared_dependency_refs and set(left.shared_dependency_refs) & set(right.shared_dependency_refs):
                return DEPENDENCY_CORRELATED
            if left.base_model_family and left.base_model_family == right.base_model_family:
                return DEPENDENCY_CORRELATED
            if left.teacher_lineage and set(left.teacher_lineage) & set(right.teacher_lineage):
                return DEPENDENCY_CORRELATED
            for name in ("renderer_profile", "layout_profile", "detector_profile", "preprocessor_profile", "postprocessor_profile", "prompt_template"):
                value = getattr(left, name)
                if value and value == getattr(right, name):
                    return DEPENDENCY_CORRELATED
    return DEPENDENCY_INDEPENDENT


def _parse_time(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                day = date.fromisoformat(value)
                parsed = datetime(day.year, day.month, day.day)
            except ValueError:
                raise KernelError(f"invalid ISO timestamp: {value!r}") from None
    else:
        raise KernelError(f"invalid timestamp: {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decision(
    *, evidence: VerificationRiskEvidenceRecord, policy: VerificationRiskPolicy,
    outcome: str, reason_code: str, reason: str, authority: bool = False,
    dependency_status: str = DEPENDENCY_UNKNOWN, risk_upper_bound: Any = None,
) -> VerificationRiskDecision:
    return VerificationRiskDecision(
        outcome=outcome,
        reason_code=reason_code,
        reason=reason,
        authority_granted=authority,
        dependency_status=dependency_status,
        policy_id=policy.policy_id,
        policy_revision=policy.policy_revision,
        claim_authority_class=policy.claim_authority_class,
        sample_count=evidence.sample_count,
        risk_upper_bound=risk_upper_bound,
    )


def evaluate_verification_risk_policy(
    evidence: VerificationRiskEvidenceRecord,
    policy: VerificationRiskPolicy,
    *,
    disclosures: Sequence[DependencyDisclosureRecord] = (),
    claim_authority_class: str | None = None,
    as_of: str | date | datetime | None = None,
) -> VerificationRiskDecision:
    """Pure conservative policy evaluation for one scoped evidence artifact."""
    if not isinstance(evidence, VerificationRiskEvidenceRecord):
        raise KernelError("evidence must be VerificationRiskEvidenceRecord")
    if not isinstance(policy, VerificationRiskPolicy):
        raise KernelError("policy must be VerificationRiskPolicy")
    requested_class = claim_authority_class or policy.claim_authority_class
    if requested_class != policy.claim_authority_class or evidence.claim_authority_class != requested_class:
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_UNAVAILABLE, reason_code=REASON_CLAIM_AUTHORITY, reason="evidence and policy must name same claim-relative authority class")
    if (
        evidence.policy_id != policy.policy_id
        or evidence.policy_revision != policy.policy_revision
    ):
        return _decision(
            evidence=evidence,
            policy=policy,
            outcome=OUTCOME_UNAVAILABLE,
            reason_code=REASON_SCOPE,
            reason="risk evidence was evaluated under a different policy revision",
        )
    if evidence.workflow_class != policy.workflow_class or evidence.evaluation_slice_id != policy.evaluation_slice_id:
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_UNAVAILABLE, reason_code=REASON_SCOPE, reason="evidence is outside policy workflow or evaluation slice")
    if evidence.sample_count < policy.min_sample_count or evidence.sample_count == 0:
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_UNCERTAIN, reason_code=REASON_INSUFFICIENT, reason="sample support is below policy minimum")
    if evidence.shift_status != SHIFT_MATCHED and not policy.allow_shifted:
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_ABSTAINED, reason_code=REASON_SHIFT, reason="matched-slice evidence cannot be promoted on shifted or unknown distribution")
    if evidence.expires_at is not None:
        if as_of is None:
            return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_UNAVAILABLE, reason_code=REASON_EXPIRED, reason="expiry cannot be established without an as-of time")
        if _parse_time(as_of) > _parse_time(evidence.expires_at):
            return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_UNAVAILABLE, reason_code=REASON_EXPIRED, reason="risk evidence is expired")
    if policy.high_risk and evidence.model_only and (evidence.consensus or evidence.evidence_kind == EVIDENCE_MODEL):
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_ABSTAINED, reason_code=REASON_MODEL_ONLY_HIGH_RISK, reason="model-only evidence cannot establish high-risk verification")
    if (
        policy.high_risk
        and evidence.claim_authority_class
        not in HIGH_RISK_NON_MODEL_AUTHORITY_CLASSES
    ):
        return _decision(
            evidence=evidence,
            policy=policy,
            outcome=OUTCOME_ABSTAINED,
            reason_code=REASON_MODEL_ONLY_HIGH_RISK,
            reason=(
                "high-risk verification requires applicable source-native, "
                "deterministic, or human-reviewed authority"
            ),
        )
    dependency_status = evidence.dependency_status
    if evidence.evidence_kind == EVIDENCE_MODEL and disclosures:
        dependency_status = classify_dependency_status(disclosures, evidence.witness_refs)
    if policy.require_independent_witnesses and evidence.evidence_kind == EVIDENCE_MODEL and dependency_status != DEPENDENCY_INDEPENDENT:
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_ABSTAINED if policy.high_risk else OUTCOME_ACCEPTED_WITH_WARNING, reason_code=REASON_UNKNOWN_OR_CORRELATED, reason="witness lineage is unknown or correlated; witness count is not independence", dependency_status=dependency_status)
    upper = evidence.risk_upper_bound if evidence.risk_upper_bound is not None else evidence.risk_estimate
    if upper is None or policy.risk_bound is None:
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_UNCERTAIN, reason_code=REASON_INSUFFICIENT, reason="no empirical risk bound available for this claim slice", dependency_status=dependency_status)
    if _as_decimal(upper) > _as_decimal(policy.risk_bound):
        return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_ABSTAINED, reason_code=REASON_RISK_BOUND, reason="empirical upper risk bound exceeds policy bound", dependency_status=dependency_status, risk_upper_bound=upper)
    return _decision(evidence=evidence, policy=policy, outcome=OUTCOME_VERIFIED, reason_code=REASON_SUFFICIENT, reason="scoped risk evidence satisfies policy", authority=True, dependency_status=dependency_status, risk_upper_bound=upper)


def _risk_gate_payload_json(
    payload_json: Any, *, record_id: str
) -> Mapping[str, Any]:
    """Decode one prepared/stored record payload for commit-time gating."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise VerificationRiskGateError(
            f"record {record_id!r} has invalid JSON payload: {exc}"
        ) from None
    if not isinstance(payload, Mapping):
        raise VerificationRiskGateError(
            f"record {record_id!r} payload must be an object"
        )
    return payload


def _risk_gate_payload(record: Any, *, record_id: str) -> Mapping[str, Any]:
    return _risk_gate_payload_json(record.payload_json, record_id=record_id)


def _risk_gate_mapping(
    value: Any, *, record_id: str, field_name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationRiskGateError(
            f"assessment {record_id!r} {field_name} must be an object"
        )
    return value


async def check_batch_verification_risk(
    session: Any,
    *,
    workspace_id: str,
    batch_records: Mapping[str, Any],
    current_head: int,
) -> None:
    """Apply PR75's narrow authoritative risk gate to one commit batch.

    Gate activates only for a newly submitted ``verified`` assessment in
    ``high_risk.source_native.v1``.  It runs after PR74 structural proof
    validation, while the commit transaction still holds the writer lock.
    Every other workflow/outcome remains governed by PR74 alone.
    """
    del current_head  # Reserved for future slice-cut checks; no wall-clock use.

    active: list[tuple[str, Mapping[str, Any]]] = []
    for record_id, record in batch_records.items():
        if getattr(record, "record_class", None) != "claim_assessment":
            continue
        payload = _risk_gate_payload(record, record_id=record_id)
        if (
            payload.get("outcome") == OUTCOME_VERIFIED
            and payload.get("workflow_class") == HIGH_RISK_SOURCE_NATIVE_WORKFLOW
        ):
            active.append((record_id, payload))
    if not active:
        return

    # Pull only referenced committed rows.  Batch records overlay committed
    # state, matching PR74's visibility semantics.
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow

    refs: set[str] = set()
    for record_id, payload in active:
        evidence_refs = payload.get("evidence_refs") or ()
        if isinstance(evidence_refs, str) or not isinstance(evidence_refs, Sequence):
            raise VerificationRiskGateError(
                f"assessment {record_id!r} evidence_refs must be a sequence"
            )
        refs.update(ref for ref in evidence_refs if isinstance(ref, str))

    committed_records: dict[str, tuple[str, str]] = {}
    if refs:
        rows = (
            await session.execute(
                select(
                    KernelRecordRow.id,
                    KernelRecordRow.record_class,
                    KernelRecordRow.payload_json,
                ).where(
                    KernelRecordRow.workspace_id == workspace_id,
                    KernelRecordRow.id.in_(sorted(refs)),
                )
            )
        ).all()
        committed_records = {
            row.id: (row.record_class, row.payload_json) for row in rows
        }

    # A new assessment's proof support records are normally all in its batch,
    # but load committed support rows too so the check remains explicit about
    # the authority relation it consumes.
    support_rows = (
        await session.execute(
            select(KernelRecordRow.id, KernelRecordRow.payload_json).where(
                KernelRecordRow.workspace_id == workspace_id,
                KernelRecordRow.record_class == "proof_support",
            )
        )
    ).all()
    committed_supports: dict[str, Mapping[str, Any]] = {}
    for support_id, payload_json in support_rows:
        committed_supports[support_id] = _risk_gate_payload_json(
            payload_json,
            record_id=support_id,
        )

    for assessment_id, assessment in active:
        policy = _risk_gate_mapping(
            assessment.get("policy"), record_id=assessment_id, field_name="policy"
        )
        if (
            policy.get("policy_id") != HIGH_RISK_SOURCE_NATIVE_POLICY_ID
            or policy.get("revision") != HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION
        ):
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must use policy "
                f"{HIGH_RISK_SOURCE_NATIVE_POLICY_ID}/"
                f"{HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION}"
            )

        declared_context = _risk_gate_mapping(
            assessment.get("declared_context"),
            record_id=assessment_id,
            field_name="declared_context",
        )
        risk_context = _risk_gate_mapping(
            declared_context.get("verification_risk"),
            record_id=assessment_id,
            field_name="declared_context.verification_risk",
        )
        expected_context_fields = {
            "evidence_ref",
            "evaluation_slice_id",
            "as_of",
        }
        if set(risk_context) != expected_context_fields:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} declared_context.verification_risk "
                f"must name exactly {sorted(expected_context_fields)}"
            )
        evidence_ref = risk_context["evidence_ref"]
        evaluation_slice_id = risk_context["evaluation_slice_id"]
        as_of = risk_context["as_of"]
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} verification-risk evidence_ref "
                "must be a non-empty record id"
            )
        if not isinstance(evaluation_slice_id, str) or not evaluation_slice_id:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} verification-risk "
                "evaluation_slice_id must be a non-empty string"
            )
        if not isinstance(as_of, str) or not as_of:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} verification-risk as_of "
                "must be a non-empty ISO timestamp"
            )

        evidence_refs = assessment.get("evidence_refs") or ()
        if evidence_ref not in evidence_refs:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} does not declare risk evidence "
                f"{evidence_ref!r} in evidence_refs"
            )

        # Compose proof supports from this batch and committed history.
        supports: list[tuple[str, Mapping[str, Any]]] = []
        for support_id, record in batch_records.items():
            if getattr(record, "record_class", None) != "proof_support":
                continue
            payload = _risk_gate_payload(record, record_id=support_id)
            if payload.get("holder_ref") == assessment_id:
                supports.append((support_id, payload))
        for support_id, payload in committed_supports.items():
            if payload.get("holder_ref") == assessment_id:
                supports.append((support_id, payload))
        support_refs = {
            payload.get("evidence_ref")
            for _support_id, payload in supports
            if isinstance(payload.get("evidence_ref"), str)
        }
        risk_supports = [
            payload
            for _support_id, payload in supports
            if payload.get("evidence_ref") == evidence_ref
        ]
        if not risk_supports:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} does not carry risk evidence "
                f"{evidence_ref!r} in proof support"
            )
        if any(payload.get("role") != "input" for payload in risk_supports):
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must present risk evidence "
                f"{evidence_ref!r} as role=input; empirical risk calibrates "
                "authority but is not itself an independent witness"
            )

        # At least one independent source-native fact must be supported in
        # addition to the statistical artifact.  Observations/model consensus
        # cannot substitute for a native fact.
        class_by_ref: dict[str, str] = {
            record_id: getattr(record, "record_class", "")
            for record_id, record in batch_records.items()
        }
        class_by_ref.update(
            {record_id: record_class for record_id, (record_class, _payload) in committed_records.items()}
        )
        native_fact_supports = [
            payload
            for _support_id, payload in supports
            if class_by_ref.get(payload.get("evidence_ref")) == "native_fact"
        ]
        if not native_fact_supports:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} requires a supported native_fact "
                "in addition to verification-risk evidence"
            )
        if any(payload.get("role") != "witness" for payload in native_fact_supports):
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must present its native_fact "
                "authority as role=witness"
            )

        risk_record = batch_records.get(evidence_ref)
        if risk_record is not None:
            risk_class = getattr(risk_record, "record_class", None)
            risk_payload = _risk_gate_payload(risk_record, record_id=evidence_ref)
        else:
            committed = committed_records.get(evidence_ref)
            if committed is None:
                raise VerificationRiskGateError(
                    f"risk evidence {evidence_ref!r} is not visible in workspace "
                    f"{workspace_id!r}"
                )
            risk_class, risk_payload = committed
        if risk_class != "verification_risk_evidence":
            raise VerificationRiskGateError(
                f"risk evidence reference {evidence_ref!r} resolves to "
                f"{risk_class!r}, not verification_risk_evidence"
            )
        try:
            evidence = VerificationRiskEvidenceRecord.from_payload(
                risk_payload, record_id=evidence_ref
            )
            risk_policy = VerificationRiskPolicy(
                policy_id=HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
                policy_revision=HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
                workflow_class=HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
                evaluation_slice_id=evaluation_slice_id,
                claim_authority_class=AUTHORITY_SOURCE_NATIVE,
                risk_bound=HIGH_RISK_SOURCE_NATIVE_RISK_BOUND,
                min_sample_count=HIGH_RISK_SOURCE_NATIVE_MIN_SAMPLES,
                high_risk=True,
                require_independent_witnesses=True,
                allow_shifted=False,
            )
        except (KernelError, TypeError, ValueError) as exc:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} is invalid: {exc}"
            ) from None

        if evidence.evidence_kind != EVIDENCE_SOURCE_NATIVE:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} must be source-native"
            )
        if evidence.model_only or evidence.consensus:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} cannot be model-only consensus"
            )
        if (
            evidence.policy_id != HIGH_RISK_SOURCE_NATIVE_POLICY_ID
            or evidence.policy_revision != HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION
            or evidence.workflow_class != HIGH_RISK_SOURCE_NATIVE_WORKFLOW
            or evidence.evaluation_slice_id != evaluation_slice_id
            or evidence.claim_authority_class != AUTHORITY_SOURCE_NATIVE
        ):
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} policy/workflow/slice/authority "
                "does not match high-risk source-native assessment"
            )
        if evidence.risk_upper_bound is None:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} must declare an upper risk bound"
            )
        try:
            decision = evaluate_verification_risk_policy(
                evidence,
                risk_policy,
                claim_authority_class=AUTHORITY_SOURCE_NATIVE,
                as_of=as_of,
            )
        except (KernelError, TypeError, ValueError) as exc:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} could not be evaluated: {exc}"
            ) from None
        if decision.outcome != OUTCOME_VERIFIED or not decision.authority_granted:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} failed policy: "
                f"{decision.reason_code} ({decision.reason})"
            )


evaluate_verification_risk = evaluate_verification_risk_policy


__all__ = [
    "AUTHORITY_CLASSES", "AUTHORITY_DETERMINISTIC", "AUTHORITY_EMPIRICALLY_VALIDATED_MODEL", "AUTHORITY_HUMAN_REVIEWED", "AUTHORITY_SOURCE_NATIVE",
    "DEPENDENCY_CORRELATED", "DEPENDENCY_INDEPENDENT", "DEPENDENCY_UNKNOWN", "DISCLOSURE_COMPLETE", "DISCLOSURE_PARTIAL", "DISCLOSURE_UNKNOWN",
    "DependencyDisclosureRecord", "VerificationRiskDecision", "VerificationRiskEvidenceRecord", "VerificationRiskPolicy",
    "HIGH_RISK_SOURCE_NATIVE_MIN_SAMPLES", "HIGH_RISK_SOURCE_NATIVE_POLICY_ID", "HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION", "HIGH_RISK_SOURCE_NATIVE_RISK_BOUND", "HIGH_RISK_SOURCE_NATIVE_WORKFLOW",
    "EVIDENCE_DETERMINISTIC", "EVIDENCE_HUMAN_REVIEWED", "EVIDENCE_MIXED", "EVIDENCE_MODEL", "EVIDENCE_SOURCE_NATIVE",
    "OUTCOME_ABSTAINED", "OUTCOME_ACCEPTED_WITH_WARNING", "OUTCOME_UNAVAILABLE", "OUTCOME_UNCERTAIN", "OUTCOME_VERIFIED",
    "REASON_CLAIM_AUTHORITY", "REASON_EXPIRED", "REASON_INSUFFICIENT", "REASON_MODEL_ONLY_HIGH_RISK", "REASON_RISK_BOUND", "REASON_SHIFT", "REASON_UNKNOWN_OR_CORRELATED", "REASON_SUFFICIENT",
    "SHIFT_MATCHED", "SHIFT_SHIFTED", "SHIFT_UNKNOWN", "check_batch_verification_risk", "classify_dependency_status", "evaluate_verification_risk", "evaluate_verification_risk_policy",
]
