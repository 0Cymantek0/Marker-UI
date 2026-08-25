"""PR91 evidence generator: invariant 56 browser-level review-integrity proof.

Executes the two frontend halves of the review-integrity vertical slice and
digests their results into one committed measurement artifact:

* vitest — the dedicated integrity surface suites (IntegrityPage lifecycle
  incl. bookmarked-token staleness, 409 reconciliation, no-false-success,
  conservative failures; RevisionContextCard envelope rendering);
* Playwright — the real-browser suite (``frontend/playwright.config.ts``)
  which boots the REAL backend via ``backend/e2e/launch.py`` (real routes,
  real SQLite, real as-of enforcement; only the conversion render seam
  stubbed) plus the real vite dev server, then drives Chromium through the
  full current -> stale -> rejected -> reconciled -> recovered lifecycle,
  the entry paths, the conservative-failure path, and a legacy smoke.

The artifact records counts plus named scenario outcomes so the readiness
auditor can bind exact expectations instead of trusting prose. Any missing
or non-passing scenario fails the run loudly (non-zero exit).
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
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"
ARTIFACT_PATH = (
    REPO_ROOT / "docs" / "reference" / "measurements" / "pr91-integrity-e2e-evidence.json"
)

ARTIFACT_SCHEMA = "marker.pr91.integrity_e2e_evidence.v1"
VERDICT_PASS = "integrity_surface_browser_proven"
VERDICT_FAIL = "integrity_e2e_evidence_incomplete"

VITEST_COMMAND = [
    "pnpm",
    "exec",
    "vitest",
    "run",
    "src/__tests__/IntegrityPage.test.tsx",
    "src/components/features/integrity/RevisionContextCard.test.tsx",
    "--reporter=junit",
]
PLAYWRIGHT_COMMAND = ["pnpm", "exec", "playwright", "test", "--reporter=json"]

# Named scenarios the auditor binds as explicit expectations. Each value is
# a list of (suite, needle) pairs; every needle must match at least one
# executed test AND every match must have passed — otherwise the scenario
# (and the whole bench) fails.
SCENARIOS: dict[str, list[tuple[str, str]]] = {
    "e2e_current_verified_export": [
        ("playwright", "current path: fresh load shows revision context and exports a verified markdown download"),
    ],
    "e2e_stale_after_load_race_rejected": [
        ("playwright", "stale-after-load race: server rotates the pinned token, export is rejected and reconciled"),
    ],
    "e2e_stale_before_load_bookmark": [
        ("playwright", "stale-before-load: a bookmarked stale token is detected immediately on load"),
    ],
    "e2e_recovery_refresh_retry": [
        ("playwright", "recovery loop repeats: after a verified export, another rotation re-stales the page"),
    ],
    "e2e_no_false_success": [
        ("playwright", "stale-after-load race: server rotates the pinned token, export is rejected and reconciled, never faked"),
        ("vitest", "treats a server response without a verified mode as a failure (no false success)"),
    ],
    "e2e_entry_paths": [
        ("playwright", "picker lists the seeded completed job and picking it deep-links the URL"),
        ("playwright", 'manual "Load by Job ID" form loads the job state'),
        ("playwright", '"Change job" returns to the picker and a re-pick reloads state'),
    ],
    "e2e_failure_conservative": [
        ("playwright", "unreachable backend renders a conservative error; Retry recovers once the API answers"),
    ],
    "e2e_legacy_smoke": [
        ("playwright", "conversion home renders its heading and upload entry"),
        ("playwright", "history lists the seeded completed job"),
        ("playwright", "settings renders"),
    ],
    "vitest_integrity_page": [
        ("vitest", "IntegrityPage.test.tsx"),
    ],
    "vitest_revision_context": [
        ("vitest", "RevisionContextCard.test.tsx"),
    ],
}

PLAYWRIGHT_TIMEOUT_SECONDS = 1500


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


def run_vitest(junit_path: Path) -> dict[str, dict[str, str]]:
    proc = subprocess.run(
        [
            *VITEST_COMMAND,
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
            f"vitest produced no junit report (exit {proc.returncode}): "
            f"{proc.stdout[-2000:]} {proc.stderr[-2000:]}"
        )
    outcomes = _junit_outcomes(junit_path)
    if not outcomes:
        raise RuntimeError("vitest junit report carried no testcases")
    if proc.returncode != 0 and _counts(outcomes)["failed"] == 0:
        raise RuntimeError(f"vitest exited {proc.returncode} without failed cases: {proc.stdout[-2000:]}")
    return outcomes


def _iter_playwright_tests(node: Any, titles: list[str]) -> list[tuple[str, str]]:
    """Recursively collect ("full > title > path", status) from reporter JSON."""
    collected: list[tuple[str, str]] = []
    title = node.get("title") or ""
    child_titles = titles + ([title] if title else [])
    for suite in node.get("suites", []) or []:
        collected.extend(_iter_playwright_tests(suite, child_titles))
    for spec in node.get("specs", []) or []:
        spec_title = spec.get("title") or ""
        status = None
        for test in spec.get("tests", []) or []:
            status = test.get("status")
        full = " > ".join([*child_titles, spec_title])
        collected.append((full, status or "didNotRun"))
    return collected


def _playwright_status_to_outcome(status: str) -> str:
    if status == "expected":
        return "passed"
    if status == "unexpected" or status == "flaky":
        return "failed"
    return "skipped"


def run_playwright() -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    proc = subprocess.run(
        PLAYWRIGHT_COMMAND,
        cwd=FRONTEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
        check=False,
        timeout=PLAYWRIGHT_TIMEOUT_SECONDS,
    )
    stdout = proc.stdout or ""
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            f"playwright produced no JSON report (exit {proc.returncode}): "
            f"{stdout[-2000:]} {proc.stderr[-2000:]}"
        )
    try:
        report = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"playwright stdout is not parsable JSON (exit {proc.returncode}): {exc}; "
            f"tail: {stdout[-2000:]}"
        ) from exc

    outcomes: dict[str, dict[str, str]] = {}
    for suite in report.get("suites", []) or []:
        for full, status in _iter_playwright_tests(suite, []):
            outcomes[full] = {"outcome": _playwright_status_to_outcome(status)}
    if not outcomes:
        raise RuntimeError(f"playwright JSON report carried no tests (exit {proc.returncode})")
    if proc.returncode != 0:
        raise RuntimeError(
            f"playwright exited {proc.returncode}: {stdout[-2000:]} {proc.stderr[-2000:]}"
        )
    stats = report.get("stats", {}) or {}
    return outcomes, {
        "reported_expected": stats.get("expected", 0),
        "reported_unexpected": stats.get("unexpected", 0),
        "reported_flaky": stats.get("flaky", 0),
        "reported_skipped": stats.get("skipped", 0),
        "reported_did_not_run": stats.get("didNotRun", 0),
    }


def _scenario_outcomes(
    scenarios: dict[str, list[tuple[str, str]]],
    vitest: dict[str, dict[str, str]],
    playwright: dict[str, dict[str, str]],
) -> dict[str, str]:
    pools = {"vitest": vitest, "playwright": playwright}
    results: dict[str, str] = {}
    for label, needles in scenarios.items():
        matched_any = False
        states: set[str] = set()
        for suite, needle in needles:
            pool = pools[suite]
            matches = [key for key in pool if needle in key]
            if not matches:
                results[label] = f"missing {suite}:{needle}"
                matched_any = False
                break
            matched_any = True
            states.update(pool[key]["outcome"] for key in matches)
        if not matched_any:
            continue
        if "failed" in states:
            results[label] = "failed"
        elif states == {"passed"}:
            results[label] = "passed"
        else:
            results[label] = "skipped"
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="pr91-integrity-") as tmp:
        vitest_outcomes = run_vitest(Path(tmp) / "vitest-junit.xml")
        playwright_outcomes, playwright_stats = run_playwright()

    vitest_counts = _counts(vitest_outcomes)
    e2e_counts = _counts(playwright_outcomes)
    scenarios = _scenario_outcomes(SCENARIOS, vitest_outcomes, playwright_outcomes)

    all_green = (
        vitest_counts["failed"] == 0
        and vitest_counts["skipped"] == 0
        and e2e_counts["failed"] == 0
        and e2e_counts["skipped"] == 0
        and e2e_counts["total"] == playwright_stats["reported_expected"]
        and all(state == "passed" for state in scenarios.values())
    )

    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commands": {
            "vitest": {
                "argv": VITEST_COMMAND,
                "cwd": "frontend",
            },
            "playwright": {
                "argv": PLAYWRIGHT_COMMAND,
                "cwd": "frontend",
                "config": "frontend/playwright.config.ts (boots backend/e2e/launch.py + vite dev; chromium)",
            },
        },
        "vitest": vitest_counts,
        "e2e": e2e_counts,
        "e2e_playwright_stats": playwright_stats,
        "scenarios": scenarios,
        "verdict": VERDICT_PASS if all_green else VERDICT_FAIL,
    }

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {ARTIFACT_PATH}")
    print(json.dumps(artifact, indent=2))
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
