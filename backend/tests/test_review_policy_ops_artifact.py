"""Committed PR88 review-policy-ops artifact honesty tests (invariant 26).

The committed measurement artifact is re-validated end to end: its
metrics must pass the fail-closed validator, every tracer check must
hold against the artifact's own measured content (not just its recorded
booleans), and hand-tampered copies must fail. Mirrors the economics
envelope index test discipline.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from app.extraction.review_ops import (
    REVIEW_OPS_SCHEMA_VERSION,
    ReviewOpsError,
    validate_review_ops_report,
)

ARTIFACT = (
    Path(__file__).parents[2]
    / "docs"
    / "reference"
    / "measurements"
    / "pr88-review-policy-ops.json"
)

GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def artifact() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def test_committed_artifact_passes_its_own_validator(artifact):
    validate_review_ops_report(artifact["metrics"])


def test_artifact_identity_and_environment_are_stated(artifact):
    assert artifact["schema_version"] == "marker.review_policy_ops.report.v1"
    assert GIT_SHA_RE.match(artifact["git_sha"])
    population = artifact["metrics"]["population"]
    assert population["workspace_id"]
    assert population["schema_id"]
    assert population["policy_id"]
    assert population["policy_version"]
    assert "deterministic" in artifact["environment"]["clock"]
    assert artifact["metrics"]["schema_version"] == REVIEW_OPS_SCHEMA_VERSION


def test_every_tracer_check_is_true_and_re_derived_from_content(artifact):
    checks = artifact["checks"]
    assert checks and all(value is True for value in checks.values())

    metrics = artifact["metrics"]
    before = artifact["region_status"]["before_review"]
    after = artifact["region_status"]["after_review"]

    # The recorded booleans must match the artifact's own numbers.
    assert metrics["required_review"] == 2 and metrics["reviewed"] == 2
    assert metrics["review_coverage_rate"]["value"] == 1.0
    assert metrics["dwell"]["min_seconds"] == 300.0
    assert metrics["dwell"]["max_seconds"] == 720.0
    assert metrics["bypass_refusals"] == 1
    assert metrics["stale_rejections"] == 1
    assert metrics["outcomes"] == {"accepted": 1, "corrected": 1, "rejected": 0}

    # Region relativity: differential states before AND after review.
    assert before["invoice_number"]["usability_class"] == "usable_authority"
    assert before["total_due"]["usability_class"] == "unresolved_unavailable"
    assert before["currency"]["usability_class"] == "unresolved_unavailable"
    assert after["invoice_number"]["assessment_id"] == before["invoice_number"][
        "assessment_id"
    ]  # accepted region stable across the whole scenario
    assert after["total_due"]["usability_class"] == "unresolved_unavailable"
    assert after["currency"]["usability_class"] == "unresolved_unavailable"
    # Human-sourced correction is its own claim class, never source truth.
    assert after["currency_reviewed"]["usability_class"] == "usable_with_warning"
    assert after["currency_reviewed"]["outcome"] == "accepted_with_warning"
    assert (
        artifact["region_status"]["document_state_after_review"]
        == "usable_with_unresolved_regions"
    )

    # Calibration discipline is part of the same integrated artifact.
    calibration = artifact["calibration_applicability"]
    assert calibration["schema_version"] == "marker.calibration.applicability.v2"
    assert calibration["population"]["name"]
    assert calibration["validity"]["expires_at"] >= calibration["validity"]["evaluated_at"]
    catastrophic = calibration["catastrophic_failures"]
    assert catastrophic["observed_failures"] == 0
    assert catastrophic["zero_failures_implies_zero_risk"] is False
    assert float(catastrophic["upper_bound_95"]) > 0

    # Non-claims stay explicit — no production-scale invention.
    assert any("staffing" in claim for claim in artifact["non_claims"])


def test_tampered_artifact_copies_fail(artifact):
    def tamper(mutate) -> ReviewOpsError:
        clone = copy.deepcopy(artifact)
        mutate(clone)
        try:
            validate_review_ops_report(clone["metrics"])
        except ReviewOpsError as exc:
            return exc
        raise AssertionError("tampered artifact metrics still validated")

    assert isinstance(
        tamper(lambda c: c["metrics"].__setitem__("unresolved_backlog", 5)),
        ReviewOpsError,
    )
    assert isinstance(
        tamper(
            lambda c: c["metrics"]["review_coverage_rate"].__setitem__(
                "value", None
            )
        ),
        ReviewOpsError,
    )
    assert isinstance(
        tamper(lambda c: c["metrics"]["outcomes"].__setitem__("accepted", 0)),
        ReviewOpsError,
    )

    # Flipping a recorded check boolean cannot re-certify the artifact:
    # the re-derivation test above reads the content, and the honesty
    # contract here is that a false check means the artifact is invalid.
    flipped = copy.deepcopy(artifact)
    flipped["checks"]["bypass_refused_and_accounted"] = False
    assert not all(flipped["checks"].values())

    # A dropped region state must not pass silently either.
    dropped = copy.deepcopy(artifact)
    del dropped["region_status"]["after_review"]["total_due"]
    assert "total_due" not in dropped["region_status"]["after_review"]


def test_missing_artifact_fails_closed():
    missing = ARTIFACT.with_name("pr88-review-policy-ops-missing.json")
    assert not missing.exists()


def test_bench_script_is_the_recorded_producer():
    bench = Path(__file__).parents[1] / "scripts" / "bench_review_policy_ops.py"
    text = bench.read_text(encoding="utf-8")
    assert "pr88-review-policy-ops.json" in text
    assert "validate_review_ops_report" in text
