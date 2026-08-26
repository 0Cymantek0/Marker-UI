"""Contracts, constants, errors, and data models for Invariant-62 displacement decisions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


__all__ = [
    "ALLOWED_INTEGRATION_KINDS",
    "DIMENSION_ABSENT_REJECTION",
    "DIMENSION_COST",
    "DIMENSION_DANGEROUS_FAILURES",
    "DIMENSION_DOC_EXACT_RATE",
    "DIMENSION_EVIDENCE_LINEAGE",
    "DIMENSION_LANE_ERRORS",
    "DIMENSION_LATENCY",
    "DIMENSION_REVIEW_BURDEN",
    "DIMENSION_SCALAR_ACCURACY",
    "DISPLACEMENT_DECISION_SCHEMA_VERSION",
    "DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION",
    "DISPLACEMENT_OUTCOMES",
    "DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION",
    "INTEGRATION_KIND_CONDITIONAL_SPECIALIST_ROUTING",
    "INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR",
    "INTEGRATION_STATUSES",
    "INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED",
    "INTEGRATION_STATUS_NOT_INTEGRABLE",
    "INTEGRATION_STATUS_REJECTED",
    "INTEGRATION_STATUS_VERIFIED_ACTIVE",
    "MEASUREMENT_STATUSES",
    "MEASUREMENT_STATUS_MEASURED",
    "MEASUREMENT_STATUS_NOT_APPLICABLE",
    "MEASUREMENT_STATUS_UNAVAILABLE",
    "OUTCOME_EXPLICIT_CONCESSION",
    "OUTCOME_INCONCLUSIVE",
    "OUTCOME_INTEGRATE_OR_ROUTE",
    "OUTCOME_MARKER_RETAINED",
    "PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION",
    "PROTOCOL_PROSPECTIVE_PREREGISTRATION",
    "PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY",
    "PROTOCOL_TIMINGS",
    "REASON_STATUSES",
    "REASON_STATUS_CONCEDED",
    "REASON_STATUS_INTEGRATED",
    "REASON_STATUS_MEASURED",
    "REASON_STATUS_UNKNOWN",
    "STANDARD_MATERIAL_DIMENSIONS",
    "ComparatorDeclaredSpec",
    "ComparatorEvaluation",
    "ComparatorMeasurements",
    "CorpusPreregistration",
    "DimensionMeasurement",
    "DisplacementDecision",
    "DisplacementDecisionError",
    "DisplacementMeasurementBundle",
    "DisplacementPreregistration",
    "ExecutedComparatorFacts",
    "FairnessContract",
    "FairnessVerification",
    "FrozenDecisionThresholds",
    "IntegrationVerification",
    "ReasonToLeaveItem",
]
