"""Integration/reproducibility tests against the real committed ledger.

These prove the audit runs from the repository as committed, covers
exactly the 62 governing invariants, regenerates byte-identical reports,
and that corrupting real evidence inputs flips statuses instead of
letting false readiness survive (demos C/e of the PR84A plan).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from readiness.auditor import Auditor
from readiness.gitmeta import GitMeta
from readiness.inventory import EXPECTED_IDS, load_inventory
from readiness.ledger import load_ledger
from readiness.report import build_machine_report, render_machine_report

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "docs/reference/readiness/readiness-ledger.json"
EVIDENCE_RUN = REPO_ROOT / "docs/reference/readiness/pr84a-evidence-run.json"
REPORT_JSON = REPO_ROOT / "docs/reference/readiness/pr84a-readiness-report.json"
REPORT_MD = REPO_ROOT / "docs/reference/readiness/pr84a-readiness-report.md"
CLI = REPO_ROOT / "backend/scripts/readiness_audit.py"


def _audit_real(evidence_run: dict | None = None):
    inventory = load_inventory()
    entries = load_ledger(LEDGER, EXPECTED_IDS)
    evidence = evidence_run if evidence_run is not None else json.loads(
        EVIDENCE_RUN.read_text(encoding="utf-8")
    )
    return Auditor(GitMeta(REPO_ROOT), REPO_ROOT).audit(inventory, entries, evidence)


def test_real_ledger_covers_all_62_governing_invariants() -> None:
    entries = load_ledger(LEDGER, EXPECTED_IDS)
    assert [e.id for e in entries] == list(range(1, 63))


def test_real_audit_derives_complete_report() -> None:
    audit = _audit_real()
    assert not audit.errors
    assert len(audit.invariants) == 62
    counts = audit.counts
    assert sum(counts.values()) == 62
    assert audit.verdict == ("READY" if counts["proven"] == 62 else "NOT_READY")
    for result in audit.invariants:
        if result.derived_status == "proven":
            assert result.environments, f"inv {result.id} proven without environment scope"
        else:
            assert result.gap_type and result.gap_note


def test_proven_claims_always_carry_executable_bindings() -> None:
    entries = load_ledger(LEDGER, EXPECTED_IDS)
    for entry in entries:
        if entry.status_claim == "proven":
            assert entry.bindings, f"inv {entry.id} proven claim has no executable binding"
            assert all(b.kind != "reference" for b in entry.bindings)


def test_evidence_run_covers_every_ledger_binding() -> None:
    entries = load_ledger(LEDGER, EXPECTED_IDS)
    evidence = json.loads(EVIDENCE_RUN.read_text(encoding="utf-8"))
    ledger_keys = {
        f"{entry.id}:{binding.binding_key}" for entry in entries for binding in entry.bindings
    }
    result_keys = {r["binding_key"] for r in evidence["results"]}
    assert ledger_keys == result_keys


def test_machine_report_matches_committed_report() -> None:
    audit = _audit_real()
    committed = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    assert build_machine_report(audit) == committed


def test_regeneration_is_byte_deterministic() -> None:
    first = render_machine_report(_audit_real())
    second = render_machine_report(_audit_real())
    assert first == second
    assert first == REPORT_JSON.read_text(encoding="utf-8")


def test_cli_integrity_mode_accepts_honest_not_ready() -> None:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--mode", "integrity"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NOT_READY" in proc.stdout or "verdict" in proc.stdout


def test_cli_release_gate_rejects_not_ready() -> None:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--mode", "release-gate"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
    )
    audit = _audit_real()
    if audit.verdict != "READY":
        assert proc.returncode == 1
        assert "release-gate" in proc.stdout
    else:  # pragma: no cover - branch taken only at final PR84 closeout
        assert proc.returncode == 0


def test_real_scope_tampering_invalidates_proof() -> None:
    """Demo C: mutate the recorded blob identity of a real proven binding."""
    evidence = json.loads(EVIDENCE_RUN.read_text(encoding="utf-8"))
    target = None
    for result in evidence["results"]:
        if result["binding_key"].startswith("2:") and result["outcome"] == "passed":
            target = result
            break
    assert target is not None
    scope_file = next(iter(target["scope_blobs"]))
    target["scope_blobs"][scope_file] = "0" * 40

    audit = _audit_real(evidence)
    assert audit.invariants[1].derived_status == "no_evidence"
    assert any(f.code == "stale_scope" for f in audit.errors)
    assert any(f.code == "claim_mismatch" and "claims 'proven'" in f.message for f in audit.errors)


def test_real_failed_result_flips_status_and_exposes_dishonesty() -> None:
    """Demo E: a failed direct proof behind a proven claim cannot survive."""
    evidence = json.loads(EVIDENCE_RUN.read_text(encoding="utf-8"))
    target = None
    for result in evidence["results"]:
        if result["binding_key"].startswith("9:") and result["outcome"] == "passed":
            target = result
            break
    assert target is not None
    target["outcome"] = "failed"
    target["detail"] = "fabricated failure"

    audit = _audit_real(evidence)
    assert audit.invariants[8].derived_status == "failed"
    assert audit.verdict == "NOT_READY"
    assert any(f.code == "claim_mismatch" for f in audit.errors)


def test_real_strict_skip_cannot_back_a_proven_claim() -> None:
    """A skipped strict proof must downgrade, never certify."""
    evidence = json.loads(EVIDENCE_RUN.read_text(encoding="utf-8"))
    for result in evidence["results"]:
        if result["binding_key"].startswith("33:"):
            target = result
            break
    else:  # pragma: no cover - guard against ledger renumbering
        raise AssertionError("inv 33 binding missing")
    target["outcome"] = "skipped_env_gated"
    target["detail"] = "PostgreSQL not configured"

    audit = _audit_real(evidence)
    assert audit.invariants[32].derived_status == "no_evidence"
    assert audit.invariants[32].reason == "environment_limited"
    assert any(f.code == "claim_mismatch" for f in audit.errors)


def test_paper_only_proof_is_structurally_impossible() -> None:
    """Demo D: a proven claim with only context docs cannot even parse."""
    from readiness.ledger import LedgerError, parse_ledger

    raw = json.loads(LEDGER.read_text(encoding="utf-8"))
    for entry in raw["invariants"]:
        if entry["id"] == 35:
            entry["bindings"] = []
            entry["context_docs"] = ["docs/reference/truth-kernel.md"]
            break
    try:
        parse_ledger(raw, EXPECTED_IDS)
    except LedgerError as exc:
        assert "cannot rest on context_docs" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("docs-only proven claim parsed successfully")
