"""Capability and architecture subsystem accountability contract (invariant 59).

Governing requirement:
"Every model/capability and architecture subsystem passes complexity-adjusted utility
and has a support owner, rollback, expiry, and kill condition."

Dispositions supported:
- promoted: fully shipped in production workflow; requires support owner, verified viable rollback,
  active expiry boundary (evaluated_at <= retest_deadline, no future date), objective un-triggered
  kill condition, evidence identity, and complexity-adjusted utility proof with current evidence.
- experimental_shadow: deployed in shadow/experimental mode; requires supporting evidence for disposition.
- non_promoted: evaluated and intentionally not promoted per invariant 61 (valid outcome); requires evidence.
- disabled: capability is turned off; requires full utility_basis or explicit non-empty disabled_rationale.

Fail-closed semantics:
- Exact schema_version required on mapping inputs.
- Stale or superseded evidence cannot support promoted status.
- Expired retest deadlines or triggered kill conditions invalidate promoted status.
- Rollback paths and kill conditions must be concrete and testable, not generic prose.
- Kill threshold must be a finite number or explicit non-empty string; rejects bool, NaN, inf, containers.
- Unknown fields at top level or in nested objects fail closed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

CAPABILITY_MATRIX_SCHEMA_VERSION = "marker.capability_matrix.v1"

DISPOSITION_PROMOTED = "promoted"
DISPOSITION_EXPERIMENTAL_SHADOW = "experimental_shadow"
DISPOSITION_NON_PROMOTED = "non_promoted"
DISPOSITION_DISABLED = "disabled"

DISPOSITIONS = frozenset(
    {
        DISPOSITION_PROMOTED,
        DISPOSITION_EXPERIMENTAL_SHADOW,
        DISPOSITION_NON_PROMOTED,
        DISPOSITION_DISABLED,
    }
)

EVIDENCE_CURRENT = "current"
EVIDENCE_STALE = "stale"
EVIDENCE_SUPERSEDED = "superseded"
EVIDENCE_LIFECYCLES = frozenset({EVIDENCE_CURRENT, EVIDENCE_STALE, EVIDENCE_SUPERSEDED})

UTILITY_CONCLUSIONS = frozenset(
    {
        "promoted_complexity_justified",
        "non_promoted_research_accepted",
        "shadow_experimental_retained",
        "decommissioned_or_disabled",
    }
)

OPERATIONAL_BURDEN_STATUSES = frozenset(
    {
        "measured",
        "unavailable",
        "not_applicable",
    }
)

KILL_ACTIONS = frozenset(
    {
        "fail_closed_and_disable",
        "demote_to_experimental",
        "disable_route",
        "route_to_fallback",
    }
)

RETEST_TRIGGERS = frozenset(
    {
        "time_expiry",
        "model_or_operator_change",
        "runtime_or_dependency_change",
        "policy_revision_change",
        "drift_or_distribution_shift",
        "support_drop_or_error_spike",
    }
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$")

_GENERIC_ROLLBACK_STRINGS = frozenset(
    {
        "revert commit",
        "git revert",
        "rollback",
        "tbd",
        "none",
        "n/a",
    }
)

_GENERIC_KILL_STRINGS = frozenset(
    {
        "disable if bad",
        "if errors occur",
        "remove later",
        "kill if needed",
        "tbd",
        "none",
        "n/a",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "name",
        "category",
        "disposition",
        "support_owner",
        "rollback",
        "expiry",
        "kill_condition",
        "utility_basis",
        "disabled_rationale",
        "unresolved_limits",
        "metadata",
    }
)

_ROLLBACK_KEYS = frozenset({"mechanism", "procedure", "verified"})
_EXPIRY_KEYS = frozenset({"evaluated_at", "retest_deadline", "triggers"})
_KILL_KEYS = frozenset(
    {"trigger_expression", "evaluation_metric", "threshold", "action", "triggered", "trigger_reason"}
)
_UTILITY_KEYS = frozenset(
    {
        "evidence_artifact",
        "evidence_sha256",
        "lifecycle",
        "complexity_adjusted_conclusion",
        "operational_burden_status",
        "operational_burden_reason",
        "quality_gain",
        "operational_cost_delta",
        "justification_summary",
    }
)


class CapabilityAccountabilityError(ValueError):
    """Raised when capability record or matrix violates fail-closed constraints."""


@dataclass(frozen=True)
class CapabilityUtilityBasis:
    evidence_artifact: str
    evidence_sha256: str
    lifecycle: str
    complexity_adjusted_conclusion: str
    operational_burden_status: str
    operational_burden_reason: str | None = None
    quality_gain: float | None = None
    operational_cost_delta: Mapping[str, Any] | None = None
    justification_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "evidence_artifact": self.evidence_artifact,
            "evidence_sha256": self.evidence_sha256,
            "lifecycle": self.lifecycle,
            "complexity_adjusted_conclusion": self.complexity_adjusted_conclusion,
            "operational_burden_status": self.operational_burden_status,
            "justification_summary": self.justification_summary,
        }
        if self.operational_burden_reason is not None:
            out["operational_burden_reason"] = self.operational_burden_reason
        if self.quality_gain is not None:
            out["quality_gain"] = self.quality_gain
        if self.operational_cost_delta is not None:
            out["operational_cost_delta"] = dict(self.operational_cost_delta)
        return out


@dataclass(frozen=True)
class RollbackPath:
    mechanism: str  # e.g., 'feature_flag', 'config_switch', 'route_fallback', 'adapter_fallback'
    procedure: str
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "procedure": self.procedure,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class ExpiryBoundary:
    evaluated_at: str
    retest_deadline: str
    triggers: tuple[str, ...]

    def is_expired(self, as_of_iso: str | None = None) -> bool:
        ref_dt = datetime.now(timezone.utc)
        if as_of_iso:
            clean = as_of_iso.replace("Z", "+00:00")
            ref_dt = datetime.fromisoformat(clean)
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        deadline_clean = self.retest_deadline.replace("Z", "+00:00")
        if "T" not in deadline_clean:
            deadline_clean += "T23:59:59+00:00"
        deadline_dt = datetime.fromisoformat(deadline_clean)
        if deadline_dt.tzinfo is None:
            deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
        return ref_dt > deadline_dt

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated_at": self.evaluated_at,
            "retest_deadline": self.retest_deadline,
            "triggers": list(self.triggers),
        }


@dataclass(frozen=True)
class KillCondition:
    trigger_expression: str
    evaluation_metric: str
    threshold: float | str
    action: str  # e.g., 'demote_to_experimental', 'disable_route', 'fail_closed_and_disable'
    triggered: bool = False
    trigger_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "trigger_expression": self.trigger_expression,
            "evaluation_metric": self.evaluation_metric,
            "threshold": self.threshold,
            "action": self.action,
            "triggered": self.triggered,
        }
        if self.trigger_reason is not None:
            out["trigger_reason"] = self.trigger_reason
        return out


@dataclass(frozen=True)
class CapabilityRecord:
    id: str
    name: str
    category: str
    disposition: str
    support_owner: str
    rollback: RollbackPath
    expiry: ExpiryBoundary
    kill_condition: KillCondition
    utility_basis: CapabilityUtilityBasis | None = None
    disabled_rationale: str | None = None
    unresolved_limits: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": CAPABILITY_MATRIX_SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "disposition": self.disposition,
            "support_owner": self.support_owner,
            "rollback": self.rollback.to_dict(),
            "expiry": self.expiry.to_dict(),
            "kill_condition": self.kill_condition.to_dict(),
            "unresolved_limits": list(self.unresolved_limits),
        }
        if self.utility_basis is not None:
            out["utility_basis"] = self.utility_basis.to_dict()
        if self.disabled_rationale is not None:
            out["disabled_rationale"] = self.disabled_rationale
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out


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
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as exc:
        errors.append(f"{field_name} invalid ISO-8601 date format: {exc}")
        return None


def validate_capability_record(
    record: CapabilityRecord | Mapping[str, Any],
    as_of_date: str | None = None,
) -> list[str]:
    """Validate a single capability record against fail-closed constraints."""
    errors: list[str] = []

    if isinstance(record, CapabilityRecord):
        rec_dict = record.to_dict()
    elif isinstance(record, Mapping):
        rec_dict = dict(record)
        schema = rec_dict.get("schema_version")
        if schema != CAPABILITY_MATRIX_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {CAPABILITY_MATRIX_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return ["capability record must be CapabilityRecord or Mapping"]

    # Reject unknown top-level keys
    for k in rec_dict:
        if k not in _TOP_LEVEL_KEYS:
            errors.append(f"unknown field {k!r} in capability record")

    cid = rec_dict.get("id")
    if not isinstance(cid, str) or not cid.strip():
        errors.append("capability 'id' must be a non-empty string")

    name = rec_dict.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("capability 'name' must be a non-empty string")

    category = rec_dict.get("category")
    if not isinstance(category, str) or not category.strip():
        errors.append("capability 'category' must be a non-empty string")

    disposition = rec_dict.get("disposition")
    if disposition not in DISPOSITIONS:
        errors.append(f"disposition must be one of {sorted(DISPOSITIONS)}, got {disposition!r}")

    owner = rec_dict.get("support_owner")
    if not isinstance(owner, str) or not owner.strip():
        errors.append("support_owner must name a non-empty domain or team owner")

    # Rollback validation
    rollback = rec_dict.get("rollback")
    if not isinstance(rollback, Mapping) or not rollback:
        errors.append("rollback must be a non-empty object")
    else:
        for rk in rollback:
            if rk not in _ROLLBACK_KEYS:
                errors.append(f"unknown field {rk!r} in rollback object")
        mech = rollback.get("mechanism")
        proc = rollback.get("procedure")
        ver = rollback.get("verified")
        if not isinstance(mech, str) or not mech.strip():
            errors.append("rollback.mechanism must be a non-empty string")
        if not isinstance(proc, str) or not proc.strip():
            errors.append("rollback.procedure must be a non-empty string")
        elif proc.strip().lower() in _GENERIC_ROLLBACK_STRINGS:
            errors.append(f"rollback.procedure cannot be generic placeholder {proc!r}")
        if not isinstance(ver, bool):
            errors.append("rollback.verified must be a boolean")
        elif disposition == DISPOSITION_PROMOTED and ver is not True:
            errors.append(f"promoted capability {cid!r} requires verified rollback path (verified=True)")

    # Expiry validation
    expiry = rec_dict.get("expiry")
    if not isinstance(expiry, Mapping) or not expiry:
        errors.append("expiry must be a non-empty object")
    else:
        for ek in expiry:
            if ek not in _EXPIRY_KEYS:
                errors.append(f"unknown field {ek!r} in expiry object")
        eval_at_str = expiry.get("evaluated_at")
        deadline_str = expiry.get("retest_deadline")
        triggers = expiry.get("triggers")

        dt_eval = _parse_iso_dt(eval_at_str, "expiry.evaluated_at", errors)
        dt_deadline = _parse_iso_dt(deadline_str, "expiry.retest_deadline", errors)

        if dt_eval is not None and dt_deadline is not None:
            if dt_eval > dt_deadline:
                errors.append(
                    f"expiry.evaluated_at ({eval_at_str}) must be <= retest_deadline ({deadline_str})"
                )

        if as_of_date and dt_eval is not None:
            dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
            if dt_as_of is not None and dt_eval > dt_as_of:
                errors.append(
                    f"expiry.evaluated_at is in the future relative to as_of_date ({eval_at_str} > {as_of_date})"
                )

        if not isinstance(triggers, (list, tuple)) or not triggers:
            errors.append("expiry.triggers must be a non-empty list of retest trigger conditions")
        else:
            for trg in triggers:
                if trg not in RETEST_TRIGGERS:
                    errors.append(
                        f"expiry.trigger {trg!r} not in known vocabulary {sorted(RETEST_TRIGGERS)}"
                    )

        if disposition == DISPOSITION_PROMOTED and eval_at_str and deadline_str:
            exp_obj = ExpiryBoundary(
                evaluated_at=eval_at_str,
                retest_deadline=deadline_str,
                triggers=tuple(triggers or ()),
            )
            if exp_obj.is_expired(as_of_date):
                errors.append(
                    f"promoted capability {cid!r} is expired (retest deadline {deadline_str} passed as of {as_of_date or 'now'})"
                )

    # Kill condition validation
    kill = rec_dict.get("kill_condition")
    if not isinstance(kill, Mapping) or not kill:
        errors.append("kill_condition must be a non-empty object")
    else:
        for kk in kill:
            if kk not in _KILL_KEYS:
                errors.append(f"unknown field {kk!r} in kill_condition object")
        expr = kill.get("trigger_expression")
        metric = kill.get("evaluation_metric")
        thresh = kill.get("threshold")
        act = kill.get("action")
        trigd = kill.get("triggered", False)
        trig_reason = kill.get("trigger_reason")

        if not isinstance(expr, str) or not expr.strip():
            errors.append("kill_condition.trigger_expression must be a non-empty string")
        elif expr.strip().lower() in _GENERIC_KILL_STRINGS:
            errors.append(f"kill_condition.trigger_expression cannot be placeholder {expr!r}")
        if not isinstance(metric, str) or not metric.strip():
            errors.append("kill_condition.evaluation_metric must be a non-empty string")

        if isinstance(thresh, bool):
            errors.append("kill_condition.threshold cannot be a boolean")
        elif isinstance(thresh, (int, float)):
            if math.isnan(thresh) or math.isinf(thresh):
                errors.append("kill_condition.threshold must be a finite number")
        elif isinstance(thresh, str):
            if not thresh.strip():
                errors.append("kill_condition.threshold string cannot be empty")
        else:
            errors.append("kill_condition.threshold must be a finite number or non-empty string")

        if act not in KILL_ACTIONS:
            errors.append(f"kill_condition.action must be one of {sorted(KILL_ACTIONS)}, got {act!r}")
        if not isinstance(trigd, bool):
            errors.append("kill_condition.triggered must be a boolean")
        elif trigd is True:
            if disposition == DISPOSITION_PROMOTED:
                errors.append(
                    f"promoted capability {cid!r} has kill condition is triggered ({trig_reason or expr}) and cannot remain promoted"
                )
            if not isinstance(trig_reason, str) or not trig_reason.strip():
                errors.append("triggered kill_condition requires non-empty trigger_reason")

    # Limits validation
    limits = rec_dict.get("unresolved_limits")
    if not isinstance(limits, (list, tuple)):
        errors.append("unresolved_limits must be a list/tuple of strings")
    else:
        for lim in limits:
            if not isinstance(lim, str) or not lim.strip():
                errors.append("unresolved limit entry must be a non-empty string")
        if disposition in (DISPOSITION_PROMOTED, DISPOSITION_EXPERIMENTAL_SHADOW, DISPOSITION_NON_PROMOTED):
            if not limits:
                errors.append(f"{disposition} capability cannot declare empty unresolved_limits")

    # Utility basis validation
    utility = rec_dict.get("utility_basis")
    if disposition in (DISPOSITION_PROMOTED, DISPOSITION_NON_PROMOTED, DISPOSITION_EXPERIMENTAL_SHADOW):
        if not isinstance(utility, Mapping) or not utility:
            errors.append(
                f"{disposition} capability {cid!r} requires utility_basis demonstrating complexity-adjusted utility or disposition justification"
            )
        else:
            for uk in utility:
                if uk not in _UTILITY_KEYS:
                    errors.append(f"unknown field {uk!r} in utility_basis object")
            art = utility.get("evidence_artifact")
            sha = utility.get("evidence_sha256")
            lc = utility.get("lifecycle")
            conc = utility.get("complexity_adjusted_conclusion")
            op_status = utility.get("operational_burden_status")
            op_reason = utility.get("operational_burden_reason")
            q_gain = utility.get("quality_gain")
            cost_delta = utility.get("operational_cost_delta")
            just = utility.get("justification_summary")

            if not isinstance(art, str) or not art.strip():
                errors.append("utility_basis.evidence_artifact must be a valid path string")
            if not isinstance(sha, str) or not _HEX64.match(sha):
                errors.append("utility_basis.evidence_sha256 must be a 64-char hex SHA-256 digest")
            if lc not in EVIDENCE_LIFECYCLES:
                errors.append(
                    f"utility_basis.lifecycle must be one of {sorted(EVIDENCE_LIFECYCLES)}, got {lc!r}"
                )
            elif disposition == DISPOSITION_PROMOTED and lc in (EVIDENCE_STALE, EVIDENCE_SUPERSEDED):
                errors.append(
                    f"promoted capability {cid!r} cannot rest on {lc} evidence ({art})"
                )

            if conc not in UTILITY_CONCLUSIONS:
                errors.append(
                    f"utility_basis.complexity_adjusted_conclusion must be one of {sorted(UTILITY_CONCLUSIONS)}, got {conc!r}"
                )

            if op_status not in OPERATIONAL_BURDEN_STATUSES:
                errors.append(
                    f"utility_basis.operational_burden_status must be one of {sorted(OPERATIONAL_BURDEN_STATUSES)}, got {op_status!r}"
                )
            elif op_status in ("unavailable", "not_applicable"):
                if not isinstance(op_reason, str) or not op_reason.strip():
                    errors.append(
                        f"operational_burden_status {op_status!r} requires non-empty reason explaining absence"
                    )

            if q_gain is not None:
                if not isinstance(q_gain, (int, float)) or isinstance(q_gain, bool) or math.isnan(q_gain) or math.isinf(q_gain):
                    errors.append("quality_gain must be a finite float")

            if cost_delta is not None:
                if not isinstance(cost_delta, Mapping) or not cost_delta:
                    errors.append("operational_cost_delta must be a non-empty mapping")

            if not isinstance(just, str) or not just.strip():
                errors.append("utility_basis.justification_summary must be a non-empty string")
    elif disposition == DISPOSITION_DISABLED:
        if utility is not None and isinstance(utility, Mapping):
            for uk in utility:
                if uk not in _UTILITY_KEYS:
                    errors.append(f"unknown field {uk!r} in utility_basis object")
            art = utility.get("evidence_artifact")
            sha = utility.get("evidence_sha256")
            lc = utility.get("lifecycle")
            conc = utility.get("complexity_adjusted_conclusion")
            op_status = utility.get("operational_burden_status")
            op_reason = utility.get("operational_burden_reason")

            if not isinstance(art, str) or not art.strip():
                errors.append("utility_basis.evidence_artifact must be a valid path string")
            if not isinstance(sha, str) or not _HEX64.match(sha):
                errors.append("utility_basis.evidence_sha256 must be a 64-char hex SHA-256 digest")
            if lc not in EVIDENCE_LIFECYCLES:
                errors.append(f"utility_basis.lifecycle must be one of {sorted(EVIDENCE_LIFECYCLES)}")
            if conc not in UTILITY_CONCLUSIONS:
                errors.append(f"utility_basis.complexity_adjusted_conclusion must be one of {sorted(UTILITY_CONCLUSIONS)}")
            if op_status not in OPERATIONAL_BURDEN_STATUSES:
                errors.append(f"utility_basis.operational_burden_status must be one of {sorted(OPERATIONAL_BURDEN_STATUSES)}")
            elif op_status in ("unavailable", "not_applicable") and (not isinstance(op_reason, str) or not op_reason.strip()):
                errors.append(
                    f"operational_burden_status {op_status!r} requires non-empty reason explaining absence"
                )
        else:
            dis_rat = rec_dict.get("disabled_rationale")
            if not isinstance(dis_rat, str) or not dis_rat.strip():
                errors.append("disabled capability without utility_basis requires non-empty disabled_rationale")

    return errors
