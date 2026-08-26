"""Leadership claim contract and fail-closed validation (invariant 60).

Governing requirement:
"Every leadership claim names workflow, source/policy/hardware profile, competitors,
date, catastrophic budget, review burden, and unresolved limits."

Plan claim shape:
"On <workflow> for <declared corpus/slices> under <policy and hardware profile>,
Marker UI <beats / ties while reducing workflow burden / routes to / concedes loss to>
the best tested alternative as of <date>, with <quality, catastrophic, latency, cost,
and reliability evidence>. Unresolved limits are <limits>."

Fail-closed constraints:
- Exact schema_version required on mapping inputs.
- All 9 dimensions are strictly mandatory; omitting any single dimension fails validation.
- Review burden and catastrophic risk cannot silently be encoded as 0 when unknown.
- Catastrophic error budget: probabilities/rates in [0.0, 1.0], positive trials required,
  observed_rate <= upper_bound_95, upper_bound_95 <= max_acceptable_rate for beats/ties.
- For beats/ties/routes_to: non-empty corpus_scope required on claim; each evidence binding
  must declare non-empty workflow_scope, corpus_scope, and comparator_scope; comparator_scope
  must be an exact set equality with claim competitors (not just subset), and workflow/corpus
  must match claim exactly.
- Stale or superseded evidence cannot support beats, ties, or routes_to claims.
- Anti-universalization: exact canonical disclaimer required; un-scoped universal/global
  superiority assertions are rejected mechanically.
- Unknown fields at top level or nested objects are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

LEADERSHIP_CLAIM_SCHEMA_VERSION = "marker.leadership_claim.v1"

CLAIM_BEATS = "beats"
CLAIM_TIES_REDUCING_BURDEN = "ties_reducing_burden"
CLAIM_ROUTES_TO = "routes_to"
CLAIM_CONCEDED_LOSS = "conceded_loss"
CLAIM_WITHHELD = "withheld"

CLAIM_DISPOSITIONS = frozenset(
    {
        CLAIM_BEATS,
        CLAIM_TIES_REDUCING_BURDEN,
        CLAIM_ROUTES_TO,
        CLAIM_CONCEDED_LOSS,
        CLAIM_WITHHELD,
    }
)

EVIDENCE_CURRENT = "current"
EVIDENCE_STALE = "stale"
EVIDENCE_SUPERSEDED = "superseded"
EVIDENCE_LIFECYCLES = frozenset({EVIDENCE_CURRENT, EVIDENCE_STALE, EVIDENCE_SUPERSEDED})

REVIEW_BURDEN_STATUSES = frozenset({"measured", "unavailable", "not_applicable"})

CANONICAL_UNIVERSAL_DISCLAIMER = (
    "Claim is machine-scoped to declared workflow, corpus, and profiles only; "
    "no universal or global superiority claim is made."
)

_FORBIDDEN_UNIVERSAL_PATTERNS = frozenset(
    {
        "global superiority",
        "universal standard",
        "best overall",
        "dominates all",
        "beats all alternatives",
    }
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$"
)

_TOP_LEVEL_CLAIM_KEYS = frozenset(
    {
        "schema_version",
        "claim_id",
        "workflow",
        "source_profile",
        "policy_profile",
        "hardware_profile",
        "competitors",
        "evidence_date",
        "catastrophic_budget",
        "review_burden",
        "unresolved_limits",
        "disposition",
        "evidence_bindings",
        "corpus_scope",
        "universal_disclaimer",
    }
)

ALLOWED_BOUND_METHODS = frozenset(
    {
        "rule_of_three",
        "exact_binomial",
        "clopper_pearson_exact",
        "poisson_exact",
        "wilson_score",
        "one_sided_95_clopper_pearson_upper_bound",
    }
)


def calculate_one_sided_95_upper_bound(
    trials: int,
    observed_events: int = 0,
    method: str = "rule_of_three",
) -> float:
    """Derive an honest one-sided 95% upper confidence bound without external dependencies."""
    import math

    if not isinstance(trials, int) or isinstance(trials, bool) or trials <= 0:
        raise ValueError(f"trials must be positive integer, got {trials!r}")
    if (
        not isinstance(observed_events, int)
        or isinstance(observed_events, bool)
        or observed_events < 0
        or observed_events > trials
    ):
        raise ValueError(
            f"observed_events must be integer in [0, {trials}], got {observed_events!r}"
        )
    if method not in ALLOWED_BOUND_METHODS:
        raise ValueError(
            f"unsupported bound method {method!r}, allowed: {sorted(ALLOWED_BOUND_METHODS)}"
        )

    if observed_events == 0:
        if method in ("rule_of_three", "poisson_exact"):
            return min(1.0, -math.log(0.05) / trials)
        elif method in (
            "exact_binomial",
            "clopper_pearson_exact",
            "one_sided_95_clopper_pearson_upper_bound",
        ):
            return min(1.0, 1.0 - (0.05 ** (1.0 / trials)))
        elif method == "wilson_score":
            z = 1.6448536269514722
            return min(1.0, (z * z) / (trials + z * z))
    else:
        k = observed_events
        n = trials
        if method in (
            "exact_binomial",
            "clopper_pearson_exact",
            "one_sided_95_clopper_pearson_upper_bound",
        ):

            def _bin_cdf(p: float) -> float:
                if p <= 0.0:
                    return 1.0
                if p >= 1.0:
                    return 0.0
                total = 0.0
                for j in range(k + 1):
                    total += math.comb(n, j) * (p**j) * ((1.0 - p) ** (n - j))
                return total

            low, high = k / n, 1.0
            for _ in range(60):
                mid = (low + high) / 2.0
                if _bin_cdf(mid) > 0.05:
                    low = mid
                else:
                    high = mid
            return (low + high) / 2.0
        elif method in ("rule_of_three", "poisson_exact"):

            def _pois_cdf(lam: float) -> float:
                total = 0.0
                for j in range(k + 1):
                    total += (lam**j) / math.factorial(j)
                return math.exp(-lam) * total

            low_lam, high_lam = float(k), float(k + 15)
            for _ in range(60):
                mid = (low_lam + high_lam) / 2.0
                if _pois_cdf(mid) > 0.05:
                    low_lam = mid
                else:
                    high_lam = mid
            return min(1.0, ((low_lam + high_lam) / 2.0) / n)
        elif method == "wilson_score":
            z = 1.6448536269514722
            p_hat = k / n
            denom = 1.0 + (z * z) / n
            center = p_hat + (z * z) / (2.0 * n)
            rad = z * math.sqrt((p_hat * (1.0 - p_hat) / n) + ((z * z) / (4.0 * n * n)))
            return min(1.0, (center + rad) / denom)

    return min(1.0, -math.log(0.05) / trials)


_BUDGET_KEYS = frozenset(
    {
        "max_acceptable_rate",
        "observed_rate",
        "bound_method",
        "upper_bound_95",
        "trials",
        "zero_is_not_zero_risk_acknowledged",
        "unit",
    }
)

_REVIEW_KEYS = frozenset(
    {
        "status",
        "self_flagged_count",
        "unverified_emitted_count",
        "queue_time_ms_p50",
        "reason",
    }
)

_BINDING_KEYS = frozenset(
    {
        "artifact_path",
        "artifact_sha256",
        "lifecycle",
        "workflow_scope",
        "corpus_scope",
        "comparator_scope",
        "metric_pointers",
    }
)


class LeadershipClaimError(ValueError):
    """Raised when a leadership claim violates fail-closed honesty constraints."""


@dataclass(frozen=True)
class CatastrophicBudget:
    max_acceptable_rate: float
    observed_rate: float
    bound_method: str
    upper_bound_95: float
    trials: int
    zero_is_not_zero_risk_acknowledged: bool = True
    unit: str = "documents"

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_acceptable_rate": self.max_acceptable_rate,
            "observed_rate": self.observed_rate,
            "bound_method": self.bound_method,
            "upper_bound_95": self.upper_bound_95,
            "trials": self.trials,
            "zero_is_not_zero_risk_acknowledged": self.zero_is_not_zero_risk_acknowledged,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ReviewBurden:
    status: str  # 'measured' | 'unavailable' | 'not_applicable'
    self_flagged_count: int | None = None
    unverified_emitted_count: int | None = None
    queue_time_ms_p50: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.self_flagged_count is not None:
            out["self_flagged_count"] = self.self_flagged_count
        if self.unverified_emitted_count is not None:
            out["unverified_emitted_count"] = self.unverified_emitted_count
        if self.queue_time_ms_p50 is not None:
            out["queue_time_ms_p50"] = self.queue_time_ms_p50
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class ClaimEvidenceBinding:
    artifact_path: str
    artifact_sha256: str
    lifecycle: str
    metric_pointers: Mapping[str, Any]
    workflow_scope: str = ""
    corpus_scope: str = ""
    comparator_scope: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "lifecycle": self.lifecycle,
            "workflow_scope": self.workflow_scope,
            "corpus_scope": self.corpus_scope,
            "comparator_scope": list(self.comparator_scope),
            "metric_pointers": dict(self.metric_pointers),
        }


@dataclass(frozen=True)
class LeadershipClaim:
    claim_id: str
    workflow: str
    source_profile: str
    policy_profile: str
    hardware_profile: str
    competitors: tuple[str, ...]
    evidence_date: str
    catastrophic_budget: CatastrophicBudget
    review_burden: ReviewBurden
    unresolved_limits: tuple[str, ...]
    disposition: str
    evidence_bindings: tuple[ClaimEvidenceBinding, ...]
    corpus_scope: str = ""
    universal_disclaimer: str = CANONICAL_UNIVERSAL_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LEADERSHIP_CLAIM_SCHEMA_VERSION,
            "claim_id": self.claim_id,
            "workflow": self.workflow,
            "source_profile": self.source_profile,
            "policy_profile": self.policy_profile,
            "hardware_profile": self.hardware_profile,
            "competitors": list(self.competitors),
            "evidence_date": self.evidence_date,
            "catastrophic_budget": self.catastrophic_budget.to_dict(),
            "review_burden": self.review_burden.to_dict(),
            "unresolved_limits": list(self.unresolved_limits),
            "disposition": self.disposition,
            "evidence_bindings": [b.to_dict() for b in self.evidence_bindings],
            "corpus_scope": self.corpus_scope,
            "universal_disclaimer": self.universal_disclaimer,
        }


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


def _check_prob_range(val: Any, field_name: str, errors: list[str]) -> bool:
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        errors.append(f"{field_name} must be a float, got {val!r}")
        return False
    if val < 0.0 or val > 1.0:
        errors.append(f"{field_name} must be in [0.0, 1.0], got {val!r}")
        return False
    return True


def validate_leadership_claim(
    claim: LeadershipClaim | Mapping[str, Any],
    as_of_date: str | None = None,
) -> list[str]:
    """Validate a leadership claim against Invariant 60 fail-closed constraints."""
    errors: list[str] = []

    if isinstance(claim, LeadershipClaim):
        c_dict = claim.to_dict()
    elif isinstance(claim, Mapping):
        c_dict = dict(claim)
        schema = c_dict.get("schema_version")
        if schema != LEADERSHIP_CLAIM_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {LEADERSHIP_CLAIM_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return ["leadership claim must be LeadershipClaim or Mapping"]

    # Reject unknown top-level keys
    for k in c_dict:
        if k not in _TOP_LEVEL_CLAIM_KEYS:
            errors.append(f"unknown field {k!r} in leadership claim")

    cid = c_dict.get("claim_id")
    if not isinstance(cid, str) or not cid.strip():
        errors.append("claim_id must be a non-empty string")

    # 1. Workflow
    wf = c_dict.get("workflow")
    if not isinstance(wf, str) or not wf.strip():
        errors.append("workflow is mandatory and must be a non-empty string")

    # 2. Source profile
    sp = c_dict.get("source_profile")
    if not isinstance(sp, str) or not sp.strip():
        errors.append("source_profile is mandatory and must be a non-empty string")

    # 3. Policy profile
    pp = c_dict.get("policy_profile")
    if not isinstance(pp, str) or not pp.strip():
        errors.append("policy_profile is mandatory and must be a non-empty string")

    # 4. Hardware profile
    hp = c_dict.get("hardware_profile")
    if not isinstance(hp, str) or not hp.strip():
        errors.append("hardware_profile is mandatory and must be a non-empty string")

    # 5. Competitors / comparator set
    comps = c_dict.get("competitors")
    if not isinstance(comps, (list, tuple)) or not comps:
        errors.append(
            "competitors must be a non-empty list of named comparator systems"
        )
    else:
        for c in comps:
            if not isinstance(c, str) or not c.strip():
                errors.append(
                    f"competitor identifier must be non-empty string, got {c!r}"
                )

    # 6. Date / evidence timestamp
    edate_str = c_dict.get("evidence_date")
    dt_eval = _parse_iso_dt(edate_str, "evidence_date", errors)
    if as_of_date and dt_eval is not None:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_eval > dt_as_of:
            errors.append(
                f"evidence_date is in the future relative to as_of_date ({edate_str} > {as_of_date})"
            )

    # 7. Catastrophic budget
    cb = c_dict.get("catastrophic_budget")
    disp = c_dict.get("disposition")
    if not isinstance(cb, Mapping) or not cb:
        errors.append("catastrophic_budget is mandatory and must be a non-empty object")
    else:
        for bk in cb:
            if bk not in _BUDGET_KEYS:
                errors.append(f"unknown field {bk!r} in catastrophic_budget object")
        max_rate = cb.get("max_acceptable_rate")
        obs_rate = cb.get("observed_rate")
        method = cb.get("bound_method")
        ub95 = cb.get("upper_bound_95")
        trials = cb.get("trials")
        zero_ack = cb.get("zero_is_not_zero_risk_acknowledged")

        ok_max = _check_prob_range(
            max_rate, "catastrophic_budget.max_acceptable_rate", errors
        )
        ok_obs = _check_prob_range(
            obs_rate, "catastrophic_budget.observed_rate", errors
        )
        ok_ub = _check_prob_range(ub95, "catastrophic_budget.upper_bound_95", errors)

        if not isinstance(method, str) or not method.strip():
            errors.append(
                "catastrophic_budget.bound_method must specify statistical bound method"
            )
        elif method not in ALLOWED_BOUND_METHODS:
            errors.append(
                f"catastrophic_budget.bound_method {method!r} is unsupported; allowed methods: {sorted(ALLOWED_BOUND_METHODS)}"
            )

        unit = cb.get("unit")
        if unit is not None and (not isinstance(unit, str) or not unit.strip()):
            errors.append(
                "catastrophic_budget.unit must be a non-empty string identifying unit of observation"
            )

        if not isinstance(trials, int) or isinstance(trials, bool) or trials <= 0:
            errors.append("catastrophic_budget.trials must be a positive integer")

        if ok_obs and ok_ub and obs_rate is not None and ub95 is not None:
            if obs_rate > ub95:
                errors.append(
                    f"catastrophic_budget violation: observed_rate must be <= upper_bound_95 ({obs_rate} > {ub95})"
                )
            if obs_rate == 0:
                if ub95 <= 0:
                    errors.append(
                        "catastrophic_budget violation: zero observed failures on positive trials "
                        "cannot have zero or negative upper bound (zero observed is not zero risk)"
                    )
                if zero_ack is not True:
                    errors.append(
                        "catastrophic_budget.zero_is_not_zero_risk_acknowledged must be True"
                    )

            if (
                method in ALLOWED_BOUND_METHODS
                and isinstance(trials, int)
                and trials > 0
            ):
                obs_events = int(round(obs_rate * trials))
                try:
                    calc_ub = calculate_one_sided_95_upper_bound(
                        trials, obs_events, method
                    )
                    # Reject if declared upper_bound_95 is significantly lower than honest mathematical bound
                    if ub95 < calc_ub - 0.01:
                        errors.append(
                            f"catastrophic_budget violation: declared upper_bound_95 ({ub95}) is lower than derived mathematical bound ({calc_ub:.6f}) under method {method!r}"
                        )
                except Exception as exc:
                    errors.append(f"catastrophic_budget bound calculation error: {exc}")

        if ok_max and ok_ub and max_rate is not None and ub95 is not None:
            if disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN) and ub95 > max_rate:
                errors.append(
                    f"catastrophic_budget violation: upper_bound_95 exceeds max_acceptable_rate ({ub95} > {max_rate})"
                )

    # 8. Review burden
    rb = c_dict.get("review_burden")
    if not isinstance(rb, Mapping) or not rb:
        errors.append("review_burden is mandatory and must be a non-empty object")
    else:
        for rk in rb:
            if rk not in _REVIEW_KEYS:
                errors.append(f"unknown field {rk!r} in review_burden object")
        status = rb.get("status")
        if status not in REVIEW_BURDEN_STATUSES:
            errors.append(
                f"review_burden.status must be one of {sorted(REVIEW_BURDEN_STATUSES)}, got {status!r}"
            )
        elif status == "measured":
            self_flagged = rb.get("self_flagged_count")
            unverified = rb.get("unverified_emitted_count")
            q_time = rb.get("queue_time_ms_p50")
            if (
                not isinstance(self_flagged, int)
                or isinstance(self_flagged, bool)
                or self_flagged < 0
            ):
                errors.append(
                    "measured review_burden requires non-negative self_flagged_count"
                )
            if (
                not isinstance(unverified, int)
                or isinstance(unverified, bool)
                or unverified < 0
            ):
                errors.append(
                    "measured review_burden requires non-negative unverified_emitted_count"
                )
            if q_time is not None and (
                not isinstance(q_time, (int, float))
                or isinstance(q_time, bool)
                or q_time < 0
            ):
                errors.append(
                    "measured review_burden requires non-negative queue_time_ms_p50"
                )
        elif status in ("unavailable", "not_applicable"):
            reason = rb.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(
                    f"review_burden status {status!r} requires non-empty reason explaining absence"
                )

    # 9. Unresolved limits
    limits = c_dict.get("unresolved_limits")
    if not isinstance(limits, (list, tuple)) or not limits:
        errors.append(
            "unresolved_limits is mandatory and must be a non-empty list of known limitations"
        )
    else:
        for lim in limits:
            if not isinstance(lim, str) or not lim.strip():
                errors.append("unresolved limit entry must be a non-empty string")

    # Disposition
    if disp not in CLAIM_DISPOSITIONS:
        errors.append(
            f"disposition must be one of {sorted(CLAIM_DISPOSITIONS)}, got {disp!r}"
        )

    # Corpus scope
    corpus_scope = c_dict.get("corpus_scope", "")
    if disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN, CLAIM_ROUTES_TO):
        if not isinstance(corpus_scope, str) or not corpus_scope.strip():
            errors.append(f"claim disposition {disp!r} requires non-empty corpus_scope")

    # Evidence bindings & scope checks
    bindings = c_dict.get("evidence_bindings")
    if not isinstance(bindings, (list, tuple)):
        errors.append(
            "evidence_bindings must be a list of ClaimEvidenceBinding objects"
        )
    elif (
        disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN, CLAIM_ROUTES_TO)
        and not bindings
    ):
        errors.append(
            f"claim disposition {disp!r} requires at least one evidence binding"
        )
    elif bindings:
        for i, b in enumerate(bindings):
            if not isinstance(b, Mapping):
                errors.append(f"evidence_binding #{i} must be an object")
                continue
            for bk in b:
                if bk not in _BINDING_KEYS:
                    errors.append(f"unknown field {bk!r} in evidence_binding #{i}")
            path = b.get("artifact_path")
            sha = b.get("artifact_sha256")
            lc = b.get("lifecycle")
            b_wf = b.get("workflow_scope")
            b_corpus = b.get("corpus_scope")
            b_comps = b.get("comparator_scope", [])
            ptrs = b.get("metric_pointers")

            if not isinstance(path, str) or not path.strip():
                errors.append(
                    f"evidence_binding #{i} artifact_path must be non-empty string"
                )
            if not isinstance(sha, str) or not _HEX64.match(sha):
                errors.append(
                    f"evidence_binding #{i} artifact_sha256 must be 64-char hex SHA-256"
                )
            if lc not in EVIDENCE_LIFECYCLES:
                errors.append(
                    f"evidence_binding #{i} lifecycle must be one of {sorted(EVIDENCE_LIFECYCLES)}"
                )
            elif lc in (EVIDENCE_STALE, EVIDENCE_SUPERSEDED) and disp in (
                CLAIM_BEATS,
                CLAIM_TIES_REDUCING_BURDEN,
                CLAIM_ROUTES_TO,
            ):
                errors.append(
                    f"claim claiming {disp} cannot rest on {lc} evidence ({path})"
                )

            # Scope checks against claim
            if disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN, CLAIM_ROUTES_TO):
                if not isinstance(b_wf, str) or not b_wf.strip():
                    errors.append(
                        f"evidence_binding #{i} workflow_scope must be non-empty string"
                    )
                elif wf and b_wf != wf:
                    errors.append(
                        f"evidence_binding #{i} workflow_scope {b_wf!r} does not match claim workflow {wf!r}"
                    )

                if not isinstance(b_corpus, str) or not b_corpus.strip():
                    errors.append(
                        f"evidence_binding #{i} corpus_scope must be non-empty string"
                    )
                elif corpus_scope and b_corpus != corpus_scope:
                    errors.append(
                        f"evidence_binding #{i} corpus_scope {b_corpus!r} does not match claim corpus_scope {corpus_scope!r}"
                    )

                if not isinstance(b_comps, (list, tuple)) or not b_comps:
                    errors.append(
                        f"evidence_binding #{i} comparator_scope must be a non-empty list of comparator identifiers"
                    )
                elif comps:
                    if set(b_comps) != set(comps):
                        errors.append(
                            f"evidence_binding #{i} comparator_scope must match claim competitors exactly: {sorted(set(b_comps))} != {sorted(set(comps))}"
                        )
            else:
                if b_wf and wf and b_wf != wf:
                    errors.append(
                        f"evidence_binding #{i} workflow_scope {b_wf!r} does not match claim workflow {wf!r}"
                    )
                if b_corpus and corpus_scope and b_corpus != corpus_scope:
                    errors.append(
                        f"evidence_binding #{i} corpus_scope {b_corpus!r} does not match claim corpus_scope {corpus_scope!r}"
                    )

            if not isinstance(ptrs, Mapping) or not ptrs:
                errors.append(
                    f"evidence_binding #{i} metric_pointers must be non-empty mapping"
                )

    # Anti-universalization check
    disclaimer = c_dict.get("universal_disclaimer")
    if not isinstance(disclaimer, str) or not disclaimer.strip():
        errors.append(
            "universal_disclaimer is mandatory to prevent global overstatement"
        )
    elif disclaimer.strip() != CANONICAL_UNIVERSAL_DISCLAIMER:
        disclaimer_lower = disclaimer.lower()
        has_forbidden = False
        for forbidden in _FORBIDDEN_UNIVERSAL_PATTERNS:
            if forbidden in disclaimer_lower:
                errors.append(
                    f"universal_disclaimer cannot make un-scoped universal/global superiority assertions: found {forbidden!r}"
                )
                has_forbidden = True
        if not has_forbidden:
            errors.append(
                f"universal_disclaimer must match canonical bounded disclaimer {CANONICAL_UNIVERSAL_DISCLAIMER!r}"
            )

    return errors
