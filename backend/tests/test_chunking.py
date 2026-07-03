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
