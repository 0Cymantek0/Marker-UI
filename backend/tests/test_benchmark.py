"""Tests for the OCR/VLM benchmark harness (plan §9.4).

Covers the scoring metrics (CER/WER, TEDS-lite, facts) and the regression-gate
logic that decides whether an engine swap is justified. The harness must refuse
to swap on faith: a candidate engine only wins when it both clears the
golden-match gate and beats the baseline.
"""

from __future__ import annotations

from app.benchmark.metrics import (
    character_error_rate,
    facts_recall,
    score_outputs,
    table_similarity,
    word_error_rate,
)
from app.benchmark.runner import (
    GOLDEN_MATCH_THRESHOLD,
    BenchmarkReport,
    PdfBenchmarkCase,
    PdfEngineOutput,
    PHASE3_LITEPARSE_FAST_PATH_CLASSES,
    PHASE3_PDF_CLASSES,
    BenchmarkSample,
    compare_marker_liteparse_pdfs,
    compare_configs,
    run_benchmark,
    validate_phase3_pdf_corpus,
)
from app.benchmark.phase3_pdf_corpus import generate_phase3_pdf_cases, load_phase3_pdf_cases
from app.conversion.table_evidence import attach_table_evidence, extract_markdown_tables


# ---------------------------------------------------------------------------
# CER / WER
# ---------------------------------------------------------------------------


def test_cer_zero_on_exact_match():
    assert character_error_rate("hello world", "hello world") == 0.0


def test_cer_one_on_total_mismatch_against_empty_ref():
    # Empty reference + non-empty hypothesis: all hypothesis chars are errors.
    assert character_error_rate("", "abc") == 1.0


def test_cer_counts_single_substitution():
    # "cat" -> "cot": 1 substitution over 3 reference chars.
    assert abs(character_error_rate("cat", "cot") - (1 / 3)) < 1e-9


def test_wer_counts_word_edits():
    # one substitution over three reference words
    assert abs(word_error_rate("the quick fox", "the slow fox") - (1 / 3)) < 1e-9


def test_cer_perfect_when_both_empty():
    assert character_error_rate("", "") == 0.0


# ---------------------------------------------------------------------------
# Facts recall (chart/structured)
# ---------------------------------------------------------------------------


def test_facts_recall_finds_numbers_regardless_of_formatting():
    # The machine-checkable facts (the numbers) survive cosmetic markdown diffs.
    ref = "Revenue was 100 in Q1 and 140 in Q2."
    hyp = "| Q1 | 100 |\n| Q2 | 140 |"
    assert facts_recall(ref, hyp) == 1.0


def test_facts_recall_partial_when_a_number_missing():
    ref = "values 10 20 30"
    hyp = "values 10 20"
    assert abs(facts_recall(ref, hyp) - (2 / 3)) < 1e-9


def test_facts_recall_one_when_no_facts_in_reference():
    # No numeric facts to check -> vacuously perfect (text metric carries it).
    assert facts_recall("just prose", "different prose") == 1.0


# ---------------------------------------------------------------------------
# Table similarity (TEDS-lite)
# ---------------------------------------------------------------------------


def test_table_similarity_identical_tables():
    t = [["a", "b"], ["1", "2"]]
    assert table_similarity(t, t) == 1.0


def test_table_similarity_partial_on_one_wrong_cell():
    ref = [["a", "b"], ["1", "2"]]
    hyp = [["a", "b"], ["1", "9"]]
    # 3 of 4 cells match.
    assert abs(table_similarity(ref, hyp) - 0.75) < 1e-9


def test_table_similarity_handles_shape_mismatch():
    ref = [["a", "b"], ["1", "2"]]
    hyp = [["a", "b"]]
    # Missing cells count against the score but it never raises.
    score = table_similarity(ref, hyp)
    assert 0.0 <= score < 1.0


# ---------------------------------------------------------------------------
# Combined score
# ---------------------------------------------------------------------------


def test_score_outputs_perfect_text():
    score = score_outputs(
        sample_id="s1",
        reference_text="hello world",
        hypothesis_text="hello world",
    )
    assert score.combined == 1.0
    assert score.cer == 0.0
    assert score.details == {"reference_len": 11, "hypothesis_len": 11}


def test_score_outputs_blends_table_when_present():
    score = score_outputs(
        sample_id="s2",
        reference_text="totals",
        hypothesis_text="totals",
        reference_table=[["a", "b"], ["1", "2"]],
        hypothesis_table=[["a", "b"], ["1", "9"]],
    )
    # Table is imperfect (0.75), so combined sits below 1.0 even though text matches.
    assert score.combined < 1.0
    assert score.table_score == 0.75


def test_markdown_table_evidence_extracts_headers_and_rows():
    tables = extract_markdown_tables(
        "Report\n\n"
        "| Quarter | Revenue | Cost |\n"
        "| --- | --- | --- |\n"
        "| Q1 | 100 | 40 |\n"
        "| Q2 | 140 | 55 |\n"
    )

    assert tables == [
        {
            "source": "markdown",
            "headers": ["Quarter", "Revenue", "Cost"],
            "rows": [["Q1", "100", "40"], ["Q2", "140", "55"]],
            "row_count": 2,
            "column_count": 3,
        }
    ]


def test_table_evidence_lets_markdown_table_score_as_structured_table():
    text = (
        "Table heavy benchmark.\n\n"
        "| Quarter | Revenue | Cost |\n"
        "| --- | --- | --- |\n"
        "| Q1 | 100 | 40 |\n"
        "| Q2 | 140 | 55 |\n"
        "| Q3 | 160 | 65 |\n"
    )
    metadata = attach_table_evidence({"engine": "marker_pdf"}, text)

    score = score_outputs(
        sample_id="table_heavy",
        reference_text="Quarter Q1 revenue 100 cost 40. Quarter Q2 revenue 140 cost 55. Quarter Q3 revenue 160 cost 65.",
        hypothesis_text=text,
        reference_table=[
            ["Quarter", "Revenue", "Cost"],
            ["Q1", "100", "40"],
            ["Q2", "140", "55"],
            ["Q3", "160", "65"],
        ],
        hypothesis_table=metadata["table"],
    )

    assert metadata["table_evidence"] == {
        "source": "markdown_pipe_table",
        "table_count": 1,
    }
    assert score.table_score == 1.0


# ---------------------------------------------------------------------------
# Regression gate
# ---------------------------------------------------------------------------


def _report(name: str, pairs: list[tuple[str, str, str]]) -> BenchmarkReport:
    samples = [
        BenchmarkSample(sample_id=sid, reference_text=ref, hypothesis_text=hyp)
        for sid, ref, hyp in pairs
    ]
    return run_benchmark(name, samples)


def test_report_passing_when_above_threshold():
    report = _report(
        "surya",
        [("a", "hello world", "hello world"), ("b", "foo bar", "foo bar")],
    )
    assert report.mean_combined == 1.0
    assert report.passing
    assert report.regressions() == []


def test_report_flags_regressions_below_threshold():
    report = _report(
        "surya",
        [("a", "hello world", "completely different text here")],
    )
    assert not report.passing
    assert "a" in report.regressions()


def test_compare_configs_refuses_swap_when_candidate_fails_gate():
    baseline = _report("surya", [("a", "hello world", "hello world")])
    # Candidate is garbage -> below gate -> never swap even if it differs.
    candidate = _report("glm_ocr", [("a", "hello world", "zzz")])
    verdict = compare_configs(baseline, candidate)
    assert verdict["should_swap"] is False
    assert verdict["candidate_passes_gate"] is False


def test_compare_configs_refuses_swap_when_not_better():
    baseline = _report("surya", [("a", "hello world", "hello world")])
    candidate = _report("glm_ocr", [("a", "hello world", "hello world")])
    verdict = compare_configs(baseline, candidate)
    # Equal score -> delta 0 -> no swap (no swap on faith / no-op churn).
    assert verdict["delta"] == 0.0
    assert verdict["should_swap"] is False


def test_compare_configs_swaps_when_better_and_passing():
    baseline = _report("surya", [("a", "hello world there", "hello world")])
    candidate = _report("glm_ocr", [("a", "hello world there", "hello world there")])
    verdict = compare_configs(baseline, candidate)
    assert verdict["candidate_passes_gate"] is True
    assert verdict["delta"] > 0
    assert verdict["should_swap"] is True


def test_threshold_is_eighty_percent():
    # Pin the golden-match threshold the gate inherits from markitdown #4.
    assert GOLDEN_MATCH_THRESHOLD == 0.80


# ---------------------------------------------------------------------------
# Phase 3 Marker-vs-LiteParse PDF gate
# ---------------------------------------------------------------------------


def _phase3_cases() -> list[PdfBenchmarkCase]:
    return [
        PdfBenchmarkCase(
            sample_id=f"{document_class}-1",
            pdf_path=f"fixtures/{document_class}.pdf",
            document_class=document_class,
            reference_text=f"{document_class} reference 100",
        )
        for document_class in PHASE3_PDF_CLASSES
    ]


def test_phase3_corpus_requires_all_planned_pdf_classes():
    incomplete = _phase3_cases()[:-1]

    try:
        validate_phase3_pdf_corpus(incomplete)
    except ValueError as exc:
        assert PHASE3_PDF_CLASSES[-1] in str(exc)
    else:
        raise AssertionError("Phase 3 corpus validation should fail")


def test_phase3_compare_runs_marker_and_liteparse_across_required_classes():
    calls: list[tuple[str, str]] = []

    def marker_engine(case: PdfBenchmarkCase) -> PdfEngineOutput:
        calls.append(("marker", case.document_class))
        return PdfEngineOutput(text=case.reference_text)

    def liteparse_engine(case: PdfBenchmarkCase) -> dict[str, object]:
        calls.append(("liteparse", case.document_class))
        return {"text": case.reference_text, "metadata": {"engine": "liteparse_pdf"}}

    comparison = compare_marker_liteparse_pdfs(
        _phase3_cases(),
        marker_engine=marker_engine,
        liteparse_engine=liteparse_engine,
    )

    assert comparison.marker_report.sample_count == len(PHASE3_PDF_CLASSES)
    assert comparison.liteparse_report.sample_count == len(PHASE3_PDF_CLASSES)
    assert comparison.ready_for_phase4
    assert comparison.verdict["should_swap"] is False
    assert comparison.covered_classes == PHASE3_PDF_CLASSES
    assert calls == [
        ("marker", document_class) for document_class in PHASE3_PDF_CLASSES
    ] + [
        ("liteparse", document_class) for document_class in PHASE3_PDF_CLASSES
    ]


def test_phase3_compare_blocks_phase4_when_liteparse_fails_gate():
    def marker_engine(case: PdfBenchmarkCase) -> str:
        return case.reference_text

    def liteparse_engine(case: PdfBenchmarkCase) -> str:
        return "garbage"

    comparison = compare_marker_liteparse_pdfs(
        _phase3_cases(),
        marker_engine=marker_engine,
        liteparse_engine=liteparse_engine,
    )

    assert comparison.marker_report.passing
    assert not comparison.liteparse_report.passing
    assert not comparison.ready_for_phase4
    assert comparison.verdict["liteparse_regressions"]


def test_phase3_ready_for_phase4_when_liteparse_fast_path_passes_only():
    """Phase 3 gates fast-path routing, not a broad LiteParse PDF swap."""

    def marker_engine(case: PdfBenchmarkCase) -> str:
        return case.reference_text

    def liteparse_engine(case: PdfBenchmarkCase) -> str:
        if case.document_class in PHASE3_LITEPARSE_FAST_PATH_CLASSES:
            return case.reference_text
        return "garbage"

    comparison = compare_marker_liteparse_pdfs(
        _phase3_cases(),
        marker_engine=marker_engine,
        liteparse_engine=liteparse_engine,
    )

    assert not comparison.liteparse_report.passing
    assert not comparison.verdict["candidate_passes_gate"]
    assert comparison.verdict["liteparse_fast_path_passes_gate"]
    assert comparison.verdict["liteparse_fast_path_regressions"] == []
    assert comparison.ready_for_phase4


def test_phase3_table_heavy_uses_markdown_table_metadata_for_scoring():
    table = [
        ["Quarter", "Revenue", "Cost"],
        ["Q1", "100", "40"],
        ["Q2", "140", "55"],
        ["Q3", "160", "65"],
    ]
    cases = [
        PdfBenchmarkCase(
            sample_id="clean",
            pdf_path="fixtures/clean.pdf",
            document_class="clean_digital",
            reference_text="Clean digital reference 100",
        ),
        PdfBenchmarkCase(
            sample_id="scanned",
            pdf_path="fixtures/scanned.pdf",
            document_class="scanned",
            reference_text="Scanned reference 250",
        ),
        PdfBenchmarkCase(
            sample_id="sandwich",
            pdf_path="fixtures/sandwich.pdf",
            document_class="sandwich",
            reference_text="Sandwich account A 300 account B 450",
            reference_table=[["Account", "Balance"], ["A", "300"], ["B", "450"]],
        ),
        PdfBenchmarkCase(
            sample_id="table",
            pdf_path="fixtures/table.pdf",
            document_class="table_heavy",
            reference_text="Table heavy benchmark. Quarter Q1 revenue 100 cost 40. Quarter Q2 revenue 140 cost 55. Quarter Q3 revenue 160 cost 65.",
            reference_table=table,
        ),
        PdfBenchmarkCase(
            sample_id="formula",
            pdf_path="fixtures/formula.pdf",
            document_class="formula_heavy",
            reference_text="Formula reference 1",
        ),
    ]

    table_markdown = (
        "Table heavy benchmark.\n\n"
        "| Quarter | Revenue | Cost |\n"
        "| --- | --- | --- |\n"
        "| Q1 | 100 | 40 |\n"
        "| Q2 | 140 | 55 |\n"
        "| Q3 | 160 | 65 |\n"
    )

    def marker_engine(case: PdfBenchmarkCase) -> PdfEngineOutput:
        if case.document_class == "table_heavy":
            metadata = attach_table_evidence({}, table_markdown)
            return PdfEngineOutput(
                text=table_markdown,
                table=metadata["table"],
                metadata=metadata,
            )
        return PdfEngineOutput(text=case.reference_text)

    comparison = compare_marker_liteparse_pdfs(
        cases,
        marker_engine=marker_engine,
        liteparse_engine=marker_engine,
    )

    table_score = next(
        score
        for score in comparison.marker_report.scores
        if score.sample_id == "table_heavy:table"
    )
    assert table_score.table_score == 1.0
    assert table_score.combined > 0.50


def test_phase3_generated_pdf_corpus_covers_required_classes(tmp_path):
    cases = generate_phase3_pdf_cases(tmp_path)
    loaded = load_phase3_pdf_cases(tmp_path)

    assert validate_phase3_pdf_corpus(cases) == PHASE3_PDF_CLASSES
    assert validate_phase3_pdf_corpus(loaded) == PHASE3_PDF_CLASSES
    assert (tmp_path / "golden.json").is_file()
    assert {case.document_class for case in cases} == set(PHASE3_PDF_CLASSES)
    assert all(case.pdf_path.is_file() for case in cases)
