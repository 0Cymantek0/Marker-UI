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

from app.benchmark.phase3_pdf_corpus import generate_phase3_pdf_cases
from app.benchmark.runner import PdfBenchmarkCase, PdfEngineOutput, compare_marker_liteparse_pdfs
from app.conversion.converters.liteparse_pdf import LiteParsePdfConverter
from app.conversion.converters.marker_pdf import MarkerPdfConverter
from app.services.marker_service import MarkerService

MAX_OUTPUT_PREVIEW_CHARS = 500


def _preview_text(text: str, limit: int = MAX_OUTPUT_PREVIEW_CHARS) -> str:
    preview = " ".join((text or "").split())
    if len(preview) <= limit:
        return preview
    return f"{preview[:limit].rstrip()}..."


def _record_output(
    observations: dict[str, dict[str, dict[str, Any]]],
    engine_name: str,
    case: PdfBenchmarkCase,
    output: PdfEngineOutput,
) -> None:
    sample_key = f"{case.document_class}:{case.sample_id}"
    observations.setdefault(engine_name, {})[sample_key] = {
        "document_class": case.document_class,
        "text_len": len(output.text or ""),
        "text_preview": _preview_text(output.text),
        "table_present": output.table is not None,
        "metadata_keys": sorted(output.metadata.keys()),
    }


def _liteparse_engine(
    converter: LiteParsePdfConverter,
    observations: dict[str, dict[str, dict[str, Any]]],
):
    def run(case: PdfBenchmarkCase) -> PdfEngineOutput:
        result = converter.convert(str(case.pdf_path), {"liteparse_timeout": 120})
        output = PdfEngineOutput(
            text=result.text,
            table=result.metadata.get("table"),
            metadata=result.metadata,
        )
        _record_output(observations, "liteparse_pdf", case, output)
        return output

    return run


def _marker_engine(
    converter: MarkerPdfConverter,
    observations: dict[str, dict[str, dict[str, Any]]],
):
    def run(case: PdfBenchmarkCase) -> PdfEngineOutput:
        result = converter.convert(
            str(case.pdf_path),
            {
                "output_format": "markdown",
                "disable_multiprocessing": True,
                "image_handling_mode": "extraction",
            },
        )
        output = PdfEngineOutput(
            text=result.text,
            table=result.metadata.get("table"),
            metadata=result.metadata,
        )
        _record_output(observations, "marker_pdf", case, output)
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
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    cases = generate_phase3_pdf_cases(output_dir)
    marker_converter = MarkerPdfConverter(MarkerService())
    liteparse_converter = LiteParsePdfConverter()
    output_observations: dict[str, dict[str, dict[str, Any]]] = {}

    comparison = compare_marker_liteparse_pdfs(
        cases,
        marker_engine=_marker_engine(marker_converter, output_observations),
        liteparse_engine=_liteparse_engine(liteparse_converter, output_observations),
    )

    report = {
        "covered_classes": list(comparison.covered_classes),
        "ready_for_phase4": comparison.ready_for_phase4,
        "verdict": comparison.verdict,
        "marker_report": _score_dict(comparison.marker_report),
        "liteparse_report": _score_dict(comparison.liteparse_report),
        "engine_outputs": output_observations,
    }
    report_path = output_dir / args.report_name
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report["verdict"], indent=2))
    print(f"report={report_path}")
    return 0 if comparison.ready_for_phase4 else 2


if __name__ == "__main__":
    raise SystemExit(main())
