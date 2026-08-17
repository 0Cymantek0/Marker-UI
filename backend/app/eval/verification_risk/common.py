"""Shared validation contracts for verification-risk evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

VERIFICATION_RISK_CORPUS_SCHEMA_VERSION = "marker.verification_risk_corpus.v1"
VERIFICATION_RISK_REPORT_SCHEMA_VERSION = "marker.verification_risk_report.v1"
WILSON_Z_95 = 1.959963984540054

Disclosure = Literal["complete", "partial", "unknown"]

_CORPUS_FIELDS = frozenset(
    {
        "$schema",
        "schema_version",
        "name",
        "witnesses",
        "samples",
        "metadata",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "sample_id",
        "id",
        "label",
        "truth",
        "outcomes",
        "witnesses",
        "slice",
        "slice_id",
        "case",
        "distribution",
        "risk_level",
        "catastrophic",
        "catastrophic_label",
        "metadata",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "prediction",
        "value",
        "correct",
        "confidence",
        "score",
        "catastrophic",
        "metadata",
    }
)
_LIST_OUTCOME_FIELDS = _OUTCOME_FIELDS | frozenset({"id", "witness_id"})
_WITNESS_FIELDS = frozenset(
    {
        "id",
        "witness_id",
        "label",
        "kind",
        "model_family",
        "base_lineage",
        "base_model",
        "teacher_lineage",
        "disclosure",
        "lineage_status",
        "renderer",
        "cropper",
        "detector",
        "preprocessor",
        "postprocessor",
        "prompt_identity",
        "runtime_profile",
        "quantization",
        "shared_dependency_group",
        "alias_of",
        "authority_class",
        "source_native",
        "dependency_profile",
        "metadata",
    }
)
_DEPENDENCY_PROFILE_FIELDS = frozenset(
    {
        "model_family",
        "base_lineage",
        "base_model",
        "teacher_lineage",
        "disclosure",
        "lineage_status",
        "renderer",
        "cropper",
        "detector",
        "preprocessor",
        "postprocessor",
        "prompt_identity",
        "runtime_profile",
        "quantization",
        "shared_dependency_group",
    }
)

# Operational fields do not describe semantic corpus/evaluation content.
# ``runtime_profile`` remains semantic; plain ``runtime`` is treated as
# operational metadata by this report contract and therefore excluded.
_NON_SEMANTIC_KEYS = frozenset(
    {
        "runtime",
        "runtime_ms",
        "runtime_seconds",
        "elapsed_ms",
        "duration_ms",
        "evaluation_runtime_ms",
        "measured_runtime_ms",
        "wall_time_ms",
        "generation_runtime_ms",
        "generated_at",
        "created_at",
        "updated_at",
        "timestamp",
        "operational_cost",
    }
)

class VerificationRiskError(ValueError):
    """Invalid or unsafe verification-risk corpus/evaluation input."""


def _reject_unknown_fields(
    data: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    context: str,
) -> None:
    unknown = sorted(str(key) for key in data if str(key) not in allowed)
    if unknown:
        raise VerificationRiskError(f"{context} contains unknown fields: {', '.join(unknown)}")

def _as_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise VerificationRiskError(f"{field_name} must be boolean")


def _as_probability(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationRiskError(f"{field_name} must be a number in [0, 1]")
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise VerificationRiskError(f"{field_name} must be a finite number in [0, 1]")
    return probability


def _as_text(value: Any, *, field_name: str, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise VerificationRiskError(f"{field_name} is required")
        return None
    text = str(value).strip()
    if required and not text:
        raise VerificationRiskError(f"{field_name} must not be empty")
    return text or None


def _normalise_disclosure(data: Mapping[str, Any]) -> Disclosure:
    profile = data.get("dependency_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    raw = (
        data.get("disclosure")
        or data.get("lineage_status")
        or profile.get("disclosure")
        or profile.get("lineage_status")
        or "unknown"
    )
    value = str(raw).strip().lower().replace("-", "_")
    if value in {"complete", "fully_disclosed", "known"}:
        return "complete"
    if value in {"partial", "partially_disclosed"}:
        return "partial"
    if value in {"unknown", "undisclosed", "black_box"}:
        return "unknown"
    raise VerificationRiskError(
        f"unsupported dependency disclosure {raw!r}; expected complete, partial, or unknown"
    )


def _profile_value(data: Mapping[str, Any], key: str) -> Any:
    profile = data.get("dependency_profile")
    if isinstance(profile, Mapping) and key in profile:
        return profile[key]
    return data.get(key)
