"""Validated witness, outcome, sample, and corpus models."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .common import (
    VERIFICATION_RISK_CORPUS_SCHEMA_VERSION,
    Disclosure,
    _DEPENDENCY_PROFILE_FIELDS,
    _LIST_OUTCOME_FIELDS,
    _OUTCOME_FIELDS,
    _SAMPLE_FIELDS,
    _WITNESS_FIELDS,
    _as_bool,
    _as_probability,
    _as_text,
    _normalise_disclosure,
    _profile_value,
    _reject_unknown_fields,
    VerificationRiskError,
)

#: Every dependency dimension of a witness, in canonical order. This is
#: the single source of truth for :attr:`WitnessProfile.dependency_key`:
#: ``model_family`` is a dimension (shared families correlate exactly
#: like shared renderers), and adding one is an identity-affecting
#: change that must be declared here, not discovered downstream.
_DEPENDENCY_DIMENSIONS: tuple[str, ...] = (
    "alias_of",
    "shared_dependency_group",
    "base_lineage",
    "teacher_lineage",
    "model_family",
    "renderer",
    "cropper",
    "detector",
    "preprocessor",
    "postprocessor",
    "prompt_identity",
)
from .identity import _identity

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

        return tuple(getattr(self, name) for name in _DEPENDENCY_DIMENSIONS)

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
        allow_witness_id_fields: bool = False,
    ) -> "WitnessOutcome":
        if not isinstance(data, Mapping):
            raise VerificationRiskError(f"outcome for witness {witness_id!r} must be an object")
        _reject_unknown_fields(
            data,
            _LIST_OUTCOME_FIELDS if allow_witness_id_fields else _OUTCOME_FIELDS,
            context=f"outcome for witness {witness_id!r}",
        )
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
        # Non-finite numerics fail closed at the load boundary: a NaN
        # prediction is truthy in Python and would silently count as a
        # verifying vote inside majority baselines.
        if isinstance(prediction, float) and not math.isfinite(prediction):
            raise VerificationRiskError(
                f"outcome for witness {witness_id!r}: non-finite prediction "
                f"{prediction!r} cannot enter risk evidence"
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
        _reject_unknown_fields(data, _SAMPLE_FIELDS, context=f"sample {sample_id!r}")
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
                allow_witness_id_fields=isinstance(raw_outcomes, Sequence)
                and not isinstance(raw_outcomes, (str, bytes, bytearray)),
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
