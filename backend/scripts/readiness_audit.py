#!/usr/bin/env python
"""PR84A readiness audit CLI.

Modes:
  run-evidence   Execute bound evidence (pytest + measurement artifacts),
                 write the evidence-run artifact, and regenerate reports.
  audit          Derive statuses from the committed evidence run and
                 regenerate both reports in place.
  integrity      Audit without writing; fail on structural violations,
                 stale/dangling proof, claim mismatches, or generated
                 reports drifting from the committed ones. An honest
                 NOT READY verdict passes this mode.
  release-gate   Integrity plus the requirement that every governing
                 invariant is proven (the final PR84 closeout gate).

Usage (repository root):
  python backend/scripts/readiness_audit.py --mode integrity
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from readiness.auditor import Auditor  # noqa: E402
from readiness.gitmeta import GitMeta  # noqa: E402
from readiness.inventory import (  # noqa: E402
    EXPECTED_IDS,
    InventoryError,
    load_inventory,
    masterplan_drift,
)
from readiness.ledger import LedgerError, load_ledger  # noqa: E402
from readiness.report import render_machine_report, render_markdown_report  # noqa: E402
from readiness.runner import EvidenceRunner  # noqa: E402

LEDGER_PATH = REPO_ROOT / "docs/reference/readiness/readiness-ledger.json"
EVIDENCE_RUN_PATH = REPO_ROOT / "docs/reference/readiness/pr84a-evidence-run.json"
REPORT_JSON_PATH = REPO_ROOT / "docs/reference/readiness/pr84a-readiness-report.json"
REPORT_MD_PATH = REPO_ROOT / "docs/reference/readiness/pr84a-readiness-report.md"


def _load_inputs() -> tuple:
    inventory = load_inventory()
    drift = masterplan_drift(inventory)
    if drift:
        raise InventoryError("inventory drifted from master plan: " + "; ".join(drift))
    entries = load_ledger(LEDGER_PATH, EXPECTED_IDS)
    evidence_run = json.loads(EVIDENCE_RUN_PATH.read_text(encoding="utf-8"))
    return inventory, entries, evidence_run


def _audit() -> object:
    inventory, entries, evidence_run = _load_inputs()
    auditor = Auditor(GitMeta(REPO_ROOT), REPO_ROOT)
    return auditor.audit(inventory, entries, evidence_run)


def _write_reports(audit) -> None:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(render_machine_report(audit), encoding="utf-8", newline="\n")
    REPORT_MD_PATH.write_text(render_markdown_report(audit), encoding="utf-8", newline="\n")


def _print_summary(audit) -> None:
    counts = audit.counts
    print(f"verdict: {audit.verdict}")
    print(
        f"proven={counts['proven']} failed={counts['failed']} "
        f"no_evidence={counts['no_evidence']} (of 62)"
    )
    for finding in audit.findings:
        print(f"[{finding.severity}] {finding.code}: {finding.message}")


def mode_run_evidence() -> int:
    inventory = load_inventory()
    entries = load_ledger(LEDGER_PATH, EXPECTED_IDS)
    artifact = EvidenceRunner(REPO_ROOT, entries).run()
    EVIDENCE_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_RUN_PATH.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    audit = _audit()
    _write_reports(audit)
    _print_summary(audit)
    return 0


def mode_audit() -> int:
    audit = _audit()
    _write_reports(audit)
    _print_summary(audit)
    return 0


def mode_integrity(require_ready: bool = False) -> int:
    failed = False
    try:
        audit = _audit()
    except (InventoryError, LedgerError, OSError, json.JSONDecodeError) as exc:
        print(f"[error] input-invalid: {exc}")
        return 1

    for finding in audit.findings:
        if finding.severity == "error":
            print(f"[error] {finding.code}: {finding.message}")
            failed = True
        else:
            print(f"[warning] {finding.code}: {finding.message}")

    for label, path, rendered in (
        ("machine report", REPORT_JSON_PATH, render_machine_report(audit)),
        ("human report", REPORT_MD_PATH, render_markdown_report(audit)),
    ):
        if not path.is_file():
            print(f"[error] report-missing: committed {label} not found at {path}")
            failed = True
        elif path.read_text(encoding="utf-8") != rendered:
            print(
                f"[error] report-drift: committed {label} differs from regenerated "
                "content; regenerate with --mode run-evidence or --mode audit"
            )
            failed = True

    _print_summary(audit)
    if require_ready and not failed:
        if audit.verdict != "READY":
            print("[error] release-gate: verdict is NOT READY; final gate requires READY")
            failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("run-evidence", "audit", "integrity", "release-gate"),
    )
    args = parser.parse_args(argv)
    if args.mode == "run-evidence":
        return mode_run_evidence()
    if args.mode == "audit":
        return mode_audit()
    if args.mode == "integrity":
        return mode_integrity()
    return mode_integrity(require_ready=True)


if __name__ == "__main__":
    raise SystemExit(main())
