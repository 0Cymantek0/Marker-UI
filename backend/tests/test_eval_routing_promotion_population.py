"""Final-holdout population and contamination-control tests (invariant 25)."""

from __future__ import annotations

import json

import pytest

from app.eval.routing_promotion.population import (
    BEST_SINGLE_WITNESS,
    DEVELOPMENT_EVIDENCE,
    HOLDOUT_WITNESS_IDS,
    POPULATION_ID,
    build_final_holdout_corpus,
    development_corpora,
    evaluate_leakage,
    holdout_population_document,
)
from app.eval.verification_risk.baselines import witness_dependency_keys
from app.eval.verification_risk.common import VerificationRiskError
from app.eval.verification_risk.loaders import load_verification_risk_corpus


@pytest.fixture(scope="module")
def holdout():
    return build_final_holdout_corpus()


@pytest.fixture(scope="module")
def dev_corpora():
    return development_corpora()


class TestPopulationConstruction:
    def test_construction_is_deterministic(self):
        first = build_final_holdout_corpus()
        second = build_final_holdout_corpus()
        assert first.semantic_identity == second.semantic_identity
        assert holdout_population_document() == json.loads(
            json.dumps(holdout_population_document())
        )

    def test_document_passes_fail_closed_loader_unchanged(self):
        corpus = load_verification_risk_corpus(holdout_population_document())
        assert corpus.name == POPULATION_ID
        with pytest.raises(VerificationRiskError):
            load_verification_risk_corpus(
                {
                    **holdout_population_document(),
                    "samples": [
                        {**holdout_population_document()["samples"][0], "surprise": 1}
                    ],
                }
            )

    def test_non_finite_predictions_are_rejected_at_the_load_boundary(self):
        document = holdout_population_document()
        document["samples"][0]["outcomes"]["ocr-p7"]["prediction"] = float("nan")
        with pytest.raises(VerificationRiskError):
            load_verification_risk_corpus(document)

    def test_slice_composition_matches_the_declared_design(self, holdout):
        counts = {
            slice_id: len(holdout.samples_for_slice(slice_id))
            for slice_id in holdout.slice_ids
        }
        assert counts == {"heldout-matched": 39, "heldout-shifted": 25, "heldout-thin": 3}
        catastrophic = {
            slice_id: sum(
                1 for sample in holdout.samples_for_slice(slice_id) if sample.catastrophic
            )
            for slice_id in holdout.slice_ids
        }
        assert catastrophic == {"heldout-matched": 18, "heldout-shifted": 4, "heldout-thin": 0}

    def test_witnesses_are_fresh_families_with_a_declared_best_single(self, holdout):
        assert sorted(w.witness_id for w in holdout.witnesses) == sorted(HOLDOUT_WITNESS_IDS)
        assert holdout.metadata["baseline_best_single_witness"] == BEST_SINGLE_WITNESS
        # The correlated pair shares exactly the rend-p7 renderer.
        keys = {w.witness_id: set(witness_dependency_keys(w)) for w in holdout.witnesses}
        shared = keys["ocr-p7"] & keys["pipe-q9"]
        assert {("dim", "renderer", "rend-p7")} <= shared
        # No other pair of witnesses shares any dependency dimension.
        ids = sorted(keys)
        for index, first in enumerate(ids):
            for second in ids[index + 1 :]:
                pair = {first, second}
                if pair == {"ocr-p7", "pipe-q9"}:
                    continue
                assert keys[first] & keys[second] == set(), (first, second)

    def test_population_metadata_binds_the_exclusion_manifest(self, holdout):
        excluded = holdout.metadata["population"]["excluded_development_evidence"]
        assert excluded == [entry["evidence_id"] for entry in DEVELOPMENT_EVIDENCE]


class TestLeakageControls:
    def test_real_holdout_is_clean_against_all_declared_development_evidence(
        self, holdout, dev_corpora
    ):
        report = evaluate_leakage(holdout, development=dev_corpora)
        assert report.clean
        assert len(report.checked_evidence) == len(DEVELOPMENT_EVIDENCE)
        assert report.population_identity == holdout.semantic_identity

    def test_sample_id_overlap_is_detected(self, holdout, dev_corpora):
        _entry, corpus = dev_corpora[0]
        dev_sample_id = corpus.samples[0].sample_id
        document = holdout_population_document()
        document["samples"].append(
            {
                "sample_id": dev_sample_id,
                "label": "verified",
                "outcomes": {"ocr-p7": {"prediction": "verified"}},
                "slice": "heldout-matched",
            }
        )
        contaminated = load_verification_risk_corpus(document)
        report = evaluate_leakage(contaminated, development=dev_corpora)
        assert not report.clean
        assert any(dev_sample_id in item for item in report.sample_id_overlaps)

    def test_renamed_development_sample_is_still_detected(self, holdout, dev_corpora):
        """Renaming a consumed sample must not restore pristine status.

        A development corpus built over the same witnesses (the realistic
        tuning-loop case) cannot donate a sample to the holdout by renaming
        its id: sample-content identity ignores ids and still collides.
        """

        document = holdout_population_document()
        donated = dict(document["samples"][0])
        donated["sample_id"] = "laundered-001"
        tuning_document = {
            "schema_version": "marker.verification_risk_corpus.v1",
            "name": "tuning-corpus-over-same-witnesses",
            "witnesses": document["witnesses"],
            "samples": [donated],
            "metadata": {"baseline_best_single_witness": BEST_SINGLE_WITNESS},
        }
        tuning_corpus = load_verification_risk_corpus(tuning_document)
        entry = {
            "evidence_id": "tuning-corpus",
            "kind": "procedural_builder",
            "location": "<test>",
            "role": "development_tuning",
            "expected_semantic_identity": tuning_corpus.semantic_identity,
        }
        report = evaluate_leakage(
            holdout, development=((entry, tuning_corpus), *dev_corpora)
        )
        assert not report.clean
        assert any("laundered-001" in item for item in report.sample_content_overlaps)

    def test_reused_development_witness_family_is_detected(self, holdout, dev_corpora):
        _entry, corpus = dev_corpora[0]
        dev_witness = next(w for w in corpus.witnesses if w.witness_id == "model-a")
        reused = {
            "id": "model-a",
            "kind": "model",
            "model_family": dev_witness.model_family,
            "base_lineage": dev_witness.base_lineage,
            "disclosure": "complete",
            "renderer": dev_witness.renderer,
            "cropper": dev_witness.cropper,
            "detector": dev_witness.detector,
        }
        document = holdout_population_document()
        document["witnesses"].append(reused)
        for sample in document["samples"]:
            first = next(iter(sample["outcomes"]))
            sample["outcomes"]["model-a"] = dict(sample["outcomes"][first])
        contaminated = load_verification_risk_corpus(document)
        report = evaluate_leakage(contaminated, development=dev_corpora)
        assert not report.clean
        assert report.witness_dependency_overlaps

    def test_stale_exclusion_manifest_fails_closed(self, holdout, dev_corpora):
        stale_entry = {
            **dev_corpora[0][0],
            "expected_semantic_identity": "sha256:" + "0" * 64,
        }
        report = evaluate_leakage(
            holdout, development=((stale_entry, dev_corpora[0][1]), *dev_corpora[1:])
        )
        assert not report.clean
        assert report.manifest_mismatches

    def test_pr75_and_pr82a_corpora_are_contaminated_as_holdouts(self, dev_corpora):
        """Consumed corpora can never serve as their own pristine holdout."""

        for entry, corpus in dev_corpora:
            report = evaluate_leakage(corpus, development=dev_corpora)
            assert not report.clean, entry["evidence_id"]

    def test_development_evidence_manifest_pins_current_revisions(self, monkeypatch):
        import app.eval.routing_promotion.population as module

        tampered = (
            {
                **module.DEVELOPMENT_EVIDENCE[0],
                "expected_semantic_identity": "sha256:" + "f" * 64,
            },
            *module.DEVELOPMENT_EVIDENCE[1:],
        )
        monkeypatch.setattr(module, "DEVELOPMENT_EVIDENCE", tampered)
        report = evaluate_leakage(build_final_holdout_corpus())
        assert not report.clean
        assert report.manifest_mismatches
