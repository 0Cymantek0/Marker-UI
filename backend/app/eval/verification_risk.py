"""Deterministic verification-risk evaluation over labeled witness outcomes.

This module is deliberately report-only.  It consumes a small, versioned JSON
corpus and exposes dependency, joint-error, disagreement, calibration, and
baseline measurements without making a claim authoritative by itself.

Confidence values in this module mean ``P(correct)``.  They are observations;
calibration results below are derived from labeled outcomes and a declared
slice.  Missing support and zero denominators remain explicit ``None`` values.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


VERIFICATION_RISK_CORPUS_SCHEMA_VERSION = "marker.verification_risk_corpus.v1"
VERIFICATION_RISK_REPORT_SCHEMA_VERSION = "marker.verification_risk_report.v1"
WILSON_Z_95 = 1.959963984540054

Disclosure = Literal["complete", "partial", "unknown"]

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


def _canonical_value(value: Any) -> Any:
    """Return JSON-safe canonical value with operational metadata removed."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _NON_SEMANTIC_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VerificationRiskError("non-finite value cannot enter semantic identity")
        return value
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _identity(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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


@dataclass(frozen=True)
class WitnessProfile:
    """Stable witness/dependency disclosure used by risk policies."""

    witness_id: str
    label: str = ""
    kind: str = "model"
    model_family: str | None = None
    base_lineage: str | None = None
    teacher_lineage: str | None = None
    disclosure: Disclosure = "unknown"
    renderer: str | None = None
    cropper: str | None = None
    detector: str | None = None
    preprocessor: str | None = None
    postprocessor: str | None = None
    prompt_identity: str | None = None
    runtime_profile: str | None = None
    quantization: str | None = None
    shared_dependency_group: str | None = None
    alias_of: str | None = None
    authority_class: str | None = None
    source_native: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WitnessProfile":
        if not isinstance(data, Mapping):
            raise VerificationRiskError("witness entry must be an object")
        _reject_unknown_fields(data, _WITNESS_FIELDS, context="witness")
        witness_id = _as_text(data.get("witness_id", data.get("id")), field_name="witness_id")
        assert witness_id is not None
        kind = str(data.get("kind") or "model").strip().lower()
        profile = data.get("dependency_profile")
        if profile is not None and not isinstance(profile, Mapping):
            raise VerificationRiskError(f"witness {witness_id!r} dependency_profile must be an object")
        if isinstance(profile, Mapping):
            _reject_unknown_fields(
                profile,
                _DEPENDENCY_PROFILE_FIELDS,
                context=f"witness {witness_id!r} dependency_profile",
            )
        authority_class = data.get("authority_class")
        declared_source_native = (
            _as_bool(data["source_native"], field_name=f"witness {witness_id}.source_native")
            if "source_native" in data
            else False
        )
        source_native = declared_source_native or kind in {
            "source_native",
            "deterministic",
            "human_reviewed",
        }
        if authority_class is None and source_native:
            authority_class = "source_native" if kind != "human_reviewed" else "human_reviewed"
        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise VerificationRiskError(f"witness {witness_id!r} metadata must be an object")
        return cls(
            witness_id=witness_id,
            label=str(data.get("label") or witness_id),
            kind=kind,
            model_family=_as_text(_profile_value(data, "model_family"), field_name="model_family", required=False),
            base_lineage=_as_text(
                _profile_value(data, "base_lineage") or _profile_value(data, "base_model"),
                field_name="base_lineage",
                required=False,
            ),
            teacher_lineage=_as_text(
                _profile_value(data, "teacher_lineage"),
                field_name="teacher_lineage",
                required=False,
            ),
            disclosure=_normalise_disclosure(data),
            renderer=_as_text(_profile_value(data, "renderer"), field_name="renderer", required=False),
            cropper=_as_text(_profile_value(data, "cropper"), field_name="cropper", required=False),
            detector=_as_text(_profile_value(data, "detector"), field_name="detector", required=False),
            preprocessor=_as_text(
                _profile_value(data, "preprocessor"),
                field_name="preprocessor",
                required=False,
            ),
            postprocessor=_as_text(
                _profile_value(data, "postprocessor"),
                field_name="postprocessor",
                required=False,
            ),
            prompt_identity=_as_text(
                _profile_value(data, "prompt_identity"),
                field_name="prompt_identity",
                required=False,
            ),
            runtime_profile=_as_text(
                _profile_value(data, "runtime_profile"),
                field_name="runtime_profile",
                required=False,
            ),
            quantization=_as_text(
                _profile_value(data, "quantization"),
                field_name="quantization",
                required=False,
            ),
            shared_dependency_group=_as_text(
                _profile_value(data, "shared_dependency_group"),
                field_name="shared_dependency_group",
                required=False,
            ),
            alias_of=_as_text(data.get("alias_of"), field_name="alias_of", required=False),
            authority_class=str(authority_class).strip() if authority_class is not None else None,
            source_native=source_native,
            metadata=dict(metadata or {}),
        )

    @property
    def is_authority_bearing(self) -> bool:
        return self.source_native or self.kind in {"deterministic", "source_native", "human_reviewed"}

    @property
    def has_known_lineage(self) -> bool:
        return self.disclosure == "complete" and bool(self.base_lineage or self.model_family)

    @property
    def dependency_key(self) -> tuple[str | None, ...]:
        """Key for conservative grouping; unknown fields stay unknown."""

        return (
            self.alias_of,
            self.shared_dependency_group,
            self.base_lineage,
            self.teacher_lineage,
            self.renderer,
            self.cropper,
            self.detector,
            self.preprocessor,
            self.postprocessor,
            self.prompt_identity,
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "witness_id": self.witness_id,
            "label": self.label,
            "kind": self.kind,
            "model_family": self.model_family,
            "base_lineage": self.base_lineage,
            "teacher_lineage": self.teacher_lineage,
            "disclosure": self.disclosure,
            "renderer": self.renderer,
            "cropper": self.cropper,
            "detector": self.detector,
            "preprocessor": self.preprocessor,
            "postprocessor": self.postprocessor,
            "prompt_identity": self.prompt_identity,
            "runtime_profile": self.runtime_profile,
            "quantization": self.quantization,
            "shared_dependency_group": self.shared_dependency_group,
            "alias_of": self.alias_of,
            "authority_class": self.authority_class,
            "source_native": self.source_native,
            "metadata": dict(self.metadata),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "dependency_key": list(self.dependency_key),
        }


@dataclass(frozen=True)
class WitnessOutcome:
    """One witness prediction for one labeled sample."""

    prediction: Any
    confidence: float | None = None
    catastrophic: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        label: Any,
        witness_id: str,
    ) -> "WitnessOutcome":
        if not isinstance(data, Mapping):
            raise VerificationRiskError(f"outcome for witness {witness_id!r} must be an object")
        if "prediction" in data:
            prediction = data["prediction"]
        elif "value" in data:
            prediction = data["value"]
        elif "correct" in data:
            correct = _as_bool(data["correct"], field_name=f"{witness_id}.correct")
            if correct:
                prediction = label
            elif isinstance(label, bool):
                prediction = not label
            else:
                # Correctness-only inputs cannot disclose which wrong class was
                # chosen.  Stable sentinel preserves error/agreement semantics.
                prediction = f"__incorrect__:{witness_id}"
        else:
            raise VerificationRiskError(
                f"outcome for witness {witness_id!r} missing prediction/value/correct"
            )
        confidence_value = data.get("confidence", data.get("score"))
        confidence = (
            None
            if confidence_value is None
            else _as_probability(confidence_value, field_name=f"{witness_id}.confidence")
        )
        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise VerificationRiskError(f"{witness_id!r} outcome metadata must be an object")
        catastrophic = (
            _as_bool(data["catastrophic"], field_name=f"{witness_id}.catastrophic")
            if "catastrophic" in data
            else False
        )
        return cls(
            prediction=prediction,
            confidence=confidence,
            catastrophic=catastrophic,
            metadata=dict(metadata or {}),
        )

    def is_error(self, label: Any) -> bool:
        return self.prediction != label

    def as_dict(self) -> dict[str, Any]:
        return {
            "prediction": self.prediction,
            "confidence": self.confidence,
            "catastrophic": self.catastrophic,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class LabeledSample:
    """One immutable labeled case with one or more witness outcomes."""

    sample_id: str
    label: Any
    outcomes: Mapping[str, WitnessOutcome]
    slice_id: str = "default"
    case: str | None = None
    distribution: str = "matched"
    risk_level: str = "normal"
    catastrophic: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        witness_ids: set[str],
    ) -> "LabeledSample":
        if not isinstance(data, Mapping):
            raise VerificationRiskError("sample entry must be an object")
        sample_id = _as_text(data.get("sample_id", data.get("id")), field_name="sample_id")
        assert sample_id is not None
        if "label" in data:
            label = data["label"]
        elif "truth" in data:
            label = data["truth"]
        else:
            raise VerificationRiskError(f"sample {sample_id!r} missing label")

        raw_outcomes = data.get("outcomes", data.get("witnesses"))
        if isinstance(raw_outcomes, Mapping):
            outcome_items = list(raw_outcomes.items())
        elif isinstance(raw_outcomes, Sequence) and not isinstance(raw_outcomes, (str, bytes, bytearray)):
            outcome_items = []
            seen: set[str] = set()
            for item in raw_outcomes:
                if not isinstance(item, Mapping):
                    raise VerificationRiskError(f"sample {sample_id!r} outcome entry must be an object")
                outcome_id = _as_text(
                    item.get("witness_id", item.get("id")),
                    field_name=f"sample {sample_id}.witness_id",
                )
                assert outcome_id is not None
                if outcome_id in seen:
                    raise VerificationRiskError(
                        f"duplicate witness outcome {outcome_id!r} in sample {sample_id!r}"
                    )
                seen.add(outcome_id)
                outcome_items.append((outcome_id, item))
        else:
            raise VerificationRiskError(f"sample {sample_id!r} outcomes must be an object or list")

        outcomes: dict[str, WitnessOutcome] = {}
        for raw_witness_id, raw_outcome in outcome_items:
            witness_id = str(raw_witness_id).strip()
            if not witness_id:
                raise VerificationRiskError(f"sample {sample_id!r} contains empty witness id")
            if witness_id not in witness_ids:
                raise VerificationRiskError(
                    f"sample {sample_id!r} references unknown witness {witness_id!r}"
                )
            if witness_id in outcomes:
                raise VerificationRiskError(
                    f"duplicate witness outcome {witness_id!r} in sample {sample_id!r}"
                )
            outcomes[witness_id] = WitnessOutcome.from_mapping(
                raw_outcome,
                label=label,
                witness_id=witness_id,
            )
        if len(outcomes) < 1:
            raise VerificationRiskError(f"sample {sample_id!r} must contain at least one outcome")

        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise VerificationRiskError(f"sample {sample_id!r} metadata must be an object")
        slice_id = str(data.get("slice", data.get("slice_id", "default")) or "default").strip()
        distribution = str(data.get("distribution", slice_id) or slice_id).strip().lower()
        risk_level = str(data.get("risk_level", "normal") or "normal").strip().lower()
        catastrophic_value = data.get("catastrophic", data.get("catastrophic_label", False))
        catastrophic = _as_bool(
            catastrophic_value,
            field_name=f"sample {sample_id}.catastrophic",
        )
        return cls(
            sample_id=sample_id,
            label=label,
            outcomes=outcomes,
            slice_id=slice_id or "default",
            case=_as_text(data.get("case"), field_name="case", required=False),
            distribution=distribution or "matched",
            risk_level=risk_level or "normal",
            catastrophic=catastrophic,
            metadata=dict(metadata or {}),
        )

    def is_in_slice(self, slice_id: str | None) -> bool:
        return slice_id is None or self.slice_id == slice_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "label": self.label,
            "slice": self.slice_id,
            "case": self.case,
            "distribution": self.distribution,
            "risk_level": self.risk_level,
            "catastrophic": self.catastrophic,
            "outcomes": {
                key: self.outcomes[key].as_dict() for key in sorted(self.outcomes)
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VerificationRiskCorpus:
    """Validated deterministic corpus."""

    name: str
    witnesses: tuple[WitnessProfile, ...]
    samples: tuple[LabeledSample, ...]
    schema_version: str = VERIFICATION_RISK_CORPUS_SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def witness_by_id(self) -> dict[str, WitnessProfile]:
        return {witness.witness_id: witness for witness in self.witnesses}

    @property
    def sample_by_id(self) -> dict[str, LabeledSample]:
        return {sample.sample_id: sample for sample in self.samples}

    @property
    def slice_ids(self) -> tuple[str, ...]:
        return tuple(sorted({sample.slice_id for sample in self.samples}))

    @property
    def distributions(self) -> tuple[str, ...]:
        return tuple(sorted({sample.distribution for sample in self.samples}))

    def samples_for_slice(self, slice_id: str | None = None) -> tuple[LabeledSample, ...]:
        return tuple(sample for sample in self.samples if sample.is_in_slice(slice_id))

    def samples_for_distribution(
        self,
        distribution: str | None = None,
    ) -> tuple[LabeledSample, ...]:
        if distribution is None:
            return self.samples
        return tuple(sample for sample in self.samples if sample.distribution == distribution)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "witnesses": [
                witness.semantic_payload()
                for witness in sorted(self.witnesses, key=lambda item: item.witness_id)
            ],
            "samples": [
                sample.as_dict()
                for sample in sorted(self.samples, key=lambda item: item.sample_id)
            ],
            "metadata": dict(self.metadata),
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    @property
    def artifact_identity(self) -> str:
        return self.semantic_identity

    def as_dict(self) -> dict[str, Any]:
        return {
            "$schema": self.schema_version,
            "schema_version": self.schema_version,
            "name": self.name,
            "witnesses": [witness.as_dict() for witness in self.witnesses],
            "samples": [sample.as_dict() for sample in self.samples],
            "metadata": dict(self.metadata),
            "semantic_identity": self.semantic_identity,
        }


def load_verification_risk_corpus(
    source: str | Path | Mapping[str, Any],
) -> VerificationRiskCorpus:
    """Load and validate corpus from path or already-decoded mapping.

    Duplicate sample/witness ids, duplicate list outcomes, and unknown witness
    references fail closed.  Runtime fields are retained in ``metadata`` but
    excluded from ``semantic_identity``.
    """

    if isinstance(source, Mapping):
        data = dict(source)
        source_name = "<mapping>"
    else:
        path = Path(source)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise VerificationRiskError(f"cannot read corpus {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise VerificationRiskError(f"invalid JSON corpus {path}: {exc}") from exc
        source_name = str(path)
    if not isinstance(data, Mapping):
        raise VerificationRiskError(f"corpus {source_name} root must be an object")
    schema_version = data.get("schema_version", data.get("$schema"))
    if schema_version != VERIFICATION_RISK_CORPUS_SCHEMA_VERSION:
        raise VerificationRiskError(
            f"unsupported corpus schema_version {schema_version!r}; expected "
            f"{VERIFICATION_RISK_CORPUS_SCHEMA_VERSION}"
        )
    raw_witnesses = data.get("witnesses")
    if not isinstance(raw_witnesses, Sequence) or isinstance(raw_witnesses, (str, bytes, bytearray)):
        raise VerificationRiskError("corpus witnesses must be a list")
    witnesses: list[WitnessProfile] = []
    seen_witnesses: set[str] = set()
    for raw_witness in raw_witnesses:
        witness = WitnessProfile.from_mapping(raw_witness)
        if witness.witness_id in seen_witnesses:
            raise VerificationRiskError(f"duplicate witness id {witness.witness_id!r}")
        seen_witnesses.add(witness.witness_id)
        witnesses.append(witness)
    if not witnesses:
        raise VerificationRiskError("corpus must contain at least one witness")

    raw_samples = data.get("samples")
    if not isinstance(raw_samples, Sequence) or isinstance(raw_samples, (str, bytes, bytearray)):
        raise VerificationRiskError("corpus samples must be a list")
    samples: list[LabeledSample] = []
    seen_samples: set[str] = set()
    for raw_sample in raw_samples:
        sample = LabeledSample.from_mapping(raw_sample, witness_ids=seen_witnesses)
        if sample.sample_id in seen_samples:
            raise VerificationRiskError(f"duplicate sample id {sample.sample_id!r}")
        seen_samples.add(sample.sample_id)
        samples.append(sample)
    if not samples:
        raise VerificationRiskError("corpus must contain at least one sample")
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise VerificationRiskError("corpus metadata must be an object")
    name = _as_text(data.get("name"), field_name="corpus name", required=False) or source_name
    return VerificationRiskCorpus(
        name=name,
        witnesses=tuple(witnesses),
        samples=tuple(samples),
        schema_version=VERIFICATION_RISK_CORPUS_SCHEMA_VERSION,
        metadata=dict(metadata or {}),
    )


def load_corpus(source: str | Path | Mapping[str, Any]) -> VerificationRiskCorpus:
    """Short alias for :func:`load_verification_risk_corpus`."""

    return load_verification_risk_corpus(source)


@dataclass(frozen=True)
class RateEstimate:
    """Count/rate plus deterministic Wilson 95% interval.

    ``rate``, ``lower``, and ``upper`` are ``None`` when denominator is zero;
    no NaN-shaped uncertainty is emitted.
    """

    count: int
    denominator: int
    rate: float | None
    lower: float | None
    upper: float | None
    status: str = "defined"

    @classmethod
    def from_counts(cls, count: int, denominator: int) -> "RateEstimate":
        if count < 0 or denominator < 0 or count > denominator:
            raise VerificationRiskError(
                f"invalid rate counts count={count}, denominator={denominator}"
            )
        if denominator == 0:
            return cls(
                count=count,
                denominator=0,
                rate=None,
                lower=None,
                upper=None,
                status="undefined_zero_denominator",
            )
        proportion = count / denominator
        z = WILSON_Z_95
        z_squared = z * z
        centre = proportion + z_squared / (2 * denominator)
        scale = 1 + z_squared / denominator
        spread = z * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z_squared / (4 * denominator * denominator)
        )
        return cls(
            count=count,
            denominator=denominator,
            rate=proportion,
            lower=max(0.0, (centre - spread) / scale),
            upper=min(1.0, (centre + spread) / scale),
        )

    @property
    def wilson_95(self) -> tuple[float, float] | None:
        if self.lower is None or self.upper is None:
            return None
        return (self.lower, self.upper)

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "denominator": self.denominator,
            "rate": self.rate,
            "lower": self.lower,
            "upper": self.upper,
            "wilson_95": list(self.wilson_95) if self.wilson_95 is not None else None,
            "status": self.status,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def _rate(count: int, denominator: int) -> RateEstimate:
    return RateEstimate.from_counts(count, denominator)


@dataclass(frozen=True)
class PairRiskMetrics:
    witness_a: str
    witness_b: str
    slice_id: str | None
    sample_count: int
    marginal_error: Mapping[str, RateEstimate]
    joint_error: RateEstimate
    agreement: RateEstimate
    disagreement: RateEstimate
    conditional_error_when_agree: RateEstimate
    conditional_error_when_disagree: RateEstimate
    conditional_joint_error_when_disagree: RateEstimate
    per_witness_disagreement_accuracy: Mapping[str, RateEstimate]
    catastrophic_joint_failures: RateEstimate
    catastrophic_sample_count: int
    disagreement_case_count: int
    agreement_case_count: int

    @property
    def pair(self) -> tuple[str, str]:
        return (self.witness_a, self.witness_b)

    @property
    def marginal_errors(self) -> Mapping[str, RateEstimate]:
        return self.marginal_error

    @property
    def double_fault(self) -> RateEstimate:
        return self.joint_error

    @property
    def joint_error_rate(self) -> float | None:
        return self.joint_error.rate

    def as_dict(self) -> dict[str, Any]:
        marginal = {
            witness_id: result.as_dict()
            for witness_id, result in sorted(self.marginal_error.items())
        }
        disagreement_accuracy = {
            witness_id: result.as_dict()
            for witness_id, result in sorted(self.per_witness_disagreement_accuracy.items())
        }
        return {
            "pair": [self.witness_a, self.witness_b],
            "witness_a": self.witness_a,
            "witness_b": self.witness_b,
            "slice_id": self.slice_id,
            "sample_count": self.sample_count,
            "marginal_error": marginal,
            "marginal_errors": marginal,
            "joint_error": self.joint_error.as_dict(),
            "double_fault": self.joint_error.as_dict(),
            "agreement": self.agreement.as_dict(),
            "disagreement": self.disagreement.as_dict(),
            "conditional_error_when_agree": self.conditional_error_when_agree.as_dict(),
            "conditional_error_when_disagree": self.conditional_error_when_disagree.as_dict(),
            "conditional_joint_error_when_disagree": self.conditional_joint_error_when_disagree.as_dict(),
            "per_witness_disagreement_accuracy": disagreement_accuracy,
            "catastrophic_joint_failures": self.catastrophic_joint_failures.as_dict(),
            "catastrophic_joint_error": self.catastrophic_joint_failures.as_dict(),
            "catastrophic_sample_count": self.catastrophic_sample_count,
            "agreement_case_count": self.agreement_case_count,
            "disagreement_case_count": self.disagreement_case_count,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def _resolve_pair(
    witness_a: str | Sequence[str],
    witness_b: str | None,
) -> tuple[str, str]:
    if witness_b is None:
        if isinstance(witness_a, str) or len(witness_a) != 2:
            raise VerificationRiskError("pair must contain exactly two witness ids")
        first, second = (str(item).strip() for item in witness_a)
    else:
        if not isinstance(witness_a, str):
            raise VerificationRiskError("witness_a must be a witness id when witness_b is supplied")
        first, second = witness_a.strip(), witness_b.strip()
    if not first or not second or first == second:
        raise VerificationRiskError("pair requires two distinct non-empty witness ids")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


def evaluate_pair(
    corpus: VerificationRiskCorpus,
    witness_a: str | Sequence[str],
    witness_b: str | None = None,
    *,
    slice_id: str | None = None,
) -> PairRiskMetrics:
    """Evaluate exact pair statistics for one declared slice."""

    first, second = _resolve_pair(witness_a, witness_b)
    witness_ids = corpus.witness_by_id
    if first not in witness_ids or second not in witness_ids:
        missing = first if first not in witness_ids else second
        raise VerificationRiskError(f"unknown witness {missing!r}")
    samples = [
        sample
        for sample in corpus.samples_for_slice(slice_id)
        if first in sample.outcomes and second in sample.outcomes
    ]
    total = len(samples)
    first_errors = second_errors = joint_errors = 0
    agreement_count = disagreement_count = 0
    agreement_errors = disagreement_errors = disagreement_joint_errors = 0
    first_disagreement_correct = second_disagreement_correct = 0
    catastrophic_joint = catastrophic_samples = 0
    for sample in samples:
        outcome_a = sample.outcomes[first]
        outcome_b = sample.outcomes[second]
        first_error = outcome_a.is_error(sample.label)
        second_error = outcome_b.is_error(sample.label)
        if first_error:
            first_errors += 1
        if second_error:
            second_errors += 1
        if first_error and second_error:
            joint_errors += 1
        if outcome_a.prediction == outcome_b.prediction:
            agreement_count += 1
            if first_error or second_error:
                agreement_errors += 1
        else:
            disagreement_count += 1
            if first_error or second_error:
                disagreement_errors += 1
            if first_error and second_error:
                disagreement_joint_errors += 1
            if not first_error:
                first_disagreement_correct += 1
            if not second_error:
                second_disagreement_correct += 1
        if sample.catastrophic or outcome_a.catastrophic or outcome_b.catastrophic:
            catastrophic_samples += 1
            if first_error and second_error:
                catastrophic_joint += 1
    return PairRiskMetrics(
        witness_a=first,
        witness_b=second,
        slice_id=slice_id,
        sample_count=total,
        marginal_error={
            first: _rate(first_errors, total),
            second: _rate(second_errors, total),
        },
        joint_error=_rate(joint_errors, total),
        agreement=_rate(agreement_count, total),
        disagreement=_rate(disagreement_count, total),
        conditional_error_when_agree=_rate(agreement_errors, agreement_count),
        conditional_error_when_disagree=_rate(disagreement_errors, disagreement_count),
        conditional_joint_error_when_disagree=_rate(
            disagreement_joint_errors,
            disagreement_count,
        ),
        per_witness_disagreement_accuracy={
            first: _rate(first_disagreement_correct, disagreement_count),
            second: _rate(second_disagreement_correct, disagreement_count),
        },
        catastrophic_joint_failures=_rate(catastrophic_joint, catastrophic_samples),
        catastrophic_sample_count=catastrophic_samples,
        disagreement_case_count=disagreement_count,
        agreement_case_count=agreement_count,
    )


def evaluate_pairs(
    corpus: VerificationRiskCorpus,
    *,
    slice_id: str | None = None,
) -> dict[tuple[str, str], PairRiskMetrics]:
    """Evaluate every lexicographically ordered witness pair."""

    witness_ids = sorted(witness.witness_id for witness in corpus.witnesses)
    return {
        (first, second): evaluate_pair(corpus, first, second, slice_id=slice_id)
        for index, first in enumerate(witness_ids)
        for second in witness_ids[index + 1 :]
    }


@dataclass(frozen=True)
class CalibrationResult:
    witness_id: str
    corpus_identity: str
    slice_id: str | None
    distribution: str | None
    method_id: str
    method_version: str
    target_event: str
    split_definition: Mapping[str, Any]
    support_uncertainty_method: str
    sample_count: int
    missing_confidence_count: int
    support_required: int
    support_sufficient: bool
    status: str
    brier_score: float | None
    expected_calibration_error: float | None
    maximum_calibration_error: float | None
    accuracy: RateEstimate
    bins: tuple[Mapping[str, Any], ...] = ()

    @property
    def ece(self) -> float | None:
        return self.expected_calibration_error

    @property
    def metric(self) -> str:
        return "brier_score_and_expected_calibration_error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "witness_id": self.witness_id,
            "corpus_identity": self.corpus_identity,
            "slice_id": self.slice_id,
            "distribution": self.distribution,
            "method_id": self.method_id,
            "method_version": self.method_version,
            "target_event": self.target_event,
            "split_definition": dict(self.split_definition),
            "support_uncertainty_method": self.support_uncertainty_method,
            "sample_count": self.sample_count,
            "missing_confidence_count": self.missing_confidence_count,
            "support_required": self.support_required,
            "support_sufficient": self.support_sufficient,
            "status": self.status,
            "metric": self.metric,
            "brier_score": self.brier_score,
            "expected_calibration_error": self.expected_calibration_error,
            "ece": self.ece,
            "maximum_calibration_error": self.maximum_calibration_error,
            "accuracy": self.accuracy.as_dict(),
            "support_uncertainty": self.accuracy.as_dict(),
            "bins": [dict(item) for item in self.bins],
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def evaluate_calibration(
    corpus: VerificationRiskCorpus,
    witness_id: str,
    *,
    slice_id: str | None = None,
    distribution: str | None = None,
    min_samples: int = 5,
    bin_count: int = 10,
) -> CalibrationResult:
    """Calculate deterministic Brier/ECE calibration on one scoped slice."""

    if min_samples < 1:
        raise VerificationRiskError("min_samples must be positive")
    if bin_count < 1:
        raise VerificationRiskError("bin_count must be positive")
    if witness_id not in corpus.witness_by_id:
        raise VerificationRiskError(f"unknown witness {witness_id!r}")
    selected = list(corpus.samples_for_slice(slice_id))
    if distribution is not None:
        selected = [sample for sample in selected if sample.distribution == distribution]
    scoped = [sample for sample in selected if witness_id in sample.outcomes]
    scored = [sample for sample in scoped if sample.outcomes[witness_id].confidence is not None]
    missing = len(scoped) - len(scored)
    support = len(scored)
    support_sufficient = support >= min_samples
    if support == 0:
        return CalibrationResult(
            witness_id=witness_id,
            corpus_identity=corpus.semantic_identity,
            slice_id=slice_id,
            distribution=distribution,
            method_id="equal_width_ece_and_brier",
            method_version="marker.calibration.ece_brier.v1",
            target_event="witness_prediction_correct",
            split_definition=dict(corpus.metadata.get("calibration_split", {})),
            support_uncertainty_method="wilson_score_95_accuracy",
            sample_count=0,
            missing_confidence_count=missing,
            support_required=min_samples,
            support_sufficient=False,
            status="insufficient_support" if missing or not support_sufficient else "ok",
            brier_score=None,
            expected_calibration_error=None,
            maximum_calibration_error=None,
            accuracy=_rate(0, 0),
            bins=(),
        )
    correct_values = [
        0 if sample.outcomes[witness_id].is_error(sample.label) else 1 for sample in scored
    ]
    confidence_values = [
        sample.outcomes[witness_id].confidence for sample in scored
    ]
    # Type narrowing for confidence after filtering above.
    confidence_numbers = [float(value) for value in confidence_values if value is not None]
    brier = sum(
        (confidence - correct) ** 2
        for confidence, correct in zip(confidence_numbers, correct_values, strict=True)
    ) / support
    bins: list[dict[str, Any]] = []
    weighted_gap = 0.0
    max_gap = 0.0
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        members = [
            position
            for position, confidence in enumerate(confidence_numbers)
            if (lower <= confidence < upper) or (index == bin_count - 1 and confidence == upper)
        ]
        if not members:
            continue
        mean_confidence = sum(confidence_numbers[position] for position in members) / len(members)
        empirical_accuracy = sum(correct_values[position] for position in members) / len(members)
        gap = abs(mean_confidence - empirical_accuracy)
        weighted_gap += len(members) / support * gap
        max_gap = max(max_gap, gap)
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(members),
                "mean_confidence": mean_confidence,
                "empirical_accuracy": empirical_accuracy,
                "gap": gap,
            }
        )
    return CalibrationResult(
        witness_id=witness_id,
        corpus_identity=corpus.semantic_identity,
        slice_id=slice_id,
        distribution=distribution,
        method_id="equal_width_ece_and_brier",
        method_version="marker.calibration.ece_brier.v1",
        target_event="witness_prediction_correct",
        split_definition=dict(corpus.metadata.get("calibration_split", {})),
        support_uncertainty_method="wilson_score_95_accuracy",
        sample_count=support,
        missing_confidence_count=missing,
        support_required=min_samples,
        support_sufficient=support_sufficient,
        status="ok" if support_sufficient else "insufficient_support",
        brier_score=brier,
        expected_calibration_error=weighted_gap,
        maximum_calibration_error=max_gap,
        accuracy=_rate(sum(correct_values), support),
        bins=tuple(bins),
    )


def evaluate_calibration_slices(
    corpus: VerificationRiskCorpus,
    witness_id: str,
    *,
    slices: Sequence[str] = ("matched", "shifted", "insufficient"),
    min_samples: int = 5,
    bin_count: int = 10,
) -> dict[str, CalibrationResult]:
    """Evaluate named matched/shifted/insufficient slices independently."""

    return {
        slice_name: evaluate_calibration(
            corpus,
            witness_id,
            distribution=slice_name,
            min_samples=min_samples,
            bin_count=bin_count,
        )
        for slice_name in slices
    }


@dataclass(frozen=True)
class BaselineResult:
    name: str
    slice_id: str | None
    sample_count: int
    evaluated_sample_ids: tuple[str, ...]
    selected_witnesses: tuple[str, ...]
    status: str
    not_applicable_reason: str | None
    accepted_count: int
    false_verified_count: int
    catastrophic_error_count: int
    disagreement_count: int
    coverage: RateEstimate
    false_verified_rate: RateEstimate
    false_verified_fraction: RateEstimate
    abstention_rate: RateEstimate
    catastrophic_error_rate: RateEstimate
    disagreement_rate: RateEstimate
    brier_score: float | None
    runtime_ms: float | None = None

    @property
    def accepted_fraction(self) -> RateEstimate:
        return self.coverage

    @property
    def abstention(self) -> RateEstimate:
        return self.abstention_rate

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slice_id": self.slice_id,
            "sample_count": self.sample_count,
            "evaluated_sample_ids": list(self.evaluated_sample_ids),
            "selected_witnesses": list(self.selected_witnesses),
            "status": self.status,
            "not_applicable_reason": self.not_applicable_reason,
            "accepted_count": self.accepted_count,
            "false_verified_count": self.false_verified_count,
            "catastrophic_error_count": self.catastrophic_error_count,
            "disagreement_count": self.disagreement_count,
            "coverage": self.coverage.as_dict(),
            "false_verified_rate": self.false_verified_rate.as_dict(),
            "false_verified_fraction": self.false_verified_fraction.as_dict(),
            "abstention_rate": self.abstention_rate.as_dict(),
            "catastrophic_error_rate": self.catastrophic_error_rate.as_dict(),
            "disagreement_rate": self.disagreement_rate.as_dict(),
            "brier_score": self.brier_score,
        }

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "runtime_ms": self.runtime_ms,
            "semantic_identity": self.semantic_identity,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


BASELINE_NAMES: tuple[str, ...] = (
    "deterministic_source_native_only",
    "best_single_witness",
    "naive_majority_vote",
    "correlation_blind_ensemble",
    "dependency_aware_policy",
)


@dataclass(frozen=True)
class BaselineComparison:
    """Five baseline results, evaluated over same sample slice."""

    corpus_identity: str
    slice_id: str | None
    baselines: Mapping[str, BaselineResult]
    baseline_order: tuple[str, ...] = BASELINE_NAMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VERIFICATION_RISK_REPORT_SCHEMA_VERSION,
            "corpus_identity": self.corpus_identity,
            "slice_id": self.slice_id,
            "baseline_order": list(self.baseline_order),
            "baselines": {
                name: self.baselines[name].as_dict() for name in self.baseline_order
            },
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


@dataclass(frozen=True)
class VerificationRiskReport:
    """Combined deterministic report suitable for benchmark serialization."""

    corpus_identity: str
    slice_id: str | None
    pairs: Mapping[tuple[str, str], PairRiskMetrics]
    calibration: Mapping[str, CalibrationResult]
    baselines: BaselineComparison
    runtime_ms: float | None = None

    @property
    def semantic_identity(self) -> str:
        return _identity(self.semantic_payload())

    def semantic_payload(self) -> dict[str, Any]:
        pair_entries = [
            self.pairs[key].as_dict() for key in sorted(self.pairs)
        ]
        return {
            "schema_version": VERIFICATION_RISK_REPORT_SCHEMA_VERSION,
            "corpus_identity": self.corpus_identity,
            "slice_id": self.slice_id,
            "pairs": pair_entries,
            "calibration": {
                key: self.calibration[key].as_dict() for key in sorted(self.calibration)
            },
            "baselines": self.baselines.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "runtime_ms": self.runtime_ms,
            "semantic_identity": self.semantic_identity,
        }


def _sample_prediction(
    sample: LabeledSample,
    witness_ids: Sequence[str],
) -> tuple[Any | None, bool]:
    """Return vote and whether it is accepted by a deterministic vote rule."""

    votes = [sample.outcomes[item].prediction for item in witness_ids if item in sample.outcomes]
    if not votes:
        return None, False
    counts: dict[str, tuple[Any, int]] = {}
    for vote in votes:
        # JSON labels are expected; repr fallback keeps unusual hashability out
        # of the public contract while preserving deterministic tie ordering.
        key = _canonical_json(vote)
        value, count = counts.get(key, (vote, 0))
        counts[key] = (value, count + 1)
    ordered = sorted(counts.values(), key=lambda item: (-item[1], _canonical_json(item[0])))
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return None, False
    return ordered[0][0], True


def _confidence_weighted_prediction(
    sample: LabeledSample,
    witness_ids: Sequence[str],
) -> tuple[Any | None, bool]:
    """Return confidence-weighted vote without consulting dependency metadata."""

    weights: dict[str, tuple[Any, float]] = {}
    for witness_id in witness_ids:
        outcome = sample.outcomes.get(witness_id)
        if outcome is None:
            continue
        key = _canonical_json(outcome.prediction)
        prediction, total = weights.get(key, (outcome.prediction, 0.0))
        weight = outcome.confidence if outcome.confidence is not None else 1.0
        weights[key] = (prediction, total + weight)
    if not weights:
        return None, False
    ordered = sorted(weights.values(), key=lambda item: (-item[1], _canonical_json(item[0])))
    if len(ordered) > 1 and math.isclose(ordered[0][1], ordered[1][1], rel_tol=0.0, abs_tol=1e-12):
        return None, False
    return ordered[0][0], True


def _baseline_result(
    name: str,
    samples: Sequence[LabeledSample],
    selected_witnesses: Sequence[str],
    decisions: Mapping[str, tuple[Any | None, bool]],
    *,
    status: str = "ok",
    not_applicable_reason: str | None = None,
    runtime_ms: float | None = None,
) -> BaselineResult:
    sample_count = len(samples)
    accepted_count = false_verified = catastrophic_error = disagreement_count = 0
    brier_values: list[float] = []
    for sample in samples:
        vote, accepted = decisions.get(sample.sample_id, (None, False))
        witness_votes = [
            sample.outcomes[item].prediction
            for item in selected_witnesses
            if item in sample.outcomes
        ]
        if len(witness_votes) > 1 and len({_canonical_json(value) for value in witness_votes}) > 1:
            disagreement_count += 1
        if accepted:
            accepted_count += 1
            error = vote != sample.label
            if error:
                false_verified += 1
                if sample.catastrophic:
                    catastrophic_error += 1
        if accepted and selected_witnesses:
            confidences = [
                sample.outcomes[item].confidence
                for item in selected_witnesses
                if item in sample.outcomes and sample.outcomes[item].confidence is not None
            ]
            if confidences:
                brier_values.append(
                    (sum(confidences) / len(confidences) - int(vote == sample.label)) ** 2
                )
    brier = sum(brier_values) / len(brier_values) if brier_values else None
    return BaselineResult(
        name=name,
        slice_id=None,
        sample_count=sample_count,
        evaluated_sample_ids=tuple(sample.sample_id for sample in samples),
        selected_witnesses=tuple(selected_witnesses),
        status=status,
        not_applicable_reason=not_applicable_reason,
        accepted_count=accepted_count,
        false_verified_count=false_verified,
        catastrophic_error_count=catastrophic_error,
        disagreement_count=disagreement_count,
        coverage=_rate(accepted_count, sample_count),
        false_verified_rate=_rate(false_verified, accepted_count),
        false_verified_fraction=_rate(false_verified, sample_count),
        abstention_rate=_rate(sample_count - accepted_count, sample_count),
        catastrophic_error_rate=_rate(catastrophic_error, accepted_count),
        disagreement_rate=_rate(disagreement_count, sample_count),
        brier_score=brier,
        runtime_ms=runtime_ms,
    )


def _source_native_ids(corpus: VerificationRiskCorpus) -> tuple[str, ...]:
    return tuple(
        sorted(
            witness.witness_id
            for witness in corpus.witnesses
            if witness.source_native or witness.kind in {"source_native", "deterministic"}
        )
    )


def _dependency_aware_ids(corpus: VerificationRiskCorpus) -> tuple[str, ...]:
    selected: list[WitnessProfile] = []
    seen_groups: set[tuple[Any, ...]] = set()
    for witness in sorted(corpus.witnesses, key=lambda item: item.witness_id):
        if witness.disclosure != "complete" or witness.alias_of:
            continue
        if not witness.has_known_lineage:
            continue
        if witness.shared_dependency_group:
            group: tuple[Any, ...] = ("shared", witness.shared_dependency_group)
        elif witness.teacher_lineage:
            group = ("teacher", witness.teacher_lineage)
        elif witness.base_lineage:
            group = ("lineage", witness.base_lineage)
        else:
            group = ("profile", *witness.dependency_key)
        # Unknown dependency key cannot prove diversity; retain only when all
        # known fields establish a distinct complete lineage.
        if group in seen_groups:
            continue
        seen_groups.add(group)
        selected.append(witness)
    return tuple(witness.witness_id for witness in selected)


def _dependency_aware_decisions(
    corpus: VerificationRiskCorpus,
    samples: Sequence[LabeledSample],
    selected: Sequence[str],
    *,
    empirical_gate_passed: bool,
) -> dict[str, tuple[Any | None, bool]]:
    source_native = set(_source_native_ids(corpus))
    decisions: dict[str, tuple[Any | None, bool]] = {}
    for sample in samples:
        if not empirical_gate_passed:
            decisions[sample.sample_id] = (None, False)
            continue
        # High-risk model consensus alone can never become verified.  Authority
        # bearing source-native/human/deterministic evidence is separate.
        authority_present = any(
            witness_id in sample.outcomes for witness_id in set(selected) & source_native
        )
        if sample.risk_level == "high" and not authority_present:
            decisions[sample.sample_id] = (None, False)
            continue
        vote, accepted = _sample_prediction(sample, selected)
        if len(selected) < 2 and not (set(selected) & source_native):
            accepted = False
        decisions[sample.sample_id] = (vote, accepted)
    return decisions


def _dependency_empirical_gate(
    corpus: VerificationRiskCorpus,
    selected: Sequence[str],
    *,
    slice_id: str | None,
    min_samples: int = 5,
    max_joint_error_upper: float = 0.6,
) -> tuple[bool, str, str | None]:
    """Require measured pair support and bounded double-fault uncertainty."""

    if len(selected) < 2:
        return False, "insufficient_support", "fewer than two dependency-diverse witnesses"
    pair_metrics = [
        evaluate_pair(corpus, first, second, slice_id=slice_id)
        for index, first in enumerate(selected)
        for second in selected[index + 1 :]
    ]
    least_supported = min(pair_metrics, key=lambda metrics: metrics.sample_count)
    if least_supported.sample_count < min_samples:
        return (
            False,
            "insufficient_support",
            f"pair {least_supported.pair!r} support {least_supported.sample_count} "
            f"is below required {min_samples}",
        )
    worst = max(
        pair_metrics,
        key=lambda metrics: metrics.joint_error.upper
        if metrics.joint_error.upper is not None
        else float("inf"),
    )
    upper = worst.joint_error.upper
    if upper is None or upper > max_joint_error_upper:
        return (
            False,
            "risk_bound_not_met",
            f"pair {worst.pair!r} joint-error Wilson upper {upper!r} "
            f"exceeds policy bound {max_joint_error_upper}",
        )
    return True, "ok", None


def evaluate_baselines(
    corpus: VerificationRiskCorpus,
    *,
    slice_id: str | None = None,
    best_single_witness_id: str | None = None,
    runtime_ms: float | None = None,
) -> BaselineComparison:
    """Compare all five required policies over identical labeled samples."""

    samples = corpus.samples_for_slice(slice_id)
    witness_ids = tuple(sorted(witness.witness_id for witness in corpus.witnesses))
    declared_source_native = _source_native_ids(corpus)
    source_native = tuple(
        witness_id
        for witness_id in declared_source_native
        if any(witness_id in sample.outcomes for sample in samples)
    )
    source_decisions = {
        sample.sample_id: _sample_prediction(sample, source_native) for sample in samples
    }
    source_result = _baseline_result(
        BASELINE_NAMES[0],
        samples,
        source_native,
        source_decisions,
        status="ok" if source_native else "not_applicable",
        not_applicable_reason=None
        if source_native
        else "corpus has no source-native/deterministic witness",
        runtime_ms=runtime_ms,
    )

    declared_best = best_single_witness_id or corpus.metadata.get("baseline_best_single_witness")
    if not isinstance(declared_best, str) or not declared_best.strip():
        raise VerificationRiskError(
            "best-single baseline requires corpus metadata baseline_best_single_witness "
            "or explicit best_single_witness_id"
        )
    declared_best = declared_best.strip()
    if declared_best not in corpus.witness_by_id:
        raise VerificationRiskError(f"declared best-single witness {declared_best!r} is unknown")
    best_decisions = {
        sample.sample_id: (
            (sample.outcomes[declared_best].prediction, True)
            if declared_best in sample.outcomes
            else (None, False)
        )
        for sample in samples
    }
    best_single = _baseline_result(
        BASELINE_NAMES[1],
        samples,
        (declared_best,),
        best_decisions,
        status="ok"
        if any(declared_best in sample.outcomes for sample in samples)
        else "not_applicable",
        not_applicable_reason=None
        if any(declared_best in sample.outcomes for sample in samples)
        else f"declared witness {declared_best!r} has no outcomes on selected slice",
        runtime_ms=runtime_ms,
    )

    naive_decisions = {
        sample.sample_id: _sample_prediction(sample, witness_ids) for sample in samples
    }
    naive = _baseline_result(
        BASELINE_NAMES[2],
        samples,
        witness_ids,
        naive_decisions,
        runtime_ms=runtime_ms,
    )

    # Correlation-blind ensemble weights votes by source confidence while
    # intentionally ignoring dependency disclosures.  This is distinct from
    # the unweighted naive majority baseline.
    ensemble_decisions = {
        sample.sample_id: (
            _confidence_weighted_prediction(sample, witness_ids)[0],
            len(
                [item for item in witness_ids if item in sample.outcomes]
            ) >= 2
            and _confidence_weighted_prediction(sample, witness_ids)[1],
        )
        for sample in samples
    }
    ensemble = _baseline_result(
        BASELINE_NAMES[3],
        samples,
        witness_ids,
        ensemble_decisions,
        runtime_ms=runtime_ms,
    )

    dependency_ids = tuple(
        witness_id
        for witness_id in _dependency_aware_ids(corpus)
        if any(witness_id in sample.outcomes for sample in samples)
    )
    empirical_passed, dependency_status, dependency_reason = _dependency_empirical_gate(
        corpus,
        dependency_ids,
        slice_id=slice_id,
    )
    dependency_decisions = _dependency_aware_decisions(
        corpus,
        samples,
        dependency_ids,
        empirical_gate_passed=empirical_passed,
    )
    dependency = _baseline_result(
        BASELINE_NAMES[4],
        samples,
        dependency_ids,
        dependency_decisions,
        status=dependency_status,
        not_applicable_reason=dependency_reason,
        runtime_ms=runtime_ms,
    )
    results = {
        BASELINE_NAMES[0]: source_result,
        BASELINE_NAMES[1]: best_single,
        BASELINE_NAMES[2]: naive,
        BASELINE_NAMES[3]: ensemble,
        BASELINE_NAMES[4]: dependency,
    }
    # Dataclass stores requested slice for every result; preserve semantic
    # identity independent of runtime measurement.
    results = {
        name: BaselineResult(**{**result.__dict__, "slice_id": slice_id})
        for name, result in results.items()
    }
    return BaselineComparison(
        corpus_identity=corpus.semantic_identity,
        slice_id=slice_id,
        baselines=results,
    )


def evaluate_all_baselines(
    corpus: VerificationRiskCorpus,
    *,
    runtime_ms: float | None = None,
) -> dict[str | None, BaselineComparison]:
    """Return one five-baseline comparison for all and each corpus slice."""

    return {
        None: evaluate_baselines(corpus, runtime_ms=runtime_ms),
        **{
            slice_name: evaluate_baselines(corpus, slice_id=slice_name, runtime_ms=runtime_ms)
            for slice_name in corpus.slice_ids
        },
    }


def evaluate_verification_risk(
    corpus: VerificationRiskCorpus,
    *,
    slice_id: str | None = None,
    calibration_witness_ids: Sequence[str] | None = None,
    min_calibration_samples: int = 5,
    runtime_ms: float | None = None,
) -> VerificationRiskReport:
    """Build pair, calibration, and five-baseline report for benchmark use."""

    calibration_ids = tuple(
        calibration_witness_ids
        if calibration_witness_ids is not None
        else sorted(witness.witness_id for witness in corpus.witnesses)
    )
    calibration = {
        witness_id: evaluate_calibration(
            corpus,
            witness_id,
            slice_id=slice_id,
            min_samples=min_calibration_samples,
        )
        for witness_id in calibration_ids
    }
    return VerificationRiskReport(
        corpus_identity=corpus.semantic_identity,
        slice_id=slice_id,
        pairs=evaluate_pairs(corpus, slice_id=slice_id),
        calibration=calibration,
        baselines=evaluate_baselines(corpus, slice_id=slice_id, runtime_ms=runtime_ms),
        runtime_ms=runtime_ms,
    )


def semantic_artifact_identity(value: Any) -> str:
    """Hash semantic content while excluding operational runtime metadata."""

    if isinstance(value, VerificationRiskCorpus):
        return value.semantic_identity
    if isinstance(value, BaselineResult):
        return value.semantic_identity
    if isinstance(value, VerificationRiskReport):
        return value.semantic_identity
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    return _identity(value)


# Friendly aliases for benchmark callers and older naming conventions.
RiskWitness = WitnessProfile
RiskSample = LabeledSample
RiskCorpus = VerificationRiskCorpus
PairEvaluation = PairRiskMetrics
CalibrationEvaluation = CalibrationResult
BaselineReport = BaselineComparison
evaluate_risk_corpus = evaluate_verification_risk
run_verification_risk_benchmark = evaluate_verification_risk


__all__ = [
    "BASELINE_NAMES",
    "BaselineComparison",
    "BaselineReport",
    "BaselineResult",
    "CalibrationEvaluation",
    "CalibrationResult",
    "LabeledSample",
    "PairEvaluation",
    "PairRiskMetrics",
    "RateEstimate",
    "RiskCorpus",
    "RiskSample",
    "RiskWitness",
    "VERIFICATION_RISK_CORPUS_SCHEMA_VERSION",
    "VERIFICATION_RISK_REPORT_SCHEMA_VERSION",
    "VerificationRiskCorpus",
    "VerificationRiskError",
    "VerificationRiskReport",
    "WitnessOutcome",
    "WitnessProfile",
    "evaluate_all_baselines",
    "evaluate_baselines",
    "evaluate_calibration",
    "evaluate_calibration_slices",
    "evaluate_pair",
    "evaluate_pairs",
    "evaluate_risk_corpus",
    "evaluate_verification_risk",
    "load_corpus",
    "load_verification_risk_corpus",
    "run_verification_risk_benchmark",
    "semantic_artifact_identity",
]
