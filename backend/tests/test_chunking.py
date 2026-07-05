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
