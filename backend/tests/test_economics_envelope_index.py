"""Economics envelope index tests — the composed proof the readiness
ledger binds must fail closed when any profile artifact is missing,
corrupted, or hand-weakened.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.economics_envelope_index import PROFILE_ARTIFACTS, build_index

pytestmark = pytest.mark.usefixtures()

REPO = Path(__file__).resolve().parent.parent.parent
MEASUREMENTS = REPO / "docs" / "reference" / "measurements"


@pytest.fixture()
def measurements_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "measurements"
    copy.mkdir()
    for filename in PROFILE_ARTIFACTS.values():
        shutil.copy(MEASUREMENTS / filename, copy / filename)
    return copy


def test_committed_artifacts_produce_passing_index():
    index = build_index(MEASUREMENTS)
    assert index["pass"] is True
    assert index["problems"] == []
    assert all(index["checks"].values())
    for profile in PROFILE_ARTIFACTS:
        assert index["artifacts"][profile]["sha256"]
        assert index["artifacts"][profile]["validation_errors"] == []


def test_missing_artifact_fails_the_index(measurements_copy: Path):
    (measurements_copy / PROFILE_ARTIFACTS["industrial"]).unlink()
    index = build_index(measurements_copy)
    assert index["pass"] is False
    assert any("industrial artifact missing" in p for p in index["problems"])


def test_corrupted_envelope_fails_the_index(measurements_copy: Path):
    path = measurements_copy / PROFILE_ARTIFACTS["local"]
    parsed = json.loads(path.read_text(encoding="utf-8"))
    # zero-as-unavailable: force an unavailable dimension to carry a value
    for metric in parsed["dimensions"].values():
        if metric.get("status") == "unavailable":
            metric["value"] = 0
            break
    path.write_text(json.dumps(parsed), encoding="utf-8")
    index = build_index(measurements_copy)
    assert index["pass"] is False
    assert index["checks"]["all_artifacts_valid"] is False


def test_weakened_wal_measurement_fails_the_index(measurements_copy: Path):
    path = measurements_copy / PROFILE_ARTIFACTS["industrial"]
    parsed = json.loads(path.read_text(encoding="utf-8"))
    parsed["dimensions"]["wal_write_amplification"]["status"] = "not_applicable"
    parsed["dimensions"]["wal_write_amplification"]["reason"] = "hand-waved away"
    del parsed["dimensions"]["wal_write_amplification"]["value"]
    del parsed["dimensions"]["wal_write_amplification"]["derivation"]
    path.write_text(json.dumps(parsed), encoding="utf-8")
    index = build_index(measurements_copy)
    assert index["checks"]["wal_amplification_measured_on_industrial"] is False
    assert index["pass"] is False


def test_visual_decision_removed_fails_the_index(measurements_copy: Path):
    path = measurements_copy / PROFILE_ARTIFACTS["visual"]
    parsed = json.loads(path.read_text(encoding="utf-8"))
    del parsed["dimensions"]["decision"]
    path.write_text(json.dumps(parsed), encoding="utf-8")
    index = build_index(measurements_copy)
    assert index["pass"] is False
