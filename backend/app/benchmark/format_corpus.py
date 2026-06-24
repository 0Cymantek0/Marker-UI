"""Manual real-fixture corpus for non-PDF format benchmark gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.benchmark.format_benchmark import FormatBenchmarkCase, FormatEngineOutput


FORMAT_REFERENCE_TABLES: dict[str, object | None] = {
    "docx_generated": [
        ["Quarter", "Revenue", "Cost"],
        ["Q1", "100", "40"],
        ["Q2", "140", "55"],
        ["Q3", "160", "65"],
    ],
    "pptx_generated": [
        ["Region", "Sales", "Growth"],
        ["North", "10", "5%"],
        ["South", "15", "8%"],
        ["East", "8", "3%"],
    ],
    "xlsx_generated": [
        {
            "headers": ["Quarter", "Revenue", "Cost", "Margin"],
            "rows": [
                ["Q1", "100", "40", "60"],
                ["Q2", "140", "55", "85"],
                ["Q3", "160", "65", "95"],
                ["Q4", "190", "80", "110"],
            ],
        },
        {
            "headers": ["Key", "Value"],
            "rows": [["Source", "generated manual fixture"]],
        },
    ],
    "xls_generated": [
        ["Quarter", "Revenue", "Cost"],
        ["Q1", "90", "35"],
        ["Q2", "120", "50"],
        ["Q3", "150", "62"],
    ],
    "msg_generated": None,
    "audio_generated": None,
    "video_generated": None,
    "html_generated": [
        ["Name", "Score"],
        ["Ada", "98"],
        ["Lin", "91"],
    ],
    "tsv_generated": [
        ["Region", "Revenue", "Notes"],
        ["North", "100", "steady"],
        ["South", "125", "growing"],
    ],
    "zip_generated": None,
}


def manual_format_benchmark_cases(
    fixture_dir: str | Path,
) -> list[FormatBenchmarkCase]:
    """Return local/manual format cases with explicit goldens."""
    base = Path(fixture_dir)
    return [
        FormatBenchmarkCase(
            sample_id="docx_generated",
            format_class="docx",
            source_path=base / "manual_table_doc.docx",
            reference_text=(
                "Manual DOCX Conversion Fixture. This DOCX contains paragraphs, "
                "a table, and a hyperlink-like URL for native conversion checks. "
                "Quarter Q1 revenue 100 cost 40. Quarter Q2 revenue 140 cost 55. "
                "Quarter Q3 revenue 160 cost 65. Reference: "
                "https://example.com/manual-docx"
            ),
            reference_tables=FORMAT_REFERENCE_TABLES["docx_generated"],
            expected_links=("https://example.com/manual-docx",),
            expected_metadata={
                "engine.engine": "office_docx",
                "engine.needs_cloud": False,
            },
        ),
        FormatBenchmarkCase(
            sample_id="pptx_generated",
            format_class="pptx",
            source_path=base / "manual_table_slide.pptx",
            reference_text=(
                "Manual PPTX Conversion Fixture. Slide text with chart values: "
                "North 10, South 15, East 8."
            ),
            reference_tables=FORMAT_REFERENCE_TABLES["pptx_generated"],
            expected_metadata={
                "engine.engine": "office_pptx",
                "engine.needs_cloud": False,
            },
        ),
        FormatBenchmarkCase(
            sample_id="xlsx_generated",
            format_class="xlsx",
            source_path=base / "manual_workbook.xlsx",
            reference_text=(
                "manual_workbook. Sheet Revenue has Q1 revenue 100 cost 40 "
                "margin 60, Q2 revenue 140 cost 55 margin 85, Q3 revenue 160 "
                "cost 65 margin 95, Q4 revenue 190 cost 80 margin 110. "
                "Sheet Notes source generated manual fixture."
            ),
            reference_tables=FORMAT_REFERENCE_TABLES["xlsx_generated"],
            expected_metadata={
                "engine.engine": "spreadsheet",
                "engine.needs_cloud": False,
                "engine_detail.format": "xlsx",
            },
        ),
        FormatBenchmarkCase(
            sample_id="xls_generated",
            format_class="xls",
            source_path=base / "manual_legacy.xls",
            reference_text=(
                "manual_legacy. Sheet Legacy Revenue has Q1 revenue 90 cost 35, "
                "Q2 revenue 120 cost 50, and Q3 revenue 150 cost 62."
            ),
            reference_tables=FORMAT_REFERENCE_TABLES["xls_generated"],
            expected_metadata={
                "engine.engine": "spreadsheet",
                "engine.needs_cloud": False,
                "engine_detail.format": "xls",
            },
        ),
        FormatBenchmarkCase(
            sample_id="msg_generated",
            format_class="msg",
            source_path=base / "manual_outlook_msg.msg",
            reference_text=(
                "Test Email Message. This is the body of the test email message."
            ),
            expected_metadata={
                "engine.engine": "outlook_msg",
                "engine.needs_cloud": False,
                "engine_detail.format": "msg",
            },
        ),
        FormatBenchmarkCase(
            sample_id="audio_generated",
            format_class="audio",
            source_path=base / "manual_audio.wav",
            reference_text="Hello world this is marker audio test.",
            expected_metadata={
                "engine.engine": "audio",
                "engine.needs_cloud": False,
                "engine_detail.format": "wav",
            },
        ),
        FormatBenchmarkCase(
            sample_id="video_generated",
            format_class="video",
            source_path=base / "manual_video.mp4",
            reference_text=(
                "Video Timeline manual_video. Hello video timeline this is marker test. "
                "Frame 640x360 bright blue. Audio transcript is aligned to frame timestamps."
            ),
            expected_metadata={
                "engine_detail.format": "mp4",
                "engine_detail.has_audio": True,
                "video.provenance.audio": True,
                "video.provenance.frames": True,
                "video.provenance.cloud": False,
            },
        ),
        FormatBenchmarkCase(
            sample_id="html_generated",
            format_class="html",
            source_path=base / "manual_page.html",
            reference_text="Manual HTML Fixture. Visible paragraph for conversion.",
            reference_tables=FORMAT_REFERENCE_TABLES["html_generated"],
            expected_metadata={
                "engine.engine": "html",
                "engine.needs_cloud": False,
                "engine_detail.format": "html",
            },
        ),
        FormatBenchmarkCase(
            sample_id="tsv_generated",
            format_class="tsv",
            source_path=base / "manual_metrics.tsv",
            reference_text=(
                "Manual TSV Conversion Fixture. Region North revenue 100 steady. "
                "Region South revenue 125 growing."
            ),
            reference_tables=FORMAT_REFERENCE_TABLES["tsv_generated"],
            expected_metadata={
                "engine.engine": "text_data",
                "engine.needs_cloud": False,
                "engine_detail.format": "tsv",
            },
        ),
        FormatBenchmarkCase(
            sample_id="zip_generated",
            format_class="zip",
            source_path=base / "manual_archive.zip",
            reference_text=(
                "Archive manual_archive.zip contains readme.md, data/metrics.csv, "
                "suspicious path traversal name, and binary.bin. Revenue 100. "
                "Cost 40. Path traversal name should be flagged, not extracted."
            ),
            expected_metadata={
                "engine.engine": "archive",
                "engine.needs_cloud": False,
                "engine_detail.format": "zip",
            },
        ),
    ]


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_manual_native_format_outputs(
    fixture_dir: str | Path,
) -> dict[str, FormatEngineOutput]:
    """Load existing manual native markdown + metadata outputs."""
    base = Path(fixture_dir)
    outputs_dir = base / "outputs"
    outputs: dict[str, FormatEngineOutput] = {}
    for case in manual_format_benchmark_cases(base):
        text_path = outputs_dir / f"{case.sample_id}.md"
        metadata_path = outputs_dir / f"{case.sample_id}.metadata.json"
        if not text_path.is_file():
            continue
        outputs[case.sample_id] = FormatEngineOutput(
            text=text_path.read_text(encoding="utf-8"),
            metadata=_read_metadata(metadata_path),
        )
    return outputs


def load_markitdown_format_outputs(
    output_dir: str | Path,
    cases: list[FormatBenchmarkCase],
) -> dict[str, FormatEngineOutput]:
    """Load optional Markitdown outputs from a sidecar directory.

    Expected files are ``<sample_id>.md`` plus optional
    ``<sample_id>.metadata.json``. This keeps Markitdown comparison explicit
    and avoids importing Markitdown into app runtime code.
    """
    base = Path(output_dir)
    outputs: dict[str, FormatEngineOutput] = {}
    for case in cases:
        text_path = base / f"{case.sample_id}.md"
        metadata_path = base / f"{case.sample_id}.metadata.json"
        if text_path.is_file():
            outputs[case.sample_id] = FormatEngineOutput(
                text=text_path.read_text(encoding="utf-8"),
                metadata=_read_metadata(metadata_path),
            )
    return outputs
