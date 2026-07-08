from __future__ import annotations

import json

import pytest

from app.services.chunking import SCHEMA_VERSION, build_chunks_envelope, chunk_markdown, chunk_markdown_with_strategy


def test_chunk_markdown_preserves_heading_paths_and_line_spans() -> None:
    markdown = "# Title\n\nIntro paragraph.\n\n## Details\n\nFirst fact. Second fact."
    payload = chunk_markdown(
        markdown,
        source_name="doc.md",
        max_chars=200,
    )

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["chunk_kind"] == "semantic_markdown"
    assert payload["chunking_strategy"] == "markdown_heading_blocks_v2"
    assert payload["chunk_count"] == len(payload["chunks"])
    assert payload["chunks"][0]["heading_path"] == ["Title"]
    assert payload["chunks"][-1]["heading_path"] == ["Title", "Details"]
    assert payload["chunks"][-1]["start_line"] >= 5
    assert payload["chunks"][-1]["token_estimate"] > 0
    assert payload["source"]["sha256"]
    assert payload["source"]["char_count"] == len(markdown)
    assert payload["chunks"][-1]["content_hash"].startswith("sha256:")
    assert payload["chunks"][-1]["chunk_id"] == payload["chunks"][-1]["id"]
    assert payload["chunks"][-1]["section_path"] == ["Title", "Details"]
    assert payload["chunks"][-1]["contextual_text"].startswith("Title > Details")
    assert payload["chunks"][-1]["token_count"] == payload["chunks"][-1]["token_estimate"]
    assert payload["chunks"][-1]["char_start"] < payload["chunks"][-1]["char_end"]
    assert payload["chunks"][-1]["source_refs"] == [
        {
            "type": "markdown_line_span",
            "source": "doc.md",
            "start_line": payload["chunks"][-1]["start_line"],
            "end_line": payload["chunks"][-1]["end_line"],
            "char_start": payload["chunks"][-1]["char_start"],
            "char_end": payload["chunks"][-1]["char_end"],
            "heading_path": ["Title", "Details"],
            "content_types": payload["chunks"][-1]["content_types"],
        }
    ]


def test_chunk_markdown_split_text_uses_tight_source_spans() -> None:
    markdown = "# Long\n\n" + " ".join(f"term{i}" for i in range(80))
    payload = chunk_markdown(
        markdown,
        source_name="long.md",
        max_chars=200,
        overlap_chars=0,
    )

    chunks = payload["chunks"]

    assert len(chunks) > 2
    assert len({(chunk["char_start"], chunk["char_end"]) for chunk in chunks}) == len(chunks)
    assert chunks[0]["char_start"] == 0
    assert chunks[0]["char_end"] < len(markdown)
    assert chunks[-1]["char_start"] > chunks[0]["char_start"]
    assert chunks[-1]["char_end"] == len(markdown)
    for chunk in chunks:
        source_slice = markdown[chunk["char_start"] : chunk["char_end"]]
        assert source_slice == chunk["text"]
        assert chunk["source_refs"][0]["char_start"] == chunk["char_start"]
        assert chunk["source_refs"][0]["char_end"] == chunk["char_end"]


def test_build_chunks_envelope_falls_back_when_optional_strategy_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_import(name: str):
        if name.startswith("unstructured."):
            raise ImportError("missing optional dependency")
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr("app.services.chunking.importlib.import_module", fail_import)

    envelope = build_chunks_envelope(
        "# Title\n\nBody.",
        source_name="doc.md",
        strategy="unstructured_by_title",
    )

    payload = json.loads(envelope["text"])
    assert payload["chunking_strategy"] == "markdown_heading_blocks_v2"
    assert payload["chunking_strategy_requested"] == "unstructured_by_title"
    assert "ImportError" in payload["chunking_fallback_reason"]
    assert envelope["metadata"]["chunking"]["requested_strategy"] == "unstructured_by_title"
    assert "fallback_reason" in envelope["metadata"]["chunking"]


def test_chunk_markdown_unstructured_by_title_strategy_when_available() -> None:
    pytest.importorskip("unstructured.partition.md")
    pytest.importorskip("unstructured.chunking.title")

    payload = chunk_markdown_with_strategy(
        "# Title\n\nIntro paragraph.\n\n## Details\n\nFirst fact. Second fact.",
        source_name="doc.md",
        max_chars=200,
        strategy="unstructured_by_title",
    )

    assert payload["chunking_strategy"] == "unstructured_by_title"
    assert payload["chunk_count"] == 2
    assert payload["chunks"][0]["text"] == "Title\n\nIntro paragraph."
    assert payload["chunks"][0]["char_start"] == 0
    assert payload["chunks"][0]["source_refs"][0]["source"] == "doc.md"


def test_chunk_markdown_unstructured_by_title_locates_repeated_sections() -> None:
    pytest.importorskip("unstructured.partition.md")
    pytest.importorskip("unstructured.chunking.title")
    markdown = "# Repeat\n\nSame body.\n\n# Repeat\n\nSame body."

    payload = chunk_markdown_with_strategy(
        markdown,
        source_name="repeat.md",
        max_chars=200,
        strategy="unstructured_by_title",
    )

    chunks = payload["chunks"]
    assert payload["chunking_strategy"] == "unstructured_by_title"
    assert [chunk["text"] for chunk in chunks] == ["Repeat\n\nSame body.", "Repeat\n\nSame body."]
    assert [(chunk["start_line"], chunk["end_line"]) for chunk in chunks] == [(1, 3), (5, 7)]
    assert chunks[0]["char_start"] < chunks[1]["char_start"]
    assert chunks[0]["stable_id"] != chunks[1]["stable_id"]
    assert chunks[1]["metadata"]["stable_id"] == chunks[1]["stable_id"]
    assert markdown[chunks[1]["char_start"] : chunks[1]["char_end"]] == "# Repeat\n\nSame body."


def test_chunk_markdown_packs_headings_with_section_text() -> None:
    payload = chunk_markdown(
        "# Title\n\nIntro paragraph.\n\nMore context.\n\n## Details\n\nFirst fact.\n\nSecond fact.",
        source_name="doc.md",
        max_chars=240,
    )

    texts = [chunk["text"] for chunk in payload["chunks"]]

    assert not any(text.strip() in {"# Title", "## Details"} for text in texts)
    assert any(text.startswith("# Title\n\nIntro paragraph.") for text in texts)
    assert any(text.startswith("## Details\n\nFirst fact.") for text in texts)
    assert payload["chunks"][0]["start_line"] == 1
    assert payload["chunks"][0]["end_line"] >= 3


def test_chunk_markdown_keeps_oversized_section_heading_with_first_content() -> None:
    payload = chunk_markdown(
        "# Long Section\n\n" + ("alpha " * 80),
        source_name="long.md",
        max_chars=200,
    )

    texts = [chunk["text"] for chunk in payload["chunks"]]

    assert texts[0].startswith("# Long Section\n\nalpha")
    assert "# Long Section" not in texts[1:]
    assert all(text.strip() != "# Long Section" for text in texts)
    assert all(chunk["char_count"] <= 200 for chunk in payload["chunks"])


def test_chunk_markdown_respects_size_budget_when_heading_precedes_long_sentence() -> None:
    payload = chunk_markdown(
        "# Budget\n\n" + ("word " * 120),
        source_name="budget.md",
        max_chars=200,
        overlap_chars=0,
    )

    chunks = payload["chunks"]

    assert chunks[0]["text"].startswith("# Budget\n\nword")
    assert all(chunk["char_count"] <= 200 for chunk in chunks)
    assert not any(chunk["text"].strip() == "# Budget" for chunk in chunks)


def test_chunk_markdown_adds_neighbor_links_for_semantic_navigation() -> None:
    payload = chunk_markdown(
        "# One\n\n" + ("alpha " * 60) + "\n\n## Two\n\n" + ("beta " * 60),
        source_name="linked.md",
        max_chars=200,
    )

    chunks = payload["chunks"]

    assert len(chunks) >= 3
    assert chunks[0]["previous_id"] is None
    assert chunks[0]["next_id"] == chunks[1]["id"]
    assert chunks[1]["previous_id"] == chunks[0]["id"]
    assert chunks[-1]["next_id"] is None


def test_chunk_markdown_keeps_fenced_code_with_blank_lines_together() -> None:
    payload = chunk_markdown(
        "# Notes\n\n```python\nprint('first')\n\nprint('second')\n```\n\nDone.",
        source_name="code.md",
        max_chars=400,
    )

    code_chunks = [chunk for chunk in payload["chunks"] if "```python" in chunk["text"]]

    assert len(code_chunks) == 1
    assert "\n\nprint('second')" in code_chunks[0]["text"]
    assert code_chunks[0]["text"].endswith("```")


def test_chunk_markdown_splits_large_fenced_code_into_balanced_fences() -> None:
    body = "\n".join(f"print({i!r})" for i in range(80))
    payload = chunk_markdown(
        f"```python\n{body}\n```",
        source_name="large-code.md",
        max_chars=220,
    )

    code_chunks = payload["chunks"]

    assert len(code_chunks) > 1
    assert all(chunk["text"].startswith("```python\n") for chunk in code_chunks)
    assert all(chunk["text"].endswith("\n```") for chunk in code_chunks)
    assert all(chunk["char_count"] <= 220 for chunk in code_chunks)


def test_chunk_markdown_splits_large_tables_on_rows_and_repeats_header() -> None:
    rows = "\n".join(f"| Person {i} | Score {i} |" for i in range(30))
    payload = chunk_markdown(
        f"# Scores\n\n| Name | Score |\n| --- | --- |\n{rows}",
        source_name="scores.md",
        max_chars=220,
    )

    table_chunks = [chunk for chunk in payload["chunks"] if "| Name | Score |" in chunk["text"]]

    assert len(table_chunks) > 1
    assert all("| Name | Score |\n| --- | --- |" in chunk["text"] for chunk in table_chunks)
    assert all(chunk["char_count"] <= 220 for chunk in table_chunks)
    assert all("table" in chunk["content_types"] for chunk in table_chunks)
    assert not any("Person 1 | Score 1 || Person" in chunk["text"] for chunk in table_chunks)


def test_chunk_markdown_emits_rag_ready_context_and_char_spans() -> None:
    markdown = (
        "# Handbook\n\n"
        "Intro.\n\n"
        "## Install\n\n"
        "Run setup.\n\n"
        "```bash\nmarker --help\n```\n"
    )
    payload = chunk_markdown(markdown, source_name="handbook.md", max_chars=260)

    chunks = payload["chunks"]

    assert all(chunk["chunk_id"] == chunk["id"] for chunk in chunks)
    assert all(chunk["char_start"] <= chunk["char_end"] <= len(markdown) for chunk in chunks)
    assert any(chunk["section_path"] == ["Handbook", "Install"] for chunk in chunks)
    code_chunk = next(chunk for chunk in chunks if "marker --help" in chunk["text"])
    assert "fenced_code" in code_chunk["content_types"]
    assert code_chunk["contextual_text"].startswith("Handbook > Install")
    assert code_chunk["source_refs"][0]["char_start"] == code_chunk["char_start"]


def test_chunk_markdown_respects_size_budget_when_table_header_is_oversized() -> None:
    long_header = "| " + ("Column " * 40) + "|"
    payload = chunk_markdown(
        f"{long_header}\n| --- |\n| value |",
        source_name="wide-table.md",
        max_chars=200,
    )

    assert payload["chunk_count"] > 1
    assert all(chunk["char_count"] <= 200 for chunk in payload["chunks"])


def test_chunk_markdown_respects_size_budget_when_code_fence_info_is_oversized() -> None:
    long_info = "python " + ("metadata " * 30)
    payload = chunk_markdown(
        f"```{long_info}\nprint('ok')\n```",
        source_name="long-fence.md",
        max_chars=200,
    )

    assert payload["chunk_count"] > 1
    assert all(chunk["char_count"] <= 200 for chunk in payload["chunks"])


def test_chunk_markdown_tags_structural_blocks_and_extracts_assets() -> None:
    markdown = (
        "---\n"
        "title: Demo\n"
        "---\n"
        "# Guide\n\n"
        'Intro with ![Diagram](images/flow.png "Flow") and [docs](https://example.com/docs).\n\n'
        "> Quote one\n"
        ">\n"
        "> Quote two with <https://example.com/q>.\n\n"
        "- [x] First item\n"
        "  continued detail\n\n"
        "  loose continuation\n"
        "- Second item\n\n"
        "<div>\n"
        '  <img src="assets/chart.png" alt="Chart">\n'
        '  <a href="https://example.com/html">HTML Link</a>\n'
        "</div>\n"
    )

    payload = chunk_markdown(markdown, source_name="assets.md", max_chars=700)
    chunks = payload["chunks"]

    assert chunks[0]["content_types"] == ["front_matter"]
    body_chunk = chunks[1]
    assert body_chunk["heading_path"] == ["Guide"]
    assert "blockquote" in body_chunk["content_types"]
    assert "list" in body_chunk["content_types"]
    assert "task_list" in body_chunk["content_types"]
    assert "html_block" in body_chunk["content_types"]
    assert "- [x] First item\n  continued detail\n\n  loose continuation\n- Second item" in body_chunk["text"]
    assert {asset["target"] for asset in body_chunk["asset_refs"]} == {
        "images/flow.png",
        "https://example.com/docs",
        "https://example.com/q",
        "assets/chart.png",
        "https://example.com/html",
    }
    assert body_chunk["metadata"]["asset_refs"] == body_chunk["asset_refs"]


def test_chunk_markdown_does_not_treat_unclosed_front_matter_as_document() -> None:
    payload = chunk_markdown("---\n# Title\n\nBody.", source_name="not-front-matter.md", max_chars=200)

    assert "front_matter" not in payload["chunks"][0]["content_types"]
    assert payload["chunks"][-1]["heading_path"] == ["Title"]


def test_chunk_markdown_repeated_table_headers_use_row_source_spans() -> None:
    rows = "\n".join(f"| Person {i} | Score {i} |" for i in range(30))
    markdown = f"# Scores\n\n| Name | Score |\n| --- | --- |\n{rows}"

    payload = chunk_markdown(markdown, source_name="scores.md", max_chars=220)
    table_chunks = [chunk for chunk in payload["chunks"] if "| Name | Score |" in chunk["text"]]

    assert len(table_chunks) > 1
    later_chunk = table_chunks[1]
    assert later_chunk["start_line"] > table_chunks[0]["start_line"]
    assert later_chunk["char_end"] < len(markdown)
    assert later_chunk["source_refs"][0]["start_line"] == 3
    assert later_chunk["source_refs"][1]["start_line"] == later_chunk["start_line"]
    assert markdown[later_chunk["source_refs"][1]["char_start"] : later_chunk["source_refs"][1]["char_end"]].startswith(
        "| Person"
    )


def test_chunk_markdown_repeated_code_fences_use_body_source_spans() -> None:
    body = "\n".join(f"print({i!r})" for i in range(80))
    markdown = f"```python\n{body}\n```"

    payload = chunk_markdown(markdown, source_name="large-code.md", max_chars=220)
    second_chunk = payload["chunks"][1]

    assert second_chunk["text"].startswith("```python\n")
    assert second_chunk["text"].endswith("\n```")
    assert second_chunk["start_line"] > 2
    assert len(second_chunk["source_refs"]) == 3
    assert second_chunk["source_refs"][0]["start_line"] == 1
    assert second_chunk["source_refs"][1]["start_line"] == second_chunk["start_line"]
    assert second_chunk["source_refs"][2]["end_line"] == 82


def test_chunk_markdown_splits_long_lists_on_lines_before_chars() -> None:
    markdown = "\n".join(f"- item {index} " + ("detail " * 8) for index in range(20))

    payload = chunk_markdown(markdown, source_name="list.md", max_chars=220, overlap_chars=0)

    assert payload["chunk_count"] > 1
    assert all(chunk["char_count"] <= 220 for chunk in payload["chunks"])
    assert all(chunk["text"].startswith("- item") for chunk in payload["chunks"])
    assert all("\nitem " not in chunk["text"] for chunk in payload["chunks"])


def test_chunk_markdown_stable_ids_do_not_depend_on_source_name() -> None:
    markdown = "# Title\n\nBody."

    first = chunk_markdown(markdown, source_name="first.md", max_chars=200)
    second = chunk_markdown(markdown, source_name="second.md", max_chars=200)

    assert first["chunks"][0]["id"] != second["chunks"][0]["id"]
    assert first["chunks"][0]["stable_id"] == second["chunks"][0]["stable_id"]
    assert first["chunks"][0]["metadata"]["stable_id"] == first["chunks"][0]["stable_id"]


def test_chunk_markdown_uses_page_markers_as_metadata_not_chunk_text() -> None:
    markdown = (
        "<!-- pages: 1-2 -->\n\n"
        "# First Segment\n\n"
        "Alpha content.\n\n"
        "<!-- pages: 3 -->\n\n"
        "## Second Segment\n\n"
        "Beta content."
    )

    payload = chunk_markdown(markdown, source_name="paged.md", max_chars=260)
    chunks = payload["chunks"]

    assert all("<!-- pages:" not in chunk["text"] for chunk in chunks)
    assert chunks[0]["page_numbers"] == [1, 2]
    assert chunks[0]["page_range"] == "1-2"
    assert chunks[0]["metadata"]["page_numbers"] == [1, 2]
    assert chunks[0]["source_refs"][0]["page_range"] == "1-2"
    assert chunks[1]["page_numbers"] == [3]
    assert chunks[1]["source_refs"][0]["page_numbers"] == [3]


def test_chunk_markdown_recognizes_setext_headings_and_indented_code_boundaries() -> None:
    markdown = (
        "Title\n"
        "=====\n\n"
        "Intro.\n\n"
        "Subhead\n"
        "-------\n\n"
        "    def call():\n"
        "        return 1\n\n"
        "After."
    )

    payload = chunk_markdown(markdown, source_name="setext.md", max_chars=260)
    chunks = payload["chunks"]

    assert chunks[0]["heading_path"] == ["Title"]
    code_chunk = next(chunk for chunk in chunks if "def call" in chunk["text"])
    assert code_chunk["heading_path"] == ["Title", "Subhead"]
    assert "indented_code" in code_chunk["content_types"]
    assert code_chunk["text"].startswith("Subhead\n-------\n\n    def call")


def test_chunk_markdown_long_table_row_refs_header_and_row_piece_only() -> None:
    long_cell = " ".join(f"value{i}" for i in range(80))
    markdown = f"| Key | Value |\n| --- | --- |\n| a | {long_cell} |"

    payload = chunk_markdown(markdown, source_name="wide-row.md", max_chars=220)
    chunks = payload["chunks"]

    assert len(chunks) > 1
    first = chunks[0]
    assert first["text"].startswith("| Key | Value |\n| --- | --- |\n| a |")
    assert len(first["source_refs"]) == 2
    assert [(ref["start_line"], ref["end_line"]) for ref in first["source_refs"]] == [(1, 2), (3, 3)]
    assert markdown[first["source_refs"][1]["char_start"] : first["source_refs"][1]["char_end"]].startswith("| a |")


def test_chunk_markdown_unclosed_large_code_fence_does_not_ref_fake_closer() -> None:
    body = "\n".join(f"print({i!r})" for i in range(80))
    markdown = f"```python\n{body}"

    payload = chunk_markdown(markdown, source_name="unclosed-code.md", max_chars=220)
    second = payload["chunks"][1]

    assert second["text"].endswith("\n```")
    assert len(second["source_refs"]) == 2
    assert second["source_refs"][0]["start_line"] == 1
    assert second["source_refs"][1]["start_line"] == second["start_line"]
    assert second["source_refs"][-1]["end_line"] < len(markdown.splitlines())
