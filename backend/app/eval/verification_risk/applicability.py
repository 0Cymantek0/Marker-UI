"""Self-describing calibration applicability contract (PR88, invariant 23).

A :class:`CalibrationResult` reports method/version, sample support,
Wilson uncertainty, Brier/ECE, and explicit insufficient-support states
for one corpus slice — but it does not name the population it applies
to, the assumptions required to interpret it, when it expires, or what
zero observed catastrophic failures means on finite support.

This module composes a v1 result into a versioned v2 applicability
artifact that makes those facts first-class and machine-checkable. The
v1 contract and the kernel ``verification_risk_evidence`` record schema
are unchanged: old artifacts keep their original meaning, and the v2
artifact is a derived evaluation-plane contract, not a second authority.

Zero-catastrophe honesty is structural: whenever ``trials > 0`` the
interpretation carries a positive one-sided 95% upper bound (exact
Clopper-Pearson), so zero observed failures can never be serialized or
read as "risk = 0".
"""

from __future__ import annotations

import math

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .calibration import CalibrationResult
from .common import VerificationRiskError
from .metrics import RateEstimate

#: Versioned artifact type. v1 ``CalibrationResult`` dicts remain valid
#: v1 artifacts; a reader must never reinterpret one as satisfying the
#: stronger v2 semantics.
CALIBRATION_APPLICABILITY_SCHEMA_VERSION = "marker.calibration.applicability.v2"

#: Closed vocabulary of retest/invalidation triggers. At least one is
#: required; the named triggers are the machine-readable expiry contract.
RETEST_TIME_EXPIRY = "time_expiry"
RETEST_MODEL_OR_OPERATOR_CHANGE = "model_or_operator_change"
RETEST_RUNTIME_OR_PREPROCESSING_CHANGE = "runtime_or_preprocessing_change"
RETEST_POLICY_REVISION_CHANGE = "policy_revision_change"
RETEST_POPULATION_SHIFT = "population_shift"
RETEST_SUPPORT_BELOW_MINIMUM = "support_below_minimum"

RETEST_TRIGGERS = frozenset(
    {
        RETEST_TIME_EXPIRY,
        RETEST_MODEL_OR_OPERATOR_CHANGE,
        RETEST_RUNTIME_OR_PREPROCESSING_CHANGE,
        RETEST_POLICY_REVISION_CHANGE,
        RETEST_POPULATION_SHIFT,
        RETEST_SUPPORT_BELOW_MINIMUM,
    }
)

#: Closed vocabulary of assumption keys. Each names one condition that
#: must hold for the calibration statement to apply; unknown keys fail
#: closed so a future reader cannot smuggle in unstated applicability.
ASSUMPTION_WORKFLOW_CLASS = "workflow_class"
ASSUMPTION_CLAIM_AUTHORITY_CLASS = "claim_authority_class"
ASSUMPTION_POLICY_ID = "policy_id"
ASSUMPTION_POLICY_REVISION = "policy_revision"
ASSUMPTION_LABEL_DEFINITION = "label_definition"
ASSUMPTION_SAMPLING_FRAME = "sampling_frame"
ASSUMPTION_DISTRIBUTION_CLASS = "distribution_class"
ASSUMPTION_PREPROCESSING_PROFILE = "preprocessing_profile"
ASSUMPTION_RUNTIME_PROFILE = "runtime_profile"
ASSUMPTION_OPERATOR_PROFILE = "operator_profile"

ASSUMPTION_KEYS = frozenset(
    {
        ASSUMPTION_WORKFLOW_CLASS,
        ASSUMPTION_CLAIM_AUTHORITY_CLASS,
        ASSUMPTION_POLICY_ID,
        ASSUMPTION_POLICY_REVISION,
        ASSUMPTION_LABEL_DEFINITION,
        ASSUMPTION_SAMPLING_FRAME,
        ASSUMPTION_DISTRIBUTION_CLASS,
        ASSUMPTION_PREPROCESSING_PROFILE,
        ASSUMPTION_RUNTIME_PROFILE,
        ASSUMPTION_OPERATOR_PROFILE,
    }
)

#: Assumptions every artifact must state: what a "failure" means and
#: how the sample was drawn. Without both, support numbers are
#: uninterpretable.
REQUIRED_ASSUMPTION_KEYS = frozenset(
    {ASSUMPTION_LABEL_DEFINITION, ASSUMPTION_SAMPLING_FRAME}
)

SHIFT_MATCHED = "matched"
SHIFT_SHIFTED = "shifted"
SHIFT_UNKNOWN = "unknown"
SHIFT_STATES = frozenset({SHIFT_MATCHED, SHIFT_SHIFTED, SHIFT_UNKNOWN})

#: Method label recorded on the catastrophic interpretation.
CATASTROPHIC_BOUND_METHOD = "one_sided_95_clopper_pearson_upper_bound"


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationRiskError(f"{field_name} must be a non-empty string")
    return value


def _parse_timestamp(value: Any, *, field_name: str) -> float:
    """Parse an ISO-8601 timestamp (Z or offset) to a POSIX float."""
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        raise VerificationRiskError(f"{field_name} must be an ISO timestamp string")
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise VerificationRiskError(
            f"{field_name} is not a valid ISO timestamp: {value!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _decimal_string(value: float) -> str:
    """Render a bound deterministically without float-repr drift."""
    if not math.isfinite(value):
        raise VerificationRiskError("bound must be finite")
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text if text else "0"


def clopper_pearson_upper_95(failures: int, trials: int) -> str:
    """Exact one-sided 95% upper bound for a binomial failure rate.

    ``failures=0`` has the closed form ``1 - 0.05 ** (1 / trials)``;
    other counts are solved by deterministic bisection on the binomial
    CDF. The result is a canonical decimal string so it participates in
    artifact identity without float ambiguity.
    """
    if failures < 0 or trials < 0 or failures > trials:
        raise VerificationRiskError(
            f"invalid catastrophic counts failures={failures}, trials={trials}"
        )
    if trials == 0:
        raise VerificationRiskError(
            "catastrophic upper bound requires a positive denominator; "
            "zero trials must stay status=not_evaluable, never risk=0"
        )
    if failures >= trials:
        return "1"
    if failures == 0:
        return _decimal_string(1.0 - 0.05 ** (1.0 / trials))
    alpha = 0.05

    def cdf_at(p: float, k: int = failures, n: int = trials) -> float:
        return math.fsum(
            math.comb(n, i) * (p ** i) * ((1.0 - p) ** (n - i))
            for i in range(k + 1)
        )

    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if cdf_at(mid) > alpha:
            low = mid
        else:
            high = mid
    return _decimal_string((low + high) / 2.0)


@dataclass(frozen=True)
class CatastrophicFailureInterpretation:
    """What the observed catastrophic-failure count actually bounds.

    ``observed_failures``/``trials`` retain the raw counts; the upper
    bound makes the finite-sample meaning explicit. When ``trials > 0``
    the bound is strictly positive, so "zero observed failures" is
    machine-checkably NOT "zero risk". When ``trials == 0`` there is no
    estimate at all — the honest state is ``not_evaluable``.
    """

    observed_failures: int
    trials: int
    upper_bound_95: str | None
    status: str
    method: str = CATASTROPHIC_BOUND_METHOD
    statement: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.observed_failures, int)
            or isinstance(self.observed_failures, bool)
            or self.observed_failures < 0
        ):
            raise VerificationRiskError(
                f"invalid observed_failures: {self.observed_failures!r}"
            )
        if (
            not isinstance(self.trials, int)
            or isinstance(self.trials, bool)
            or self.trials < 0
        ):
            raise VerificationRiskError(f"invalid trials: {self.trials!r}")
        if self.observed_failures > self.trials:
            raise VerificationRiskError(
                "observed_failures cannot exceed trials"
            )
        if self.trials == 0:
            if self.observed_failures != 0 or self.upper_bound_95 is not None:
                raise VerificationRiskError(
                    "zero trials admit no failure count and no bound; "
                    "use status=not_evaluable"
                )
            object.__setattr__(
                self, "status", self.status if self.status == "not_evaluable" else "not_evaluable"
            )
            object.__setattr__(
                self,
                "statement",
                "no catastrophic-failure trials; no risk estimate exists",
            )
            return
        if self.upper_bound_95 is None:
            raise VerificationRiskError(
                "positive trials require an explicit upper bound; "
                "zero observed failures is not zero risk"
            )
        if not isinstance(self.upper_bound_95, str):
            raise VerificationRiskError(
                "upper_bound_95 must be a canonical decimal string"
            )
        try:
            bound = float(self.upper_bound_95)
        except ValueError:
            raise VerificationRiskError(
                f"upper_bound_95 is not decimal: {self.upper_bound_95!r}"
            ) from None
        if not math.isfinite(bound) or bound <= 0.0 or bound > 1.0:
            raise VerificationRiskError(
                "upper_bound_95 must be in (0, 1]; zero observed failures "
                "must never serialize an implied zero risk"
            )
        object.__setattr__(self, "status", "bounded")
        object.__setattr__(
            self,
            "statement",
            (
                f"{self.observed_failures} catastrophic failure(s) in "
                f"{self.trials} trial(s) bounds the one-sided 95% failure "
                f"rate at <= {self.upper_bound_95}; observed zeros do not "
                "establish zero risk"
            ),
        )

    @property
    def zero_failures_observed(self) -> bool:
        return self.trials > 0 and self.observed_failures == 0

    @classmethod
    def from_counts(
        cls, observed_failures: int, trials: int
    ) -> "CatastrophicFailureInterpretation":
        if trials == 0:
            return cls(
                observed_failures=0, trials=0, upper_bound_95=None, status="not_evaluable"
            )
        return cls(
            observed_failures=observed_failures,
            trials=trials,
            upper_bound_95=clopper_pearson_upper_95(observed_failures, trials),
            status="bounded",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed_failures": self.observed_failures,
            "trials": self.trials,
            "upper_bound_95": self.upper_bound_95,
            "status": self.status,
            "method": self.method,
            "zero_failures_observed": self.zero_failures_observed,
            "zero_failures_implies_zero_risk": False if self.trials > 0 else None,
            "statement": self.statement,
        }


@dataclass(frozen=True)
class CalibrationApplicability:
    """Self-describing applicability of one calibration result.

    Composes a v1 :class:`CalibrationResult` with the population,
    assumptions, expiry/retest contract, and catastrophic-failure
    interpretation that make the artifact reproducible and bounded.
    """

    population_name: str
    corpus_identity: str
    slice_id: str | None
    distribution: str | None
    sampling_frame: str
    assumptions: Mapping[str, str] = field(default_factory=dict)
    shift_status: str = SHIFT_UNKNOWN
    evaluated_at: str = ""
    expires_at: str = ""
    retest_triggers: frozenset[str] = frozenset()
    support_sample_count: int = 0
    support_required: int = 0
    support_sufficient: bool = False
    support_status: str = "insufficient_support"
    missing_confidence_count: int = 0
    uncertainty_method: str = ""
    method_id: str = ""
    method_version: str = ""
    target_event: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    catastrophic: CatastrophicFailureInterpretation | None = None

    def __post_init__(self) -> None:
        _require_text(self.population_name, field_name="population_name")
        _require_text(self.corpus_identity, field_name="corpus_identity")
        _require_text(self.sampling_frame, field_name="sampling_frame")
        _require_text(self.evaluated_at, field_name="evaluated_at")
        _require_text(self.expires_at, field_name="expires_at")
        _require_text(self.uncertainty_method, field_name="uncertainty_method")
        _require_text(self.method_id, field_name="method_id")
        _require_text(self.method_version, field_name="method_version")
        _require_text(self.target_event, field_name="target_event")
        if self.shift_status not in SHIFT_STATES:
            raise VerificationRiskError(
                f"invalid shift_status: {self.shift_status!r}"
            )
        if _parse_timestamp(self.expires_at, field_name="expires_at") < _parse_timestamp(
            self.evaluated_at, field_name="evaluated_at"
        ):
            raise VerificationRiskError(
                "expires_at cannot precede evaluated_at"
            )
        if not isinstance(self.assumptions, Mapping) or not self.assumptions:
            raise VerificationRiskError("assumptions must be a non-empty mapping")
        unknown = set(self.assumptions) - ASSUMPTION_KEYS
        if unknown:
            raise VerificationRiskError(
                f"unknown assumption keys {sorted(unknown)}; "
                f"allowed: {sorted(ASSUMPTION_KEYS)}"
            )
        missing_required = REQUIRED_ASSUMPTION_KEYS - set(self.assumptions)
        if missing_required:
            raise VerificationRiskError(
                f"assumptions must state {sorted(missing_required)}"
            )
        for key, value in self.assumptions.items():
            _require_text(value, field_name=f"assumptions.{key}")
        if not self.retest_triggers:
            raise VerificationRiskError(
                "at least one retest trigger is required; evidence without "
                "an invalidation condition never expires"
            )
        unknown_triggers = set(self.retest_triggers) - RETEST_TRIGGERS
        if unknown_triggers:
            raise VerificationRiskError(
                f"unknown retest triggers {sorted(unknown_triggers)}; "
                f"allowed: {sorted(RETEST_TRIGGERS)}"
            )
        if self.catastrophic is None:
            raise VerificationRiskError(
                "catastrophic failure interpretation is required; "
                "absence of a count is not the same as absence of risk"
            )
        if not isinstance(self.support_sample_count, int) or isinstance(
            self.support_sample_count, bool
        ):
            raise VerificationRiskError("support_sample_count must be an integer")

    # -- expiry ---------------------------------------------------------

    def is_expired(self, as_of: str) -> bool:
        """Machine-evaluable expiry against an as-of timestamp."""
        return (
            _parse_timestamp(as_of, field_name="as_of")
            > _parse_timestamp(self.expires_at, field_name="expires_at")
        )

    def retest_required_for(self, changes: frozenset[str]) -> bool:
        """True when any named change class hits a declared trigger."""
        return bool(changes & self.retest_triggers)

    def applies_to(
        self,
        *,
        policy_id: str | None = None,
        policy_revision: str | None = None,
        workflow_class: str | None = None,
        slice_id: str | None = None,
    ) -> bool:
        """Conservative applicability: every stated dimension must match."""
        checks = (
            (policy_id, ASSUMPTION_POLICY_ID),
            (policy_revision, ASSUMPTION_POLICY_REVISION),
            (workflow_class, ASSUMPTION_WORKFLOW_CLASS),
        )
        for requested, key in checks:
            if requested is not None and self.assumptions.get(key) != requested:
                return False
        if slice_id is not None and self.slice_id != slice_id:
            return False
        return True

    # -- serialization ----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CALIBRATION_APPLICABILITY_SCHEMA_VERSION,
            "population": {
                "name": self.population_name,
                "corpus_identity": self.corpus_identity,
                "slice_id": self.slice_id,
                "distribution": self.distribution,
                "sampling_frame": self.sampling_frame,
            },
            "assumptions": dict(sorted(self.assumptions.items())),
            "shift_status": self.shift_status,
            "validity": {
                "evaluated_at": self.evaluated_at,
                "expires_at": self.expires_at,
                "retest_triggers": sorted(self.retest_triggers),
            },
            "support": {
                "sample_count": self.support_sample_count,
                "support_required": self.support_required,
                "support_sufficient": self.support_sufficient,
                "status": self.support_status,
                "missing_confidence_count": self.missing_confidence_count,
            },
            "uncertainty_method": self.uncertainty_method,
            "calibration_method": {
                "method_id": self.method_id,
                "method_version": self.method_version,
                "target_event": self.target_event,
            },
            "metrics": dict(self.metrics),
            "catastrophic_failures": (
                self.catastrophic.as_dict() if self.catastrophic else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CalibrationApplicability":
        """Fail-closed rematerialization; unknown shapes are rejected."""
        if not isinstance(data, Mapping):
            raise VerificationRiskError(
                f"applicability must be a mapping, got {type(data).__name__}"
            )
        if data.get("schema_version") != CALIBRATION_APPLICABILITY_SCHEMA_VERSION:
            raise VerificationRiskError(
                f"unsupported applicability schema_version "
                f"{data.get('schema_version')!r}"
            )
        top = {
            "schema_version", "population", "assumptions", "shift_status",
            "validity", "support", "uncertainty_method", "calibration_method",
            "metrics", "catastrophic_failures",
        }
        unknown = set(data) - top
        if unknown:
            raise VerificationRiskError(
                f"unknown applicability fields {sorted(unknown)}"
            )
        try:
            population = data["population"]
            validity = data["validity"]
            support = data["support"]
            method = data["calibration_method"]
            catastrophic = data["catastrophic_failures"]
            if not isinstance(catastrophic, Mapping):
                raise VerificationRiskError(
                    "catastrophic_failures must be a mapping"
                )
            return cls(
                population_name=population["name"],
                corpus_identity=population["corpus_identity"],
                slice_id=population.get("slice_id"),
                distribution=population.get("distribution"),
                sampling_frame=population["sampling_frame"],
                assumptions=dict(data["assumptions"]),
                shift_status=data["shift_status"],
                evaluated_at=validity["evaluated_at"],
                expires_at=validity["expires_at"],
                retest_triggers=frozenset(validity["retest_triggers"]),
                support_sample_count=support["sample_count"],
                support_required=support["support_required"],
                support_sufficient=support["support_sufficient"],
                support_status=support["status"],
                missing_confidence_count=support["missing_confidence_count"],
                uncertainty_method=data["uncertainty_method"],
                method_id=method["method_id"],
                method_version=method["method_version"],
                target_event=method["target_event"],
                metrics=dict(data.get("metrics") or {}),
                catastrophic=CatastrophicFailureInterpretation(
                    observed_failures=catastrophic["observed_failures"],
                    trials=catastrophic["trials"],
                    upper_bound_95=catastrophic["upper_bound_95"],
                    status=catastrophic["status"],
                ),
            )
        except KeyError as exc:
            raise VerificationRiskError(
                f"applicability is missing {exc.args[0]!r}"
            ) from None


def build_applicability(
    result: CalibrationResult,
    *,
    population_name: str,
    sampling_frame: str,
    assumptions: Mapping[str, str],
    evaluated_at: str,
    expires_at: str,
    retest_triggers: frozenset[str],
    catastrophic_failures: int,
    catastrophic_trials: int,
    runtime_metrics: Mapping[str, Any] | None = None,
) -> CalibrationApplicability:
    """Compose a v1 calibration result into a v2 applicability artifact.

    ``catastrophic_trials`` is the denominator the failure count was
    observed on (e.g. ``PairRiskMetrics.catastrophic_sample_count`` or
    the calibration ``sample_count``). Runtime observations stay outside
    semantic content when passed via ``runtime_metrics`` — they are
    recorded for monitoring, never for applicability.
    """
    if not isinstance(result, CalibrationResult):
        raise VerificationRiskError(
            "result must be a CalibrationResult (v1 calibration artifact)"
        )
    metrics: dict[str, Any] = {
        "brier_score": result.brier_score,
        "expected_calibration_error": result.expected_calibration_error,
        "maximum_calibration_error": result.maximum_calibration_error,
        "accuracy": (
            result.accuracy.as_dict()
            if isinstance(result.accuracy, RateEstimate)
            else result.accuracy
        ),
        "bins": [dict(item) for item in result.bins],
    }
    if runtime_metrics:
        metrics["runtime"] = dict(runtime_metrics)
    return CalibrationApplicability(
        population_name=population_name,
        corpus_identity=result.corpus_identity,
        slice_id=result.slice_id,
        distribution=result.distribution,
        sampling_frame=sampling_frame,
        assumptions=dict(assumptions),
        shift_status=_shift_from_status(result),
        evaluated_at=evaluated_at,
        expires_at=expires_at,
        retest_triggers=frozenset(retest_triggers),
        support_sample_count=result.sample_count,
        support_required=result.support_required,
        support_sufficient=result.support_sufficient,
        support_status=result.status,
        missing_confidence_count=result.missing_confidence_count,
        uncertainty_method=result.support_uncertainty_method,
        method_id=result.method_id,
        method_version=result.method_version,
        target_event=result.target_event,
        metrics=metrics,
        catastrophic=CatastrophicFailureInterpretation.from_counts(
            catastrophic_failures, catastrophic_trials
        ),
    )


def _shift_from_status(result: CalibrationResult) -> str:
    """Derive the honest shift state from the v1 slice identity."""
    distribution = result.distribution
    if distribution == "shifted":
        return SHIFT_SHIFTED
    if distribution == "matched":
        return SHIFT_MATCHED
    return SHIFT_UNKNOWN


__all__ = [
    "CALIBRATION_APPLICABILITY_SCHEMA_VERSION",
    "RETEST_TRIGGERS",
    "RETEST_TIME_EXPIRY",
    "RETEST_MODEL_OR_OPERATOR_CHANGE",
    "RETEST_RUNTIME_OR_PREPROCESSING_CHANGE",
    "RETEST_POLICY_REVISION_CHANGE",
    "RETEST_POPULATION_SHIFT",
    "RETEST_SUPPORT_BELOW_MINIMUM",
    "ASSUMPTION_KEYS",
    "REQUIRED_ASSUMPTION_KEYS",
    "SHIFT_STATES",
    "SHIFT_MATCHED",
    "SHIFT_SHIFTED",
    "SHIFT_UNKNOWN",
    "CatastrophicFailureInterpretation",
    "CalibrationApplicability",
    "clopper_pearson_upper_95",
    "build_applicability",
]
