from __future__ import annotations

from app.services.chunking import SCHEMA_VERSION, chunk_markdown


def test_chunk_markdown_preserves_heading_paths_and_line_spans() -> None:
    payload = chunk_markdown(
        "# Title\n\nIntro paragraph.\n\n## Details\n\nFirst fact. Second fact.",
        source_name="doc.md",
        max_chars=200,
    )

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["chunk_kind"] == "semantic_markdown"
    assert payload["chunk_count"] == len(payload["chunks"])
    assert payload["chunks"][0]["heading_path"] == ["Title"]
    assert payload["chunks"][-1]["heading_path"] == ["Title", "Details"]
    assert payload["chunks"][-1]["start_line"] >= 5
    assert payload["chunks"][-1]["token_estimate"] > 0


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
    assert not any("Person 1 | Score 1 || Person" in chunk["text"] for chunk in table_chunks)


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
