"""Fail-closed validation for economics envelope artifacts.

Returns every honesty violation as a human-readable error string; an
empty list is the only passing state. The rules enforce the evidence
discipline the readiness machinery cannot express on its own:

* unavailable / not-applicable dimensions must omit ``value`` entirely
  — a ``0`` can never masquerade as "unknown";
* every quantitative metric names a unit from the closed vocabulary;
* every dimension of the declared dimension set is present, so an
  envelope cannot silently drop an inconvenient dimension;
* derived ratios name their raw numerator and denominator, both
  ``measured``, both from the same window as the ratio — cross-workload
  ratios are structurally inexpressible;
* timing metrics carry an explicit sample count, and any percentile
  claim needs at least two samples.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from app.eval.economics.contract import (
    DIMENSION_SETS,
    ENVELOPE_SCHEMA,
    RUN_MODES,
    STATUSES,
    TIMING_UNITS,
    UNITS,
)

_NUMERIC_UNITS = (
    "count", "bytes", "milliseconds", "seconds", "ratio", "rate",
    "delta_count", "delta_bytes", "delta_rate",
)
_DELTA_UNITS = ("delta_count", "delta_bytes", "delta_rate")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_PERCENTILE_KEYS = ("p50", "p90", "p95", "p99", "max")


def validate_envelope(envelope: Mapping[str, Any]) -> list[str]:
    """Validate one parsed envelope; empty list == acceptable evidence."""
    errors: list[str] = []

    if envelope.get("schema") != ENVELOPE_SCHEMA:
        errors.append(f"schema must be {ENVELOPE_SCHEMA!r}")
        return errors

    profile = envelope.get("profile")
    if not isinstance(profile, str) or not profile.strip():
        errors.append("profile must be a non-empty string")

    dimension_set = envelope.get("dimension_set")
    if dimension_set not in DIMENSION_SETS:
        errors.append(
            f"dimension_set must be one of {sorted(DIMENSION_SETS)}, got {dimension_set!r}"
        )
        return errors

    git_sha = envelope.get("git_sha")
    if not isinstance(git_sha, str) or not _HEX40.match(git_sha):
        errors.append("git_sha must be the 40-hex commit the run measured")

    generated_at = envelope.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        errors.append("generated_at must be a non-empty timestamp")

    run_mode = envelope.get("run_mode")
    if run_mode not in RUN_MODES:
        errors.append(f"run_mode must be one of {RUN_MODES}, got {run_mode!r}")

    workload = envelope.get("workload")
    if not isinstance(workload, Mapping):
        errors.append("workload must be an object")
    else:
        for key in ("identity", "fingerprint"):
            value = workload.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"workload.{key} must be a non-empty string")

    environment = envelope.get("environment")
    if not isinstance(environment, Mapping) or not environment:
        errors.append("environment must be a non-empty object")

    window_ids = _validate_windows(envelope.get("windows"), errors)
    dimensions = envelope.get("dimensions")
    if not isinstance(dimensions, Mapping) or not dimensions:
        errors.append("dimensions must be a non-empty object")
        return errors

    required = DIMENSION_SETS[dimension_set]
    for name in required:
        if name not in dimensions:
            errors.append(f"dimension {name!r} missing — every dimension of the "
                          f"set must be present, use unavailable/not_applicable states")
    for name in dimensions:
        if name not in required:
            errors.append(f"dimension {name!r} is not part of dimension_set "
                          f"{dimension_set!r}")

    counters = envelope.get("counters", {})
    if not isinstance(counters, Mapping):
        errors.append("counters must be an object of raw measured counters")
        counters = {}
    for name, counter in counters.items():
        _validate_metric(name, counter, window_ids, {}, errors, where_prefix="counter")
        if isinstance(counter, Mapping) and counter.get("status") != "measured":
            errors.append(f"counter {name!r} must be status 'measured' — counters "
                          f"exist to expose raw numerator/denominator values")

    resolved: dict[str, Any] = {**counters, **dimensions}
    for name, metric in dimensions.items():
        _validate_metric(name, metric, window_ids, resolved, errors)

    return errors


def _validate_windows(windows: Any, errors: list[str]) -> set[str]:
    if not isinstance(windows, list) or not windows:
        errors.append("windows must be a non-empty list of measurement windows")
        return set()
    ids: set[str] = set()
    for window in windows:
        if not isinstance(window, Mapping):
            errors.append("each window must be an object")
            continue
        window_id = window.get("id")
        label = window.get("label")
        if not isinstance(window_id, str) or not window_id.strip():
            errors.append(f"window {window!r} needs a non-empty id")
            continue
        if window_id in ids:
            errors.append(f"duplicate window id {window_id!r}")
        ids.add(window_id)
        if not isinstance(label, str) or not label.strip():
            errors.append(f"window {window_id!r} needs a non-empty label")
    return ids


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_metric(
    name: str,
    metric: Any,
    window_ids: set[str],
    dimensions: Mapping[str, Any],
    errors: list[str],
    where_prefix: str = "dimension",
) -> None:
    where = f"{where_prefix} {name!r}"
    if not isinstance(metric, Mapping):
        errors.append(f"{where} must be an object")
        return

    status = metric.get("status")
    if status not in STATUSES:
        errors.append(f"{where} status must be one of {STATUSES}, got {status!r}")
        return
    unit = metric.get("unit")
    if unit not in UNITS:
        errors.append(f"{where} unit must be one of {UNITS}, got {unit!r}")

    window = metric.get("window")
    if window not in window_ids:
        errors.append(f"{where} references undeclared window {window!r}")

    value = metric.get("value")
    reason = metric.get("reason")
    source = metric.get("source")

    if status in ("measured", "derived"):
        if value is None:
            errors.append(f"{where} is {status} but carries no value")
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{where} is {status} and must state how it was obtained")
        if reason is not None:
            errors.append(f"{where} is {status} and must not carry a reason")
        if unit in _NUMERIC_UNITS and value is not None:
            if not _is_number(value):
                errors.append(f"{where} unit {unit!r} requires a numeric value")
            elif unit in ("count", "bytes") and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                errors.append(f"{where} unit {unit!r} requires an integer value")
            elif unit == "delta_count" and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                errors.append(f"{where} unit 'delta_count' requires an integer value")
            elif unit == "rate" and not 0.0 <= float(value) <= 1.0:
                errors.append(f"{where} unit 'rate' requires a value in [0, 1]")
            elif unit in ("count", "bytes") and value < 0:
                errors.append(f"{where} unit {unit!r} cannot be negative")
    else:
        if value is not None:
            errors.append(f"{where} is {status} and must omit value entirely — "
                          f"encode unavailable state, never a stand-in number")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{where} is {status} and must carry a reason")

    if unit == "boolean" and status in ("measured", "derived") and not isinstance(value, bool):
        errors.append(f"{where} unit 'boolean' requires a boolean value")
    if unit == "identifier" and status in ("measured", "derived") and not isinstance(value, str):
        errors.append(f"{where} unit 'identifier' requires a string value")

    _validate_breakdown(name, metric.get("breakdown"), errors)
    _validate_samples(name, metric, unit, status, errors)
    _validate_derivation(name, metric, unit, status, window, dimensions, errors)


def _validate_breakdown(name: str, breakdown: Any, errors: list[str]) -> None:
    if breakdown is None:
        return
    where = f"dimension {name!r}"
    if not isinstance(breakdown, Mapping) or not breakdown:
        errors.append(f"{where} breakdown must be a non-empty object")
        return
    for key, value in breakdown.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{where} breakdown keys must be non-empty strings")
        if not _is_number(value):
            # signed values are fine (deltas may be negative); anything
            # non-numeric is not
            errors.append(f"{where} breakdown[{key!r}] must be a number")


def _validate_samples(
    name: str, metric: Mapping[str, Any], unit: Any, status: str, errors: list[str]
) -> None:
    samples = metric.get("samples")
    if unit not in TIMING_UNITS:
        if samples is not None:
            errors.append(f"dimension {name!r} is not a timing metric but carries samples")
        return
    if status not in ("measured", "derived"):
        return
    where = f"dimension {name!r}"
    if not isinstance(samples, Mapping):
        errors.append(f"{where} is a timing metric and must carry a samples block")
        return
    n = samples.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        errors.append(f"{where} samples must state an integer sample count n >= 1")
        return
    has_percentile = any(key in samples for key in _PERCENTILE_KEYS)
    if has_percentile and n < 2:
        errors.append(f"{where} claims a percentile/max from a single sample")
    stats = [(key, samples[key]) for key in ("min", "p50", "max") if key in samples]
    for key, value in stats:
        if not _is_number(value) or value < 0:
            errors.append(f"{where} samples.{key} must be a non-negative number")
    ordered = [value for _key, value in stats]
    if ordered and any(
        later < earlier for earlier, later in zip(ordered, ordered[1:])
    ):
        errors.append(f"{where} samples summary must be monotonic (min <= p50 <= max)")


def _validate_derivation(
    name: str,
    metric: Mapping[str, Any],
    unit: Any,
    status: str,
    window: Any,
    dimensions: Mapping[str, Any],
    errors: list[str],
) -> None:
    derivation = metric.get("derivation")
    if unit == "ratio" and status in ("measured", "derived"):
        if not isinstance(derivation, Mapping):
            errors.append(f"dimension {name!r} unit 'ratio' must declare its derivation")
            return
    if derivation is None:
        return
    where = f"dimension {name!r}"
    if status != "derived":
        errors.append(f"{where} carries a derivation but is not status 'derived'")
        return
    numerator = derivation.get("numerator")
    denominator = derivation.get("denominator")
    for role, ref in (("numerator", numerator), ("denominator", denominator)):
        if not isinstance(ref, str) or ref not in dimensions:
            errors.append(f"{where} derivation {role} {ref!r} is not a metric of this envelope")
    if numerator not in dimensions or denominator not in dimensions:
        return
    num_metric = dimensions[numerator]
    den_metric = dimensions[denominator]
    for role, ref_metric in (("numerator", num_metric), ("denominator", den_metric)):
        if not isinstance(ref_metric, Mapping):
            errors.append(f"{where} derivation {role} is not a metric object")
            continue
        if ref_metric.get("status") != "measured":
            errors.append(f"{where} derivation {role} must reference a 'measured' raw "
                          f"counter, not a derived or missing value")
        if ref_metric.get("unit") == "ratio":
            errors.append(f"{where} derivation {role} must not itself be a ratio")
    num_window = num_metric.get("window") if isinstance(num_metric, Mapping) else None
    den_window = den_metric.get("window") if isinstance(den_metric, Mapping) else None
    if num_window != den_window or num_window != window:
        errors.append(
            f"{where} ratio combines numerator window {num_window!r} and denominator "
            f"window {den_window!r} into {window!r} — cross-workload ratios are invalid"
        )
    den_value = den_metric.get("value") if isinstance(den_metric, Mapping) else None
    if _is_number(den_value) and den_value == 0:
        errors.append(f"{where} ratio has a zero denominator")


def load_envelope(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Parse + validate an envelope artifact file."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"cannot read envelope {path}: {exc}"]
    if not isinstance(parsed, dict):
        return {}, [f"envelope {path} must be a JSON object"]
    return parsed, validate_envelope(parsed)
