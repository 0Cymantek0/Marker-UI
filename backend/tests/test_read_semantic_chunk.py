"""Tests for semantic chunk reading via the MCP/agent read surface.

The write side (build_chunks_envelope / chunk_markdown) produces a
``marker.chunks.v1`` JSON envelope. These tests prove the read side can
return chunk N by index with its structural metadata, instead of only
offset-based paging.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.chunking import build_chunks_envelope


def _write_chunks_output(tmp_path: Path) -> Path:
    """Write a real semantic chunks JSON file + manifest sidecar to disk."""
    markdown = (
        "# Title\n\n"
        "First paragraph here.\n\n"
        "## Details\n\n"
        "Second paragraph with detail.\n"
    )
    envelope = build_chunks_envelope(markdown, source_name="doc.md")
    out_file = tmp_path / "doc.chunks.json"
    out_file.write_text(envelope["text"], encoding="utf-8")
    # Manifest sidecar so _assert_output_read_permitted accepts the path.
    # _has_marker_output_manifest requires output.text_path/final_path to match.
    manifest = tmp_path / "doc.chunks.marker.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "marker.output_manifest.v1",
                "output": {"text_path": str(out_file.resolve())},
            }
        ),
        encoding="utf-8",
    )
    return out_file


def test_read_semantic_chunk_returns_chunk_by_index(tmp_path: Path) -> None:
    """read_semantic_chunk(path, chunk_index) must return the Nth semantic chunk."""
    from app.agent_api import read_semantic_chunk

    out_file = _write_chunks_output(tmp_path)

    result = read_semantic_chunk(str(out_file), chunk_index=0)

    assert result["chunk_kind"] == "semantic_markdown"
    assert result["is_semantic_chunk"] is True
    assert result["chunk_index"] == 0
    assert "text" in result and result["text"]
    assert result["chunk_count"] >= 1
    assert "heading_path" in result
    assert "id" in result


def test_read_semantic_chunk_out_of_range_raises(tmp_path: Path) -> None:
    """An out-of-range chunk_index must raise a clear not-found error."""
    from app.agent_api import read_semantic_chunk
    from app.errors import InputNotFoundError

    out_file = _write_chunks_output(tmp_path)

    with pytest.raises(InputNotFoundError):
        read_semantic_chunk(str(out_file), chunk_index=9999)


def test_read_semantic_chunk_negative_index_raises(tmp_path: Path) -> None:
    """Negative indexes must not silently clamp to the first chunk."""
    from app.agent_api import read_semantic_chunk

    out_file = _write_chunks_output(tmp_path)

    with pytest.raises(ValueError, match="chunk_index must be >= 0"):
        read_semantic_chunk(str(out_file), chunk_index=-1)


def test_read_semantic_chunk_rejects_non_chunks_file(tmp_path: Path) -> None:
    """A file without the chunks schema must be rejected, not silently paged."""
    from app.agent_api import read_semantic_chunk
    from app.errors import InputNotAllowedError

    plain = tmp_path / "notes.md"
    plain.write_text("# just markdown\n\nno chunk schema here", encoding="utf-8")
    manifest = tmp_path / "notes.marker.json"
    manifest.write_text(
        json.dumps({"schema_version": "marker.output_manifest.v1"}),
        encoding="utf-8",
    )

    with pytest.raises(InputNotAllowedError):
        read_semantic_chunk(str(plain), chunk_index=0)


# ---------------------------------------------------------------------------
# B2: read_output_chunk dispatcher (mode = offset | semantic)
#
# The MCP tool marker_read_output_chunk must support a ``mode`` parameter so an
# agent can read semantic chunk N from a marker.chunks.v1 envelope instead of
# only doing offset paging. Default mode stays "offset" for backward compat.
# ---------------------------------------------------------------------------


def test_read_output_chunk_default_mode_is_offset_paging(tmp_path: Path) -> None:
    """mode unset (default) must return offset_text paging, not semantic."""
    from app.agent_api import read_output_chunk

    out_file = _write_chunks_output(tmp_path)
    result = read_output_chunk(str(out_file))  # no mode -> offset default

    assert result["chunk_kind"] == "offset_text"
    assert result["is_semantic_chunk"] is False


def test_read_output_chunk_semantic_mode_returns_chunk(tmp_path: Path) -> None:
    """mode='semantic' must dispatch to read_semantic_chunk."""
    from app.agent_api import read_output_chunk

    out_file = _write_chunks_output(tmp_path)
    result = read_output_chunk(str(out_file), mode="semantic", chunk_index=1)

    assert result["is_semantic_chunk"] is True
    assert result["chunk_index"] == 1
    assert result["chunk_count"] >= 2


def test_read_output_chunk_offset_mode_works_on_plain_markdown(tmp_path: Path) -> None:
    """mode='offset' must work on a plain markdown file (no chunks schema)."""
    from app.agent_api import read_output_chunk

    plain = tmp_path / "notes.md"
    plain.write_text("# plain\n\nbody text here", encoding="utf-8")
    manifest = tmp_path / "notes.marker.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "marker.output_manifest.v1",
                "output": {"text_path": str(plain)},
            }
        ),
        encoding="utf-8",
    )

    result = read_output_chunk(str(plain), mode="offset")
    assert result["chunk_kind"] == "offset_text"
    assert "body text" in result["text"]
