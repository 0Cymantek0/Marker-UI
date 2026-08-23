"""Operational review-policy accounting from durable transitions (PR88).

The extraction review seam already enforces the authority rules —
context-bound decisions, grounding, staleness rejection, idempotent
replay through kernel identity dedup. What was missing is the
OPERATIONAL record: when work entered the review-required state, when
it was adjudicated, what the outcomes were, and whether any requirement
was bypassed.

This module adds the narrowest truthful missing piece: durable
transition records (non-authoritative native-object views) written at
the authoritative moments — a run persisting review-required fields,
and a review decision being applied, refused as stale, or refused as a
bypass attempt. Every metric below is DERIVED from those committed
transitions, so the same accounting reproduces after restart/reload,
and a replayed decision cannot double-count (the kernel's identity
dedup rejects the replayed batch atomically, transition included).

This is an operations plane, not a truth plane: nothing here can
change a claim, an assessment, or an accepted value. A review decision
remains authoritative only through the existing review seam.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

REVIEW_OPS_SCHEMA_VERSION = "marker.review_policy_ops.v1"

#: Durable record-id prefix for committed review transition records.
REVIEW_OPS_RECORD_PREFIX = "extraction.reviewops."
REVIEW_OPS_SOURCE_URI = "marker://extraction/review_ops"
REVIEW_OPS_EXTRACTOR_NAME = "marker-reviewops"
REVIEW_OPS_EXTRACTOR_VERSION = "pr88.1"

#: A field entered the review-required state at run persistence.
REVIEW_TRANSITION_REQUIRED = "review_required"
#: A review decision was applied and committed.
REVIEW_TRANSITION_ACCEPTED = "review_accepted"
REVIEW_TRANSITION_CORRECTED = "review_corrected"
REVIEW_TRANSITION_REJECTED = "review_rejected"
#: A decision was refused because its bound context is no longer current.
REVIEW_TRANSITION_STALE = "review_stale"
#: An attempt to obtain acceptance without grounded evidence was refused.
REVIEW_TRANSITION_BYPASS_REFUSED = "review_bypass_refused"

REVIEW_TRANSITIONS = frozenset(
    {
        REVIEW_TRANSITION_REQUIRED,
        REVIEW_TRANSITION_ACCEPTED,
        REVIEW_TRANSITION_CORRECTED,
        REVIEW_TRANSITION_REJECTED,
        REVIEW_TRANSITION_STALE,
        REVIEW_TRANSITION_BYPASS_REFUSED,
    }
)

_DECISION_TRANSITIONS = frozenset(
    {REVIEW_TRANSITION_ACCEPTED, REVIEW_TRANSITION_CORRECTED, REVIEW_TRANSITION_REJECTED}
)


class ReviewOpsError(ValueError):
    """Raised when review-ops accounting cannot be produced honestly."""


def utc_now_iso() -> str:
    """Production clock: current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReviewOpsError(f"{field_name} must be a non-empty string")
    return value


def _parse_timestamp(value: Any, *, field_name: str = "occurred_at") -> float:
    if not isinstance(value, str) or not value:
        raise ReviewOpsError(f"{field_name} must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReviewOpsError(
            f"{field_name} is not a valid ISO timestamp: {value!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class ReviewTransition:
    """One operational review lifecycle transition (non-authoritative)."""

    kind: str
    result_identity: str
    field_path: str
    publication_set_id: str
    occurred_at: str
    reviewer: str = ""
    decision_record_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in REVIEW_TRANSITIONS:
            raise ReviewOpsError(
                f"invalid review transition kind {self.kind!r}; "
                f"allowed: {sorted(REVIEW_TRANSITIONS)}"
            )
        for name in ("result_identity", "field_path", "occurred_at"):
            _require_text(getattr(self, name), field_name=name)
        if self.decision_record_id is not None:
            _require_text(self.decision_record_id, field_name="decision_record_id")

    @property
    def case_key(self) -> tuple[str, str]:
        """The accounting unit: one field of one result identity."""
        return (self.result_identity, self.field_path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "result_identity": self.result_identity,
            "field_path": self.field_path,
            "publication_set_id": self.publication_set_id,
            "occurred_at": self.occurred_at,
            "reviewer": self.reviewer,
            "decision_record_id": self.decision_record_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewTransition":
        if not isinstance(data, Mapping):
            raise ReviewOpsError(
                f"review transition must be a mapping, got {type(data).__name__}"
            )
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ReviewOpsError(f"unknown review transition keys {sorted(unknown)}")
        try:
            return cls(
                kind=data["kind"],
                result_identity=data["result_identity"],
                field_path=data["field_path"],
                publication_set_id=data["publication_set_id"],
                occurred_at=data["occurred_at"],
                reviewer=data.get("reviewer", ""),
                decision_record_id=data.get("decision_record_id"),
                detail=data.get("detail", ""),
            )
        except KeyError as exc:
            raise ReviewOpsError(
                f"review transition is missing {exc.args[0]!r}"
            ) from None


def review_transition_record(transition: ReviewTransition):
    """Commit shape for one transition: a non-authoritative view record."""
    from app.kernel.records import NativeObjectRecord

    digest = hashlib.sha256(
        "|".join(
            (
                transition.kind,
                transition.result_identity,
                transition.field_path,
                transition.occurred_at,
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return NativeObjectRecord(
        record_id=f"{REVIEW_OPS_RECORD_PREFIX}{digest}",
        source_uri=REVIEW_OPS_SOURCE_URI,
        locator=f"{transition.kind}:{transition.case_key[0]}:{transition.case_key[1]}",
        media_type="application/json",
        extractor_name=REVIEW_OPS_EXTRACTOR_NAME,
        extractor_version=REVIEW_OPS_EXTRACTOR_VERSION,
        properties={"transition": transition.as_dict()},
    )


async def load_review_transitions(session_factory, workspace_id: str) -> tuple[ReviewTransition, ...]:
    """Reload committed transitions — restart/reload-safe accounting input."""
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(KernelRecordRow.payload_json).where(
                    KernelRecordRow.workspace_id == workspace_id,
                    KernelRecordRow.record_class == "native_object",
                    KernelRecordRow.id.like(f"{REVIEW_OPS_RECORD_PREFIX}%"),
                )
            )
        ).all()
    transitions = []
    for (payload_json,) in rows:
        payload = json.loads(payload_json)
        properties = (
            payload.get("properties") if isinstance(payload, Mapping) else None
        )
        transition = (
            properties.get("transition") if isinstance(properties, Mapping) else None
        )
        if not isinstance(transition, Mapping):
            raise ReviewOpsError(
                "committed review-ops record carries no transition payload"
            )
        transitions.append(ReviewTransition.from_dict(transition))
    # Chronological order, stable by payload order for equal timestamps.
    return tuple(
        sorted(transitions, key=lambda item: _parse_timestamp(item.occurred_at))
    )


def derive_review_metrics(
    transitions: Sequence[ReviewTransition],
    *,
    workspace_id: str,
    schema_id: str,
    policy_id: str,
    policy_version: str,
) -> dict[str, Any]:
    """Derive the operational review-policy metrics from transitions.

    Accounting unit is one field of one result identity. A case enters
    the population when it is first observed review-required; it is
    reviewed when a terminal decision transition lands; the dwell is
    the declared measure (decision timestamp minus first-required
    timestamp, deterministic timestamps, no wall-clock sleeps). Rates
    with a zero denominator stay ``None`` with an explicit undefined
    status — an invented zero is a lie about the population.
    """
    _require_text(workspace_id, field_name="workspace_id")
    _require_text(schema_id, field_name="schema_id")
    _require_text(policy_id, field_name="policy_id")
    _require_text(policy_version, field_name="policy_version")

    first_required: dict[tuple[str, str], ReviewTransition] = {}
    decisions: dict[tuple[str, str], ReviewTransition] = {}
    raw_required_events = 0
    stale_rejections = 0
    bypass_refusals = 0
    for transition in transitions:
        if transition.kind == REVIEW_TRANSITION_REQUIRED:
            raw_required_events += 1
            first_required.setdefault(transition.case_key, transition)
        elif transition.kind in _DECISION_TRANSITIONS:
            decisions.setdefault(transition.case_key, transition)
        elif transition.kind == REVIEW_TRANSITION_STALE:
            stale_rejections += 1
        elif transition.kind == REVIEW_TRANSITION_BYPASS_REFUSED:
            bypass_refusals += 1

    required = len(first_required)
    reviewed_cases = {key for key in decisions if key in first_required}
    reviewed = len(reviewed_cases)
    orphan_decisions = len(decisions) - reviewed
    outcomes = {"accepted": 0, "corrected": 0, "rejected": 0}
    dwell_values: list[float] = []
    for key, decision in decisions.items():
        if decision.kind == REVIEW_TRANSITION_ACCEPTED:
            outcomes["accepted"] += 1
        elif decision.kind == REVIEW_TRANSITION_CORRECTED:
            outcomes["corrected"] += 1
        else:
            outcomes["rejected"] += 1
        entered = first_required.get(key)
        if entered is not None:
            dwell_values.append(
                _parse_timestamp(decision.occurred_at)
                - _parse_timestamp(entered.occurred_at)
            )
    dwell_values.sort()

    adjudication_attempts = reviewed + bypass_refusals
    if required > 0:
        coverage = {
            "value": reviewed / required,
            "status": "defined",
            "count": reviewed,
            "denominator": required,
        }
    else:
        coverage = {
            "value": None,
            "status": "undefined_zero_denominator",
            "count": reviewed,
            "denominator": 0,
        }
    if adjudication_attempts > 0:
        bypass_rate = {
            "value": bypass_refusals / adjudication_attempts,
            "status": "defined",
            "count": bypass_refusals,
            "denominator": adjudication_attempts,
        }
    else:
        bypass_rate = {
            "value": None,
            "status": "undefined_zero_denominator",
            "count": 0,
            "denominator": 0,
        }
    if dwell_values:
        midpoint = len(dwell_values) // 2
        median = (
            dwell_values[midpoint]
            if len(dwell_values) % 2
            else (dwell_values[midpoint - 1] + dwell_values[midpoint]) / 2
        )
        dwell = {
            "status": "defined",
            "resolved_cases": len(dwell_values),
            "measure": "decision_at_minus_first_required_at_seconds",
            "min_seconds": dwell_values[0],
            "median_seconds": median,
            "max_seconds": dwell_values[-1],
        }
    else:
        dwell = {
            "status": "undefined_no_resolved_cases",
            "resolved_cases": 0,
            "measure": "decision_at_minus_first_required_at_seconds",
            "min_seconds": None,
            "median_seconds": None,
            "max_seconds": None,
        }

    return {
        "schema_version": REVIEW_OPS_SCHEMA_VERSION,
        "population": {
            "workspace_id": workspace_id,
            "schema_id": schema_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "accounting_unit": "one field of one extraction result identity",
            "source": "durable review transition ledger (kernel native_object views)",
        },
        "eligible_cases": required,
        "required_review": required,
        "required_events": raw_required_events,
        "reviewed": reviewed,
        "decisions_without_observed_requirement": orphan_decisions,
        "unresolved_backlog": required - reviewed,
        "review_coverage_rate": coverage,
        "dwell": dwell,
        "outcomes": outcomes,
        "stale_rejections": stale_rejections,
        "bypass_refusals": bypass_refusals,
        "bypass_rate": bypass_rate,
        "replay_contract": (
            "replayed decisions are rejected atomically by kernel identity "
            "dedup (decision record and transition commit together or not at "
            "all); replay cannot double-count burden or mint duplicate authority"
        ),
        "non_claims": (
            "fixture-scale operational accounting only; no production "
            "staffing capacity, queue infrastructure, or human-time claim"
        ),
    }


def validate_review_ops_report(data: Mapping[str, Any]) -> None:
    """Fail-closed structural validation of a review-ops report/artifact.

    Checks the invariants that make the report honest: population
    identity stated, every metric present with the right shape, rate
    objects carrying explicit zero-denominator status instead of
    invented zeroes, backlog consistent with counts, and dwell stats
    internally ordered.
    """
    if not isinstance(data, Mapping):
        raise ReviewOpsError("report must be a mapping")
    if data.get("schema_version") != REVIEW_OPS_SCHEMA_VERSION:
        raise ReviewOpsError(
            f"unsupported report schema_version {data.get('schema_version')!r}"
        )
    metrics = data.get("metrics", data)
    required_keys = {
        "population", "eligible_cases", "required_review", "required_events",
        "reviewed", "decisions_without_observed_requirement",
        "unresolved_backlog", "review_coverage_rate", "dwell", "outcomes",
        "stale_rejections", "bypass_refusals", "bypass_rate",
    }
    missing = required_keys - set(metrics)
    if missing:
        raise ReviewOpsError(f"report is missing {sorted(missing)}")

    population = metrics["population"]
    for key in ("workspace_id", "schema_id", "policy_id", "policy_version"):
        _require_text(population.get(key), field_name=f"population.{key}")

    for key in (
        "eligible_cases", "required_review", "required_events", "reviewed",
        "decisions_without_observed_requirement", "unresolved_backlog",
        "stale_rejections", "bypass_refusals",
    ):
        value = metrics[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReviewOpsError(f"{key} must be a non-negative integer")
    if metrics["eligible_cases"] != metrics["required_review"]:
        raise ReviewOpsError("eligible_cases must equal required_review")
    if (
        metrics["unresolved_backlog"]
        != metrics["required_review"] - metrics["reviewed"]
    ):
        raise ReviewOpsError("unresolved_backlog must be required_review - reviewed")

    outcomes = metrics["outcomes"]
    if set(outcomes) != {"accepted", "corrected", "rejected"}:
        raise ReviewOpsError("outcomes must name accepted/corrected/rejected")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in outcomes.values()
    ):
        raise ReviewOpsError("outcome counts must be non-negative integers")
    if sum(outcomes.values()) != metrics["reviewed"] + metrics["decisions_without_observed_requirement"]:
        raise ReviewOpsError("outcome counts must sum to all decision cases")

    for name in ("review_coverage_rate", "bypass_rate"):
        rate = metrics[name]
        if not isinstance(rate, Mapping) or set(rate) != {
            "value", "status", "count", "denominator",
        }:
            raise ReviewOpsError(f"{name} must be a rate object")
        if rate["status"] == "defined":
            value = rate["value"]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ReviewOpsError(f"{name}.value must be numeric when defined")
            if not 0.0 <= value <= 1.0:
                raise ReviewOpsError(f"{name}.value must be within [0, 1]")
            if rate["denominator"] <= 0:
                raise ReviewOpsError(
                    f"{name} cannot be defined with a non-positive denominator"
                )
        elif rate["status"] == "undefined_zero_denominator":
            if rate["value"] is not None:
                raise ReviewOpsError(
                    f"{name}.value must be None when the denominator is zero"
                )
            if rate["denominator"] != 0:
                raise ReviewOpsError(f"{name} zero-denominator status is false")
        else:
            raise ReviewOpsError(f"{name} carries an unknown status")

    dwell = metrics["dwell"]
    if dwell.get("status") == "defined":
        values = [dwell.get(key) for key in ("min_seconds", "median_seconds", "max_seconds")]
        if any(not isinstance(value, (int, float)) or value < 0 for value in values):
            raise ReviewOpsError("dwell seconds must be non-negative numbers")
        if not values[0] <= values[1] <= values[2]:
            raise ReviewOpsError("dwell min <= median <= max must hold")
        if not isinstance(dwell.get("resolved_cases"), int) or dwell["resolved_cases"] <= 0:
            raise ReviewOpsError("defined dwell requires resolved cases")
    elif dwell.get("status") == "undefined_no_resolved_cases":
        if any(dwell.get(key) is not None for key in ("min_seconds", "median_seconds", "max_seconds")):
            raise ReviewOpsError("undefined dwell must not invent seconds")
    else:
        raise ReviewOpsError("dwell carries an unknown status")


def iter_review_required_fields(result) -> Iterable[tuple[str, Any]]:
    """Yield (field_path, outcome) for every review-required field."""
    for name, outcome in result.fields.items():
        if outcome.status == "review_required":
            yield name, outcome
    for item_name, rows in (result.line_items or {}).items():
        for row in rows:
            identity_label = ".".join(f"{k}={row.identity[k]}" for k in sorted(row.identity))
            for field_name, outcome in row.fields.items():
                if outcome.status == "review_required":
                    yield f"{item_name}[{identity_label}].{field_name}", outcome


__all__ = [
    "REVIEW_OPS_SCHEMA_VERSION",
    "REVIEW_OPS_RECORD_PREFIX",
    "REVIEW_OPS_SOURCE_URI",
    "REVIEW_OPS_EXTRACTOR_NAME",
    "REVIEW_OPS_EXTRACTOR_VERSION",
    "REVIEW_TRANSITIONS",
    "REVIEW_TRANSITION_REQUIRED",
    "REVIEW_TRANSITION_ACCEPTED",
    "REVIEW_TRANSITION_CORRECTED",
    "REVIEW_TRANSITION_REJECTED",
    "REVIEW_TRANSITION_STALE",
    "REVIEW_TRANSITION_BYPASS_REFUSED",
    "ReviewOpsError",
    "ReviewTransition",
    "derive_review_metrics",
    "iter_review_required_fields",
    "load_review_transitions",
    "review_transition_record",
    "utc_now_iso",
    "validate_review_ops_report",
]
