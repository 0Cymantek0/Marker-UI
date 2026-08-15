"""Tests for Phase 3 benchmark script helpers."""

from __future__ import annotations

from app.benchmark.runner import PdfBenchmarkCase, PdfEngineOutput
from scripts.run_phase3_pdf_benchmark import (
    _merge_marker_json_table_evidence,
    _mixed_routing_engine,
)


def test_merge_marker_json_table_evidence_prefers_json_tables(tmp_path):
    case = PdfBenchmarkCase(
        sample_id="table_heavy",
        pdf_path="fixtures/table_heavy.pdf",
        document_class="table_heavy",
        reference_text="Quarter Q1 revenue 100.",
    )
    markdown_output = PdfEngineOutput(
        text="| Quarter | Revenue |\n| --- | --- |\n| Q1 | 100 |",
        table={"headers": ["Quarter"], "rows": [["Q1"]]},
        metadata={"tables": [{"source": "markdown_pipe_table"}]},
    )
    json_table = {
        "source": "marker_json_table",
        "headers": ["Quarter", "Revenue"],
        "rows": [["Q1", "100"]],
        "cells": [{"text": "Q1", "bbox": [72, 130, 200, 160]}],
    }
    json_output = PdfEngineOutput(
        text='{"children":[]}',
        table=[json_table],
        metadata={"tables": [json_table]},
    )

    merged = _merge_marker_json_table_evidence(
        markdown_output=markdown_output,
        json_output=json_output,
        case=case,
        output_dir=tmp_path,
    )

    assert merged.text == markdown_output.text
    assert merged.table == [json_table]
    assert merged.metadata["table"] == json_table
    assert merged.metadata["table_evidence"]["source"] == "marker_json_table_evidence"
    assert merged.metadata["marker_json_table_evidence"] == {
        "source": "marker_json_renderer",
        "table_count": 1,
        "artifact_path": "engine_json\\marker_pdf\\table_heavy.json",
    }
    assert (tmp_path / "engine_json" / "marker_pdf" / "table_heavy.json").is_file()


def test_merge_marker_json_table_evidence_keeps_markdown_without_json_tables(tmp_path):
    markdown_output = PdfEngineOutput(
        text="No table",
        table=None,
        metadata={},
    )
    case = PdfBenchmarkCase(
        sample_id="clean",
        pdf_path="fixtures/clean.pdf",
        document_class="clean_digital",
        reference_text="Clean",
    )

    merged = _merge_marker_json_table_evidence(
        markdown_output=markdown_output,
        json_output=PdfEngineOutput(text="{}", table=None, metadata={}),
        case=case,
        output_dir=tmp_path,
    )

    assert merged is markdown_output


def test_mixed_routing_engine_passes_probe_and_records_output(tmp_path, monkeypatch):
    case = PdfBenchmarkCase(
        sample_id="mixed",
        pdf_path=tmp_path / "mixed.pdf",
        document_class="mixed_routing",
        reference_text="Mixed",
    )
    case.pdf_path.write_bytes(b"%PDF")

    class _FakeProbe:
        def to_dict(self):
            return {"page_count": 2, "page_results": [{"page_number": 1}]}

    class _FakeService:
        def __init__(self):
            self.calls = []

        def convert_file(self, filepath, config):
            self.calls.append((filepath, config))
            return {
                "text": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                "metadata": {
                    "tables": [{"headers": ["A", "B"], "rows": [["1", "2"]]}],
                    "mixed_engine_segments": [
                        {"page_range": "1", "actual_engine": "liteparse_pdf"},
                        {"page_range": "2", "actual_engine": "marker_pdf"},
                    ],
                },
            }

    monkeypatch.setattr(
        "scripts.run_phase3_pdf_benchmark.probe_pdf",
        lambda _path: _FakeProbe(),
    )
    service = _FakeService()
    observations = {}
    output = _mixed_routing_engine(service, observations, tmp_path)(case)

    assert output.table == [{"headers": ["A", "B"], "rows": [["1", "2"]]}]
    assert "enable_mixed_pdf_routing" not in service.calls[0][1]
    assert service.calls[0][1]["probe_result"] == {
        "page_count": 2,
        "page_results": [{"page_number": 1}],
    }
    assert observations["mixed_pdf"]["mixed_routing:mixed"]["table_present"] is True
    assert (tmp_path / "engine_markdown" / "mixed_pdf" / "mixed.md").is_file()
