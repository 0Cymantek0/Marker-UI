"""Run Phase 3 Marker-vs-LiteParse PDF benchmark.

Usage:
    python backend/scripts/run_phase3_pdf_benchmark.py

The script generates the five required PDF classes, runs both real engines, and
feeds outputs into ``compare_marker_liteparse_pdfs``. Generated PDFs and reports
default to the gitignored ``backend/tests/fixtures/phase3_pdf_benchmark`` path.
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

from app.benchmark.phase3_pdf_corpus import (
    generate_mixed_routing_pdf_case,
    generate_phase3_pdf_cases,
    generate_real_mixed_routing_pdf_case,
    load_manual_real_table_heavy_pdf_cases,
)
from app.benchmark.runner import (
    PdfBenchmarkCase,
    PdfEngineOutput,
    compare_marker_liteparse_pdfs,
    compare_mixed_pdf_routing,
)
from app.conversion.converters.liteparse_pdf import LiteParsePdfConverter
from app.conversion.converters.marker_pdf import MarkerPdfConverter
from app.conversion.probe import probe_pdf
from app.services.conversion_service import ConversionService
from app.services.marker_service import MarkerService

MAX_OUTPUT_PREVIEW_CHARS = 500


def _table_list(output: PdfEngineOutput) -> list[Any]:
    if isinstance(output.table, list):
        return output.table
    if output.table is not None:
        return [output.table]
    metadata_tables = output.metadata.get("tables")
    if isinstance(metadata_tables, list):
        return metadata_tables
    metadata_table = output.metadata.get("table")
    return [metadata_table] if metadata_table is not None else []


def _preview_text(text: str, limit: int = MAX_OUTPUT_PREVIEW_CHARS) -> str:
    preview = " ".join((text or "").split())
    if len(preview) <= limit:
        return preview
    return f"{preview[:limit].rstrip()}..."


def _write_json_table_artifact(
    *,
    case: PdfBenchmarkCase,
    output_dir: Path,
    json_text: str,
) -> str:
    artifact_dir = output_dir / "engine_json" / "marker_pdf"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / f"{case.sample_id}.json"
    json_path.write_text(json_text or "", encoding="utf-8")
    return str(json_path.relative_to(output_dir))


def _merge_marker_json_table_evidence(
    *,
    markdown_output: PdfEngineOutput,
    json_output: PdfEngineOutput,
    case: PdfBenchmarkCase,
    output_dir: Path,
) -> PdfEngineOutput:
    json_tables = _table_list(json_output)
    if not json_tables:
        return markdown_output

    json_artifact_path = _write_json_table_artifact(
        case=case,
        output_dir=output_dir,
        json_text=json_output.text,
    )
    metadata = dict(markdown_output.metadata)
    metadata["table"] = json_tables[0]
    metadata["tables"] = json_tables
    metadata["table_evidence"] = {
        "source": "marker_json_table_evidence",
        "table_count": len(json_tables),
        "sources": sorted({str(table.get("source")) for table in json_tables if isinstance(table, dict)}),
    }
    metadata["marker_json_table_evidence"] = {
        "source": "marker_json_renderer",
        "table_count": len(json_tables),
        "artifact_path": json_artifact_path,
    }
    return PdfEngineOutput(
        text=markdown_output.text,
        table=json_tables,
        metadata=metadata,
    )


def _record_output(
    observations: dict[str, dict[str, dict[str, Any]]],
    engine_name: str,
    case: PdfBenchmarkCase,
    output: PdfEngineOutput,
    output_dir: Path,
) -> None:
    sample_key = f"{case.document_class}:{case.sample_id}"
    table = output.table[0] if isinstance(output.table, list) and output.table else output.table
    table = table if isinstance(table, dict) else None
    artifact_dir = output_dir / "engine_markdown" / engine_name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = artifact_dir / f"{case.sample_id}.md"
    markdown_path.write_text(output.text or "", encoding="utf-8")
    observations.setdefault(engine_name, {})[sample_key] = {
        "document_class": case.document_class,
        "text_len": len(output.text or ""),
        "text_preview": _preview_text(output.text),
        "table_present": output.table is not None,
        "table_count": len(output.metadata.get("tables") or []),
        "table_evidence_source": (output.metadata.get("table_evidence") or {}).get("source"),
        "json_table_count": (output.metadata.get("marker_json_table_evidence") or {}).get("table_count", 0),
        "json_table_artifact_path": (output.metadata.get("marker_json_table_evidence") or {}).get("artifact_path"),
        "table_rows": len(table.get("rows") or []) if table else 0,
        "table_columns": len(table.get("headers") or []) if table else 0,
        "markdown_path": str(markdown_path.relative_to(output_dir)),
        "metadata_keys": sorted(output.metadata.keys()),
    }


def _case_options(case: PdfBenchmarkCase, base: dict[str, Any]) -> dict[str, Any]:
    options = dict(base)
    conversion_options = case.metadata.get("conversion_options")
    if isinstance(conversion_options, dict):
        options.update(conversion_options)
    return options


def _liteparse_engine(
    converter: LiteParsePdfConverter,
    observations: dict[str, dict[str, dict[str, Any]]],
    output_dir: Path,
):
    def run(case: PdfBenchmarkCase) -> PdfEngineOutput:
        result = converter.convert(
            str(case.pdf_path),
            _case_options(case, {"liteparse_timeout": 120}),
        )
        output = PdfEngineOutput(
            text=result.text,
            table=result.metadata.get("tables") or result.metadata.get("table"),
            metadata=result.metadata,
        )
        _record_output(observations, "liteparse_pdf", case, output, output_dir)
        return output

    return run


def _marker_engine(
    converter: MarkerPdfConverter,
    observations: dict[str, dict[str, dict[str, Any]]],
    output_dir: Path,
    *,
    collect_json_table_evidence: bool = False,
):
    def run(case: PdfBenchmarkCase) -> PdfEngineOutput:
        base_options = {
            "output_format": "markdown",
            "disable_multiprocessing": True,
            "image_handling_mode": "extraction",
        }
        result = converter.convert(
            str(case.pdf_path),
            _case_options(case, base_options),
        )
        output = PdfEngineOutput(
            text=result.text,
            table=result.metadata.get("tables") or result.metadata.get("table"),
            metadata=result.metadata,
        )
        if collect_json_table_evidence:
            json_result = converter.convert(
                str(case.pdf_path),
                _case_options(case, {**base_options, "output_format": "json"}),
            )
            output = _merge_marker_json_table_evidence(
                markdown_output=output,
                json_output=PdfEngineOutput(
                    text=json_result.text,
                    table=json_result.metadata.get("tables") or json_result.metadata.get("table"),
                    metadata=json_result.metadata,
                ),
                case=case,
                output_dir=output_dir,
            )
        _record_output(observations, "marker_pdf", case, output, output_dir)
        return output

    return run


def _score_dict(report: Any) -> dict[str, Any]:
    payload = asdict(report)
    payload["sample_count"] = report.sample_count
    payload["mean_combined"] = report.mean_combined
    payload["mean_cer"] = report.mean_cer
    payload["passing"] = report.passing
    payload["regressions"] = report.regressions()
    return payload


def _mixed_routing_engine(
    service: ConversionService,
    observations: dict[str, dict[str, dict[str, Any]]],
    output_dir: Path,
):
    def run(case: PdfBenchmarkCase) -> PdfEngineOutput:
        probe = probe_pdf(case.pdf_path)
        result = service.convert_file(
            str(case.pdf_path),
            _case_options(
                case,
                {
                    "probe_result": probe.to_dict(),
                    "output_format": "markdown",
                    "disable_multiprocessing": True,
                    "image_handling_mode": "extraction",
                },
            ),
        )
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        output = PdfEngineOutput(
            text=str(result.get("text") or ""),
            table=metadata.get("tables") or metadata.get("table"),
            metadata=metadata,
        )
        _record_output(observations, "mixed_pdf", case, output, output_dir)
        return output

    return run


def _mixed_gate_dict(comparison: Any) -> dict[str, Any]:
    return {
        "ready_for_default": comparison.ready_for_default,
        "verdict": comparison.verdict,
        "marker_report": _score_dict(comparison.marker_report),
        "mixed_report": _score_dict(comparison.mixed_report),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(BACKEND / "tests" / "fixtures" / "phase3_pdf_benchmark"),
        help="Directory for generated PDFs, golden.json, and report JSON.",
    )
    parser.add_argument(
        "--report-name",
        default="phase3_pdf_benchmark_report.json",
        help="Report filename inside output-dir.",
    )
    parser.add_argument(
        "--include-real-docs",
        action="store_true",
        help="Add optional public/manual real-doc table-heavy fixtures.",
    )
    parser.add_argument(
        "--real-doc-fixture-dir",
        default=str(BACKEND / "tests" / "fixtures" / "manual_real_docs"),
        help="Directory containing optional manual real-doc fixtures.",
    )
    parser.add_argument(
        "--collect-marker-json-table-evidence",
        action="store_true",
        help="Run Marker JSON renderer too and use block-level table geometry when available.",
    )
    parser.add_argument(
        "--run-mixed-routing-gate",
        action="store_true",
        help=(
            "Run real-engine mixed PDF gate on the synthetic clean/scanned/table "
            "fixture, plus a public composite real-doc fixture when --include-real-docs is set."
        ),
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    cases = generate_phase3_pdf_cases(output_dir)
    if args.include_real_docs:
        cases.extend(load_manual_real_table_heavy_pdf_cases(args.real_doc_fixture_dir))
    marker_service = MarkerService()
    marker_converter = MarkerPdfConverter(marker_service)
    liteparse_converter = LiteParsePdfConverter()
    conversion_service = ConversionService(marker_service)
    output_observations: dict[str, dict[str, dict[str, Any]]] = {}

    comparison = compare_marker_liteparse_pdfs(
        cases,
        marker_engine=_marker_engine(
            marker_converter,
            output_observations,
            output_dir,
            collect_json_table_evidence=args.collect_marker_json_table_evidence,
        ),
        liteparse_engine=_liteparse_engine(liteparse_converter, output_observations, output_dir),
    )

    report: dict[str, Any] = {
        "covered_classes": list(comparison.covered_classes),
        "ready_for_phase4": comparison.ready_for_phase4,
        "verdict": comparison.verdict,
        "marker_json_table_evidence_enabled": args.collect_marker_json_table_evidence,
        "marker_report": _score_dict(comparison.marker_report),
        "liteparse_report": _score_dict(comparison.liteparse_report),
        "engine_outputs": output_observations,
    }
    if args.run_mixed_routing_gate:
        mixed_cases = [generate_mixed_routing_pdf_case(output_dir)]
        if args.include_real_docs:
            mixed_cases.append(
                generate_real_mixed_routing_pdf_case(
                    args.real_doc_fixture_dir,
                    output_dir,
                )
            )
        mixed_comparison = compare_mixed_pdf_routing(
            mixed_cases,
            marker_engine=_marker_engine(
                marker_converter,
                output_observations,
                output_dir,
                collect_json_table_evidence=args.collect_marker_json_table_evidence,
            ),
            mixed_engine=_mixed_routing_engine(
                conversion_service,
                output_observations,
                output_dir,
            ),
        )
        report["mixed_routing_gate"] = _mixed_gate_dict(mixed_comparison)
    report_path = output_dir / args.report_name
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report["verdict"], indent=2))
    print(f"report={report_path}")
    return 0 if comparison.ready_for_phase4 else 2


if __name__ == "__main__":
    raise SystemExit(main())
