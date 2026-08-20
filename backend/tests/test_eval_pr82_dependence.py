"""Adversarial dependence/verification-risk evaluation tests (PR82A Q5/Q6)."""

from __future__ import annotations

import pytest

import app.eval.pr82.dependence as dependence_module
from app.eval.pr82.dependence import (
    build_heldout_corpus,
    build_pathological_corpora,
    evaluate_dependence,
)
from app.eval.verification_risk.baselines import _dependency_aware_ids
from app.eval.verification_risk.loaders import load_verification_risk_corpus
from app.eval.verification_risk.models import VerificationRiskError


def _mini_corpus(witnesses, samples=()):
    return load_verification_risk_corpus(
        {
            "$schema": "marker.verification_risk_corpus.v1",
            "name": "mini",
            "witnesses": witnesses,
            "samples": list(samples) or [
                {
                    "id": "s1",
                    "slice": "evaluation",
                    "distribution": "matched",
                    "label": True,
                    "outcomes": {
                        witness["id"]: {"prediction": True, "confidence": 0.9}
                        for witness in witnesses
                    },
                }
            ],
        }
    )


def _witness(witness_id, *, family=None, base=None, renderer=None, cropper=None):
    return {
        "id": witness_id,
        **({"model_family": family} if family else {}),
        "dependency_profile": {
            "disclosure": "complete",
            **({"base_lineage": base} if base else {}),
            **({"renderer": renderer} if renderer else {}),
            **({"cropper": cropper} if cropper else {}),
        },
    }


class TestCorrelationFixes:
    def test_shared_renderer_dedupes_across_different_base_lineage(self):
        corpus = _mini_corpus(
            [
                _witness("a", family="fa", base="ca", renderer="shared-r", cropper="shared-c"),
                _witness("b", family="fb", base="cb", renderer="shared-r", cropper="shared-c"),
                _witness("c", family="fc", base="cc", renderer="own-r", cropper="own-c"),
            ]
        )
        assert _dependency_aware_ids(corpus) == ("a", "c")

    def test_shared_model_family_dedupes(self):
        corpus = _mini_corpus(
            [
                _witness("a", family="same-family", base="ca"),
                _witness("b", family="same-family", base="cb"),
            ]
        )
        assert _dependency_aware_ids(corpus) == ("a",)

    def test_truly_independent_witnesses_stay_selected(self):
        corpus = _mini_corpus(
            [
                _witness("a", family="fa", base="ca", renderer="ra", cropper="xa"),
                _witness("b", family="fb", base="cb", renderer="rb", cropper="xb"),
            ]
        )
        assert _dependency_aware_ids(corpus) == ("a", "b")

    def test_nan_and_inf_predictions_fail_closed_at_load(self):
        for name, payload in build_pathological_corpora().items():
            with pytest.raises(VerificationRiskError, match="non-finite prediction"):
                load_verification_risk_corpus(payload)
            assert name in {"nan_prediction", "inf_prediction"}


class TestHeldOutEvaluation:
    def test_heldout_run_has_zero_violations(self):
        result = evaluate_dependence()
        assert result.violations == ()
        assert all(result.pathological_rejected.values())

    def test_heldout_slice_excludes_masked_witness_and_verifies_nothing_false(self):
        result = evaluate_dependence()
        heldout = next(f for f in result.slices if f.slice_id == "heldout")
        assert heldout.status == "ok"
        assert "m2" not in heldout.selected_witnesses
        assert heldout.false_verified_count == 0
        # Three attack samples tie 1-1 and abstain; the other nine verify.
        assert heldout.accepted_count == 9

    def test_shifted_slice_breaks_the_bound_into_abstention(self):
        result = evaluate_dependence()
        shifted = next(f for f in result.slices if f.slice_id == "shifted")
        assert shifted.status == "risk_bound_not_met"
        assert shifted.accepted_count == 0

    def test_thin_slice_stays_abstention_despite_zero_failures(self):
        result = evaluate_dependence()
        thin = next(f for f in result.slices if f.slice_id == "thin")
        assert thin.status == "insufficient_support"
        assert thin.accepted_count == 0
        assert thin.catastrophic_error_count == 0

    def test_highrisk_model_only_consensus_never_verifies(self):
        result = evaluate_dependence()
        highrisk = next(f for f in result.slices if f.slice_id == "highrisk")
        assert highrisk.status != "ok"
        assert highrisk.accepted_count == 0

    def test_masked_pipeline_admission_would_fabricate_verifications(self, monkeypatch):
        """Negative control: the evaluator catches the attack it exists for."""
        import app.eval.verification_risk.baselines as baselines_module

        def buggy_selector(corpus):
            return ("m1", "m2", "m3", "sn")

        monkeypatch.setattr(baselines_module, "_dependency_aware_ids", buggy_selector)
        monkeypatch.setattr(dependence_module, "_dependency_aware_ids", buggy_selector)
        result = dependence_module.evaluate_dependence()
        assert result.violation_count >= 2
        assert any("masking attack succeeded" in violation for violation in result.violations)
        heldout = next(f for f in result.slices if f.slice_id == "heldout")
        assert heldout.false_verified_count == 3
        assert "false verifications: 3" in " ".join(heldout.violations)

    def test_heldout_corpus_is_deterministic(self):
        import json

        first = json.dumps(build_heldout_corpus(), sort_keys=True)
        second = json.dumps(build_heldout_corpus(), sort_keys=True)
        assert first == second
