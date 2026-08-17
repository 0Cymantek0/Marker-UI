"""Immutable dependency/risk evidence contracts and identity (PR75)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar

from app.kernel.errors import KernelError
from app.kernel.records import KernelRecord, validate_record_ref
from app.utils.canonical import (
    CanonicalSet,
    CanonicalValueError,
    canonical_json_bytes,
    record_identity_hash,
    to_json_ready,
)
from .policy import (
    AUTHORITY_CLASSES,
    DEPENDENCY_STATUSES,
    DEPENDENCY_UNKNOWN,
    DISCLOSURE_QUALITIES,
    DISCLOSURE_UNKNOWN,
    EVIDENCE_KINDS,
    EVIDENCE_MODEL,
    RECORD_TYPE_DEPENDENCY_DISCLOSURE,
    RECORD_TYPE_VERIFICATION_RISK_EVIDENCE,
    SHIFT_MATCHED,
    SHIFT_STATES,
    VERIFICATION_RISK_SCHEMA_VERSION,
    _number,
    _parse_time,
)

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

__all__ = [
    "DependencyDisclosureRecord",
    "VerificationRiskEvidenceRecord",
]
