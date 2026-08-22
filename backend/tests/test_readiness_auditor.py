"""Status/verdict derivation and adversarial evidence-integrity tests.

The trust boundary must be real: stale blobs, corrupted artifacts,
docs-only proof, strict skips, failures, and claim mismatches can never
survive as `proven`, and the verdict is always derived, never trusted.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from readiness import EVIDENCE_RUN_SCHEMA, LEDGER_SCHEMA
from readiness.auditor import Auditor
from readiness.gitmeta import StaticResolver
from readiness.inventory import EXPECTED_IDS, load_inventory
from readiness.ledger import LedgerError, parse_ledger, parse_binding
from readiness.runner import _junit_node_outcomes
from pathlib import Path

REAL_INVENTORY = load_inventory()
SHA = "f" * 40
OTHER_SHA = "e" * 40


def _test_binding(nodes=("tests/test_dummy.py::test_ok",), coverage="full", environment="sqlite-dev"):
    return {
        "kind": "test",
        "coverage": coverage,
        "environment": environment,
        "rationale": "covers the invariant end to end",
        "nodes": list(nodes),
        "scope_files": [f"backend/{nodes[0].split('::', 1)[0]}"],
    }


def _entry(claim, bindings, gap_type=None, gap_note=None, context=()):
    entry = {
        "status_claim": claim,
        "bindings": list(bindings),
        "context_docs": list(context),
    }
    if claim != "proven":
        entry["gap_type"] = gap_type or "B"
        entry["gap_note"] = gap_note or "no executable proof yet"
    return entry


def make_ledger(overrides: dict[int, dict] | None = None) -> dict:
    overrides = overrides or {}
    return {
        "schema": LEDGER_SCHEMA,
        "invariants": [
            {"id": inv_id, **_entry("proven", [_test_binding()]), **overrides.get(inv_id, {})}
            for inv_id in range(1, 63)
        ],
    }


def make_result(key, outcome, scope_blobs=None, **extra):
    result = {
        "binding_key": key,
        "kind": extra.get("kind", "test"),
        "outcome": outcome,
        "scope_blobs": scope_blobs if scope_blobs is not None else {"backend/tests/test_dummy.py": SHA},
        "nodes": ["tests/test_dummy.py::test_ok"],
    }
    result.update(extra)
    return result


def make_evidence_run(results) -> dict:
    return {
        "schema": EVIDENCE_RUN_SCHEMA,
        "generated_at": "2026-08-22T00:00:00+00:00",
        "git_head": SHA,
        "runner": {"python": "3.11.9", "platform": "test"},
        "results": list(results),
}


def audit_with(ledger_overrides, results, shas=None, tracked=None):
    ledger = parse_ledger(make_ledger(ledger_overrides), EXPECTED_IDS)
    evidence = make_evidence_run(results)
    resolver = StaticResolver(
        shas if shas is not None else {"backend/tests/test_dummy.py": SHA},
        tracked=tracked if tracked is not None else {"backend/tests/test_dummy.py"},
    )
    return Auditor(resolver, Path(".")).audit(REAL_INVENTORY, ledger, evidence), evidence


DEFAULT_KEY = "1:test:tests/test_dummy.py::test_ok"


def _results_for_all(pattern_result):
    return [pattern_result(f"{i}:test:tests/test_dummy.py::test_ok") for i in range(1, 63)]


# ---------------------------------------------------------------------------
# verdict derivation
# ---------------------------------------------------------------------------


def test_all_proven_derives_ready() -> None:
    audit, _ = audit_with({}, _results_for_all(lambda k: make_result(k, "passed")))
    assert audit.verdict == "READY"
    assert audit.counts == {"proven": 62, "failed": 0, "no_evidence": 0}
    assert not audit.errors


def test_any_failed_derives_not_ready() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results[4] = make_result("5:test:tests/test_dummy.py::test_ok", "failed")
    audit, _ = audit_with({}, results)
    assert audit.verdict == "NOT_READY"
    assert audit.counts["failed"] == 1
    assert audit.invariants[4].derived_status == "failed"


def test_any_no_evidence_derives_not_ready() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results.remove(make_result(f"62:test:tests/test_dummy.py::test_ok", "passed"))
    results.append(make_result(f"62:test:tests/test_dummy.py::test_ok", "skipped_env_gated"))
    audit, _ = audit_with({}, results)
    assert audit.verdict == "NOT_READY"
    assert audit.counts["no_evidence"] == 1


def test_verdict_is_derived_not_trusted_from_claim() -> None:
    """Claiming proven everywhere cannot rescue a failing evidence run."""
    results = _results_for_all(lambda k: make_result(k, "failed"))
    audit, _ = audit_with({}, results)
    assert audit.verdict == "NOT_READY"
    assert audit.counts == {"proven": 0, "failed": 62, "no_evidence": 0}


# ---------------------------------------------------------------------------
# adversarial evidence integrity
# ---------------------------------------------------------------------------


def test_stale_scope_blob_cannot_support_proven() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    audit, _ = audit_with(
        {}, results, shas={"backend/tests/test_dummy.py": OTHER_SHA}
    )
    assert audit.verdict == "NOT_READY"
    assert audit.counts["proven"] == 0
    assert any(f.code == "stale_scope" for f in audit.errors)
    assert all(r.derived_status == "no_evidence" for r in audit.invariants)


def test_untracked_scope_file_is_rejected() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    audit, _ = audit_with({}, results, tracked=set())
    assert any(f.code == "untracked_scope" for f in audit.errors)
    assert audit.counts["proven"] == 0


def test_missing_scope_blob_is_dangling() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    audit, _ = audit_with({}, results, shas={})
    assert audit.counts["proven"] == 0


def test_dangling_binding_without_executed_result_is_an_error() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    del results[9]
    audit, _ = audit_with({}, results)
    assert any(f.code == "dangling_binding" for f in audit.errors)


def test_orphan_result_is_an_error() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results.append(make_result("99:test:tests/test_dummy.py::test_ok", "passed"))
    audit, _ = audit_with({}, results)
    assert any(f.code == "orphan_result" for f in audit.errors)


def test_duplicate_result_key_is_an_error() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results.append(dict(results[0]))
    audit, _ = audit_with({}, results)
    assert any(f.code == "evidence_run_duplicate" for f in audit.errors)


def test_malformed_outcome_is_an_error() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results[0] = make_result(DEFAULT_KEY, "mostly-fine")
    audit, _ = audit_with({}, results)
    assert any(f.code == "evidence_run_outcome" for f in audit.errors)


def test_failed_result_backing_proven_claim_is_rejected() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results[0] = make_result(DEFAULT_KEY, "failed")
    audit, _ = audit_with({}, results)
    assert audit.invariants[0].derived_status == "failed"
    assert any(f.code == "claim_mismatch" for f in audit.errors)


def test_strict_skip_backing_proven_claim_is_rejected() -> None:
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results[0] = make_result(DEFAULT_KEY, "skipped_env_gated", detail="PostgreSQL not configured")
    audit, _ = audit_with({}, results)
    assert audit.invariants[0].derived_status == "no_evidence"
    assert audit.invariants[0].reason == "environment_limited"
    assert any(f.code == "claim_mismatch" for f in audit.errors)


def test_partial_coverage_pass_cannot_prove() -> None:
    overrides = {7: _entry("no_evidence", [_test_binding(coverage="partial")], gap_type="B")}
    results = _results_for_all(lambda k: make_result(k, "passed"))
    audit, _ = audit_with(overrides, results)
    assert audit.invariants[6].derived_status == "no_evidence"
    assert audit.invariants[6].reason == "partial_coverage_only"
    assert not audit.errors


def test_docs_only_context_cannot_prove() -> None:
    overrides = {12: _entry("no_evidence", [], context=["docs/reference/foo.md"], gap_type="B")}
    results = _results_for_all(lambda k: make_result(k, "passed"))
    audit, _ = audit_with(overrides, results)
    assert audit.invariants[11].derived_status == "no_evidence"
    assert audit.invariants[11].reason == "docs_only_no_executable_binding"


def test_proven_claim_without_bindings_is_rejected_by_schema() -> None:
    overrides = {3: _entry("proven", [])}
    with pytest.raises(LedgerError, match="cannot rest on context_docs"):
        parse_ledger(make_ledger(overrides), EXPECTED_IDS)


def test_measurement_digest_mismatch_invalidates_proof(tmp_path: Path) -> None:
    artifact = tmp_path / "m.json"
    artifact.write_text(json.dumps({"decision": {"outcome": "narrow_rerank_only"}}), encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    binding = {
        "kind": "measurement",
        "coverage": "full",
        "environment": "offline-artifact",
        "rationale": "decision outcome proves selectivity",
        "scope_files": ["docs/reference/measurements/m.json"],
        "artifact": "docs/reference/measurements/m.json",
        "artifact_expect": {"decision.outcome": "narrow_rerank_only"},
    }
    overrides = {58: _entry("proven", [binding])}
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results.append(
        make_result(
            "58:measurement:docs/reference/measurements/m.json",
            "passed",
            kind="measurement",
            scope_blobs={"docs/reference/measurements/m.json": SHA},
            artifact="docs/reference/measurements/m.json",
            artifact_sha256=digest,
        )
    )
    repo_root = tmp_path
    (repo_root / "docs/reference/measurements").mkdir(parents=True)
    (repo_root / "docs/reference/measurements/m.json").write_text(
        json.dumps({"decision": {"outcome": "promote_everything"}}), encoding="utf-8"
    )
    resolver = StaticResolver(
        {
            "backend/tests/test_dummy.py": SHA,
            "docs/reference/measurements/m.json": SHA,
        }
    )
    ledger = parse_ledger(make_ledger(overrides), EXPECTED_IDS)
    audit = Auditor(resolver, repo_root).audit(REAL_INVENTORY, ledger, make_evidence_run(results))
    assert audit.invariants[57].derived_status == "no_evidence"
    assert any(f.code == "artifact_digest_mismatch" for f in audit.errors)


def test_measurement_expectation_rechecked_at_audit_time(tmp_path: Path) -> None:
    """A digest-forged artifact that no longer carries the proving value fails."""
    import hashlib

    repo_root = tmp_path
    artifact_dir = repo_root / "docs/reference/measurements"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "m.json"
    artifact.write_text(json.dumps({"decision": {"outcome": "wrong"}}), encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    binding = {
        "kind": "measurement",
        "coverage": "full",
        "environment": "offline-artifact",
        "rationale": "r",
        "scope_files": ["docs/reference/measurements/m.json"],
        "artifact": "docs/reference/measurements/m.json",
        "artifact_expect": {"decision.outcome": "right"},
    }
    overrides = {58: _entry("proven", [binding])}
    results = _results_for_all(lambda k: make_result(k, "passed"))
    results.append(
        make_result(
            "58:measurement:docs/reference/measurements/m.json",
            "passed",
            kind="measurement",
            scope_blobs={"docs/reference/measurements/m.json": SHA},
            artifact="docs/reference/measurements/m.json",
            artifact_sha256=digest,
        )
    )
    resolver = StaticResolver(
        {"backend/tests/test_dummy.py": SHA, "docs/reference/measurements/m.json": SHA}
    )
    ledger = parse_ledger(make_ledger(overrides), EXPECTED_IDS)
    audit = Auditor(resolver, repo_root).audit(REAL_INVENTORY, ledger, make_evidence_run(results))
    assert audit.invariants[57].derived_status == "no_evidence"
    assert any(f.code == "artifact_expectation_mismatch" for f in audit.errors)


def test_underclaim_is_also_a_mismatch() -> None:
    overrides = {9: _entry("no_evidence", [_test_binding()], gap_type="B")}
    results = _results_for_all(lambda k: make_result(k, "passed"))
    audit, _ = audit_with(overrides, results)
    assert any(
        f.code == "claim_mismatch" and "claims 'no_evidence'" in f.message
        for f in audit.errors
    )


def test_evidence_run_schema_mismatch_is_an_error() -> None:
    ledger = parse_ledger(make_ledger({}), EXPECTED_IDS)
    evidence = make_evidence_run(_results_for_all(lambda k: make_result(k, "passed")))
    evidence["schema"] = "marker.pr84a_evidence_run.v0"
    audit = Auditor(StaticResolver({"backend/tests/test_dummy.py": SHA}), Path(".")).audit(
        REAL_INVENTORY, ledger, evidence
    )
    assert any(f.code == "evidence_run_schema" for f in audit.errors)


# ---------------------------------------------------------------------------
# ledger schema rules
# ---------------------------------------------------------------------------


def test_ledger_missing_invariant_is_rejected() -> None:
    raw = make_ledger({})
    raw["invariants"] = [e for e in raw["invariants"] if e["id"] != 41]
    with pytest.raises(LedgerError, match="missing governing invariants: \\[41\\]"):
        parse_ledger(raw, EXPECTED_IDS)


def test_ledger_duplicate_invariant_is_rejected() -> None:
    raw = make_ledger({})
    raw["invariants"].append(dict(raw["invariants"][0]))
    with pytest.raises(LedgerError, match="duplicate ledger invariant id"):
        parse_ledger(raw, EXPECTED_IDS)


def test_ledger_unknown_invariant_is_rejected() -> None:
    raw = make_ledger({})
    raw["invariants"].append({"id": 63, **_entry("no_evidence", [])})
    with pytest.raises(LedgerError, match="non-governing invariant ids"):
        parse_ledger(raw, EXPECTED_IDS)


def test_ledger_malformed_status_is_rejected() -> None:
    overrides = {1: {"status_claim": "probably-fine"}}
    with pytest.raises(LedgerError, match="status_claim"):
        parse_ledger(make_ledger(overrides), EXPECTED_IDS)


def test_binding_without_environment_is_rejected() -> None:
    raw_binding = _test_binding()
    del raw_binding["environment"]
    with pytest.raises(LedgerError, match="environment"):
        parse_binding(raw_binding, 1, 1)


def test_binding_node_must_be_in_scope_files() -> None:
    raw_binding = {
        "kind": "test",
        "coverage": "full",
        "environment": "sqlite-dev",
        "rationale": "r",
        "nodes": ["tests/other.py::test_x"],
        "scope_files": ["backend/tests/irrelevant.py"],
    }
    with pytest.raises(LedgerError, match="missing from scope_files"):
        parse_binding(raw_binding, 1, 1)


def test_measurement_binding_requires_expectations() -> None:
    raw_binding = {
        "kind": "measurement",
        "coverage": "full",
        "environment": "offline-artifact",
        "rationale": "r",
        "scope_files": ["docs/reference/measurements/m.json"],
        "artifact": "docs/reference/measurements/m.json",
    }
    with pytest.raises(LedgerError, match="artifact_expect"):
        parse_binding(raw_binding, 1, 1)


def test_non_proven_entry_requires_gap_classification() -> None:
    overrides = {2: {"status_claim": "no_evidence", "bindings": [], "gap_type": None}}
    with pytest.raises(LedgerError, match="gap_type"):
        parse_ledger(make_ledger(overrides), EXPECTED_IDS)


def test_proven_entry_with_gap_fields_is_rejected() -> None:
    overrides = {2: {"gap_type": "B", "gap_note": "contradictory"}}
    with pytest.raises(LedgerError, match="declares a gap"):
        parse_ledger(make_ledger(overrides), EXPECTED_IDS)


# ---------------------------------------------------------------------------
# runner junit parsing
# ---------------------------------------------------------------------------


def test_junit_outcomes_map_to_node_ids() -> None:
    junit = textwrap.dedent(
        """\
        <testsuite name="suite">
          <testcase classname="tests.test_a" name="test_plain" file="tests/test_a.py" line="1"/>
          <testcase classname="tests.test_a.TestCls" name="test_cls[param]" file="tests/test_a.py" line="2"/>
          <testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py" line="3">
            <skipped type="SkipSchema" message="PostgreSQL not configured"/>
          </testcase>
          <testcase classname="tests.test_b" name="test_fail" file="tests/test_b.py" line="4">
            <failure type="AssertionError">boom</failure>
          </testcase>
        </testsuite>
        """
    )
    path = Path("junit-fixture.xml")
    path.write_text(junit, encoding="utf-8")
    try:
        outcomes = _junit_node_outcomes(path)
    finally:
        path.unlink()
    assert outcomes["tests/test_a.py::test_plain"]["outcome"] == "passed"
    assert outcomes["tests/test_a.py::TestCls::test_cls[param]"]["outcome"] == "passed"
    assert outcomes["tests/test_a.py::test_skip"]["outcome"] == "skipped_env_gated"
    assert "PostgreSQL not configured" in outcomes["tests/test_a.py::test_skip"]["detail"]
    assert outcomes["tests/test_b.py::test_fail"]["outcome"] == "failed"


def test_binding_outcome_aggregation_rules(tmp_path: Path) -> None:
    from readiness.runner import EvidenceRunner

    runner = EvidenceRunner.__new__(EvidenceRunner)
    entry_ok = type("E", (), {"id": 1, "bindings": ()})()
    binding = parse_binding(_test_binding(nodes=("tests/test_a.py::test_x",)), 1, 1)
    node_outcomes = {
        "tests/test_a.py::test_x": {"outcome": "passed", "detail": ""},
        "tests/test_a.py::test_y": {"outcome": "failed", "detail": "boom"},
    }
    binding = parse_binding(
        _test_binding(nodes=("tests/test_a.py::test_x", "tests/test_a.py::test_y")), 1, 1
    )
    result = runner._result_for_binding(entry_ok, binding, node_outcomes, {})
    assert result["outcome"] == "failed"
    node_outcomes["tests/test_a.py::test_y"] = {"outcome": "skipped_env_gated", "detail": "no pg"}
    result = runner._result_for_binding(entry_ok, binding, node_outcomes, {})
    assert result["outcome"] == "skipped_env_gated"
