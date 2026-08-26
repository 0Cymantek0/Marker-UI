"""Focused tests for PR80B direct-specialist displacement benchmark replay."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from app.eval.accountability.displacement import (
    DIMENSION_COST,
    MEASUREMENT_STATUS_UNAVAILABLE,
    OUTCOME_MARKER_RETAINED,
    PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    REASON_STATUS_MEASURED,
    DisplacementDecisionError,
    create_pr80b_retrospective_preregistration,
    derive_displacement_decision,
    parse_pr80b_measurement_artifact,
    validate_displacement_measurement_bundle,
    validate_displacement_preregistration,
    validate_persisted_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PR80B_ARTIFACT_PATH = (
    REPO_ROOT
    / "docs"
    / "reference"
    / "measurements"
    / "pr80b-direct-specialist-displacement.json"
)
INJECTED_AS_OF_DATE = "2026-08-26T00:00:00Z"

# -----------------------------------------------------------------------------
# PR80B Actual Artifact Replay & Adversarial Parser Tests
# -----------------------------------------------------------------------------


def test_pr80b_actual_artifact_replay():
    """Replay against actual committed PR80B artifact: derives marker_retained with LLM reason measured."""
    assert PR80B_ARTIFACT_PATH.is_file(), (
        f"Committed PR80B artifact missing: {PR80B_ARTIFACT_PATH}"
    )

    prereg = create_pr80b_retrospective_preregistration(
        as_of_date="2026-08-26T00:00:00Z"
    )
    bundle = parse_pr80b_measurement_artifact(PR80B_ARTIFACT_PATH)

    # Validate preregistration and bundle
    p_errs = validate_displacement_preregistration(
        prereg, as_of_date=INJECTED_AS_OF_DATE
    )
    assert p_errs == []
    b_errs = validate_displacement_measurement_bundle(
        bundle, prereg=prereg, as_of_date=INJECTED_AS_OF_DATE
    )
    assert b_errs == []

    # Verify cost dimension is UNAVAILABLE (reported_cost was null; not zero!)
    for comp in bundle.comparators.values():
        cost_meas = comp.dimensions[DIMENSION_COST]
        assert cost_meas.status == MEASUREMENT_STATUS_UNAVAILABLE
        assert cost_meas.as_numeric() is None

    # Derive decision
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=INJECTED_AS_OF_DATE, repo_root=REPO_ROOT
    )

    # In PR80B-only replay:
    # 1. PR80A has 0 dangerous failures, 100% evidence coverage, 17 doc exact.
    # 2. LLM has 1 fabrication, 2 conflicts, 3 silent contradictions, 0% coverage.
    # 3. Candidate integration is future/unimplemented recommendation, not an active verified bridge.
    # 4. Therefore, outcome is marker_retained, and LLM generative advantage is measured.
    assert decision.outcome == OUTCOME_MARKER_RETAINED
    assert decision.fairness_passed is True
    assert decision.blockers == ()

    # Check reason ledger: LLM reasons are marked 'measured'
    llm_reasons = [
        r for r in decision.reason_ledger if r.specialist_system_id.startswith("llm")
    ]
    assert len(llm_reasons) >= 1
    assert all(r.status == REASON_STATUS_MEASURED for r in llm_reasons)

    # Check limitations disclosed
    assert decision.protocol_timing == PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY
    assert any("Retrospective frozen replay" in lim for lim in decision.limitations)
    assert any("observed-count gate" in lim for lim in decision.limitations)

    # Rederivation validation passes cleanly
    val_errs = validate_persisted_decision(
        decision, prereg, bundle, as_of_date=INJECTED_AS_OF_DATE, repo_root=REPO_ROOT
    )
    assert val_errs == []


def test_pr80b_parser_verifies_raw_bytes_sha():
    """Parser computes exact SHA-256 over raw file bytes."""
    raw_bytes = PR80B_ARTIFACT_PATH.read_bytes()
    expected_sha = hashlib.sha256(raw_bytes).hexdigest()

    bundle = parse_pr80b_measurement_artifact(PR80B_ARTIFACT_PATH)
    assert bundle.supporting_artifact_sha256 == expected_sha


def test_pr80b_parser_adversarial_missing_or_corrupt_fields():
    """Parser strictly rejects missing or corrupted fields in PR80B artifact."""
    raw_text = PR80B_ARTIFACT_PATH.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    # 1. Missing corpus
    d_no_corpus = copy.deepcopy(data)
    del d_no_corpus["corpus"]
    with pytest.raises(DisplacementDecisionError, match="Missing required 'corpus'"):
        parse_pr80b_measurement_artifact(d_no_corpus)

    # 2. Corrupt document count
    d_bad_docs = copy.deepcopy(data)
    d_bad_docs["corpus"]["documents"] = 23
    with pytest.raises(DisplacementDecisionError, match="Expected 24 corpus documents"):
        parse_pr80b_measurement_artifact(d_bad_docs)

    # 3. Missing acceptance
    d_no_acc = copy.deepcopy(data)
    del d_no_acc["acceptance"]
    with pytest.raises(
        DisplacementDecisionError, match="Missing required 'acceptance'"
    ):
        parse_pr80b_measurement_artifact(d_no_acc)

    # 4. Missing systems
    d_no_sys = copy.deepcopy(data)
    del d_no_sys["systems"]
    with pytest.raises(DisplacementDecisionError, match="Missing required 'systems'"):
        parse_pr80b_measurement_artifact(d_no_sys)

    # 5. Missing metrics
    d_no_metrics = copy.deepcopy(data)
    del d_no_metrics["metrics"]
    with pytest.raises(DisplacementDecisionError, match="Missing required 'metrics'"):
        parse_pr80b_measurement_artifact(d_no_metrics)

    # 6. Missing danger_counts
    d_no_danger = copy.deepcopy(data)
    del d_no_danger["decision"]["evidence_supporting"]["danger_counts"]
    with pytest.raises(DisplacementDecisionError, match="Missing 'danger_counts'"):
        parse_pr80b_measurement_artifact(d_no_danger)

    # 7. Missing evidence_coverage
    d_no_cov = copy.deepcopy(data)
    del d_no_cov["decision"]["evidence_supporting"]["evidence_coverage"]
    with pytest.raises(DisplacementDecisionError, match="Missing 'evidence_coverage'"):
        parse_pr80b_measurement_artifact(d_no_cov)
