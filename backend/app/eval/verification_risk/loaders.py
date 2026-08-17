"""Corpus loading and fail-closed structural validation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    VERIFICATION_RISK_CORPUS_SCHEMA_VERSION,
    _as_text,
    VerificationRiskError,
)
from .models import LabeledSample, VerificationRiskCorpus, WitnessProfile

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
