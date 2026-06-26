"""Evaluation runner that writes JSON and Markdown reports."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.benchmark.runner import GOLDEN_MATCH_THRESHOLD, BenchmarkSample, run_benchmark
from app.eval.manifest import EvalManifest, load_manifest


EVAL_REPORT_SCHEMA_VERSION = "marker.eval_report.v1"


def run_eval(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    report_name: str = "eval_report",
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = _report(manifest)
    json_path = output / f"{report_name}.json"
    md_path = output / f"{report_name}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_markdown_report(report), encoding="utf-8")
    return {
        "ok": report["summary"]["passing"],
        "report_json": str(json_path),
        "report_markdown": str(md_path),
        "report": report,
    }


def _report(manifest: EvalManifest) -> dict[str, Any]:
    benchmark_samples = [
        BenchmarkSample(
            sample_id=sample.sample_id,
            reference_text=sample.golden_text,
            hypothesis_text=sample.candidate_text,
            reference_table=sample.golden_table,
            hypothesis_table=sample.candidate_table,
        )
        for sample in manifest.samples
    ]
    benchmark_report = run_benchmark(manifest.name, benchmark_samples)
    scores_by_id = {score.sample_id: score for score in benchmark_report.scores}
    sample_reports: list[dict[str, Any]] = []
    route_checks: list[dict[str, Any]] = []
    for sample in manifest.samples:
        score = scores_by_id[sample.sample_id]
        route = _route_check(sample.routing)
        if route is not None:
            route_checks.append({"sample_id": sample.sample_id, **route})
        sample_reports.append(
            {
                "sample_id": sample.sample_id,
                "score": asdict(score),
                "routing": route,
                "metadata": sample.metadata,
            }
        )
    router_report = _router_report(route_checks)
    passing = benchmark_report.passing and router_report["passing"]
    return {
        "schema_version": EVAL_REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "name": manifest.name,
            "schema_version": manifest.schema_version,
            "metadata": manifest.metadata,
        },
        "summary": {
            "sample_count": benchmark_report.sample_count,
            "mean_combined": benchmark_report.mean_combined,
            "mean_cer": benchmark_report.mean_cer,
            "threshold": GOLDEN_MATCH_THRESHOLD,
            "passing": passing,
            "regressions": benchmark_report.regressions(),
        },
        "router_benchmark": router_report,
        "samples": sample_reports,
    }


def _route_check(routing: dict[str, Any]) -> dict[str, Any] | None:
    expected = routing.get("expected_engine")
    actual = routing.get("actual_engine")
    if expected is None and actual is None:
        return None
    return {
        "expected_engine": expected,
        "actual_engine": actual,
        "passing": bool(expected and actual and expected == actual),
        "details": {key: value for key, value in routing.items() if key not in {"expected_engine", "actual_engine"}},
    }


def _router_report(route_checks: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [
        item["sample_id"]
        for item in route_checks
        if not item["passing"]
    ]
    return {
        "sample_count": len(route_checks),
        "passing": not failures,
        "failures": failures,
        "checks": route_checks,
    }


def _markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"# Eval Report: {report['corpus']['name']}",
        "",
        f"- Samples: {summary['sample_count']}",
        f"- Mean combined: {summary['mean_combined']}",
        f"- Mean CER: {summary['mean_cer']}",
        f"- Threshold: {summary['threshold']}",
        f"- Passing: {summary['passing']}",
        "",
        "## Samples",
        "",
        "| Sample | Combined | CER | Passing |",
        "| --- | ---: | ---: | --- |",
    ]
    threshold = summary["threshold"]
    for item in report["samples"]:
        score = item["score"]
        lines.append(
            f"| {item['sample_id']} | {score['combined']} | {score['cer']} | {score['combined'] >= threshold} |"
        )
    router = report["router_benchmark"]
    lines.extend(
        [
            "",
            "## Router Benchmark",
            "",
            f"- Samples: {router['sample_count']}",
            f"- Passing: {router['passing']}",
            f"- Failures: {', '.join(router['failures']) if router['failures'] else 'none'}",
            "",
        ]
    )
    return "\n".join(lines)
