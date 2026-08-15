"""Tests for Markitdown format sidecar generation script."""

from __future__ import annotations

import json
from dataclasses import dataclass

from scripts.generate_markitdown_format_outputs import generate_outputs


@dataclass
class _FakeResult:
    markdown: str


class _FakeMarkitdown:
    def convert_local(self, path: str) -> _FakeResult:
        return _FakeResult(f"# converted\n\nsource={path}")


def test_generate_markitdown_outputs_writes_markdown_and_metadata(tmp_path):
    fixture_dir = tmp_path / "manual_real_docs"
    fixture_dir.mkdir()
    for filename in [
        "manual_table_doc.docx",
        "manual_table_slide.pptx",
        "manual_workbook.xlsx",
        "manual_page.html",
        "manual_archive.zip",
    ]:
        (fixture_dir / filename).write_bytes(b"fixture")

    output_dir = tmp_path / "markitdown_outputs"
    results = generate_outputs(
        fixture_dir=fixture_dir,
        output_dir=output_dir,
        markitdown=_FakeMarkitdown(),
    )

    assert {result.status for result in results} == {"ok"}
    assert (output_dir / "docx_generated.md").is_file()
    metadata = json.loads((output_dir / "docx_generated.metadata.json").read_text())
    assert metadata["engine"]["engine"] == "markitdown"
    assert metadata["engine"]["needs_cloud"] is False
    assert "Users" not in metadata["source_path"]
    markdown = (output_dir / "docx_generated.md").read_text()
    assert "Users" not in markdown
    assert "manual_table_doc.docx" in markdown
