"""Run deterministic Marker evaluation manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.eval.runner import run_eval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Marker eval manifest")
    parser.add_argument("--manifest", required=True, help="Path to eval manifest JSON")
    parser.add_argument("--output-dir", required=True, help="Directory for JSON/Markdown reports")
    parser.add_argument("--report-name", default="eval_report", help="Report filename stem")
    args = parser.parse_args(argv)
    result = run_eval(args.manifest, args.output_dir, report_name=args.report_name)
    print(json.dumps({k: v for k, v in result.items() if k != "report"}, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
