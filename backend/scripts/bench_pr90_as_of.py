"""PR90 evidence generator: invariant 56 operational as-of truth.

Runs the two executable halves of the invariant and digests their results
into one committed measurement artifact:

* backend — pytest over the operational as-of contract suite (convert
  status/history/export boundary incl. the TOCTOU regenerate race and
  forged/cross-job tokens) plus the extraction-review stale-rejection
  suite (the pre-existing authority for review commits);
* frontend — vitest (JUnit output) over the typed-boundary, component,
  history, integration, and dedicated integrity-surface suites that prove
  the UI exposes the as-of state, renders stale transitions accessibly,
  and retries with the refreshed token.

The artifact records counts plus named end-to-end scenario outcomes so the
readiness auditor can bind exact expectations instead of trusting prose.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
ARTIFACT_PATH = REPO_ROOT / "docs" / "reference" / "measurements" / "pr90-as-of-evidence.json"

ARTIFACT_SCHEMA = "marker.pr90.as_of_evidence.v1"

BACKEND_NODES = [
    "tests/test_as_of_contract.py",
    "tests/test_extraction_review.py",
]

FRONTEND_SPEC_FILES = [
    "src/__tests__/api.test.ts",
    "src/components/features/as-of/AsOfStatus.test.tsx",
    "src/__tests__/OutputViewer.test.tsx",
    "src/__tests__/HistoryPage.test.tsx",
    "src/__tests__/ConvertPageIntegration.test.tsx",
    "src/__tests__/IntegrityPage.test.tsx",
    "src/components/features/integrity/RevisionContextCard.test.tsx",
]

# Named end-to-end scenarios the auditor binds as explicit expectations:
# (suite, substring matched against "<describe> > <test name>").
SCENARIOS = {
    "backend_toctou_stale_download": ("backend", "download_rejects_stale_token_after_real_state_change"),
    "backend_forged_token": ("backend", "forged_token_fails_closed"),
    "backend_cross_job_replay": ("backend", "cross_job_token_replay_fails_closed"),
    "backend_extraction_stale_review": ("backend", "test_stale_review_after_publication_change_is_rejected"),
    "frontend_stale_retry": ("frontend", "surfaces a stale rejection from download and refreshes state"),
    "frontend_history_stale_retry": ("frontend", "flags stale, patches as_of from the 409 payload"),
    "integrity_page_stale_reconcile": (
        "frontend",
        "reconciles a 409 stale rejection visibly without any false success",
    ),
    "revision_context_card": ("frontend", "RevisionContextCard.test.tsx"),
}


def _junit_outcomes(junit_path: Path) -> dict[str, dict[str, str]]:
    """Map "<classname or describe> > <name>" -> outcome from JUnit XML."""
    root = ET.parse(junit_path).getroot()
    outcomes: dict[str, dict[str, str]] = {}
    for testcase in root.iter("testcase"):
        name = testcase.get("name", "")
        classname = testcase.get("classname", "")
        child_tags = {child.tag for child in testcase}
        if "failure" in child_tags or "error" in child_tags:
            outcome = "failed"
        elif "skipped" in child_tags:
            outcome = "skipped"
        else:
            outcome = "passed"
        key = f"{classname} > {name}" if classname else name
        outcomes[key] = {"outcome": outcome}
    return outcomes


def _counts(outcomes: dict[str, dict[str, str]]) -> dict[str, int]:
    passed = sum(1 for entry in outcomes.values() if entry["outcome"] == "passed")
    failed = sum(1 for entry in outcomes.values() if entry["outcome"] == "failed")
    skipped = sum(1 for entry in outcomes.values() if entry["outcome"] == "skipped")
    return {
        "total": len(outcomes),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }


def run_backend(junit_path: Path) -> dict[str, dict[str, str]]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *BACKEND_NODES,
            f"--junitxml={junit_path}",
            "-o",
            "junit_family=xunit1",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--tb=short",
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if not junit_path.is_file():
        raise RuntimeError(
            f"backend pytest produced no junit report (exit {proc.returncode}): "
            f"{proc.stdout[-2000:]} {proc.stderr[-2000:]}"
        )
    outcomes = _junit_outcomes(junit_path)
    if not outcomes:
        raise RuntimeError("backend junit report carried no testcases")
    if proc.returncode != 0 and _counts(outcomes)["failed"] == 0:
        raise RuntimeError(f"backend pytest exited {proc.returncode} without failed cases: {proc.stdout[-2000:]}")
    return outcomes


def run_frontend(junit_path: Path) -> dict[str, dict[str, str]]:
    proc = subprocess.run(
        [
            "pnpm",
            "exec",
            "vitest",
            "run",
            *FRONTEND_SPEC_FILES,
            "--reporter=junit",
            f"--outputFile={junit_path}",
        ],
        cwd=FRONTEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
        check=False,
    )
    if not junit_path.is_file():
        raise RuntimeError(
            f"frontend vitest produced no junit report (exit {proc.returncode}): "
            f"{proc.stdout[-2000:]} {proc.stderr[-2000:]}"
        )
    outcomes = _junit_outcomes(junit_path)
    if not outcomes:
        raise RuntimeError("frontend junit report carried no testcases")
    return outcomes


def _scenario_outcomes(
    scenarios: dict[str, tuple[str, str]],
    backend: dict[str, dict[str, str]],
    frontend: dict[str, dict[str, str]],
) -> dict[str, str]:
    results: dict[str, str] = {}
    for label, (suite, needle) in scenarios.items():
        pool = backend if suite == "backend" else frontend
        matches = [key for key in pool if needle in key]
        if not matches:
            results[label] = "missing"
            continue
        outcomes = {pool[key]["outcome"] for key in matches}
        if outcomes == {"passed"}:
            results[label] = "passed"
        elif "failed" in outcomes:
            results[label] = "failed"
        else:
            results[label] = "skipped"
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="pr90-asof-") as tmp:
        backend_outcomes = run_backend(Path(tmp) / "backend-junit.xml")
        frontend_outcomes = run_frontend(Path(tmp) / "frontend-junit.xml")

    backend_counts = _counts(backend_outcomes)
    frontend_counts = _counts(frontend_outcomes)
    scenarios = _scenario_outcomes(SCENARIOS, backend_outcomes, frontend_outcomes)

    all_green = (
        backend_counts["failed"] == 0
        and backend_counts["skipped"] == 0
        and frontend_counts["failed"] == 0
        and frontend_counts["skipped"] == 0
        and all(state == "passed" for state in scenarios.values())
    )

    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": backend_counts,
        "frontend": frontend_counts,
        "scenarios": scenarios,
        "verdict": "as_of_operational_contract_proven" if all_green else "as_of_evidence_incomplete",
    }

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {ARTIFACT_PATH}")
    print(json.dumps(artifact, indent=2))
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
