"""Adversarial dependence and verification-risk evaluation (PR82A).

A fresh held-out corpus — not the PR75 fixture that defined the
thresholds — attacks the dependency-aware policy on the axes PR75's own
audit named:

* shared pipeline stages (renderer/cropper/detector) masked by
  differing base lineages (fixed this phase: correlation is now
  any-shared-dimension);
* model-family sharing as a first-class dependency dimension;
* non-finite predictions (NaN/Inf) entering majority votes (fixed this
  phase: rejected at the load boundary);
* shifted distributions that must break the risk bound into abstention;
* insufficient support that must stay abstention even at zero observed
  catastrophic failures;
* high-risk model-only consensus that can never verify.

Answers preregistered Q5 (dependent witnesses cannot satisfy a
high-risk policy) and Q6 (pathological inputs fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.eval.verification_risk.baselines import (
    BASELINE_NAMES,
    _dependency_aware_ids,
    evaluate_baselines,
)
from app.eval.verification_risk.loaders import load_verification_risk_corpus
from app.eval.verification_risk.models import VerificationRiskError

DEPENDENCE_BASELINE = BASELINE_NAMES[4]

_SCHEMA = "marker.verification_risk_corpus.v1"


def _witness(
    witness_id: str,
    *,
    family: str | None = None,
    base: str | None = None,
    renderer: str | None = None,
    cropper: str | None = None,
    source_native: bool = False,
) -> dict[str, Any]:
    return {
        "id": witness_id,
        **({"model_family": family} if family else {}),
        "kind": "source_native" if source_native else "model",
        "dependency_profile": {
            "disclosure": "complete",
            **({"base_lineage": base} if base else {}),
            **({"renderer": renderer} if renderer else {}),
            **({"cropper": cropper} if cropper else {}),
        },
        **({"source_native": True} if source_native else {}),
    }


def _sample(
    sample_id: str,
    slice_id: str,
    label: bool,
    outcomes: dict[str, bool],
    *,
    risk: str = "normal",
) -> dict[str, Any]:
    return {
        "id": sample_id,
        "slice": slice_id,
        "distribution": "matched" if slice_id != "shifted" else "shifted",
        "label": label,
        "risk_level": risk,
        "outcomes": {
            witness_id: {"prediction": prediction, "confidence": 0.8}
            for witness_id, prediction in outcomes.items()
        },
    }


def build_heldout_corpus() -> dict[str, Any]:
    """Frozen held-out corpus: separate from PR75's calibration fixture.

    Witness design: ``sn`` is authority-bearing source-native evidence;
    ``m1`` and ``m2`` SHARE a renderer+cropper pipeline while carrying
    deliberately different base lineages — the masking attack; ``m3``
    is genuinely independent. On three samples the shared pipeline is
    wrong together while ``sn`` is right: if the selector wrongly
    admits both m1 and m2, they outvote ``sn`` and fabricate
    verifications.
    """
    samples: list[dict[str, Any]] = []
    # Held-out matched slice: 12 samples. On attack samples 3, 7, 11
    # the shared pipeline (m1+m2) is wrong together while ``sn`` is
    # right, and m3 has no observation: if the selector wrongly admits
    # both m1 and m2 they outvote ``sn`` 2-1 and fabricate
    # verifications; with m2 correctly dropped the vote ties and
    # abstains. Sample 5 carries honest independent noise (m3 wrong).
    for index in range(1, 13):
        label = index % 2 == 0
        if index in (3, 7, 11):
            outcomes = {"sn": label, "m1": not label, "m2": not label}
        else:
            outcomes = {
                "sn": label,
                "m1": label,
                "m2": (not label) if index == 5 else label,
                "m3": (not label) if index == 5 else label,
            }
        samples.append(_sample(f"held-{index:02d}", "heldout", label, outcomes))
    # High-risk model-only slice: no source-native outcome at all.
    for index in range(1, 4):
        label = index % 2 == 0
        samples.append(
            _sample(
                f"high-{index:02d}",
                "highrisk",
                label,
                {"m1": True, "m3": True},
                risk="high",
            )
        )
    # Shifted slice: independent witnesses disagree wildly with truth —
    # the joint-error bound must break into abstention.
    for index in range(1, 9):
        label = index % 2 == 0
        samples.append(
            _sample(
                f"shift-{index:02d}",
                "shifted",
                label,
                {"sn": label, "m1": (not label) if index <= 5 else label, "m3": (not label) if index <= 5 else label},
            )
        )
    # Insufficient-support slice: two flawless samples — zero observed
    # failures must NOT become zero risk.
    for index in range(1, 3):
        label = index % 2 == 0
        samples.append(
            _sample(f"thin-{index:02d}", "thin", label, {"sn": label, "m1": label, "m3": label})
        )
    return {
        "$schema": _SCHEMA,
        "name": "pr82a-heldout-adversarial-v1",
        "metadata": {"baseline_best_single_witness": "m1"},
        "witnesses": [
            _witness("sn", source_native=True, base="source-document"),
            _witness("m1", family="fam-one", base="ckpt-one", renderer="rend-x", cropper="crop-x"),
            _witness("m2", family="fam-two", base="ckpt-two", renderer="rend-x", cropper="crop-x"),
            _witness("m3", family="fam-three", base="ckpt-three", renderer="rend-y", cropper="crop-y"),
        ],
        "samples": samples,
    }


def build_pathological_corpora() -> dict[str, dict[str, Any]]:
    """Corpora whose payloads are invalid evidence, not wrong answers.

    Each must be rejected at the load boundary (Q6): a NaN or infinite
    prediction is truthy in Python and would count as a verifying vote
    inside majority baselines.
    """
    base = build_heldout_corpus()
    nan_samples = [
        _sample("nan-01", "heldout", True, {"sn": True, "m1": True}),
    ]
    nan_samples[0]["outcomes"]["m2"] = {"prediction": float("nan"), "confidence": 0.5}
    inf_samples = [
        _sample("inf-01", "heldout", True, {"sn": True, "m1": True}),
    ]
    inf_samples[0]["outcomes"]["m2"] = {"prediction": float("inf"), "confidence": 0.5}
    return {
        "nan_prediction": {**base, "name": "pr82a-nan-prediction", "samples": nan_samples},
        "inf_prediction": {**base, "name": "pr82a-inf-prediction", "samples": inf_samples},
    }


@dataclass
class SliceFinding:
    slice_id: str
    status: str
    accepted_count: int
    false_verified_count: int
    catastrophic_error_count: int
    selected_witnesses: tuple[str, ...]
    violations: tuple[str, ...] = ()


@dataclass
class DependenceResult:
    slices: tuple[SliceFinding, ...]
    pathological_rejected: dict[str, bool] = field(default_factory=dict)
    violations: tuple[str, ...] = ()

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    def summary(self) -> dict[str, Any]:
        return {
            "slices": [
                {
                    "slice_id": finding.slice_id,
                    "status": finding.status,
                    "accepted_count": finding.accepted_count,
                    "false_verified_count": finding.false_verified_count,
                    "catastrophic_error_count": finding.catastrophic_error_count,
                    "selected_witnesses": list(finding.selected_witnesses),
                    "violations": list(finding.violations),
                }
                for finding in self.slices
            ],
            "pathological_rejected": dict(self.pathological_rejected),
            "violations": list(self.violations),
        }


def evaluate_dependence() -> DependenceResult:
    """Run the held-out adversarial evaluation (Q5/Q6 evidence)."""
    corpus = load_verification_risk_corpus(build_heldout_corpus())
    violations: list[str] = []

    selected = _dependency_aware_ids(corpus)
    if "m2" in selected:
        violations.append(
            "selection: m2 admitted despite sharing renderer+cropper with m1 "
            "(base-lineage masking attack succeeded)"
        )

    findings: list[SliceFinding] = []
    expectations = {
        "heldout": {"status_ok": True, "max_false_verified": 0},
        "highrisk": {"status_ok": False, "must_abstain_all": True},
        "shifted": {"status_ok": False, "must_abstain_all": True},
        "thin": {"status_ok": False, "must_abstain_all": True},
    }
    for slice_id, expectation in expectations.items():
        comparison = evaluate_baselines(corpus, slice_id=slice_id)
        dependency = comparison.baselines[DEPENDENCE_BASELINE]
        slice_violations: list[str] = []
        if expectation["status_ok"]:
            if dependency.status != "ok":
                slice_violations.append(
                    f"status {dependency.status!r} with reason "
                    f"{dependency.not_applicable_reason!r}"
                )
            if dependency.false_verified_count > expectation["max_false_verified"]:
                slice_violations.append(
                    f"false verifications: {dependency.false_verified_count}"
                )
        else:
            if dependency.status == "ok":
                slice_violations.append("gate passed where it must abstain")
            if expectation.get("must_abstain_all") and dependency.accepted_count != 0:
                slice_violations.append(
                    f"accepted {dependency.accepted_count} where abstention is required"
                )
        findings.append(
            SliceFinding(
                slice_id=slice_id,
                status=dependency.status,
                accepted_count=dependency.accepted_count,
                false_verified_count=dependency.false_verified_count,
                catastrophic_error_count=dependency.catastrophic_error_count,
                selected_witnesses=dependency.selected_witnesses,
                violations=tuple(slice_violations),
            )
        )
        violations.extend(f"{slice_id}: {v}" for v in slice_violations)

    pathological_rejected: dict[str, bool] = {}
    for name, payload in build_pathological_corpora().items():
        try:
            load_verification_risk_corpus(payload)
        except VerificationRiskError:
            pathological_rejected[name] = True
        else:
            pathological_rejected[name] = False
            violations.append(f"pathological corpus {name!r} was not rejected at load")

    return DependenceResult(
        slices=tuple(findings),
        pathological_rejected=pathological_rejected,
        violations=tuple(violations),
    )
