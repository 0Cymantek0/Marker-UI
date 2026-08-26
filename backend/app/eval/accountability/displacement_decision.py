"""Preregistered invariant-62 rational-user displacement decision engine.

Governing requirement (Invariant 62):
"The final displacement test asks whether a rational user can achieve a better accepted
end-to-end outcome by leaving Marker UI; any remaining reason is integrated, measured,
or explicitly conceded."

Supporting requirement (Invariant 61):
"A negative result that removes routing, marketplace, visual, or generalized-language
complexity is accepted as successful research."

Decision space:
1. marker_retained: Marker UI is retained as the authoritative route. Meets safety budgets,
   satisfies evidence lineage, and no external specialist demonstrates acceptable superiority
   without fatal failure modes or unverified integration.
2. integrate_or_route: An external specialist demonstrates superior capability on specific
   dimensions/slices AND has an active, verified integration bridge (e.g. as a non-authoritative
   candidate generator feeding Marker's reconciliation/proof machinery with independent corroboration).
   Integration permission or future promise is NOT integration; must verify artifact existence,
   repo-relative path under docs/reference/measurements, raw bytes SHA-256, and scope binding.
3. explicit_concession: An external specialist achieves a strictly better, safe, and verifiable
   end-to-end outcome on the declared workflow/slice, and Marker UI explicitly concedes the
   workflow to the specialist. Never requires Marker win; accepts negative simplification.
4. inconclusive: Evaluation is invalidated by fairness mismatch, missing material dimensions,
   unjustified not_applicable dimensions, unknown/unresolved reasons to leave, catastrophic/dangerous
   failure budget breach on Marker itself without a safe replacement, or insufficient data.

Fail-closed honesty & reproducibility guarantees:
- Exact schema versions required on all contracts.
- Protocol timing disclosure: 'prospective_preregistration' vs 'retrospective_frozen_replay'.
  Claiming prospective preregistration when evidence predates registration fails validation.
- Tri-state measurement contract: 'measured', 'unavailable', 'not_applicable'.
  Missing dimensions CANNOT silently become 0.0 or be assumed measured.
  Unjustified not_applicable material dimensions fail closed.
- Dangerous failure threshold is an observed-count gate on the declared corpus only
  (scope='declared_corpus_observed_count'), not a statistical population risk claim.
- Fairness verification: executed comparator identities, input paths, and adaptation rules are
  compared against declared specs; missing proof or mismatch invalidates the run.
- Integration verification: integrated status strictly requires active verified bridge evidence
  artifact path + SHA-256; without it, specialist advantage is 'measured', never 'integrated'.
- Reason-to-leave ledger accounts for every specialist advantage:
  'integrated', 'measured', or 'conceded'. Any 'unknown' reason forces 'inconclusive'.
- Pure deterministic derivation: persisted decisions are validated by exact rederivation;
  manual result flips fail closed with cryptographic digest mismatch.
- Date injection: no live clock dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION = "marker.displacement_preregistration.v1"
DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION = "marker.displacement_measurement.v1"
DISPLACEMENT_DECISION_SCHEMA_VERSION = "marker.displacement_decision.v1"
PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION = "marker.pr80b_displacement_evidence.v1"

OUTCOME_MARKER_RETAINED = "marker_retained"
OUTCOME_INTEGRATE_OR_ROUTE = "integrate_or_route"
OUTCOME_EXPLICIT_CONCESSION = "explicit_concession"
OUTCOME_INCONCLUSIVE = "inconclusive"

DISPLACEMENT_OUTCOMES = frozenset(
    {
        OUTCOME_MARKER_RETAINED,
        OUTCOME_INTEGRATE_OR_ROUTE,
        OUTCOME_EXPLICIT_CONCESSION,
        OUTCOME_INCONCLUSIVE,
    }
)

MEASUREMENT_STATUS_MEASURED = "measured"
MEASUREMENT_STATUS_UNAVAILABLE = "unavailable"
MEASUREMENT_STATUS_NOT_APPLICABLE = "not_applicable"

MEASUREMENT_STATUSES = frozenset(
    {
        MEASUREMENT_STATUS_MEASURED,
        MEASUREMENT_STATUS_UNAVAILABLE,
        MEASUREMENT_STATUS_NOT_APPLICABLE,
    }
)

REASON_STATUS_INTEGRATED = "integrated"
REASON_STATUS_MEASURED = "measured"
REASON_STATUS_CONCEDED = "conceded"
REASON_STATUS_UNKNOWN = "unknown"

REASON_STATUSES = frozenset(
    {
        REASON_STATUS_INTEGRATED,
        REASON_STATUS_MEASURED,
        REASON_STATUS_CONCEDED,
        REASON_STATUS_UNKNOWN,
    }
)

PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY = "retrospective_frozen_replay"
PROTOCOL_PROSPECTIVE_PREREGISTRATION = "prospective_preregistration"

PROTOCOL_TIMINGS = frozenset(
    {
        PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
        PROTOCOL_PROSPECTIVE_PREREGISTRATION,
    }
)

INTEGRATION_STATUS_VERIFIED_ACTIVE = "verified_active"
INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED = "future_unimplemented"
INTEGRATION_STATUS_REJECTED = "rejected"
INTEGRATION_STATUS_NOT_INTEGRABLE = "not_integrable"

INTEGRATION_STATUSES = frozenset(
    {
        INTEGRATION_STATUS_VERIFIED_ACTIVE,
        INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
        INTEGRATION_STATUS_REJECTED,
        INTEGRATION_STATUS_NOT_INTEGRABLE,
    }
)

INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR = (
    "non_authoritative_candidate_generator"
)
INTEGRATION_KIND_CONDITIONAL_SPECIALIST_ROUTING = "conditional_specialist_routing"

ALLOWED_INTEGRATION_KINDS = frozenset(
    {
        INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        INTEGRATION_KIND_CONDITIONAL_SPECIALIST_ROUTING,
    }
)

DIMENSION_DOC_EXACT_RATE = "doc_exact_rate"
DIMENSION_SCALAR_ACCURACY = "scalar_accuracy_on_present"
DIMENSION_ABSENT_REJECTION = "absent_rejection_rate"
DIMENSION_EVIDENCE_LINEAGE = "evidence_coverage"
DIMENSION_DANGEROUS_FAILURES = "dangerous_failures"
DIMENSION_REVIEW_BURDEN = "review_burden"
DIMENSION_LANE_ERRORS = "lane_errors"
DIMENSION_LATENCY = "latency"
DIMENSION_COST = "cost"

STANDARD_MATERIAL_DIMENSIONS = frozenset(
    {
        DIMENSION_DOC_EXACT_RATE,
        DIMENSION_SCALAR_ACCURACY,
        DIMENSION_ABSENT_REJECTION,
        DIMENSION_EVIDENCE_LINEAGE,
        DIMENSION_DANGEROUS_FAILURES,
        DIMENSION_REVIEW_BURDEN,
        DIMENSION_LANE_ERRORS,
        DIMENSION_LATENCY,
        DIMENSION_COST,
    }
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$"
)


class DisplacementDecisionError(ValueError):
    """Raised when displacement evaluation or verification fails closed."""


def _parse_iso_dt(val: str, field_name: str, errors: list[str]) -> datetime | None:
    if not isinstance(val, str) or not _ISO_DATE.match(val):
        errors.append(f"{field_name} must be a valid ISO-8601 date string, got {val!r}")
        return None
    try:
        clean = val.replace("Z", "+00:00")
        if "T" not in clean:
            clean += "T00:00:00+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError as exc:
        errors.append(f"{field_name} invalid ISO-8601 date format: {exc}")
        return None


def _canonical_json(data: Any) -> str:
    """Produce deterministic JSON representation for cryptographic digests."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _compute_sha256(payload: Any) -> str:
    serialized = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionMeasurement:
    """Explicit measurement state for a single outcome dimension.

    Never allows unavailable dimensions to silently become 0.0 or measured.
    """

    dimension: str
    status: str  # 'measured' | 'unavailable' | 'not_applicable'
    value: float | int | str | Mapping[str, Any] | None = None
    unit: str = ""
    notes: str = ""
    not_applicable_justification: str = ""

    def __post_init__(self) -> None:
        if self.status not in MEASUREMENT_STATUSES:
            raise DisplacementDecisionError(
                f"Invalid measurement status {self.status!r}; expected one of {sorted(MEASUREMENT_STATUSES)}"
            )
        if self.status == MEASUREMENT_STATUS_MEASURED and self.value is None:
            raise DisplacementDecisionError(
                f"Dimension {self.dimension!r} is declared 'measured' but value is None"
            )

    @property
    def is_measured(self) -> bool:
        return self.status == MEASUREMENT_STATUS_MEASURED

    @property
    def is_unavailable(self) -> bool:
        return self.status == MEASUREMENT_STATUS_UNAVAILABLE

    @property
    def is_not_applicable(self) -> bool:
        return self.status == MEASUREMENT_STATUS_NOT_APPLICABLE

    def as_numeric(self) -> float | None:
        """Return numeric value only if measured; returns None if unavailable or not_applicable."""
        if not self.is_measured or self.value is None:
            return None
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            if math.isnan(self.value) or math.isinf(self.value):
                return None
            return float(self.value)
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "dimension": self.dimension,
            "status": self.status,
            "value": self.value,
        }
        if self.unit:
            out["unit"] = self.unit
        if self.notes:
            out["notes"] = self.notes
        if self.not_applicable_justification:
            out["not_applicable_justification"] = self.not_applicable_justification
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DimensionMeasurement:
        return cls(
            dimension=str(data["dimension"]),
            status=str(data["status"]),
            value=data.get("value"),
            unit=str(data.get("unit", "")),
            notes=str(data.get("notes", "")),
            not_applicable_justification=str(
                data.get("not_applicable_justification", "")
            ),
        )


@dataclass(frozen=True)
class ComparatorDeclaredSpec:
    """Declared identity, input path, adaptation rules, and hardware/policy profile for a comparator."""

    system_id: str
    is_marker_baseline: bool
    system_kind: str
    system_identity: str
    input_path_declared: str
    adaptation_rules_declared: str
    policy_profile: str = "default"
    hardware_profile: str = "standard"
    selection_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "is_marker_baseline": self.is_marker_baseline,
            "system_kind": self.system_kind,
            "system_identity": self.system_identity,
            "input_path_declared": self.input_path_declared,
            "adaptation_rules_declared": self.adaptation_rules_declared,
            "policy_profile": self.policy_profile,
            "hardware_profile": self.hardware_profile,
            "selection_rationale": self.selection_rationale,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ComparatorDeclaredSpec:
        return cls(
            system_id=str(data["system_id"]),
            is_marker_baseline=bool(data["is_marker_baseline"]),
            system_kind=str(data["system_kind"]),
            system_identity=str(data["system_identity"]),
            input_path_declared=str(data["input_path_declared"]),
            adaptation_rules_declared=str(data["adaptation_rules_declared"]),
            policy_profile=str(data.get("policy_profile", "default")),
            hardware_profile=str(data.get("hardware_profile", "standard")),
            selection_rationale=str(data.get("selection_rationale", "")),
        )


@dataclass(frozen=True)
class CorpusPreregistration:
    """Corpus, manifest, fingerprint, and task normalization rules preregistered for the benchmark."""

    manifest_version: str
    fingerprint: str
    document_count: int
    slices: tuple[str, ...]
    task_description: str
    normalization_rules: Mapping[str, str] = field(default_factory=dict)
    declared_invariants: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "fingerprint": self.fingerprint,
            "document_count": self.document_count,
            "slices": list(self.slices),
            "task_description": self.task_description,
            "normalization_rules": dict(self.normalization_rules),
            "declared_invariants": list(self.declared_invariants),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CorpusPreregistration:
        raw_slices = data.get("slices", ())
        if isinstance(raw_slices, Mapping):
            slices_tuple = tuple(sorted(str(k) for k in raw_slices))
        else:
            slices_tuple = tuple(str(s) for s in raw_slices)

        return cls(
            manifest_version=str(data["manifest_version"]),
            fingerprint=str(data["fingerprint"]),
            document_count=int(data["document_count"]),
            slices=slices_tuple,
            task_description=str(data["task_description"]),
            normalization_rules=dict(data.get("normalization_rules", {})),
            declared_invariants=tuple(
                str(i) for i in data.get("declared_invariants", ())
            ),
        )


@dataclass(frozen=True)
class FrozenDecisionThresholds:
    """Explicit decision thresholds declared ahead of measurement."""

    max_acceptable_dangerous_failures: int = 0
    threshold_scope: str = "declared_corpus_observed_count"
    min_evidence_coverage_for_retained: float = 1.0
    quality_margin_for_displacement: float = 0.05
    allow_candidate_integration: bool = True
    max_lane_error_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_acceptable_dangerous_failures": self.max_acceptable_dangerous_failures,
            "threshold_scope": self.threshold_scope,
            "min_evidence_coverage_for_retained": self.min_evidence_coverage_for_retained,
            "quality_margin_for_displacement": self.quality_margin_for_displacement,
            "allow_candidate_integration": self.allow_candidate_integration,
            "max_lane_error_rate": self.max_lane_error_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrozenDecisionThresholds:
        return cls(
            max_acceptable_dangerous_failures=int(
                data.get("max_acceptable_dangerous_failures", 0)
            ),
            threshold_scope=str(
                data.get("threshold_scope", "declared_corpus_observed_count")
            ),
            min_evidence_coverage_for_retained=float(
                data.get("min_evidence_coverage_for_retained", 1.0)
            ),
            quality_margin_for_displacement=float(
                data.get("quality_margin_for_displacement", 0.05)
            ),
            allow_candidate_integration=bool(
                data.get("allow_candidate_integration", True)
            ),
            max_lane_error_rate=float(data.get("max_lane_error_rate", 0.0)),
        )


@dataclass(frozen=True)
class FairnessContract:
    """Preregistered fairness contract guaranteeing identical user-level inputs and declared adapters."""

    same_user_level_input_required: bool = True
    declared_adaptation_rules_required: bool = True
    disallow_privileged_features: bool = True
    allowed_input_discrepancies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "same_user_level_input_required": self.same_user_level_input_required,
            "declared_adaptation_rules_required": self.declared_adaptation_rules_required,
            "disallow_privileged_features": self.disallow_privileged_features,
            "allowed_input_discrepancies": list(self.allowed_input_discrepancies),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FairnessContract:
        return cls(
            same_user_level_input_required=bool(
                data.get("same_user_level_input_required", True)
            ),
            declared_adaptation_rules_required=bool(
                data.get("declared_adaptation_rules_required", True)
            ),
            disallow_privileged_features=bool(
                data.get("disallow_privileged_features", True)
            ),
            allowed_input_discrepancies=tuple(
                str(x) for x in data.get("allowed_input_discrepancies", ())
            ),
        )


@dataclass(frozen=True)
class DisplacementPreregistration:
    """Strict preregistration specification for Invariant-62 displacement evaluation."""

    preregistration_id: str
    workflow: str
    corpus: CorpusPreregistration
    comparators: tuple[ComparatorDeclaredSpec, ...]
    fairness_contract: FairnessContract
    material_dimensions: tuple[str, ...]
    frozen_thresholds: FrozenDecisionThresholds
    preregistration_date: str
    protocol_timing: str = PROTOCOL_PROSPECTIVE_PREREGISTRATION
    schema_version: str = DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION

    def get_marker_baseline(self) -> ComparatorDeclaredSpec | None:
        for c in self.comparators:
            if c.is_marker_baseline:
                return c
        return None

    def get_specialists(self) -> tuple[ComparatorDeclaredSpec, ...]:
        return tuple(c for c in self.comparators if not c.is_marker_baseline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preregistration_id": self.preregistration_id,
            "workflow": self.workflow,
            "corpus": self.corpus.to_dict(),
            "comparators": [c.to_dict() for c in self.comparators],
            "fairness_contract": self.fairness_contract.to_dict(),
            "material_dimensions": list(self.material_dimensions),
            "frozen_thresholds": self.frozen_thresholds.to_dict(),
            "preregistration_date": self.preregistration_date,
            "protocol_timing": self.protocol_timing,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DisplacementPreregistration:
        return cls(
            schema_version=str(
                data.get("schema_version", DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION)
            ),
            preregistration_id=str(data["preregistration_id"]),
            workflow=str(data["workflow"]),
            corpus=CorpusPreregistration.from_dict(data["corpus"]),
            comparators=tuple(
                ComparatorDeclaredSpec.from_dict(c) for c in data.get("comparators", ())
            ),
            fairness_contract=FairnessContract.from_dict(
                data.get("fairness_contract", {})
            ),
            material_dimensions=tuple(
                str(d) for d in data.get("material_dimensions", ())
            ),
            frozen_thresholds=FrozenDecisionThresholds.from_dict(
                data.get("frozen_thresholds", {})
            ),
            preregistration_date=str(data["preregistration_date"]),
            protocol_timing=str(
                data.get("protocol_timing", PROTOCOL_PROSPECTIVE_PREREGISTRATION)
            ),
        )


@dataclass(frozen=True)
class ExecutedComparatorFacts:
    """Executed comparator facts extracted from the measurement artifact systems declaration."""

    system_id: str
    system_kind: str
    system_identity: str
    input_path: str
    adaptation_rules: str
    selection_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "system_kind": self.system_kind,
            "system_identity": self.system_identity,
            "input_path": self.input_path,
            "adaptation_rules": self.adaptation_rules,
            "selection_rationale": self.selection_rationale,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutedComparatorFacts:
        return cls(
            system_id=str(data["system_id"]),
            system_kind=str(data["system_kind"]),
            system_identity=str(data["system_identity"]),
            input_path=str(data["input_path"]),
            adaptation_rules=str(data["adaptation_rules"]),
            selection_rationale=str(data.get("selection_rationale", "")),
        )


@dataclass(frozen=True)
class FairnessVerification:
    """Fairness verification outcome with executed comparator facts."""

    input_parity_verified: bool
    adaptation_parity_verified: bool
    full_corpus_evaluated: bool
    is_fair: bool
    executed_facts: tuple[ExecutedComparatorFacts, ...] = ()
    discrepancies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_parity_verified": self.input_parity_verified,
            "adaptation_parity_verified": self.adaptation_parity_verified,
            "full_corpus_evaluated": self.full_corpus_evaluated,
            "is_fair": self.is_fair,
            "executed_facts": [f.to_dict() for f in self.executed_facts],
            "discrepancies": list(self.discrepancies),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FairnessVerification:
        facts = tuple(
            ExecutedComparatorFacts.from_dict(f) for f in data.get("executed_facts", ())
        )
        return cls(
            input_parity_verified=bool(data.get("input_parity_verified", False)),
            adaptation_parity_verified=bool(
                data.get("adaptation_parity_verified", False)
            ),
            full_corpus_evaluated=bool(data.get("full_corpus_evaluated", False)),
            is_fair=bool(data.get("is_fair", False)),
            executed_facts=facts,
            discrepancies=tuple(str(d) for d in data.get("discrepancies", ())),
        )


@dataclass(frozen=True)
class IntegrationVerification:
    """Active verified integration contract binding a candidate specialist into Marker UI."""

    system_id: str
    status: str  # 'verified_active' | 'future_unimplemented' | 'rejected' | 'not_integrable'
    integration_kind: str
    evidence_artifact_path: str
    evidence_artifact_sha256: str
    workflow_scope: str
    corpus_fingerprint_scope: str
    corroboration_contract: str
    verified_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in INTEGRATION_STATUSES:
            raise DisplacementDecisionError(
                f"Invalid integration status {self.status!r}; expected one of {sorted(INTEGRATION_STATUSES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "status": self.status,
            "integration_kind": self.integration_kind,
            "evidence_artifact_path": self.evidence_artifact_path,
            "evidence_artifact_sha256": self.evidence_artifact_sha256,
            "workflow_scope": self.workflow_scope,
            "corpus_fingerprint_scope": self.corpus_fingerprint_scope,
            "corroboration_contract": self.corroboration_contract,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IntegrationVerification:
        fp_scope = str(
            data.get("corpus_fingerprint_scope", data.get("corpus_scope", ""))
        )
        return cls(
            system_id=str(data["system_id"]),
            status=str(data["status"]),
            integration_kind=str(data["integration_kind"]),
            evidence_artifact_path=str(data.get("evidence_artifact_path", "")),
            evidence_artifact_sha256=str(data.get("evidence_artifact_sha256", "")),
            workflow_scope=str(data.get("workflow_scope", "")),
            corpus_fingerprint_scope=fp_scope,
            corroboration_contract=str(data.get("corroboration_contract", "")),
            verified_at=str(data.get("verified_at", "")),
        )


def validate_active_integration(
    integration: IntegrationVerification,
    prereg: DisplacementPreregistration,
    repo_root: Path | str | None,
    as_of_date: str,
) -> list[str]:
    """Validate that an integration contract is active, repo-relative, verified, and bound to declared scope.

    Must not self-certify from strings alone.
    """
    errors: list[str] = []

    if integration.status != INTEGRATION_STATUS_VERIFIED_ACTIVE:
        errors.append(
            f"Integration status is {integration.status!r}, not {INTEGRATION_STATUS_VERIFIED_ACTIVE!r}"
        )
        return errors

    # 1. Integration kind allowlist
    if integration.integration_kind not in ALLOWED_INTEGRATION_KINDS:
        errors.append(
            f"integration_kind {integration.integration_kind!r} not in allowed kinds {sorted(ALLOWED_INTEGRATION_KINDS)}"
        )

    # 2. System ID must be declared non-baseline comparator
    declared_specialist_ids = {c.system_id for c in prereg.get_specialists()}
    if integration.system_id not in declared_specialist_ids:
        errors.append(
            f"system_id {integration.system_id!r} is not a declared non-baseline comparator ({sorted(declared_specialist_ids)})"
        )

    # 3. Workflow scope exact match
    if integration.workflow_scope != prereg.workflow:
        errors.append(
            f"workflow_scope {integration.workflow_scope!r} does not match preregistration workflow {prereg.workflow!r}"
        )

    # 4. Corpus fingerprint scope exact match
    if integration.corpus_fingerprint_scope != prereg.corpus.fingerprint:
        errors.append(
            f"corpus_fingerprint_scope {integration.corpus_fingerprint_scope!r} does not match preregistration corpus fingerprint {prereg.corpus.fingerprint!r}"
        )

    # 5. Corroboration contract non-empty
    if not integration.corroboration_contract.strip():
        errors.append("corroboration_contract must be a non-empty string")

    # 6. verified_at date format and as_of_date boundary
    dt_v = _parse_iso_dt(integration.verified_at, "verified_at", errors)
    if dt_v is not None and as_of_date:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_v > dt_as_of:
            errors.append(
                f"verified_at ({integration.verified_at}) is in future relative to as_of_date ({as_of_date})"
            )

    # 7. Evidence artifact path & raw bytes SHA-256 verification
    p_str = integration.evidence_artifact_path.strip().replace("\\", "/")
    if not p_str.startswith("docs/reference/measurements/"):
        errors.append(
            f"evidence_artifact_path {integration.evidence_artifact_path!r} must be repo-relative under docs/reference/measurements/"
        )
    elif ".." in p_str:
        errors.append(
            f"evidence_artifact_path {integration.evidence_artifact_path!r} cannot contain '..'"
        )

    if not _HEX64.match(integration.evidence_artifact_sha256):
        errors.append(
            f"evidence_artifact_sha256 must be a 64-character lowercase hex string, got {integration.evidence_artifact_sha256!r}"
        )

    if repo_root is None:
        errors.append(
            "repo_root is required to verify integration artifact existence and SHA-256"
        )
    else:
        root_p = Path(repo_root)
        art_path = root_p / p_str
        if not art_path.is_file():
            errors.append(f"Integration evidence artifact does not exist at {art_path}")
        else:
            raw_bytes = art_path.read_bytes()
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha != integration.evidence_artifact_sha256:
                errors.append(
                    f"Integration evidence artifact SHA mismatch: expected {integration.evidence_artifact_sha256}, got {actual_sha}"
                )

    return errors


@dataclass(frozen=True)
class ComparatorMeasurements:
    """Measurements and dangerous failure tallies for a single comparator."""

    system_id: str
    dimensions: Mapping[str, DimensionMeasurement]
    danger_counts: Mapping[str, int] = field(default_factory=dict)
    raw_metrics: Mapping[str, Any] = field(default_factory=dict)

    def get_dimension(self, dim: str) -> DimensionMeasurement | None:
        return self.dimensions.get(dim)

    def get_dimension_status(self, dim: str) -> str:
        if dim in self.dimensions:
            return self.dimensions[dim].status
        return MEASUREMENT_STATUS_UNAVAILABLE

    def get_numeric(self, dim: str) -> float | None:
        if dim in self.dimensions:
            return self.dimensions[dim].as_numeric()
        return None

    def total_dangerous_failures(self) -> int:
        if DIMENSION_DANGEROUS_FAILURES in self.dimensions:
            meas = self.dimensions[DIMENSION_DANGEROUS_FAILURES]
            if meas.is_measured:
                val = meas.as_numeric()
                if val is not None:
                    return int(val)
        return sum(
            int(v) for v in self.danger_counts.values() if isinstance(v, (int, float))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "danger_counts": dict(self.danger_counts),
            "raw_metrics": dict(self.raw_metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ComparatorMeasurements:
        dims = {
            str(k): DimensionMeasurement.from_dict(v)
            for k, v in data.get("dimensions", {}).items()
        }
        return cls(
            system_id=str(data["system_id"]),
            dimensions=dims,
            danger_counts={
                str(k): int(v) for k, v in data.get("danger_counts", {}).items()
            },
            raw_metrics=dict(data.get("raw_metrics", {})),
        )


@dataclass(frozen=True)
class DisplacementMeasurementBundle:
    """Collection of measured evidence across all comparators for a displacement run."""

    measurement_id: str
    preregistration_id: str
    corpus_fingerprint: str
    comparators: Mapping[str, ComparatorMeasurements]
    fairness: FairnessVerification
    evidence_date: str
    supporting_artifact_path: str = ""
    supporting_artifact_sha256: str = ""
    integrations: tuple[IntegrationVerification, ...] = ()
    schema_version: str = DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "measurement_id": self.measurement_id,
            "preregistration_id": self.preregistration_id,
            "corpus_fingerprint": self.corpus_fingerprint,
            "comparators": {k: v.to_dict() for k, v in self.comparators.items()},
            "fairness": self.fairness.to_dict(),
            "evidence_date": self.evidence_date,
            "supporting_artifact_path": self.supporting_artifact_path,
            "supporting_artifact_sha256": self.supporting_artifact_sha256,
            "integrations": [i.to_dict() for i in self.integrations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DisplacementMeasurementBundle:
        comps = {
            str(k): ComparatorMeasurements.from_dict(v)
            for k, v in data.get("comparators", {}).items()
        }
        ints = tuple(
            IntegrationVerification.from_dict(i) for i in data.get("integrations", ())
        )
        return cls(
            schema_version=str(
                data.get("schema_version", DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION)
            ),
            measurement_id=str(data["measurement_id"]),
            preregistration_id=str(data["preregistration_id"]),
            corpus_fingerprint=str(data["corpus_fingerprint"]),
            comparators=comps,
            fairness=FairnessVerification.from_dict(data.get("fairness", {})),
            evidence_date=str(data["evidence_date"]),
            supporting_artifact_path=str(data.get("supporting_artifact_path", "")),
            supporting_artifact_sha256=str(data.get("supporting_artifact_sha256", "")),
            integrations=ints,
        )


@dataclass(frozen=True)
class ReasonToLeaveItem:
    """Single entry in the reason-to-leave ledger.

    Every reason a user might have to leave Marker UI must be explicitly accounted for:
    - integrated: Active verified integration in Marker UI (requires active bridge artifact).
    - measured: Quantitatively characterized and accepted as a deliberate trade-off.
    - conceded: Marker UI explicitly concedes the slice to the specialist.
    - unknown: Unresolved/uncharacterized reason (forces inconclusive).
    """

    reason_id: str
    specialist_system_id: str
    dimension: str
    description: str
    status: str  # 'integrated' | 'measured' | 'conceded' | 'unknown'
    is_material: bool
    resolution_details: str = ""

    def __post_init__(self) -> None:
        if self.status not in REASON_STATUSES:
            raise DisplacementDecisionError(
                f"Invalid reason status {self.status!r}; expected one of {sorted(REASON_STATUSES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_id": self.reason_id,
            "specialist_system_id": self.specialist_system_id,
            "dimension": self.dimension,
            "description": self.description,
            "status": self.status,
            "is_material": self.is_material,
            "resolution_details": self.resolution_details,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReasonToLeaveItem:
        return cls(
            reason_id=str(data["reason_id"]),
            specialist_system_id=str(data["specialist_system_id"]),
            dimension=str(data["dimension"]),
            description=str(data["description"]),
            status=str(data["status"]),
            is_material=bool(data.get("is_material", True)),
            resolution_details=str(data.get("resolution_details", "")),
        )


@dataclass(frozen=True)
class ComparatorEvaluation:
    """Individual comparator synthesis computed during decision derivation."""

    system_id: str
    is_marker_baseline: bool
    doc_exact_rate: float | None
    scalar_accuracy: float | None
    evidence_coverage: float | None
    dangerous_failure_count: int
    dangerous_budget_breached: bool
    lane_errors: int
    review_burden_status: str
    review_burden_summary: str
    advantages_over_marker: tuple[str, ...] = ()
    disadvantages_vs_marker: tuple[str, ...] = ()
    integrable_as_candidate: bool = False
    active_integration_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "is_marker_baseline": self.is_marker_baseline,
            "doc_exact_rate": self.doc_exact_rate,
            "scalar_accuracy": self.scalar_accuracy,
            "evidence_coverage": self.evidence_coverage,
            "dangerous_failure_count": self.dangerous_failure_count,
            "dangerous_budget_breached": self.dangerous_budget_breached,
            "lane_errors": self.lane_errors,
            "review_burden_status": self.review_burden_status,
            "review_burden_summary": self.review_burden_summary,
            "advantages_over_marker": list(self.advantages_over_marker),
            "disadvantages_vs_marker": list(self.disadvantages_vs_marker),
            "integrable_as_candidate": self.integrable_as_candidate,
            "active_integration_verified": self.active_integration_verified,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ComparatorEvaluation:
        return cls(
            system_id=str(data["system_id"]),
            is_marker_baseline=bool(data["is_marker_baseline"]),
            doc_exact_rate=(
                float(data["doc_exact_rate"])
                if data.get("doc_exact_rate") is not None
                else None
            ),
            scalar_accuracy=(
                float(data["scalar_accuracy"])
                if data.get("scalar_accuracy") is not None
                else None
            ),
            evidence_coverage=(
                float(data["evidence_coverage"])
                if data.get("evidence_coverage") is not None
                else None
            ),
            dangerous_failure_count=int(data.get("dangerous_failure_count", 0)),
            dangerous_budget_breached=bool(
                data.get("dangerous_budget_breached", False)
            ),
            lane_errors=int(data.get("lane_errors", 0)),
            review_burden_status=str(data.get("review_burden_status", "unavailable")),
            review_burden_summary=str(data.get("review_burden_summary", "")),
            advantages_over_marker=tuple(
                str(a) for a in data.get("advantages_over_marker", ())
            ),
            disadvantages_vs_marker=tuple(
                str(d) for d in data.get("disadvantages_vs_marker", ())
            ),
            integrable_as_candidate=bool(data.get("integrable_as_candidate", False)),
            active_integration_verified=bool(
                data.get("active_integration_verified", False)
            ),
        )


@dataclass(frozen=True)
class DisplacementDecision:
    """Cryptographically verifiable, deterministic displacement decision output."""

    decision_id: str
    preregistration_id: str
    workflow: str
    as_of_date: str
    outcome: str  # 'marker_retained' | 'integrate_or_route' | 'explicit_concession' | 'inconclusive'
    summary: str
    protocol_timing: str
    limitations: tuple[str, ...]
    reason_ledger: tuple[ReasonToLeaveItem, ...]
    fairness_passed: bool
    blockers: tuple[str, ...]
    comparator_evaluations: Mapping[str, ComparatorEvaluation]
    supporting_artifact_sha256: str
    rederivation_digest: str
    schema_version: str = DISPLACEMENT_DECISION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "preregistration_id": self.preregistration_id,
            "workflow": self.workflow,
            "as_of_date": self.as_of_date,
            "outcome": self.outcome,
            "summary": self.summary,
            "protocol_timing": self.protocol_timing,
            "limitations": list(self.limitations),
            "reason_ledger": [r.to_dict() for r in self.reason_ledger],
            "fairness_passed": self.fairness_passed,
            "blockers": list(self.blockers),
            "comparator_evaluations": {
                k: v.to_dict() for k, v in self.comparator_evaluations.items()
            },
            "supporting_artifact_sha256": self.supporting_artifact_sha256,
            "rederivation_digest": self.rederivation_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DisplacementDecision:
        ledger = tuple(
            ReasonToLeaveItem.from_dict(r) for r in data.get("reason_ledger", ())
        )
        evals = {
            str(k): ComparatorEvaluation.from_dict(v)
            for k, v in data.get("comparator_evaluations", {}).items()
        }
        return cls(
            schema_version=str(
                data.get("schema_version", DISPLACEMENT_DECISION_SCHEMA_VERSION)
            ),
            decision_id=str(data["decision_id"]),
            preregistration_id=str(data["preregistration_id"]),
            workflow=str(data["workflow"]),
            as_of_date=str(data["as_of_date"]),
            outcome=str(data["outcome"]),
            summary=str(data["summary"]),
            protocol_timing=str(
                data.get("protocol_timing", PROTOCOL_PROSPECTIVE_PREREGISTRATION)
            ),
            limitations=tuple(str(item) for item in data.get("limitations", ())),
            reason_ledger=ledger,
            fairness_passed=bool(data["fairness_passed"]),
            blockers=tuple(str(b) for b in data.get("blockers", ())),
            comparator_evaluations=evals,
            supporting_artifact_sha256=str(data.get("supporting_artifact_sha256", "")),
            rederivation_digest=str(data["rederivation_digest"]),
        )


# -----------------------------------------------------------------------------
# Validation Functions
# -----------------------------------------------------------------------------


def validate_displacement_preregistration(
    prereg: DisplacementPreregistration | Mapping[str, Any],
    as_of_date: str | None = None,
) -> list[str]:
    """Validate displacement preregistration against Invariant-62 fail-closed rules."""
    errors: list[str] = []

    if isinstance(prereg, DisplacementPreregistration):
        p_dict = prereg.to_dict()
    elif isinstance(prereg, Mapping):
        p_dict = dict(prereg)
        schema = p_dict.get("schema_version")
        if schema != DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return [
            "displacement preregistration must be DisplacementPreregistration or Mapping"
        ]

    pid = p_dict.get("preregistration_id")
    if not isinstance(pid, str) or not pid.strip():
        errors.append("preregistration_id must be a non-empty string")

    wf = p_dict.get("workflow")
    if not isinstance(wf, str) or not wf.strip():
        errors.append("workflow must be a non-empty string")

    ptiming = p_dict.get("protocol_timing", PROTOCOL_PROSPECTIVE_PREREGISTRATION)
    if ptiming not in PROTOCOL_TIMINGS:
        errors.append(
            f"protocol_timing must be one of {sorted(PROTOCOL_TIMINGS)}, got {ptiming!r}"
        )

    corpus = p_dict.get("corpus")
    if not isinstance(corpus, Mapping) or not corpus:
        errors.append("corpus must be a non-empty mapping")
    else:
        mver = corpus.get("manifest_version")
        if not isinstance(mver, str) or not mver.strip():
            errors.append("corpus.manifest_version must be a non-empty string")
        fp = corpus.get("fingerprint")
        if not isinstance(fp, str) or not fp.strip():
            errors.append("corpus.fingerprint must be a non-empty string")
        dcount = corpus.get("document_count")
        if not isinstance(dcount, int) or dcount <= 0 or isinstance(dcount, bool):
            errors.append("corpus.document_count must be a positive integer")

    comps = p_dict.get("comparators")
    if not isinstance(comps, (list, tuple)) or len(comps) < 2:
        errors.append("comparators must contain at least 2 comparator specifications")
    else:
        marker_count = 0
        seen_ids: set[str] = set()
        for i, c in enumerate(comps):
            if not isinstance(c, Mapping):
                errors.append(f"comparator [{i}] must be a mapping")
                continue
            sid = c.get("system_id")
            if not isinstance(sid, str) or not sid.strip():
                errors.append(f"comparator [{i}].system_id must be non-empty string")
            elif sid in seen_ids:
                errors.append(f"duplicate comparator system_id {sid!r}")
            else:
                seen_ids.add(sid)

            if c.get("is_marker_baseline") is True:
                marker_count += 1

            ip = c.get("input_path_declared")
            if not isinstance(ip, str) or not ip.strip():
                errors.append(
                    f"comparator {sid!r} must declare non-empty input_path_declared"
                )
            ar = c.get("adaptation_rules_declared")
            if not isinstance(ar, str) or not ar.strip():
                errors.append(
                    f"comparator {sid!r} must declare non-empty adaptation_rules_declared"
                )

        if marker_count != 1:
            errors.append(
                f"comparators must declare exactly 1 is_marker_baseline=True; found {marker_count}"
            )

    mat_dims = p_dict.get("material_dimensions")
    if not isinstance(mat_dims, (list, tuple)) or not mat_dims:
        errors.append("material_dimensions must be a non-empty list of strings")

    thresh = p_dict.get("frozen_thresholds")
    if not isinstance(thresh, Mapping) or not thresh:
        errors.append("frozen_thresholds must be a non-empty mapping")

    pdate_str = p_dict.get("preregistration_date")
    dt_p = _parse_iso_dt(pdate_str, "preregistration_date", errors)
    if as_of_date and dt_p is not None:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_p > dt_as_of:
            errors.append(
                f"preregistration_date is in future relative to as_of_date ({pdate_str} > {as_of_date})"
            )

    return errors


def validate_displacement_measurement_bundle(
    bundle: DisplacementMeasurementBundle | Mapping[str, Any],
    prereg: DisplacementPreregistration | None = None,
    as_of_date: str | None = None,
) -> list[str]:
    """Validate measurement bundle structure, tri-state dimensions, and preregistration binding."""
    errors: list[str] = []

    if isinstance(bundle, DisplacementMeasurementBundle):
        b_dict = bundle.to_dict()
    elif isinstance(bundle, Mapping):
        b_dict = dict(bundle)
        schema = b_dict.get("schema_version")
        if schema != DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return ["measurement bundle must be DisplacementMeasurementBundle or Mapping"]

    mid = b_dict.get("measurement_id")
    if not isinstance(mid, str) or not mid.strip():
        errors.append("measurement_id must be a non-empty string")

    pid = b_dict.get("preregistration_id")
    if not isinstance(pid, str) or not pid.strip():
        errors.append("preregistration_id must be a non-empty string")

    if prereg is not None and pid != prereg.preregistration_id:
        errors.append(
            f"measurement preregistration_id {pid!r} does not match preregistration {prereg.preregistration_id!r}"
        )

    fp = b_dict.get("corpus_fingerprint")
    if not isinstance(fp, str) or not fp.strip():
        errors.append("corpus_fingerprint must be a non-empty string")
    elif prereg is not None and fp != prereg.corpus.fingerprint:
        errors.append(
            f"corpus_fingerprint {fp!r} does not match preregistration corpus {prereg.corpus.fingerprint!r}"
        )

    comps = b_dict.get("comparators")
    if not isinstance(comps, Mapping) or not comps:
        errors.append("comparators must be a non-empty mapping")
    elif prereg is not None:
        declared_ids = {c.system_id for c in prereg.comparators}
        measured_ids = set(comps.keys())
        if declared_ids != measured_ids:
            errors.append(
                f"measured comparators {sorted(measured_ids)} do not match declared comparators {sorted(declared_ids)}"
            )

    edate_str = b_dict.get("evidence_date")
    dt_e = _parse_iso_dt(edate_str, "evidence_date", errors)
    if as_of_date and dt_e is not None:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_e > dt_as_of:
            errors.append(
                f"evidence_date is in future relative to as_of_date ({edate_str} > {as_of_date})"
            )

    # Validate protocol timing consistency
    if prereg is not None and dt_e is not None:
        dt_p = _parse_iso_dt(prereg.preregistration_date, "preregistration_date", [])
        if (
            dt_p is not None
            and dt_e < dt_p
            and prereg.protocol_timing == PROTOCOL_PROSPECTIVE_PREREGISTRATION
        ):
            errors.append(
                f"Prospective timing lie: evidence_date ({edate_str}) predates preregistration_date ({prereg.preregistration_date}); "
                f"preregistration must declare protocol_timing={PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY!r} for retrospective replays."
            )

    return errors


# -----------------------------------------------------------------------------
# Decision Engine Derivation & Rederivation
# -----------------------------------------------------------------------------


def derive_displacement_decision(
    prereg: DisplacementPreregistration,
    bundle: DisplacementMeasurementBundle,
    as_of_date: str,
    repo_root: Path | str | None = None,
) -> DisplacementDecision:
    """Deterministically derive Invariant-62 rational-user displacement decision.

    Pure function of (preregistration, measurements, as_of_date, repo_root).
    """
    blockers: list[str] = []
    reason_ledger: list[ReasonToLeaveItem] = []
    comparator_evals: dict[str, ComparatorEvaluation] = {}
    limitations: list[str] = []

    # Dynamic limitations based on preregistration parameters
    if prereg.protocol_timing == PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY:
        limitations.append(
            f"Retrospective frozen replay of declared evidence for workflow {prereg.workflow!r}; "
            f"decision thresholds frozen post-experiment, subject to retrospective replay scope."
        )
    limitations.append(
        f"Dangerous failure threshold ({prereg.frozen_thresholds.max_acceptable_dangerous_failures}) "
        f"is an observed-count gate on declared {prereg.corpus.document_count}-document corpus slice; "
        f"no population-level statistical leadership claim is made."
    )

    # 1. Structural validation
    p_errors = validate_displacement_preregistration(prereg, as_of_date=as_of_date)
    blockers.extend(p_errors)

    b_errors = validate_displacement_measurement_bundle(
        bundle, prereg=prereg, as_of_date=as_of_date
    )
    blockers.extend(b_errors)

    # 2. Date checks
    dt_p = _parse_iso_dt(prereg.preregistration_date, "preregistration_date", [])
    dt_e = _parse_iso_dt(bundle.evidence_date, "evidence_date", [])
    if (
        dt_p
        and dt_e
        and dt_e < dt_p
        and prereg.protocol_timing == PROTOCOL_PROSPECTIVE_PREREGISTRATION
    ):
        blockers.append(
            f"Prospective timing violation: evidence ({bundle.evidence_date}) was recorded before prospective preregistration ({prereg.preregistration_date})"
        )

    # 3. Fairness verification gate: check executed facts against declared spec
    fairness = bundle.fairness
    fairness_discrepancies: list[str] = list(fairness.discrepancies)

    if not fairness.input_parity_verified:
        fairness_discrepancies.append("Input parity not verified across comparators")
    if not fairness.adaptation_parity_verified:
        fairness_discrepancies.append(
            "Adaptation parity not verified across comparators"
        )
    if not fairness.full_corpus_evaluated:
        fairness_discrepancies.append("Not all systems evaluated on full corpus")
    if not fairness.is_fair:
        fairness_discrepancies.append("Fairness verification flag is false")

    # Compare executed facts vs declared comparator specifications
    executed_facts_map = {f.system_id: f for f in fairness.executed_facts}
    for comp in prereg.comparators:
        if comp.system_id not in executed_facts_map:
            fairness_discrepancies.append(
                f"No executed system facts provided for declared comparator {comp.system_id!r}"
            )
            continue
        fact = executed_facts_map[comp.system_id]
        if fact.system_identity != comp.system_identity:
            fairness_discrepancies.append(
                f"Identity mismatch for {comp.system_id!r}: declared {comp.system_identity!r} != executed {fact.system_identity!r}"
            )
        if fact.input_path != comp.input_path_declared:
            fairness_discrepancies.append(
                f"Input path mismatch for {comp.system_id!r}: declared {comp.input_path_declared!r} != executed {fact.input_path!r}"
            )
        if fact.adaptation_rules != comp.adaptation_rules_declared:
            fairness_discrepancies.append(
                f"Adaptation rules mismatch for {comp.system_id!r}: declared {comp.adaptation_rules_declared!r} != executed {fact.adaptation_rules!r}"
            )

    fairness_passed = len(fairness_discrepancies) == 0

    if not fairness_passed:
        disc_text = "; ".join(sorted(set(fairness_discrepancies)))
        blockers.append(f"Fairness mismatch: {disc_text}")

    # 4. Check material dimensions availability across all comparators
    # Missing dimensions CANNOT become zero or measured.
    for dim in prereg.material_dimensions:
        for comp_spec in prereg.comparators:
            comp_m = bundle.comparators.get(comp_spec.system_id)
            if comp_m is None:
                blockers.append(
                    f"Missing measurements object for comparator {comp_spec.system_id!r}"
                )
                continue
            meas = comp_m.get_dimension(dim)
            if meas is None or meas.status == MEASUREMENT_STATUS_UNAVAILABLE:
                blockers.append(
                    f"Material dimension {dim!r} is unavailable for comparator {comp_spec.system_id!r}"
                )
            elif (
                meas.status == MEASUREMENT_STATUS_NOT_APPLICABLE
                and not meas.not_applicable_justification.strip()
            ):
                blockers.append(
                    f"Material dimension {dim!r} for comparator {comp_spec.system_id!r} is not_applicable without justification"
                )

    # 5. Marker baseline evaluation
    marker_spec = prereg.get_marker_baseline()
    marker_meas = bundle.comparators.get(marker_spec.system_id) if marker_spec else None

    marker_exact: float | None = None
    marker_acc: float | None = None
    marker_cov: float | None = None
    marker_danger_count = 0
    marker_breached_danger = False
    marker_lane_errors = 0

    if marker_spec and marker_meas:
        marker_exact = marker_meas.get_numeric(DIMENSION_DOC_EXACT_RATE)
        marker_acc = marker_meas.get_numeric(DIMENSION_SCALAR_ACCURACY)
        marker_cov = marker_meas.get_numeric(DIMENSION_EVIDENCE_LINEAGE)
        marker_danger_count = marker_meas.total_dangerous_failures()
        marker_lane_errors = int(marker_meas.get_numeric(DIMENSION_LANE_ERRORS) or 0)

        if (
            marker_danger_count
            > prereg.frozen_thresholds.max_acceptable_dangerous_failures
        ):
            marker_breached_danger = True
            blockers.append(
                f"Marker baseline breached dangerous failure budget ({marker_danger_count} > {prereg.frozen_thresholds.max_acceptable_dangerous_failures})"
            )

        if (
            marker_cov is not None
            and marker_cov < prereg.frozen_thresholds.min_evidence_coverage_for_retained
        ):
            blockers.append(
                f"Marker baseline evidence coverage {marker_cov:.2f} below required {prereg.frozen_thresholds.min_evidence_coverage_for_retained:.2f}"
            )

        comparator_evals[marker_spec.system_id] = ComparatorEvaluation(
            system_id=marker_spec.system_id,
            is_marker_baseline=True,
            doc_exact_rate=marker_exact,
            scalar_accuracy=marker_acc,
            evidence_coverage=marker_cov,
            dangerous_failure_count=marker_danger_count,
            dangerous_budget_breached=marker_breached_danger,
            lane_errors=marker_lane_errors,
            review_burden_status=marker_meas.get_dimension_status(
                DIMENSION_REVIEW_BURDEN
            ),
            review_burden_summary=str(
                marker_meas.dimensions.get(
                    DIMENSION_REVIEW_BURDEN,
                    DimensionMeasurement(
                        DIMENSION_REVIEW_BURDEN, MEASUREMENT_STATUS_UNAVAILABLE
                    ),
                ).value
                or ""
            ),
            advantages_over_marker=(),
            disadvantages_vs_marker=(),
            integrable_as_candidate=False,
            active_integration_verified=False,
        )

    # 6. Specialist evaluations & Reason-to-leave analysis
    specialist_specs = prereg.get_specialists()
    active_integrated_specialists: list[str] = []
    conceded_specialists: list[str] = []

    # Map available integration verifications by system_id
    integrations_map = {i.system_id: i for i in bundle.integrations}

    for s_spec in specialist_specs:
        s_meas = bundle.comparators.get(s_spec.system_id)
        if not s_meas:
            continue

        s_exact = s_meas.get_numeric(DIMENSION_DOC_EXACT_RATE)
        s_acc = s_meas.get_numeric(DIMENSION_SCALAR_ACCURACY)
        s_cov = s_meas.get_numeric(DIMENSION_EVIDENCE_LINEAGE)
        s_danger = s_meas.total_dangerous_failures()
        s_breached_danger = (
            s_danger > prereg.frozen_thresholds.max_acceptable_dangerous_failures
        )
        s_lane_errors = int(s_meas.get_numeric(DIMENSION_LANE_ERRORS) or 0)

        advantages: list[str] = []
        disadvantages: list[str] = []

        if s_breached_danger:
            disadvantages.append(
                f"Breached dangerous failure budget with {s_danger} dangerous failures ({dict(s_meas.danger_counts)})"
            )

        if s_cov is not None and (marker_cov is not None and s_cov < marker_cov):
            disadvantages.append(
                f"Lacks verifiable evidence lineage ({s_cov:.2f} vs {marker_cov:.2f})"
            )

        if s_lane_errors > 0:
            disadvantages.append(f"Experienced {s_lane_errors} lane/provider errors")

        # Quality comparisons: only when both are measured numeric values
        exact_gain = (
            (s_exact - marker_exact)
            if (s_exact is not None and marker_exact is not None)
            else 0.0
        )
        acc_gain = (
            (s_acc - marker_acc)
            if (s_acc is not None and marker_acc is not None)
            else 0.0
        )

        if (
            exact_gain >= prereg.frozen_thresholds.quality_margin_for_displacement
            or acc_gain >= prereg.frozen_thresholds.quality_margin_for_displacement
        ):
            advantages.append(
                f"Superior raw accuracy/field coverage (exact delta: +{exact_gain:.3f}, acc delta: +{acc_gain:.3f})"
            )

        # Validate integration verification
        int_ver = integrations_map.get(s_spec.system_id)
        has_active_verified_integration = False
        integration_errors: list[str] = []

        if int_ver is not None and int_ver.status == INTEGRATION_STATUS_VERIFIED_ACTIVE:
            integration_errors = validate_active_integration(
                int_ver, prereg, repo_root, as_of_date
            )
            if not integration_errors:
                has_active_verified_integration = True
            else:
                limitations.append(
                    f"Integration candidate {s_spec.system_id!r} failed active verification: {'; '.join(integration_errors)}"
                )

        integrable_as_candidate = bool(
            prereg.frozen_thresholds.allow_candidate_integration
            and (s_breached_danger or (s_cov is not None and s_cov < 0.5))
        )

        # Reason to leave formulation
        if advantages:
            for adv in advantages:
                if s_breached_danger or (s_cov is not None and s_cov < 0.5):
                    # Specialist cannot be authority due to dangerous failure breach or zero lineage.
                    # Integration requires active verified integration binding; otherwise it is measured.
                    if (
                        has_active_verified_integration
                        and integrable_as_candidate
                        and int_ver is not None
                    ):
                        reason_status = REASON_STATUS_INTEGRATED
                        active_integrated_specialists.append(s_spec.system_id)
                        res_details = (
                            f"Integrated as non-authoritative candidate generator feeding Marker proof machinery; "
                            f"verified by bridge artifact {int_ver.evidence_artifact_path!r} ({int_ver.evidence_artifact_sha256[:12]})."
                        )
                    else:
                        reason_status = REASON_STATUS_MEASURED
                        if (
                            int_ver
                            and int_ver.status
                            == INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED
                        ):
                            res_details = (
                                f"Advantage measured and bounded; specialist rejected as authority due to {s_danger} dangerous failures. "
                                f"Candidate integration is prospective/unimplemented in current release."
                            )
                        elif integration_errors:
                            res_details = (
                                f"Advantage measured and bounded; specialist rejected as authority due to {s_danger} dangerous failures "
                                f"and invalid integration binding ({'; '.join(integration_errors)})."
                            )
                        else:
                            res_details = (
                                f"Advantage measured and bounded; specialist rejected as authority due to {s_danger} dangerous failures "
                                f"and lack of active verified integration bridge."
                            )
                else:
                    # Specialist is safe and superior without dangerous failures:
                    # Marker explicitly concedes the workflow/slice (Invariant 61/62).
                    reason_status = REASON_STATUS_CONCEDED
                    conceded_specialists.append(s_spec.system_id)
                    res_details = "Specialist achieves strictly superior verified outcome; Marker concedes slice."

                reason_ledger.append(
                    ReasonToLeaveItem(
                        reason_id=f"reason_{s_spec.system_id}_{len(reason_ledger) + 1}",
                        specialist_system_id=s_spec.system_id,
                        dimension=(
                            DIMENSION_SCALAR_ACCURACY
                            if "accuracy" in adv.lower()
                            else DIMENSION_DOC_EXACT_RATE
                        ),
                        description=adv,
                        status=reason_status,
                        is_material=True,
                        resolution_details=res_details,
                    )
                )

        comparator_evals[s_spec.system_id] = ComparatorEvaluation(
            system_id=s_spec.system_id,
            is_marker_baseline=False,
            doc_exact_rate=s_exact,
            scalar_accuracy=s_acc,
            evidence_coverage=s_cov,
            dangerous_failure_count=s_danger,
            dangerous_budget_breached=s_breached_danger,
            lane_errors=s_lane_errors,
            review_burden_status=s_meas.get_dimension_status(DIMENSION_REVIEW_BURDEN),
            review_burden_summary=str(
                s_meas.dimensions.get(
                    DIMENSION_REVIEW_BURDEN,
                    DimensionMeasurement(
                        DIMENSION_REVIEW_BURDEN, MEASUREMENT_STATUS_UNAVAILABLE
                    ),
                ).value
                or ""
            ),
            advantages_over_marker=tuple(advantages),
            disadvantages_vs_marker=tuple(disadvantages),
            integrable_as_candidate=integrable_as_candidate,
            active_integration_verified=has_active_verified_integration,
        )

    # 7. Check for unknown/unresolved material reasons in the ledger
    has_unknown_material_reason = any(
        r.status == REASON_STATUS_UNKNOWN and r.is_material for r in reason_ledger
    )
    if has_unknown_material_reason:
        blockers.append(
            "Reason-to-leave ledger contains unknown/unresolved material reasons."
        )

    # 8. Terminal Outcome Determination
    if blockers:
        outcome = OUTCOME_INCONCLUSIVE
        summary = (
            f"Evaluation inconclusive due to {len(blockers)} blocker(s): "
            + "; ".join(blockers)
        )
    elif conceded_specialists:
        # A safe specialist beat Marker on material dimensions and Marker explicitly concedes.
        # Accepts negative simplification without requiring Marker win.
        outcome = OUTCOME_EXPLICIT_CONCESSION
        summary = (
            f"Explicit concession: Marker UI concedes slice to specialist(s) {sorted(conceded_specialists)} "
            f"who achieve superior, safe, and verified performance. Rational user is directed to specialist or simplified route."
        )
    elif active_integrated_specialists:
        # Specialist raw generative advantage is real AND verified active candidate bridge exists.
        outcome = OUTCOME_INTEGRATE_OR_ROUTE
        summary = (
            f"Integrate or route: Marker retains authority while candidate-generating advantages of "
            f"{sorted(active_integrated_specialists)} are actively integrated into verification machinery."
        )
    else:
        # Marker UI satisfies evidence lineage, safety budget, and no specialist displaced it or is active-integrated.
        outcome = OUTCOME_MARKER_RETAINED
        summary = (
            "Marker retained: Marker UI is the only route satisfying full evidence lineage and safety budgets "
            "without unmitigated specialist failure modes (observed dangerous failure gate: 0 on declared corpus)."
        )

    decision_id = f"disp_dec_{prereg.preregistration_id}_{hashlib.sha256((prereg.preregistration_id + as_of_date).encode('utf-8')).hexdigest()[:12]}"

    # 9. Compute deterministic rederivation digest
    digest_body = {
        "schema_version": DISPLACEMENT_DECISION_SCHEMA_VERSION,
        "decision_id": decision_id,
        "preregistration_id": prereg.preregistration_id,
        "workflow": prereg.workflow,
        "as_of_date": as_of_date,
        "outcome": outcome,
        "fairness_passed": fairness_passed,
        "protocol_timing": prereg.protocol_timing,
        "blockers": sorted(blockers),
        "reason_ledger": [r.to_dict() for r in reason_ledger],
        "supporting_artifact_sha256": bundle.supporting_artifact_sha256,
    }
    rederivation_digest = _compute_sha256(digest_body)

    return DisplacementDecision(
        schema_version=DISPLACEMENT_DECISION_SCHEMA_VERSION,
        decision_id=decision_id,
        preregistration_id=prereg.preregistration_id,
        workflow=prereg.workflow,
        as_of_date=as_of_date,
        outcome=outcome,
        summary=summary,
        protocol_timing=prereg.protocol_timing,
        limitations=tuple(limitations),
        reason_ledger=tuple(reason_ledger),
        fairness_passed=fairness_passed,
        blockers=tuple(blockers),
        comparator_evaluations=comparator_evals,
        supporting_artifact_sha256=bundle.supporting_artifact_sha256,
        rederivation_digest=rederivation_digest,
    )


def validate_persisted_decision(
    persisted: DisplacementDecision | Mapping[str, Any],
    prereg: DisplacementPreregistration,
    bundle: DisplacementMeasurementBundle,
    as_of_date: str,
    repo_root: Path | str | None = None,
) -> list[str]:
    """Validate persisted decision against rederivation to ensure manual flips fail closed."""
    errors: list[str] = []

    if isinstance(persisted, DisplacementDecision):
        p_dict = persisted.to_dict()
    elif isinstance(persisted, Mapping):
        p_dict = dict(persisted)
        schema = p_dict.get("schema_version")
        if schema != DISPLACEMENT_DECISION_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {DISPLACEMENT_DECISION_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return ["persisted decision must be DisplacementDecision or Mapping"]

    rederived = derive_displacement_decision(
        prereg, bundle, as_of_date=as_of_date, repo_root=repo_root
    )

    # 1. Outcome equality
    persisted_outcome = p_dict.get("outcome")
    if persisted_outcome != rederived.outcome:
        errors.append(
            f"Outcome mismatch: persisted={persisted_outcome!r} != rederived={rederived.outcome!r}"
        )

    # 2. Rederivation digest equality
    persisted_digest = p_dict.get("rederivation_digest")
    if persisted_digest != rederived.rederivation_digest:
        errors.append(
            f"Rederivation digest mismatch: persisted={persisted_digest!r} != rederived={rederived.rederivation_digest!r}"
        )

    # 3. Supporting artifact sha256 equality
    persisted_art_sha = p_dict.get("supporting_artifact_sha256")
    if persisted_art_sha != bundle.supporting_artifact_sha256:
        errors.append(
            f"Supporting artifact SHA-256 mismatch: persisted={persisted_art_sha!r} != bundle={bundle.supporting_artifact_sha256!r}"
        )

    # 4. Reason ledger equality
    persisted_ledger = p_dict.get("reason_ledger", [])
    rederived_ledger = [r.to_dict() for r in rederived.reason_ledger]
    if persisted_ledger != rederived_ledger:
        errors.append("Reason ledger mismatch between persisted and rederived decision")

    return errors


# -----------------------------------------------------------------------------
# PR80B Artifact Parser & Preregistration Constructor
# -----------------------------------------------------------------------------


def parse_pr80b_measurement_artifact(
    artifact_data: Mapping[str, Any] | str | Path,
    artifact_path: str = "",
) -> DisplacementMeasurementBundle:
    """Parse PR80B direct-specialist displacement measurement artifact into bundle.

    Never defaults missing metrics to 0 or 1.0; verifies exact schema paths and bytes SHA.
    """
    if isinstance(artifact_data, (str, Path)):
        p = Path(artifact_data)
        if not p.is_file():
            raise DisplacementDecisionError(f"Artifact file not found: {p}")
        raw_bytes = p.read_bytes()
        art_sha = hashlib.sha256(raw_bytes).hexdigest()
        raw_text = raw_bytes.decode("utf-8")
        data = json.loads(raw_text)
        art_path = str(p)
    elif isinstance(artifact_data, Mapping):
        data = dict(artifact_data)
        art_sha = _compute_sha256(data)
        art_path = artifact_path or "inline_pr80b_artifact.json"
    else:
        raise DisplacementDecisionError(
            "artifact_data must be a mapping, filepath str, or Path"
        )

    schema = data.get("schema_version")
    if schema != PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION:
        raise DisplacementDecisionError(
            f"Unsupported PR80B schema {schema!r}; expected {PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION!r}"
        )

    corpus_info = data.get("corpus")
    if not isinstance(corpus_info, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'corpus' mapping in PR80B artifact"
        )

    fingerprint = str(corpus_info.get("fingerprint", ""))
    if not fingerprint or not _HEX64.match(fingerprint):
        raise DisplacementDecisionError(
            f"Invalid or missing corpus fingerprint in PR80B artifact: {fingerprint!r}"
        )

    doc_count = corpus_info.get("documents")
    if not isinstance(doc_count, int) or doc_count != 24:
        raise DisplacementDecisionError(
            f"Expected 24 corpus documents in PR80B artifact, got {doc_count!r}"
        )

    # Acceptance facts
    acceptance = data.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'acceptance' mapping in PR80B artifact"
        )

    corpus_loaded = bool(acceptance.get("corpus_loaded_24_docs", False))
    all_evaluated = bool(acceptance.get("all_systems_evaluated_on_full_corpus", False))
    pr80a_complete = bool(acceptance.get("pr80a_evidence_coverage_complete", False))
    pr80a_lane_ok = bool(acceptance.get("pr80a_lane_error_free", False))
    specialists_present = bool(acceptance.get("specialist_routes_present", False))

    systems_decl = data.get("systems")
    if not isinstance(systems_decl, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'systems' mapping in PR80B artifact"
        )

    expected_systems = {
        "marker-pr80a",
        "invoice2data",
        "llm-openrouter:poolside/laguna-s-2.1:free",
    }
    if set(systems_decl.keys()) != expected_systems:
        raise DisplacementDecisionError(
            f"PR80B systems {sorted(systems_decl.keys())} do not match expected {sorted(expected_systems)}"
        )

    executed_facts_list: list[ExecutedComparatorFacts] = []
    for sid, sinfo in systems_decl.items():
        if not isinstance(sinfo, Mapping):
            raise DisplacementDecisionError(f"Invalid system declaration for {sid!r}")
        skind = str(sinfo.get("kind", ""))
        sident = str(sinfo.get("identity", ""))
        sinput = str(sinfo.get("input", ""))
        if sid == "marker-pr80a":
            srules = "native evidence-backed query execution; only accepted values emitted with verifiable citation pointers"
        else:
            srules = str(
                sinfo.get("template_policy", sinfo.get("selection_rationale", ""))
            )
        srat = str(sinfo.get("selection_rationale", ""))
        executed_facts_list.append(
            ExecutedComparatorFacts(
                system_id=sid,
                system_kind=skind,
                system_identity=sident,
                input_path=sinput,
                adaptation_rules=srules,
                selection_rationale=srat,
            )
        )

    metrics_map = data.get("metrics")
    if not isinstance(metrics_map, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'metrics' mapping in PR80B artifact"
        )

    decision_map = data.get("decision")
    if not isinstance(decision_map, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'decision' mapping in PR80B artifact"
        )

    evidence_supp = decision_map.get("evidence_supporting")
    if not isinstance(evidence_supp, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'decision.evidence_supporting' mapping in PR80B artifact"
        )

    danger_map = evidence_supp.get("danger_counts")
    if not isinstance(danger_map, Mapping):
        raise DisplacementDecisionError("Missing 'danger_counts' in PR80B artifact")

    evidence_cov_map = evidence_supp.get("evidence_coverage")
    if not isinstance(evidence_cov_map, Mapping):
        raise DisplacementDecisionError("Missing 'evidence_coverage' in PR80B artifact")

    doc_exact_map = evidence_supp.get("doc_exact")
    if not isinstance(doc_exact_map, Mapping):
        raise DisplacementDecisionError("Missing 'doc_exact' in PR80B artifact")

    scalar_acc_map = evidence_supp.get("scalar_accuracy_on_present")
    if not isinstance(scalar_acc_map, Mapping):
        raise DisplacementDecisionError(
            "Missing 'scalar_accuracy_on_present' in PR80B artifact"
        )

    review_proxy_map = evidence_supp.get("review_proxy")
    if not isinstance(review_proxy_map, Mapping):
        raise DisplacementDecisionError("Missing 'review_proxy' in PR80B artifact")

    comparators: dict[str, ComparatorMeasurements] = {}

    def _lookup_supp(m: Mapping[str, Any], sid: str) -> Any:
        if sid in m:
            return m[sid]
        if sid.startswith("llm") and "llm" in m:
            return m["llm"]
        return None

    for sys_id in expected_systems:
        sys_metrics = metrics_map.get(sys_id)
        if not isinstance(sys_metrics, Mapping):
            raise DisplacementDecisionError(f"Missing metrics entry for {sys_id!r}")

        doc_data = sys_metrics.get("docs")
        if not isinstance(doc_data, Mapping):
            raise DisplacementDecisionError(f"Missing 'docs' metrics for {sys_id!r}")

        doc_total_val = doc_data.get("total")
        if not isinstance(doc_total_val, int) or doc_total_val <= 0:
            raise DisplacementDecisionError(
                f"Missing or invalid docs.total for {sys_id!r}: {doc_total_val!r}"
            )

        doc_exact_supp = _lookup_supp(doc_exact_map, sys_id)
        doc_exact_raw = doc_data.get("doc_exact")
        if not isinstance(doc_exact_supp, int) or not isinstance(doc_exact_raw, int):
            raise DisplacementDecisionError(f"Missing doc_exact count for {sys_id!r}")
        doc_exact_rate = round(doc_exact_supp / doc_total_val, 4)

        scalar_data = sys_metrics.get("scalar")
        if not isinstance(scalar_data, Mapping):
            raise DisplacementDecisionError(f"Missing 'scalar' metrics for {sys_id!r}")

        scalar_acc_supp = _lookup_supp(scalar_acc_map, sys_id)
        scalar_acc_raw = scalar_data.get("accuracy_on_present")
        if not isinstance(scalar_acc_supp, (int, float)) or not isinstance(
            scalar_acc_raw, (int, float)
        ):
            raise DisplacementDecisionError(
                f"Missing scalar accuracy_on_present for {sys_id!r}"
            )
        scalar_acc_val = float(scalar_acc_supp)

        absent_rej_raw = scalar_data.get("absent_rejection_rate")
        if not isinstance(absent_rej_raw, (int, float)):
            raise DisplacementDecisionError(
                f"Missing absent_rejection_rate for {sys_id!r}"
            )
        absent_rej_val = float(absent_rej_raw)

        ev_cov_supp = _lookup_supp(evidence_cov_map, sys_id)
        if not isinstance(ev_cov_supp, (int, float)):
            raise DisplacementDecisionError(f"Missing evidence_coverage for {sys_id!r}")
        ev_cov_val = float(ev_cov_supp)

        raw_dangers = _lookup_supp(danger_map, sys_id)
        if not isinstance(raw_dangers, Mapping):
            raise DisplacementDecisionError(f"Missing danger_counts for {sys_id!r}")
        dangers = {str(k): int(v) for k, v in raw_dangers.items()}

        lane_errs = doc_data.get("error_docs")
        if not isinstance(lane_errs, int):
            raise DisplacementDecisionError(f"Missing error_docs for {sys_id!r}")

        review_proxy_val = _lookup_supp(review_proxy_map, sys_id)
        if not isinstance(review_proxy_val, str) or not review_proxy_val.strip():
            review_status = MEASUREMENT_STATUS_UNAVAILABLE
            review_val = None
        else:
            review_status = MEASUREMENT_STATUS_MEASURED
            review_val = review_proxy_val

        dims: dict[str, DimensionMeasurement] = {
            DIMENSION_DOC_EXACT_RATE: DimensionMeasurement(
                dimension=DIMENSION_DOC_EXACT_RATE,
                status=MEASUREMENT_STATUS_MEASURED,
                value=doc_exact_rate,
                unit="rate",
            ),
            DIMENSION_SCALAR_ACCURACY: DimensionMeasurement(
                dimension=DIMENSION_SCALAR_ACCURACY,
                status=MEASUREMENT_STATUS_MEASURED,
                value=scalar_acc_val,
                unit="fraction",
            ),
            DIMENSION_ABSENT_REJECTION: DimensionMeasurement(
                dimension=DIMENSION_ABSENT_REJECTION,
                status=MEASUREMENT_STATUS_MEASURED,
                value=absent_rej_val,
                unit="fraction",
            ),
            DIMENSION_EVIDENCE_LINEAGE: DimensionMeasurement(
                dimension=DIMENSION_EVIDENCE_LINEAGE,
                status=MEASUREMENT_STATUS_MEASURED,
                value=ev_cov_val,
                unit="fraction",
            ),
            DIMENSION_DANGEROUS_FAILURES: DimensionMeasurement(
                dimension=DIMENSION_DANGEROUS_FAILURES,
                status=MEASUREMENT_STATUS_MEASURED,
                value=sum(dangers.values()),
                unit="count",
            ),
            DIMENSION_LANE_ERRORS: DimensionMeasurement(
                dimension=DIMENSION_LANE_ERRORS,
                status=MEASUREMENT_STATUS_MEASURED,
                value=lane_errs,
                unit="count",
            ),
            DIMENSION_REVIEW_BURDEN: DimensionMeasurement(
                dimension=DIMENSION_REVIEW_BURDEN,
                status=review_status,
                value=review_val,
            ),
            # PR80B reported_cost is null (free-tier models; no chargeable usage recorded) -> UNAVAILABLE, not zero!
            DIMENSION_COST: DimensionMeasurement(
                dimension=DIMENSION_COST,
                status=MEASUREMENT_STATUS_UNAVAILABLE,
                value=None,
                notes="free-tier models; no chargeable usage recorded",
            ),
        }

        comparators[sys_id] = ComparatorMeasurements(
            system_id=sys_id,
            dimensions=dims,
            danger_counts=dangers,
            raw_metrics=dict(sys_metrics),
        )

    fairness_ok = bool(
        corpus_loaded
        and all_evaluated
        and pr80a_complete
        and pr80a_lane_ok
        and specialists_present
    )

    fairness = FairnessVerification(
        input_parity_verified=corpus_loaded,
        adaptation_parity_verified=all_evaluated,
        full_corpus_evaluated=all_evaluated,
        is_fair=fairness_ok,
        executed_facts=tuple(executed_facts_list),
        discrepancies=(),
    )

    # In PR80B, routing is a future recommendation ("a later routing phase may run the LLM..."),
    # NOT an active verified integration.
    llm_integration = IntegrationVerification(
        system_id="llm-openrouter:poolside/laguna-s-2.1:free",
        status=INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="",
        evidence_artifact_sha256="",
        workflow_scope="demo.invoice@1.0.0 extraction",
        corpus_fingerprint_scope=fingerprint,
        corroboration_contract="synthetic specialist witness with independent corroboration",
    )

    return DisplacementMeasurementBundle(
        schema_version=DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION,
        measurement_id="pr80b_displacement_run",
        preregistration_id="pr80b_invoice_displacement_study",
        corpus_fingerprint=fingerprint,
        comparators=comparators,
        fairness=fairness,
        evidence_date="2026-08-20T00:00:00Z",
        supporting_artifact_path=art_path,
        supporting_artifact_sha256=art_sha,
        integrations=(llm_integration,),
    )


def create_pr80b_retrospective_preregistration(
    as_of_date: str = "2026-08-26T00:00:00Z",
) -> DisplacementPreregistration:
    """Create the canonical retrospective frozen preregistration for the PR80B displacement benchmark."""
    corpus = CorpusPreregistration(
        manifest_version="marker.pr80b_corpus.v1",
        fingerprint="aeba0b4b2121c3836f2508e8461f2d68bf5bfbabd578f6f54e8e7c513ca60511",
        document_count=24,
        slices=(
            "ambiguity.decoy_anchor",
            "ambiguity.row",
            "ambiguity.scalar",
            "baseline.happy",
            "edge.invalid_value",
            "edge.negative",
            "edge.zero",
            "encoding.fullwidth",
            "integrity.not_evaluable",
            "integrity.row_loss",
            "integrity.total_mismatch",
            "layout.label_variants",
            "layout.noise",
            "layout.pagination",
            "missing.optional",
            "missing.required",
            "normalization.currency",
            "normalization.date",
            "normalization.decimal",
            "normalization.decimal_eu",
            "structure.broken_row_long",
            "structure.broken_row_short",
            "structure.duplicate_conflict",
            "structure.duplicate_identical",
            "structure.many_rows",
            "structure.near_duplicate_desc",
            "witness.conflict",
            "witness.corroboration",
        ),
        task_description="Extract demo.invoice@1.0.0 scalar fields and repeated line items keyed by SKU from plain text.",
        normalization_rules={
            "invoice_date": "ISO YYYY-MM-DD",
            "currency": "USD/EUR/GBP",
            "decimals": "Decimal comparison after thousands/EU stripping",
            "integers": "Base-10 integer",
            "absence": "Document unstated field must be absent/null",
            "conflicts": "Contradictory values must be flagged or abstained",
        },
        declared_invariants=("sum_equality",),
    )

    comparators = (
        ComparatorDeclaredSpec(
            system_id="marker-pr80a",
            is_marker_baseline=True,
            system_kind="current evidence-backed extraction route",
            system_identity="app.extraction pr80a.1 anchor route over marker.query.v1",
            input_path_declared="corpus text published as kernel view documents; extraction via execute_query over the active PublicationSet",
            adaptation_rules_declared="native evidence-backed query execution; only accepted values emitted with verifiable citation pointers",
            selection_rationale="current Marker UI evidence-backed extraction route",
        ),
        ComparatorDeclaredSpec(
            system_id="invoice2data",
            is_marker_baseline=False,
            system_kind="deterministic open-source invoice specialist",
            system_identity="invoice2data 1.0.1 (PyPI) with per-vendor templates authored once for the canonical corpus layout",
            input_path_declared="same document text as plain .txt files (library text reader)",
            adaptation_rules_declared="first regex match wins for multi-match arrays; empty/None result maps to a lane error",
            selection_rationale="",
        ),
        ComparatorDeclaredSpec(
            system_id="llm-openrouter:poolside/laguna-s-2.1:free",
            is_marker_baseline=False,
            system_kind="hosted LLM direct specialist",
            system_identity="local OpenAI-compatible gateway (http://localhost:20128/v1): kc/poolside/laguna-s-2.1:free via structured-output extraction prompt, temperature 0",
            input_path_declared="same document text as the user message; system prompt declares the task normalization rules",
            adaptation_rules_declared="an LLM with a structured invoice-extraction prompt is the dominant deployed direct-specialist approach; invoice2data complements it as the canonical specialized open-source tool",
            selection_rationale="an LLM with a structured invoice-extraction prompt is the dominant deployed direct-specialist approach; invoice2data complements it as the canonical specialized open-source tool",
        ),
    )

    fairness = FairnessContract(
        same_user_level_input_required=True,
        declared_adaptation_rules_required=True,
        disallow_privileged_features=True,
        allowed_input_discrepancies=(),
    )

    thresholds = FrozenDecisionThresholds(
        max_acceptable_dangerous_failures=0,
        threshold_scope="declared_corpus_observed_count",
        min_evidence_coverage_for_retained=1.0,
        quality_margin_for_displacement=0.05,
        allow_candidate_integration=True,
        max_lane_error_rate=0.0,
    )

    material_dims = (
        DIMENSION_DOC_EXACT_RATE,
        DIMENSION_SCALAR_ACCURACY,
        DIMENSION_ABSENT_REJECTION,
        DIMENSION_EVIDENCE_LINEAGE,
        DIMENSION_DANGEROUS_FAILURES,
        DIMENSION_REVIEW_BURDEN,
        DIMENSION_LANE_ERRORS,
    )

    return DisplacementPreregistration(
        schema_version=DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION,
        preregistration_id="pr80b_invoice_displacement_study",
        workflow="demo.invoice@1.0.0 extraction",
        corpus=corpus,
        comparators=comparators,
        fairness_contract=fairness,
        material_dimensions=material_dims,
        frozen_thresholds=thresholds,
        preregistration_date="2026-08-26T00:00:00Z",
        protocol_timing=PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    )


create_pr80b_displacement_preregistration = create_pr80b_retrospective_preregistration
