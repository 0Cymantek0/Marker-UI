"""Tests for the OCR/VLM benchmark harness (plan §9.4).

Covers the scoring metrics (CER/WER, TEDS-lite, facts) and the regression-gate
logic that decides whether an engine swap is justified. The harness must refuse
to swap on faith: a candidate engine only wins when it both clears the
golden-match gate and beats the baseline.
"""

from __future__ import annotations

import json

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
    compare_mixed_pdf_routing,
    compare_configs,
    run_benchmark,
    validate_phase3_pdf_corpus,
)
from app.benchmark.phase3_pdf_corpus import (
    generate_mixed_routing_pdf_case,
    generate_real_mixed_routing_pdf_case,
    generate_phase3_pdf_cases,
    load_manual_real_table_heavy_pdf_cases,
    load_phase3_pdf_cases,
)
from app.conversion.table_evidence import (
    attach_table_evidence,
    extract_html_tables,
    extract_marker_json_tables,
    extract_markdown_tables,
    extract_tables,
)


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


def test_table_similarity_matches_multiple_tables_best_effort():
    ref = [
        {"headers": ["A"], "rows": [["1"]]},
        {"headers": ["B"], "rows": [["2"]]},
    ]
    hyp = [
        {"headers": ["B"], "rows": [["2"]]},
        {"headers": ["A"], "rows": [["1"]]},
    ]

    assert table_similarity(ref, hyp) == 1.0


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
    assert score.details == {
        "reference_len": 11,
        "hypothesis_len": 11,
        "scoring_mode": "text",
    }


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

    assert tables[0]["source"] == "markdown_pipe_table"
    assert tables[0]["headers"] == ["Quarter", "Revenue", "Cost"]
    assert tables[0]["rows"] == [["Q1", "100", "40"], ["Q2", "140", "55"]]
    assert tables[0]["row_count"] == 2
    assert tables[0]["column_count"] == 3


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
        "source": "rendered_text_tables",
        "table_count": 1,
        "sources": ["markdown_pipe_table"],
    }
    assert score.table_score == 1.0
    assert score.combined == 1.0
    assert score.details["scoring_mode"] == "table_structured"


def test_markdown_table_evidence_handles_caption_and_missing_outer_pipes():
    tables = extract_markdown_tables(
        "**Table 1: sample values**\n\n"
        "Name | Score\n"
        "--- | ---\n"
        "Ada | 98\n"
        "Lin | 91\n"
    )

    assert tables[0]["caption"] == "Table 1: sample values"
    assert tables[0]["headers"] == ["Name", "Score"]
    assert tables[0]["rows"] == [["Ada", "98"], ["Lin", "91"]]


def test_markdown_table_evidence_promotes_first_data_row_after_empty_header():
    tables = extract_markdown_tables(
        "|  |  |  |\n"
        "| --- | --- | --- |\n"
        "| Quarter | Revenue | Cost |\n"
        "| Q1 | 100 | 40 |\n"
    )

    assert tables[0]["headers"] == ["Quarter", "Revenue", "Cost"]
    assert tables[0]["rows"] == [["Q1", "100", "40"]]


def test_html_table_evidence_preserves_spans_and_caption():
    tables = extract_html_tables(
        "<table><caption>Table 2: regions</caption>"
        "<tr><th rowspan='2'>Region</th><th colspan='2'>Sales</th></tr>"
        "<tr><th>2025</th><th>2026</th></tr>"
        "<tr><td>North</td><td>10</td><td>12</td></tr>"
        "</table>"
    )

    assert tables[0]["caption"] == "Table 2: regions"
    assert tables[0]["headers"] == ["Region", "Sales", ""]
    assert tables[0]["rows"] == [["", "2025", "2026"], ["North", "10", "12"]]
    assert tables[0]["cells"][0]["rowspan"] == 2
    assert tables[0]["cells"][1]["colspan"] == 2


def test_table_evidence_stitches_repeated_markdown_headers():
    tables = extract_tables(
        "Page 1\n\n"
        "| Quarter | Revenue | Cost |\n"
        "| --- | --- | --- |\n"
        "| Q1 | 100 | 40 |\n"
        "\nPage 2\n\n"
        "| Quarter | Revenue | Cost |\n"
        "| --- | --- | --- |\n"
        "| Q2 | 140 | 55 |\n"
    )

    assert len(tables) == 1
    assert tables[0]["multi_page"] is True
    assert tables[0]["segment_count"] == 2
    assert tables[0]["headers"] == ["Quarter", "Revenue", "Cost"]
    assert tables[0]["rows"] == [["Q1", "100", "40"], ["Q2", "140", "55"]]


def test_table_evidence_stitches_html_cells_with_row_offsets():
    tables = extract_tables(
        "<table><tr><th>Quarter</th><th>Revenue</th></tr>"
        "<tr><td>Q1</td><td>100</td></tr></table>"
        "<p>continued on next page</p>"
        "<table><tr><th>Quarter</th><th>Revenue</th></tr>"
        "<tr><td>Q2</td><td>140</td></tr></table>"
    )

    assert len(tables) == 1
    assert tables[0]["rows"] == [["Q1", "100"], ["Q2", "140"]]
    assert tables[0]["segment_count"] == 2
    assert [cell["row"] for cell in tables[0]["cells"] if cell["text"] in {"Q1", "Q2"}] == [1, 2]


def test_attach_table_evidence_reports_stitched_table_count():
    metadata = attach_table_evidence(
        {},
        "| Quarter | Revenue |\n"
        "| --- | --- |\n"
        "| Q1 | 100 |\n\n"
        "| Quarter | Revenue |\n"
        "| --- | --- |\n"
        "| Q2 | 140 |\n",
    )

    assert metadata["table_evidence"]["table_count"] == 1
    assert metadata["tables"][0]["segment_count"] == 2
    assert metadata["table"]["rows"] == [["Q1", "100"], ["Q2", "140"]]


def test_marker_json_table_evidence_preserves_layout_cells():
    payload = {
        "children": [
            {
                "id": "page-1",
                "block_type": "BlockTypes.Page",
                "html": "",
                "bbox": [0, 0, 600, 800],
                "polygon": [[0, 0], [600, 0], [600, 800], [0, 800]],
                "children": [
                    {
                        "id": "table-1",
                        "block_type": "BlockTypes.Table",
                        "html": (
                            "<table><tr><th>Quarter</th><th>Revenue</th></tr>"
                            "<tr><td>Q1</td><td>100</td></tr></table>"
                        ),
                        "bbox": [72, 100, 420, 200],
                        "polygon": [[72, 100], [420, 100], [420, 200], [72, 200]],
                        "children": [
                            {
                                "id": "cell-1",
                                "block_type": "BlockTypes.TableCell",
                                "html": "<th>Quarter</th>",
                                "bbox": [72, 100, 200, 130],
                                "polygon": [[72, 100], [200, 100], [200, 130], [72, 130]],
                            },
                            {
                                "id": "cell-2",
                                "block_type": "BlockTypes.TableCell",
                                "html": "<th>Revenue</th>",
                                "bbox": [200, 100, 420, 130],
                                "polygon": [[200, 100], [420, 100], [420, 130], [200, 130]],
                            },
                            {
                                "id": "cell-3",
                                "block_type": "BlockTypes.TableCell",
                                "html": "<td>Q1</td>",
                                "bbox": [72, 130, 200, 160],
                                "polygon": [[72, 130], [200, 130], [200, 160], [72, 160]],
                            },
                            {
                                "id": "cell-4",
                                "block_type": "BlockTypes.TableCell",
                                "html": "<td>100</td>",
                                "bbox": [200, 130, 420, 160],
                                "polygon": [[200, 130], [420, 130], [420, 160], [200, 160]],
                            },
                        ],
                    }
                ],
            }
        ],
    }

    tables = extract_marker_json_tables(json.dumps(payload))
    routed_tables = extract_tables(json.dumps(payload))

    assert tables[0]["source"] == "marker_json_table"
    assert len(routed_tables) == 1
    assert routed_tables[0]["source"] == "marker_json_table"
    assert tables[0]["page_number"] == 1
    assert tables[0]["bbox"] == [72, 100, 420, 200]
    assert tables[0]["headers"] == ["Quarter", "Revenue"]
    assert tables[0]["rows"] == [["Q1", "100"]]
    assert tables[0]["cells"][2]["block_id"] == "cell-3"
    assert tables[0]["cells"][2]["bbox"] == [72, 130, 200, 160]


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
    assert table_score.combined == 1.0
    assert table_score.details["scoring_mode"] == "table_structured"


def test_phase3_generated_pdf_corpus_covers_required_classes(tmp_path):
    cases = generate_phase3_pdf_cases(tmp_path)
    loaded = load_phase3_pdf_cases(tmp_path)

    assert validate_phase3_pdf_corpus(cases) == PHASE3_PDF_CLASSES
    assert validate_phase3_pdf_corpus(loaded) == PHASE3_PDF_CLASSES
    assert (tmp_path / "golden.json").is_file()
    assert {case.document_class for case in cases} == set(PHASE3_PDF_CLASSES)
    assert all(case.pdf_path.is_file() for case in cases)


def test_manual_real_table_heavy_case_loads_optional_public_fixture(tmp_path):
    fixture_dir = tmp_path / "manual_real_docs"
    fixture_dir.mkdir()
    (fixture_dir / "table_heavy_sample_tables.pdf").write_bytes(b"%PDF-1.4\n")
    (fixture_dir / "MANIFEST.json").write_text(
        '{"web_pdfs":{"table_heavy_sample_tables.pdf":"https://example.com/sample.pdf"}}',
        encoding="utf-8",
    )

    cases = load_manual_real_table_heavy_pdf_cases(fixture_dir)

    assert len(cases) == 1
    assert cases[0].document_class == "real_table_heavy"
    assert cases[0].metadata["conversion_options"] == {"page_range": "1"}
    assert cases[0].metadata["source_url"] == "https://example.com/sample.pdf"
    assert len(cases[0].reference_table) == 3


def test_mixed_routing_gate_requires_no_worse_score_and_segment_metadata():
    case = PdfBenchmarkCase(
        sample_id="mixed",
        pdf_path="fixtures/mixed.pdf",
        document_class="mixed_routing",
        reference_text="Page 1 clean 100. Page 2 scanned 250. Page 3 table Q1 100.",
        reference_table=[["Quarter", "Revenue"], ["Q1", "100"]],
    )

    def marker_engine(_case: PdfBenchmarkCase) -> PdfEngineOutput:
        return PdfEngineOutput(text=case.reference_text, table=case.reference_table)

    def mixed_engine(_case: PdfBenchmarkCase) -> PdfEngineOutput:
        return PdfEngineOutput(
            text=case.reference_text,
            table=case.reference_table,
            metadata={
                "mixed_engine_segments": [
                    {"page_range": "1", "actual_engine": "liteparse_pdf"},
                    {"page_range": "2-3", "actual_engine": "marker_pdf"},
                ]
            },
        )

    comparison = compare_mixed_pdf_routing([case], marker_engine, mixed_engine)

    assert comparison.ready_for_default
    assert comparison.verdict["mixed_no_worse_than_marker"] is True
    assert comparison.verdict["mixed_segment_metadata_failures"] == []


def test_mixed_routing_gate_blocks_missing_segment_metadata():
    case = PdfBenchmarkCase(
        sample_id="mixed",
        pdf_path="fixtures/mixed.pdf",
        document_class="mixed_routing",
        reference_text="Page 1 clean 100. Page 2 scanned 250.",
    )

    def perfect_engine(_case: PdfBenchmarkCase) -> PdfEngineOutput:
        return PdfEngineOutput(text=case.reference_text)

    comparison = compare_mixed_pdf_routing([case], perfect_engine, perfect_engine)

    assert not comparison.ready_for_default
    assert comparison.verdict["mixed_segment_metadata_failures"] == [
        "mixed_routing:mixed"
    ]


def test_generate_mixed_routing_pdf_case_has_expected_segments(tmp_path):
    case = generate_mixed_routing_pdf_case(tmp_path)

    assert case.document_class == "mixed_routing"
    assert case.pdf_path.is_file()
    assert case.metadata["expected_segments"] == [
        {"page_range": "1", "engine": "liteparse_pdf"},
        {"page_range": "2-3", "engine": "marker_pdf"},
    ]
    assert "Scanned invoice total 250" in case.reference_text
    assert case.reference_table == [
        ["Quarter", "Revenue", "Cost"],
        ["Q1", "100", "40"],
        ["Q2", "140", "55"],
    ]


def test_generate_real_mixed_routing_pdf_case_merges_public_pages(tmp_path):
    from reportlab.pdfgen import canvas

    fixture_dir = tmp_path / "manual_real_docs"
    output_dir = tmp_path / "phase3"
    fixture_dir.mkdir()

    def write_pdf(path, pages):
        c = canvas.Canvas(str(path))
        for lines in pages:
            y = 740
            for line in lines:
                c.drawString(72, y, line)
                y -= 18
            c.showPage()
        c.save()

    write_pdf(
        fixture_dir / "clean_annual_report.pdf",
        [
            [f"Annual filler page {page}"]
            for page in range(1, 5)
        ]
        + [["Message From the President and Chief Executive Officer", "2024 CWB Alberta"]],
    )
    write_pdf(fixture_dir / "scanned_image_only.pdf", [["Scanned image placeholder"]])
    write_pdf(
        fixture_dir / "table_heavy_sample_tables.pdf",
        [["Table 4: table 3 with column headers added", "Daniel Radcliffe"]],
    )
    (fixture_dir / "MANIFEST.json").write_text(
        '{"web_pdfs":{"clean_annual_report.pdf":"https://example.com/a.pdf",'
        '"scanned_image_only.pdf":"https://example.com/s.pdf",'
        '"table_heavy_sample_tables.pdf":"https://example.com/t.pdf"}}',
        encoding="utf-8",
    )

    case = generate_real_mixed_routing_pdf_case(fixture_dir, output_dir)

    assert case.document_class == "mixed_routing"
    assert case.sample_id == "real_mixed_public_pages"
    assert case.pdf_path.is_file()
    assert case.metadata["expected_segments"] == [
        {"page_range": "1", "engine": "liteparse_pdf"},
        {"page_range": "2-3", "engine": "marker_pdf"},
    ]
    assert case.metadata["source_pages"] == {
        "clean_annual_report.pdf": 5,
        "scanned_image_only.pdf": 1,
        "table_heavy_sample_tables.pdf": 1,
    }
    assert case.metadata["source_urls"]["clean_annual_report.pdf"] == "https://example.com/a.pdf"
    assert len(case.reference_table) == 3
