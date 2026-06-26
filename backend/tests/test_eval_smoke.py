"""Smoke tests for deterministic eval harness."""

from __future__ import annotations

import json
from pathlib import Path

from app import cli
from app.eval.manifest import EVAL_MANIFEST_SCHEMA_VERSION, load_manifest
from app.eval.runner import EVAL_REPORT_SCHEMA_VERSION, run_eval


def test_eval_manifest_loads_inline_and_path_samples(tmp_path: Path):
    golden = tmp_path / "golden.md"
    candidate = tmp_path / "candidate.md"
    golden.write_text("Invoice total 100 paid 40", encoding="utf-8")
    candidate.write_text("Invoice total 100 paid 40", encoding="utf-8")
    manifest = _write_manifest(
        tmp_path,
        samples=[
            {
                "sample_id": "path-sample",
                "golden_path": "golden.md",
                "candidate_path": "candidate.md",
            }
        ],
    )

    loaded = load_manifest(manifest)

    assert loaded.name == "smoke"
    assert loaded.samples[0].golden_text == "Invoice total 100 paid 40"
    assert loaded.samples[0].candidate_text == "Invoice total 100 paid 40"


def test_eval_runner_writes_json_and_markdown_reports(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path,
        samples=[
            {
                "sample_id": "text-perfect",
                "golden_text": "Quarter Q1 revenue 100 cost 40.",
                "candidate_text": "Quarter Q1 revenue 100 cost 40.",
                "routing": {"expected_engine": "text_data", "actual_engine": "text_data"},
            },
            {
                "sample_id": "table-perfect",
                "golden_text": "Q1 100 40",
                "candidate_text": "Q1 100 40",
                "golden_table": {"headers": ["Quarter", "Revenue"], "rows": [["Q1", "100"]]},
                "candidate_table": {"headers": ["Quarter", "Revenue"], "rows": [["Q1", "100"]]},
            },
        ],
    )

    result = run_eval(manifest, tmp_path / "reports")

    assert result["ok"] is True
    report_json = Path(result["report_json"])
    report_md = Path(result["report_markdown"])
    assert report_json.is_file()
    assert report_md.is_file()
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["schema_version"] == EVAL_REPORT_SCHEMA_VERSION
    assert report["summary"]["sample_count"] == 2
    assert report["summary"]["mean_combined"] == 1.0
    assert report["router_benchmark"]["passing"] is True
    assert "# Eval Report: smoke" in report_md.read_text(encoding="utf-8")


def test_cli_eval_run_writes_reports_and_prints_json(tmp_path: Path, capsys):
    manifest = _write_manifest(
        tmp_path,
        samples=[
            {
                "sample_id": "cli-sample",
                "golden_text": "alpha 10 beta 20",
                "candidate_text": "alpha 10 beta 20",
            }
        ],
    )
    output_dir = tmp_path / "cli-reports"

    code = cli.main(
        [
            "eval",
            "run",
            "--manifest",
            str(manifest),
            "--output-dir",
            str(output_dir),
            "--report-name",
            "cli_eval",
            "--json",
        ]
    )

    assert code == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert Path(payload["report_json"]).name == "cli_eval.json"
    assert Path(payload["report_markdown"]).name == "cli_eval.md"


def _write_manifest(tmp_path: Path, *, samples: list[dict]) -> Path:
    path = tmp_path / "eval_manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": EVAL_MANIFEST_SCHEMA_VERSION,
                "name": "smoke",
                "samples": samples,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
