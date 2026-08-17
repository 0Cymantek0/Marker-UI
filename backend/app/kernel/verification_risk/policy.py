"""Pure, conservative verification-risk policy and versioned constants (PR75)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TYPE_CHECKING

from app.kernel.errors import KernelError
from app.utils.canonical import CanonicalValueError, DecimalValue

if TYPE_CHECKING:
    from .records import DependencyDisclosureRecord, VerificationRiskEvidenceRecord

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
    from .records import VerificationRiskEvidenceRecord

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

evaluate_verification_risk = evaluate_verification_risk_policy

__all__ = [
    "VERIFICATION_RISK_SCHEMA_VERSION",
    "RECORD_TYPE_DEPENDENCY_DISCLOSURE",
    "RECORD_TYPE_VERIFICATION_RISK_EVIDENCE",
    "DISCLOSURE_COMPLETE",
    "DISCLOSURE_PARTIAL",
    "DISCLOSURE_UNKNOWN",
    "DISCLOSURE_QUALITIES",
    "DEPENDENCY_INDEPENDENT",
    "DEPENDENCY_CORRELATED",
    "DEPENDENCY_UNKNOWN",
    "DEPENDENCY_STATUSES",
    "SHIFT_MATCHED",
    "SHIFT_SHIFTED",
    "SHIFT_UNKNOWN",
    "SHIFT_STATES",
    "EVIDENCE_MODEL",
    "EVIDENCE_SOURCE_NATIVE",
    "EVIDENCE_DETERMINISTIC",
    "EVIDENCE_HUMAN_REVIEWED",
    "EVIDENCE_MIXED",
    "EVIDENCE_KINDS",
    "AUTHORITY_SOURCE_NATIVE",
    "AUTHORITY_DETERMINISTIC",
    "AUTHORITY_HUMAN_REVIEWED",
    "AUTHORITY_EMPIRICALLY_VALIDATED_MODEL",
    "AUTHORITY_CLASSES",
    "HIGH_RISK_NON_MODEL_AUTHORITY_CLASSES",
    "HIGH_RISK_SOURCE_NATIVE_WORKFLOW",
    "HIGH_RISK_SOURCE_NATIVE_POLICY_ID",
    "HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION",
    "HIGH_RISK_SOURCE_NATIVE_RISK_BOUND",
    "HIGH_RISK_SOURCE_NATIVE_MIN_SAMPLES",
    "OUTCOME_VERIFIED",
    "OUTCOME_ACCEPTED_WITH_WARNING",
    "OUTCOME_UNCERTAIN",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_ABSTAINED",
    "REASON_SUFFICIENT",
    "REASON_INSUFFICIENT",
    "REASON_EXPIRED",
    "REASON_SHIFT",
    "REASON_UNKNOWN_OR_CORRELATED",
    "REASON_RISK_BOUND",
    "REASON_MODEL_ONLY_HIGH_RISK",
    "REASON_CLAIM_AUTHORITY",
    "REASON_SCOPE",
    "VerificationRiskPolicy",
    "VerificationRiskDecision",
    "classify_dependency_status",
    "evaluate_verification_risk_policy",
    "evaluate_verification_risk",
]
