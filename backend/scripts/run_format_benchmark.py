"""Run native format benchmark and optional Markitdown comparison.

Usage:
    python backend/scripts/run_format_benchmark.py
    python backend/scripts/run_format_benchmark.py --markitdown-output-dir path/to/outputs
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.benchmark.format_benchmark import compare_native_markitdown_formats
from app.benchmark.format_corpus import (
    load_manual_native_format_outputs,
    load_markitdown_format_outputs,
    manual_format_benchmark_cases,
)


def _report_dict(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None
    payload = asdict(report)
    payload["sample_count"] = report.sample_count
    payload["mean_combined"] = report.mean_combined
    payload["passing"] = report.passing
    payload["regressions"] = report.regressions()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        default=str(BACKEND / "tests" / "fixtures" / "manual_real_docs"),
        help="Directory containing manual native source fixtures and outputs.",
    )
    parser.add_argument(
        "--markitdown-output-dir",
        help=(
            "Optional directory containing Markitdown <sample_id>.md outputs "
            "and optional metadata sidecars."
        ),
    )
    parser.add_argument(
        "--report-path",
        default=str(
            BACKEND
            / "tests"
            / "fixtures"
            / "manual_real_docs"
            / "outputs"
            / "format_benchmark_report.json"
        ),
        help="Where to write the benchmark report JSON.",
    )
    args = parser.parse_args()

    cases = manual_format_benchmark_cases(args.fixture_dir)
    native_outputs = load_manual_native_format_outputs(args.fixture_dir)
    markitdown_outputs = (
        load_markitdown_format_outputs(args.markitdown_output_dir, cases)
        if args.markitdown_output_dir
        else None
    )
    comparison = compare_native_markitdown_formats(
        cases,
        native_outputs,
        markitdown_outputs,
    )
    report = {
        "verdict": comparison.verdict,
        "native_report": _report_dict(comparison.native_report),
        "markitdown_report": _report_dict(comparison.markitdown_report),
    }
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(comparison.verdict, indent=2))
    print(f"report={report_path}")
    return 0 if comparison.verdict.get("native_passes_gate") else 2


if __name__ == "__main__":
    raise SystemExit(main())
