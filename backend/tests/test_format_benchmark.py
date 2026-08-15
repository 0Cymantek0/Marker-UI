"""Tests for non-PDF format benchmark gate."""

from __future__ import annotations

from pathlib import Path

from app.benchmark.format_benchmark import (
    FormatBenchmarkCase,
    FormatEngineOutput,
    compare_native_markitdown_formats,
    run_format_benchmark,
    score_format_output,
)
from app.benchmark.format_corpus import (
    load_manual_native_format_outputs,
    manual_format_benchmark_cases,
)


def test_format_score_combines_content_links_metadata_and_privacy():
    case = FormatBenchmarkCase(
        sample_id="docx",
        format_class="docx",
        source_path="fixture.docx",
        reference_text="Quarter Q1 revenue 100 cost 40. Reference https://example.com/doc",
        reference_tables=[
            ["Quarter", "Revenue", "Cost"],
            ["Q1", "100", "40"],
        ],
        expected_links=("https://example.com/doc",),
        expected_metadata={
            "engine.engine": "office_docx",
            "engine.needs_cloud": False,
        },
    )
    output = FormatEngineOutput(
        text=(
            "| Quarter | Revenue | Cost |\n"
            "| --- | --- | --- |\n"
            "| Q1 | 100 | 40 |\n"
            "Reference https://example.com/doc"
        ),
        metadata={"engine": {"engine": "office_docx", "needs_cloud": False}},
    )

    score = score_format_output(case, output)

    assert score.content.table_score == 1.0
    assert score.link_score == 1.0
    assert score.metadata_score == 1.0
    assert score.privacy_score == 1.0
    assert score.combined == 1.0
    assert score.details["source_path"] == "fixture.docx"


def test_format_score_flags_missing_link_metadata_and_privacy():
    case = FormatBenchmarkCase(
        sample_id="html",
        format_class="html",
        source_path="fixture.html",
        reference_text="Visible paragraph 100",
        expected_links=("https://example.com/missing",),
        expected_metadata={"engine.engine": "html"},
    )
    output = FormatEngineOutput(
        text="Visible paragraph 100 from C:\\Users\\someone\\private.html",
        metadata={"engine": {"engine": "wrong", "needs_cloud": True}},
    )

    score = score_format_output(case, output)

    assert score.link_score == 0.0
    assert score.metadata_score == 0.0
    assert score.privacy_score == 0.0
    assert score.combined < 1.0
    assert score.details["missing_links"] == ["https://example.com/missing"]
    assert score.details["missing_metadata"] == ["engine.engine"]
    assert set(score.details["privacy_violations"]) == {"private_path", "needs_cloud"}


def test_compare_formats_without_markitdown_outputs_reports_native_only():
    case = FormatBenchmarkCase(
        sample_id="txt",
        format_class="txt",
        source_path="fixture.txt",
        reference_text="hello 100",
        expected_metadata={"engine.engine": "text"},
    )
    outputs = {
        "txt": FormatEngineOutput(
            text="hello 100",
            metadata={"engine": {"engine": "text", "needs_cloud": False}},
        )
    }

    comparison = compare_native_markitdown_formats([case], outputs)

    assert comparison.markitdown_report is None
    assert comparison.verdict["comparison_available"] is False
    assert comparison.verdict["native_passes_gate"] is True


def test_compare_formats_uses_markitdown_as_baseline_when_available():
    case = FormatBenchmarkCase(
        sample_id="txt",
        format_class="txt",
        source_path="fixture.txt",
        reference_text="hello 100",
    )
    native = {"txt": FormatEngineOutput(text="hello 100")}
    markitdown = {"txt": FormatEngineOutput(text="bad")}

    comparison = compare_native_markitdown_formats([case], native, markitdown)

    assert comparison.markitdown_report is not None
    assert comparison.verdict["comparison_available"] is True
    assert comparison.verdict["baseline"] == "markitdown"
    assert comparison.verdict["candidate"] == "native"
    assert comparison.verdict["should_swap"] is True


def test_run_format_benchmark_requires_all_outputs():
    case = FormatBenchmarkCase(
        sample_id="missing",
        format_class="txt",
        source_path="fixture.txt",
        reference_text="hello",
    )

    try:
        run_format_benchmark("native", [case], {})
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing output should fail")


def test_manual_format_fixture_outputs_score_above_gate():
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "manual_real_docs"
    cases = manual_format_benchmark_cases(fixture_dir)
    outputs = load_manual_native_format_outputs(fixture_dir)

    report = run_format_benchmark("native", cases, outputs)

    assert report.sample_count == 10
    assert report.passing
    assert report.regressions() == []
    assert {score.format_class for score in report.scores} == {
        "docx",
        "pptx",
        "xlsx",
        "xls",
        "msg",
        "audio",
        "video",
        "html",
        "tsv",
        "zip",
    }
