"""Economics envelope contract tests — positive shape plus every
misleading-evidence form that must fail closed (invariant 57/58 honesty):

* unavailable dimensions encoded as zero (or any stand-in value);
* missing/unknown units; missing raw counters behind a ratio;
* ratios whose numerator and denominator come from different windows;
* percentiles claimed without an honest sample count;
* dropped dimensions (every dimension of the declared set must be
  stated, even as unavailable/not_applicable);
* missing provenance (commit, timestamp, workload fingerprint).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.economics.contract import ENVELOPE_SCHEMA
from app.eval.economics.validate import load_envelope, validate_envelope

GIT_SHA = "a" * 40


def _metric_measured(**overrides) -> dict:
    metric = {
        "status": "measured",
        "unit": "count",
        "window": "ingest",
        "value": 42,
        "source": "exact SELECT COUNT(*)",
    }
    metric.update(overrides)
    return metric


def _envelope() -> dict:
    """Minimal valid invariant-57 envelope covering every dimension."""
    return {
        "schema": ENVELOPE_SCHEMA,
        "profile": "local-sqlite-dev",
        "dimension_set": "invariant_57",
        "git_sha": GIT_SHA,
        "generated_at": "2026-08-23T00:00:00+00:00",
        "run_mode": "offline",
        "model_participation": {"mode": "none"},
        "workload": {
            "identity": "pr81a corpus x revision",
            "fingerprint": "sha256:abc",
        },
        "environment": {"python": "3.11.9", "database": "sqlite"},
        "windows": [
            {"id": "ingest", "label": "seed 16 docs"},
            {"id": "full", "label": "entire workload"},
        ],
        "dimensions": {
            "database_rows": _metric_measured(
                value=120, breakdown={"logical_authority": 80, "lexical": 40}
            ),
            "payload_objects": _metric_measured(unit="bytes", value=2048),
            "wal_write_amplification": {
                "status": "derived",
                "unit": "ratio",
                "window": "full",
                "value": 2.0,
                "source": "wal_bytes_generated / logical_payload_bytes",
                "derivation": {
                    "numerator": "wal_bytes_generated",
                    "denominator": "logical_payload_bytes",
                },
            },
            "retained_generations": _metric_measured(value=2),
            "fts_storage": _metric_measured(unit="bytes", value=512),
            "vector_storage": {
                "status": "not_applicable",
                "unit": "bytes",
                "window": "ingest",
                "reason": "no vector store implementation exists on this profile",
            },
            "visual_storage": {
                "status": "unavailable",
                "unit": "bytes",
                "window": "ingest",
                "reason": "visual state not exercised by this workload",
            },
            "copy_bytes": _metric_measured(unit="bytes", value=1024),
            "cold_start": {
                "status": "measured",
                "unit": "milliseconds",
                "window": "ingest",
                "value": 1500.0,
                "source": "perf_counter around build+publish",
                "samples": {"n": 3, "min": 1400.0, "p50": 1500.0, "max": 1600.0},
            },
            "review_burden": _metric_measured(value=4),
            "reprocessing": _metric_measured(unit="count", value=1),
        },
        "counters": {
            "wal_bytes_generated": {
                "status": "measured",
                "unit": "bytes",
                "window": "full",
                "value": 8192,
                "source": "pg_stat_wal delta",
            },
            "logical_payload_bytes": {
                "status": "measured",
                "unit": "bytes",
                "window": "full",
                "value": 4096,
                "source": "payload store counters",
            },
        },
        "non_claims": ["no human-time review claims"],
    }


def _dimension(envelope: dict, name: str) -> dict:
    return envelope["dimensions"][name]


def _counter(envelope: dict, name: str) -> dict:
    return envelope["counters"][name]


def test_valid_envelope_passes():
    assert validate_envelope(_envelope()) == []


def test_wrong_schema_fails():
    envelope = _envelope()
    envelope["schema"] = "marker.something.else.v1"
    assert validate_envelope(envelope) == [
        f"schema must be {ENVELOPE_SCHEMA!r}"
    ]


def test_dropping_a_dimension_fails_instead_of_silent_omission():
    envelope = _envelope()
    del envelope["dimensions"]["review_burden"]
    errors = validate_envelope(envelope)
    assert any("dimension 'review_burden' missing" in e for e in errors)


def test_unknown_dimension_fails():
    envelope = _envelope()
    envelope["dimensions"]["vibes"] = _metric_measured()
    assert any("'vibes' is not part of dimension_set" in e
               for e in validate_envelope(envelope))


def test_unknown_dimension_set_fails():
    envelope = _envelope()
    envelope["dimension_set"] = "invariant_99"
    assert validate_envelope(envelope) and validate_envelope(envelope)[0].startswith(
        "dimension_set must be"
    )


def test_unavailable_dimension_encoded_as_zero_fails():
    envelope = _envelope()
    _dimension(envelope, "visual_storage").update({"value": 0})
    del _dimension(envelope, "visual_storage")["reason"]
    errors = validate_envelope(envelope)
    assert any("must omit value entirely" in e for e in errors)
    assert any("must carry a reason" in e for e in errors)


def test_unavailable_without_reason_fails():
    envelope = _envelope()
    del _dimension(envelope, "visual_storage")["reason"]
    assert any("must carry a reason" in e for e in validate_envelope(envelope))


def test_measured_without_value_or_source_fails():
    envelope = _envelope()
    del _dimension(envelope, "fts_storage")["value"]
    del _dimension(envelope, "fts_storage")["source"]
    errors = validate_envelope(envelope)
    assert any("carries no value" in e for e in errors)
    assert any("must state how it was obtained" in e for e in errors)


def test_measured_with_reason_is_contradictory_and_fails():
    envelope = _envelope()
    _dimension(envelope, "fts_storage")["reason"] = "odd mix"
    assert any("must not carry a reason" in e for e in validate_envelope(envelope))


def test_unknown_unit_fails():
    envelope = _envelope()
    _dimension(envelope, "payload_objects")["unit"] = "floppies"
    assert any("unit must be one of" in e for e in validate_envelope(envelope))


def test_missing_unit_fails():
    envelope = _envelope()
    del _dimension(envelope, "payload_objects")["unit"]
    assert any("unit must be one of" in e for e in validate_envelope(envelope))


def test_count_must_be_integer_and_nonnegative():
    envelope = _envelope()
    _dimension(envelope, "database_rows")["value"] = 4.5
    assert any("unit 'count' requires an integer" in e
               for e in validate_envelope(envelope))
    _dimension(envelope, "database_rows")["value"] = -1
    assert any("cannot be negative" in e for e in validate_envelope(envelope))


def test_rate_out_of_range_fails():
    envelope = _envelope()
    _dimension(envelope, "database_rows").update(
        {"unit": "rate", "value": 1.5}
    )
    assert any("unit 'rate' requires a value in [0, 1]" in e
               for e in validate_envelope(envelope))


def test_boolean_unit_requires_boolean():
    envelope = _envelope()
    _dimension(envelope, "database_rows").update({"unit": "boolean", "value": 1})
    assert any("unit 'boolean' requires a boolean value" in e
               for e in validate_envelope(envelope))


def test_ratio_without_derivation_fails():
    envelope = _envelope()
    del _dimension(envelope, "wal_write_amplification")["derivation"]
    assert any("must declare its derivation" in e
               for e in validate_envelope(envelope))


def test_ratio_with_derived_numerator_fails():
    envelope = _envelope()
    _dimension(envelope, "wal_write_amplification")["derivation"] = {
        "numerator": "wal_write_amplification",
        "denominator": "logical_payload_bytes",
    }
    assert any("must reference a 'measured' raw counter" in e
               for e in validate_envelope(envelope))


def test_ratio_with_zero_denominator_fails():
    envelope = _envelope()
    _counter(envelope, "logical_payload_bytes")["value"] = 0
    assert any("zero denominator" in e for e in validate_envelope(envelope))


def test_cross_window_ratio_fails():
    envelope = _envelope()
    _counter(envelope, "wal_bytes_generated")["window"] = "ingest"
    errors = validate_envelope(envelope)
    assert any("cross-workload ratios are invalid" in e for e in errors)


def test_ratio_referencing_unknown_dimension_fails():
    envelope = _envelope()
    _dimension(envelope, "wal_write_amplification")["derivation"] = {
        "numerator": "wal_bytes_from_someone_elses_run",
        "denominator": "logical_payload_bytes",
    }
    assert any("is not a metric of this envelope" in e
               for e in validate_envelope(envelope))


def test_derivation_on_measured_status_fails():
    envelope = _envelope()
    _dimension(envelope, "wal_write_amplification")["status"] = "measured"
    assert any("carries a derivation but is not status 'derived'" in e
               for e in validate_envelope(envelope))


def test_timing_metric_without_samples_fails():
    envelope = _envelope()
    del _dimension(envelope, "cold_start")["samples"]
    assert any("must carry a samples block" in e
               for e in validate_envelope(envelope))


def test_percentile_without_sample_count_fails():
    envelope = _envelope()
    _dimension(envelope, "cold_start")["samples"] = {"min": 1.0, "p50": 1.5, "max": 2.0}
    assert any("integer sample count n >= 1" in e
               for e in validate_envelope(envelope))


def test_percentile_from_single_sample_fails():
    envelope = _envelope()
    _dimension(envelope, "cold_start")["samples"] = {
        "n": 1, "min": 1.0, "p50": 1.5, "max": 2.0
    }
    assert any("single sample" in e for e in validate_envelope(envelope))


def test_non_monotonic_sample_summary_fails():
    envelope = _envelope()
    _dimension(envelope, "cold_start")["samples"] = {
        "n": 3, "min": 1600.0, "p50": 1500.0, "max": 1400.0
    }
    assert any("monotonic" in e for e in validate_envelope(envelope))


def test_non_timing_metric_with_samples_fails():
    envelope = _envelope()
    _dimension(envelope, "database_rows")["samples"] = {"n": 3}
    assert any("not a timing metric but carries samples" in e
               for e in validate_envelope(envelope))


def test_missing_provenance_fails():
    envelope = _envelope()
    envelope["git_sha"] = "HEAD"
    assert any("git_sha must be the 40-hex" in e for e in validate_envelope(envelope))
    envelope["git_sha"] = GIT_SHA
    envelope["generated_at"] = ""
    assert any("generated_at" in e for e in validate_envelope(envelope))
    envelope["generated_at"] = "2026-08-23T00:00:00+00:00"
    envelope["workload"]["fingerprint"] = ""
    assert any("workload.fingerprint" in e for e in validate_envelope(envelope))


def test_bad_run_mode_fails():
    envelope = _envelope()
    envelope["run_mode"] = "trust-me"
    assert any("run_mode must be one of" in e for e in validate_envelope(envelope))


def test_undeclared_window_reference_fails():
    envelope = _envelope()
    _dimension(envelope, "database_rows")["window"] = "mystery"
    assert any("undeclared window 'mystery'" in e
               for e in validate_envelope(envelope))


def test_duplicate_window_ids_fail():
    envelope = _envelope()
    envelope["windows"].append({"id": "ingest", "label": "again"})
    assert any("duplicate window id" in e for e in validate_envelope(envelope))


def test_negative_breakdown_fails():
    envelope = _envelope()
    _dimension(envelope, "database_rows")["breakdown"] = {"logical_authority": -5}
    assert any("must be a non-negative number" in e
               for e in validate_envelope(envelope))


def test_dimension_not_an_object_fails():
    envelope = _envelope()
    envelope["dimensions"]["fts_storage"] = 42
    assert any("must be an object" in e for e in validate_envelope(envelope))


def test_unknown_status_fails():
    envelope = _envelope()
    _dimension(envelope, "fts_storage")["status"] = "approximately"
    assert any("status must be one of" in e for e in validate_envelope(envelope))


def test_load_envelope_rejects_corrupt_file(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    parsed, errors = load_envelope(bad)
    assert parsed == {} and errors and "cannot read envelope" in errors[0]

    array = tmp_path / "array.json"
    array.write_text(json.dumps([1, 2]), encoding="utf-8")
    parsed, errors = load_envelope(array)
    assert parsed == {} and "must be a JSON object" in errors[0]


def test_load_envelope_validates_content(tmp_path: Path):
    envelope = _envelope()
    del envelope["dimensions"]["review_burden"]
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    _parsed, errors = load_envelope(path)
    assert any("dimension 'review_burden' missing" in e for e in errors)


def test_counter_must_be_measured():
    envelope = _envelope()
    _counter(envelope, "wal_bytes_generated")["status"] = "derived"
    assert any("counter 'wal_bytes_generated' must be status 'measured'" in e
               for e in validate_envelope(envelope))


def test_zero_measured_is_still_valid_when_sourced():
    envelope = _envelope()
    _dimension(envelope, "reprocessing")["value"] = 0
    assert validate_envelope(envelope) == []


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda e: e.update({"schema": "x"}), id="schema"),
        pytest.param(lambda e: e["dimensions"].pop("cold_start"), id="dropped-dim"),
        pytest.param(
            lambda e: e["dimensions"]["visual_storage"].update({"value": 0}),
            id="zero-as-unavailable",
        ),
        pytest.param(
            lambda e: e["dimensions"]["wal_write_amplification"].pop("derivation"),
            id="ratio-without-derivation",
        ),
        pytest.param(
            lambda e: e["counters"]["wal_bytes_generated"].update({"window": "ingest"}),
            id="cross-window-ratio",
        ),
        pytest.param(
            lambda e: e["dimensions"]["cold_start"].pop("samples"),
            id="timing-without-samples",
        ),
    ],
)
def test_misleading_forms_each_fail(mutation):
    envelope = _envelope()
    mutation(envelope)
    assert validate_envelope(envelope) != []
