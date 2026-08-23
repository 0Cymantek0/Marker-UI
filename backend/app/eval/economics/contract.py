"""Envelope contract for economics evidence (invariants 57/58).

An envelope is a flat mapping of named dimensions to one metric record
each. Every metric states whether it was measured, derived, is
unavailable, or is not applicable to the profile — ``0`` and missing are
never allowed to stand in for "unknown". Timing values must carry sample
counts; derived ratios must name their raw numerator and denominator and
both must come from the same measurement window, so cross-workload
ratios are structurally impossible to express.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

ENVELOPE_SCHEMA = "marker.economics_envelope.v1"

#: metric states — anything outside this set fails validation
STATUSES = ("measured", "derived", "unavailable", "not_applicable")

#: closed unit vocabulary; every quantitative metric must name one
UNITS = (
    "count",
    "bytes",
    "milliseconds",
    "seconds",
    "ratio",
    "rate",
    "boolean",
    "identifier",
)

#: timing units require a samples block with an explicit sample count
TIMING_UNITS = ("milliseconds", "seconds")

#: dimension registry per invariant — envelopes declare which set they
#: report and unknown dimension names fail validation
INV57_DIMENSIONS = (
    "database_rows",
    "payload_objects",
    "wal_write_amplification",
    "retained_generations",
    "fts_storage",
    "vector_storage",
    "visual_storage",
    "copy_bytes",
    "cold_start",
    "review_burden",
    "reprocessing",
)
INV58_DIMENSIONS = (
    "quality_gain",
    "storage_delta",
    "build_delta",
    "query_delta",
    "model_service_delta",
    "acl_complexity",
    "disabled_state_proof",
    "decision",
)
DIMENSION_SETS = {"invariant_57": INV57_DIMENSIONS, "invariant_58": INV58_DIMENSIONS}

#: run modes for any envelope whose workload touches a model service
RUN_MODES = ("offline", "offline-replay", "live", "hybrid")


@dataclass(frozen=True)
class Metric:
    """One dimension of the envelope.

    ``measured``/``derived`` metrics carry ``value`` + ``unit`` +
    ``source`` (how the number was obtained). ``unavailable`` /
    ``not_applicable`` metrics must omit ``value`` entirely and carry a
    ``reason`` — an unavailable metric encoded as ``0`` fails
    validation. Timing metrics carry ``samples`` (``n`` plus summary);
    a percentile without ``n`` fails validation.
    """

    status: str
    unit: str
    window: str
    value: int | float | str | bool | None = None
    source: str | None = None
    reason: str | None = None
    breakdown: Mapping[str, int | float] | None = None
    samples: Mapping[str, Any] | None = None
    derivation: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "unit": self.unit, "window": self.window}
        if self.value is not None:
            out["value"] = self.value
        if self.source is not None:
            out["source"] = self.source
        if self.reason is not None:
            out["reason"] = self.reason
        if self.breakdown is not None:
            out["breakdown"] = dict(self.breakdown)
        if self.samples is not None:
            out["samples"] = dict(self.samples)
        if self.derivation is not None:
            out["derivation"] = dict(self.derivation)
        return out


def measured(
    value: int | float | str | bool,
    unit: str,
    window: str,
    source: str,
    *,
    breakdown: Mapping[str, int | float] | None = None,
    samples: Mapping[str, Any] | None = None,
) -> Metric:
    return Metric(
        status="measured", unit=unit, window=window, value=value,
        source=source, breakdown=breakdown, samples=samples,
    )


def derived(
    value: int | float,
    unit: str,
    window: str,
    source: str,
    derivation: Mapping[str, str],
) -> Metric:
    return Metric(
        status="derived", unit=unit, window=window, value=value,
        source=source, derivation=derivation,
    )


def unavailable(unit: str, window: str, reason: str) -> Metric:
    return Metric(status="unavailable", unit=unit, window=window, reason=reason)


def not_applicable(unit: str, window: str, reason: str) -> Metric:
    return Metric(status="not_applicable", unit=unit, window=window, reason=reason)


@dataclass
class Envelope:
    """One profile's economics envelope plus its provenance."""

    profile: str
    dimension_set: str
    workload: dict[str, Any]
    environment: dict[str, Any]
    windows: list[dict[str, Any]] = field(default_factory=list)
    dimensions: dict[str, Metric] = field(default_factory=dict)
    counters: dict[str, Metric] = field(default_factory=dict)
    run_mode: str = "offline"
    model_participation: dict[str, Any] = field(default_factory=dict)
    non_claims: list[str] = field(default_factory=list)
    git_sha: str = ""
    generated_at: str = ""

    def set(self, name: str, metric: Metric) -> None:
        self.dimensions[name] = metric

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ENVELOPE_SCHEMA,
            "profile": self.profile,
            "dimension_set": self.dimension_set,
            "git_sha": self.git_sha,
            "generated_at": self.generated_at,
            "run_mode": self.run_mode,
            "model_participation": dict(self.model_participation),
            "workload": self.workload,
            "environment": self.environment,
            "windows": list(self.windows),
            "dimensions": {name: m.to_dict() for name, m in self.dimensions.items()},
            "counters": {name: m.to_dict() for name, m in self.counters.items()},
            "non_claims": list(self.non_claims),
        }
