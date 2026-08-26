"""Independent final holdout population and contamination controls.

Workstream B of the invariant 25 slice.  The promotion decision must be
made on evidence that did not participate in the tuning/fixing loop:

- the PR75 fixture corpus helped define evaluator semantics and thresholds
  (development/tuning evidence);
- the PR82A adversarial corpus was fresh once, then legitimately drove
  evaluator fixes and is therefore consumed regression/adversarial
  evidence (masterplan 14B.8: audit training/benchmark overlap; NIST AITE
  sequestered-testbed principle: reused test data gradually becomes
  development data).

This module builds a separate, procedurally generated population from
declared document-family semantics — fresh witness families, fresh
dependency structures, rotated degradation settings — and provides the
machine-checkable leakage controls that keep it honest:

- declared exclusion manifest pinning each development corpus by semantic
  identity (stale manifest = invalid evidence);
- sample-id and sample-content overlap detection (renaming a development
  sample does not launder it);
- witness dependency-key overlap detection (a holdout reusing a
  development family is not an unseen family).

Construction is deterministic arithmetic: no RNG, labels assigned by
declared per-case ground-truth rules, and no case is derived from any
development corpus outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.eval.verification_risk.baselines import witness_dependency_keys
from app.eval.verification_risk.identity import _identity
from app.eval.verification_risk.loaders import load_verification_risk_corpus
from app.eval.verification_risk.models import VerificationRiskCorpus

POPULATION_ID = "inv25-final-holdout-v1"
BACKEND_ROOT = Path(__file__).resolve().parents[3]
PR75_FIXTURE_PATH = BACKEND_ROOT / "conformance" / "fixtures" / "verification_risk_corpus_v1.json"

VERIFIED = "verified"
REJECTED = "rejected"

BEST_SINGLE_WITNESS = "ocr-p7"
HOLDOUT_WITNESS_IDS: tuple[str, ...] = ("layout-k4", "native-2", "ocr-p7", "pipe-q9")

#: Development evidence this population must stay disjoint from.  The
#: expected semantic identities pin the exact revisions; if either corpus
#: changes, the manifest is stale and every promotion decision against it
#: fails closed until the exclusion is re-declared deliberately.
DEVELOPMENT_EVIDENCE: tuple[dict[str, str], ...] = (
    {
        "evidence_id": "pr75-verification-risk-corpus-v1",
        "kind": "committed_fixture",
        "location": "backend/conformance/fixtures/verification_risk_corpus_v1.json",
        "role": "development_tuning",
        "expected_semantic_identity": (
            "sha256:a6cde0976cdfb22daf1900ebaf6b5446db0f52c52e9c81181b9e1aecf6df7103"
        ),
    },
    {
        "evidence_id": "pr82a-adversarial-dependence-corpus",
        "kind": "procedural_builder",
        "location": "app.eval.pr82.dependence.build_heldout_corpus",
        "role": "consumed_adversarial_regression",
        "expected_semantic_identity": (
            "sha256:b4853e7bec8321df4c4710db7e34382ec17f9963cac3de70c547d77aff5a13ae"
        ),
    },
)

CONSTRUCTION_POLICY = (
    "Procedural deterministic construction (arithmetic label rules, no RNG). "
    "Witness families, base lineages, renderer/cropper/detector values, and "
    "document families are fresh values unseen in PR75 or PR82A; ocr-p7 and "
    "pipe-q9 intentionally share the rend-p7 renderer to preserve a "
    "correlated-pair structure. The shifted slice rotates degradation and "
    "render settings per masterplan 14B.8. No case is derived from any "
    "development-corpus outcome, and no evaluator behavior was observed "
    "while choosing the construction rules."
)


def _witness(
    witness_id: str,
    *,
    kind: str,
    label: str,
    model_family: str,
    base_lineage: str,
    renderer: str | None,
    cropper: str | None,
    detector: str | None,
) -> dict[str, Any]:
    return {
        "id": witness_id,
        "label": label,
        "kind": kind,
        "model_family": model_family,
        "base_lineage": base_lineage,
        "disclosure": "complete",
        "renderer": renderer,
        "cropper": cropper,
        "detector": detector,
    }


def _holdout_witnesses() -> list[dict[str, Any]]:
    return [
        _witness(
            "layout-k4",
            kind="model",
            label="Layout parser K4",
            model_family="layout-kappa-4",
            base_lineage="ckpt-k4-2026",
            renderer="rend-k4",
            cropper="crop-k4",
            detector="det-k4",
        ),
        _witness(
            "native-2",
            kind="source_native",
            label="Source text layer extractor 2",
            model_family="source-text-layer-2",
            base_lineage="native-extractor-2",
            renderer=None,
            cropper=None,
            detector=None,
        ),
        _witness(
            "ocr-p7",
            kind="model",
            label="OCR engine P7 (declared best single)",
            model_family="ocr-prime-7",
            base_lineage="ckpt-p7-2026",
            renderer="rend-p7",
            cropper="crop-p7",
            detector="det-p7",
        ),
        _witness(
            "pipe-q9",
            kind="model",
            label="Pipeline engine Q9 (shares rend-p7 renderer)",
            model_family="pipe-q-9",
            base_lineage="ckpt-q9-2026",
            renderer="rend-p7",
            cropper="crop-q9",
            detector="det-q9",
        ),
    ]


# Right/wrong confidence values per model witness; native-2 is deterministic
# and carries no confidence.
_CONFIDENCE_RIGHT = {"ocr-p7": 0.93, "layout-k4": 0.81, "pipe-q9": 0.90}
_CONFIDENCE_WRONG = {"ocr-p7": 0.88, "layout-k4": 0.77, "pipe-q9": 0.86}
_CONFIDENCE_SHIFTED = {"ocr-p7": 0.72, "layout-k4": 0.66, "pipe-q9": 0.70}


def _flip(label: str) -> str:
    return REJECTED if label == VERIFIED else VERIFIED


def _outcome(prediction: str, label: str, witness_id: str, *, shifted: bool) -> dict[str, Any]:
    if witness_id not in _CONFIDENCE_RIGHT:
        # native-2 is deterministic and carries no confidence.
        return {"prediction": prediction}
    if prediction != label:
        confidence = _CONFIDENCE_WRONG[witness_id]
    elif shifted:
        confidence = _CONFIDENCE_SHIFTED[witness_id]
    else:
        confidence = _CONFIDENCE_RIGHT[witness_id]
    return {"prediction": prediction, "confidence": confidence}


def _sample(
    sample_id: str,
    *,
    case: str,
    slice_id: str,
    distribution: str,
    label: str,
    outcomes: dict[str, str],
    catastrophic: bool = False,
    risk_level: str = "normal",
    source_family: str,
    shifted: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "population_id": POPULATION_ID,
        "population_case": case,
        "source_family": source_family,
    }
    if shifted:
        metadata["degradation"] = "rotated-rescan-profile-q3"
    return {
        "sample_id": sample_id,
        "label": label,
        "slice": slice_id,
        "case": case,
        "distribution": distribution,
        "risk_level": risk_level,
        "catastrophic": catastrophic,
        "outcomes": {
            witness_id: _outcome(prediction, label, witness_id, shifted=shifted)
            for witness_id, prediction in outcomes.items()
        },
        "metadata": metadata,
    }


def _clean_sample(index: int, *, slice_id: str, distribution: str, prefix: str,
                  family: str, hard: bool = False, catastrophic: bool = False) -> dict[str, Any]:
    label = VERIFIED if index % 2 == 0 else REJECTED
    outcomes = {wid: label for wid in HOLDOUT_WITNESS_IDS}
    return _sample(
        f"{prefix}-{index + 1:03d}",
        case="clean-agree" if not hard else "high-stakes-clean",
        slice_id=slice_id,
        distribution=distribution,
        label=label,
        outcomes=outcomes,
        catastrophic=catastrophic,
        source_family=family,
        shifted=distribution == "shifted",
    )


def _matched_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    # clean-agree x12: every witness correct.
    for index in range(12):
        samples.append(
            _clean_sample(index, slice_id="heldout-matched", distribution="matched",
                          prefix="fhm-clean", family="fam-ledger-2026q3")
        )
    # native-parser-bug x2: the deterministic source path has an edge bug;
    # diverse models are right.
    for index in range(2):
        samples.append(
            _sample(
                f"fhm-npb-{index + 1:02d}",
                case="native-parser-bug",
                slice_id="heldout-matched",
                distribution="matched",
                label=REJECTED,
                outcomes={
                    "layout-k4": REJECTED,
                    "native-2": VERIFIED,
                    "ocr-p7": REJECTED,
                    "pipe-q9": REJECTED,
                },
                source_family="fam-ledger-2026q3",
            )
        )
    # ocr-misread x3: the best single engine makes ordinary mistakes.
    for index in range(3):
        samples.append(
            _sample(
                f"fhm-ocr-{index + 1:02d}",
                case="ocr-misread",
                slice_id="heldout-matched",
                distribution="matched",
                label=REJECTED,
                outcomes={
                    "layout-k4": REJECTED,
                    "native-2": REJECTED,
                    "ocr-p7": VERIFIED,
                    "pipe-q9": REJECTED,
                },
                source_family="fam-ledger-2026q3",
            )
        )
    # shared-renderer-consensus x4: ocr-p7 and pipe-q9 agree wrongly through
    # the shared rend-p7 renderer; the first two are silent-corruption
    # (catastrophic) opportunities.
    for index in range(4):
        samples.append(
            _sample(
                f"fhm-src-{index + 1:02d}",
                case="shared-renderer-consensus",
                slice_id="heldout-matched",
                distribution="matched",
                label=REJECTED,
                outcomes={
                    "layout-k4": REJECTED,
                    "native-2": REJECTED,
                    "ocr-p7": VERIFIED,
                    "pipe-q9": VERIFIED,
                },
                catastrophic=index < 2,
                source_family="fam-forms-2026q3",
            )
        )
    # model-only-high-risk x6: high-risk samples with no source-native
    # outcome where the model-only consensus is confidently wrong.
    for index in range(6):
        samples.append(
            _sample(
                f"fhm-mhr-{index + 1:02d}",
                case="model-only-high-risk",
                slice_id="heldout-matched",
                distribution="matched",
                label=REJECTED,
                outcomes={"ocr-p7": VERIFIED, "pipe-q9": VERIFIED},
                catastrophic=True,
                risk_level="high",
                source_family="fam-forms-2026q3",
            )
        )
    # high-stakes-clean x10: catastrophic-opportunity documents where every
    # witness is correct (exposure trials without failure).
    for index in range(10):
        samples.append(
            _clean_sample(index, slice_id="heldout-matched", distribution="matched",
                          prefix="fhm-hsc", family="fam-contracts-2026q3",
                          hard=True, catastrophic=True)
        )
    # layout-misread x2: an independent model makes ordinary mistakes.
    for index in range(2):
        samples.append(
            _sample(
                f"fhm-lmr-{index + 1:02d}",
                case="layout-misread",
                slice_id="heldout-matched",
                distribution="matched",
                label=REJECTED,
                outcomes={
                    "layout-k4": VERIFIED,
                    "native-2": REJECTED,
                    "ocr-p7": REJECTED,
                    "pipe-q9": REJECTED,
                },
                source_family="fam-ledger-2026q3",
            )
        )
    return samples


def _shifted_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    # degraded-clean x8: rotated rescan; everyone still correct.
    for index in range(8):
        samples.append(
            _clean_sample(index, slice_id="heldout-shifted", distribution="shifted",
                          prefix="fhs-dc", family="fam-archive-2026q4")
        )
    # degraded-scan-trap x7 (ordinary) + degraded-scan-catastrophic x4: the
    # rescan degrades the shared text layer and rend-p7 pipeline together;
    # the layout path survives.
    for index in range(7):
        samples.append(
            _sample(
                f"fhs-trap-{index + 1:02d}",
                case="degraded-scan-trap",
                slice_id="heldout-shifted",
                distribution="shifted",
                label=REJECTED,
                outcomes={
                    "layout-k4": REJECTED,
                    "native-2": VERIFIED,
                    "ocr-p7": VERIFIED,
                    "pipe-q9": VERIFIED,
                },
                source_family="fam-archive-2026q4",
                shifted=True,
            )
        )
    for index in range(4):
        samples.append(
            _sample(
                f"fhs-cat-{index + 1:02d}",
                case="degraded-scan-catastrophic",
                slice_id="heldout-shifted",
                distribution="shifted",
                label=REJECTED,
                outcomes={
                    "layout-k4": REJECTED,
                    "native-2": VERIFIED,
                    "ocr-p7": VERIFIED,
                    "pipe-q9": VERIFIED,
                },
                catastrophic=True,
                source_family="fam-archive-2026q4",
                shifted=True,
            )
        )
    # shifted-clean-hard x6: unseen organization, lower confidence, correct.
    for index in range(6):
        samples.append(
            _clean_sample(index, slice_id="heldout-shifted", distribution="shifted",
                          prefix="fhs-hard", family="fam-unseen-org-2026q4")
        )
    return samples


def _thin_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    # thin-correlated-only x3: only the correlated pair reports; support is
    # deliberately below every empirical gate floor.
    for index in range(3):
        label = VERIFIED if index % 2 == 0 else REJECTED
        samples.append(
            _sample(
                f"fht-thin-{index + 1:02d}",
                case="thin-correlated-only",
                slice_id="heldout-thin",
                distribution="insufficient",
                label=label,
                outcomes={"ocr-p7": label, "pipe-q9": label},
                source_family="fam-edge-2026q3",
            )
        )
    return samples


def holdout_population_document() -> dict[str, Any]:
    """Deterministic corpus document for the final holdout population."""

    return {
        "schema_version": "marker.verification_risk_corpus.v1",
        "name": POPULATION_ID,
        "witnesses": _holdout_witnesses(),
        "samples": [
            *_matched_samples(),
            *_shifted_samples(),
            *_thin_samples(),
        ],
        "metadata": {
            "baseline_best_single_witness": BEST_SINGLE_WITNESS,
            "population": {
                "population_id": POPULATION_ID,
                "construction_policy": CONSTRUCTION_POLICY,
                "seed_policy": "deterministic arithmetic; no RNG",
                "excluded_development_evidence": [
                    entry["evidence_id"] for entry in DEVELOPMENT_EVIDENCE
                ],
            },
        },
    }


def build_final_holdout_corpus() -> VerificationRiskCorpus:
    """Build and fail-closed-validate the final holdout population."""

    return load_verification_risk_corpus(holdout_population_document())


def development_corpora() -> tuple[tuple[dict[str, str], VerificationRiskCorpus], ...]:
    """Resolve every declared development corpus (fail closed on drift)."""

    from app.eval.pr82.dependence import build_heldout_corpus

    resolved: list[tuple[dict[str, str], VerificationRiskCorpus]] = []
    for entry in DEVELOPMENT_EVIDENCE:
        if entry["kind"] == "committed_fixture":
            corpus = load_verification_risk_corpus(PR75_FIXTURE_PATH)
        else:
            corpus = load_verification_risk_corpus(build_heldout_corpus())
        resolved.append((entry, corpus))
    return tuple(resolved)


def _sample_content_identity(sample: Any) -> str:
    """Identity of a sample's content, excluding its id and bookkeeping.

    Copying a development sample under a new id keeps this identity and is
    still detected as contamination.
    """

    payload = {
        "label": sample.label,
        "slice": sample.slice_id,
        "case": sample.case,
        "distribution": sample.distribution,
        "risk_level": sample.risk_level,
        "catastrophic": sample.catastrophic,
        "outcomes": {key: sample.outcomes[key].as_dict() for key in sorted(sample.outcomes)},
    }
    return _identity(payload)


@dataclass(frozen=True)
class LeakageReport:
    """Machine-checkable contamination report for one holdout population."""

    population_identity: str
    checked_evidence: tuple[dict[str, Any], ...]
    sample_id_overlaps: tuple[str, ...]
    sample_content_overlaps: tuple[str, ...]
    witness_dependency_overlaps: tuple[str, ...]
    manifest_mismatches: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (
            self.sample_id_overlaps
            or self.sample_content_overlaps
            or self.witness_dependency_overlaps
            or self.manifest_mismatches
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "population_identity": self.population_identity,
            "clean": self.clean,
            "checked_evidence": [dict(item) for item in self.checked_evidence],
            "sample_id_overlaps": list(self.sample_id_overlaps),
            "sample_content_overlaps": list(self.sample_content_overlaps),
            "witness_dependency_overlaps": list(self.witness_dependency_overlaps),
            "manifest_mismatches": list(self.manifest_mismatches),
        }


def evaluate_leakage(
    holdout: VerificationRiskCorpus,
    *,
    development: tuple[tuple[dict[str, str], VerificationRiskCorpus], ...] | None = None,
) -> LeakageReport:
    """Prove the holdout is disjoint from all declared development evidence.

    Any sample-id collision, sample-content collision, shared witness
    dependency key, or stale manifest entry makes the report unclean and a
    promotion decision built on it invalid.
    """

    if development is None:
        development = development_corpora()
    holdout_ids = {sample.sample_id for sample in holdout.samples}
    holdout_content = {
        _sample_content_identity(sample): sample.sample_id for sample in holdout.samples
    }
    holdout_keys: set[tuple[Any, ...]] = set()
    for witness in holdout.witnesses:
        holdout_keys.update(witness_dependency_keys(witness))

    id_overlaps: list[str] = []
    content_overlaps: list[str] = []
    dependency_overlaps: list[str] = []
    manifest_mismatches: list[str] = []
    checked: list[dict[str, Any]] = []

    for entry, corpus in development:
        actual_identity = corpus.semantic_identity
        if actual_identity != entry["expected_semantic_identity"]:
            manifest_mismatches.append(
                f"{entry['evidence_id']}: declared {entry['expected_semantic_identity']} "
                f"but resolved {actual_identity}"
            )
        dev_ids = {sample.sample_id for sample in corpus.samples}
        shared_ids = sorted(holdout_ids & dev_ids)
        if shared_ids:
            id_overlaps.append(f"{entry['evidence_id']}: {shared_ids}")
        dev_content = {
            _sample_content_identity(sample): sample.sample_id for sample in corpus.samples
        }
        shared_content = sorted(set(holdout_content) & set(dev_content))
        if shared_content:
            content_overlaps.append(
                f"{entry['evidence_id']}: "
                f"{[(holdout_content[key], dev_content[key]) for key in shared_content]}"
            )
        dev_keys: set[tuple[Any, ...]] = set()
        for witness in corpus.witnesses:
            dev_keys.update(witness_dependency_keys(witness))
        shared_keys = sorted(
            f"{key}" for key in holdout_keys & dev_keys
        )
        if shared_keys:
            dependency_overlaps.append(f"{entry['evidence_id']}: {shared_keys}")
        checked.append(
            {
                "evidence_id": entry["evidence_id"],
                "role": entry["role"],
                "declared_semantic_identity": entry["expected_semantic_identity"],
                "resolved_semantic_identity": actual_identity,
                "sample_count": len(corpus.samples),
            }
        )

    return LeakageReport(
        population_identity=holdout.semantic_identity,
        checked_evidence=tuple(checked),
        sample_id_overlaps=tuple(id_overlaps),
        sample_content_overlaps=tuple(content_overlaps),
        witness_dependency_overlaps=tuple(dependency_overlaps),
        manifest_mismatches=tuple(manifest_mismatches),
    )
