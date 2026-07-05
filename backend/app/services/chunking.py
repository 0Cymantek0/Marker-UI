"""Deterministic Markdown chunking for native Markdown-only converters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "marker.chunks.v1"
DEFAULT_MAX_CHARS = 1800
DEFAULT_OVERLAP_CHARS = 160


@dataclass(frozen=True)
class MarkdownBlock:
    text: str
    start_line: int
    end_line: int
    heading_path: tuple[str, ...] = field(default_factory=tuple)
    kind: str = "text"


def build_chunks_envelope(
    markdown: str,
    *,
    source_name: str,
    metadata: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> dict[str, Any]:
    """Return a legacy conversion envelope containing JSON chunk payload."""

    payload = chunk_markdown(
        markdown,
        source_name=source_name,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )
    chunk_metadata = {
        "schema_version": SCHEMA_VERSION,
        "chunk_kind": "semantic_markdown",
        "source_format": "markdown",
        "chunk_count": payload["chunk_count"],
        "max_chars": max_chars,
        "overlap_chars": overlap_chars,
    }
    return {
        "text": json.dumps(payload, ensure_ascii=False, indent=2),
        "extension": "json",
        "images": {},
        "metadata": {
            **(metadata or {}),
            "chunking": chunk_metadata,
        },
    }


def chunk_markdown(
    markdown: str,
    *,
    source_name: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> dict[str, Any]:
    """Chunk Markdown by headings and paragraphs, splitting oversized blocks.

    This is a deterministic fallback for converters that only emit Markdown.
    It preserves heading paths and line spans so downstream RAG tooling can cite
    source-adjacent context without confusing this with Marker's native chunk
    renderer.
    """

    max_chars = max(200, int(max_chars or DEFAULT_MAX_CHARS))
    overlap_chars = max(0, min(int(overlap_chars or 0), max_chars // 3))
    chunks: list[dict[str, Any]] = []
    for block in _markdown_blocks(markdown):
        for piece in _split_block(block, max_chars=max_chars, overlap_chars=overlap_chars):
            text = piece.strip()
            if not text:
                continue
            index = len(chunks)
            chunks.append(
                {
                    "id": _chunk_id(source_name, index, text),
                    "index": index,
                    "text": text,
                    "heading_path": list(block.heading_path),
                    "start_line": block.start_line,
                    "end_line": block.end_line,
                    "char_count": len(text),
                    "token_estimate": max(1, (len(text) + 3) // 4),
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "chunk_kind": "semantic_markdown",
        "source": {"name": source_name},
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def _markdown_blocks(markdown: str) -> list[MarkdownBlock]:
    lines = markdown.splitlines()
    blocks: list[MarkdownBlock] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    block_start = 1

    def flush(end_line: int, *, kind: str = "text") -> None:
        nonlocal buffer, block_start
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(
                MarkdownBlock(
                    text=text,
                    start_line=block_start,
                    end_line=end_line,
                    heading_path=tuple(title for _level, title in heading_stack),
                    kind=kind,
                )
            )
        buffer = []

    line_no = 1
    while line_no <= len(lines):
        line = lines[line_no - 1]
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush(line_no - 1)
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_stack = [(lvl, text) for lvl, text in heading_stack if lvl < level]
            heading_stack.append((level, title))
            block_start = line_no
            buffer = [line]
            flush(line_no, kind="heading")
            block_start = line_no + 1
            line_no += 1
            continue

        fence = _fence_marker(line)
        if fence:
            flush(line_no - 1)
            block_start = line_no
            fence_lines = [line]
            line_no += 1
            while line_no <= len(lines):
                fence_lines.append(lines[line_no - 1])
                if _is_matching_fence(lines[line_no - 1], fence):
                    break
                line_no += 1
            buffer = fence_lines
            flush(min(line_no, len(lines)), kind="fenced_code")
            block_start = line_no + 1
            line_no += 1
            continue

        if _looks_like_table_start(lines, line_no - 1):
            flush(line_no - 1)
            block_start = line_no
            table_lines = [line, lines[line_no]]
            line_no += 2
            while line_no <= len(lines):
                candidate = lines[line_no - 1]
                if not candidate.strip() or "|" not in candidate:
                    break
                table_lines.append(candidate)
                line_no += 1
            buffer = table_lines
            flush(line_no - 1, kind="table")
            block_start = line_no
            continue
        if not line.strip():
            flush(line_no - 1)
            block_start = line_no + 1
            line_no += 1
            continue
        if not buffer:
            block_start = line_no
        buffer.append(line)
        line_no += 1
    flush(len(lines))
    return blocks


def _split_block(block: MarkdownBlock, *, max_chars: int, overlap_chars: int) -> list[str]:
    text = block.text
    if len(text) <= max_chars:
        return [text]
    if block.kind == "table":
        return _split_table_block(text, max_chars=max_chars)
    if block.kind == "fenced_code":
        return _split_fenced_code_block(text, max_chars=max_chars)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            prefix = current[-overlap_chars:].strip() if overlap_chars else ""
            current = f"{prefix} {sentence}".strip() if prefix else sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence
        while len(current) > max_chars:
            pieces.append(current[:max_chars].rstrip())
            prefix = current[max(0, max_chars - overlap_chars) : max_chars].strip() if overlap_chars else ""
            current = f"{prefix} {current[max_chars:]}".strip() if prefix else current[max_chars:].strip()
    if current:
        pieces.append(current)
    return pieces


def _fence_marker(line: str) -> str | None:
    match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
    return match.group(1) if match else None


def _is_matching_fence(line: str, opener: str) -> bool:
    marker = _fence_marker(line)
    return bool(marker and opener and marker[0] == opener[0] and len(marker) >= len(opener))


def _looks_like_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if "|" not in header or "|" not in separator:
        return False
    return bool(re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", separator))


def _split_table_block(text: str, *, max_chars: int) -> list[str]:
    lines = text.splitlines()
    if len(lines) <= 2:
        return _split_text_by_chars(text, max_chars=max_chars)

    header = lines[:2]
    rows = lines[2:]
    header_text = "\n".join(header)
    pieces: list[str] = []
    current_rows: list[str] = []

    def flush_rows() -> None:
        nonlocal current_rows
        if current_rows:
            pieces.append("\n".join([*header, *current_rows]))
            current_rows = []

    for row in rows:
        candidate = "\n".join([*header, *current_rows, row])
        if current_rows and len(candidate) > max_chars:
            flush_rows()
            candidate = "\n".join([*header, row])
        if len(candidate) > max_chars:
            flush_rows()
            for row_piece in _split_text_by_chars(row, max_chars=max_chars - len(header_text) - 1):
                pieces.append("\n".join([*header, row_piece]))
        else:
            current_rows.append(row)
    flush_rows()
    return pieces or [text]


def _split_fenced_code_block(text: str, *, max_chars: int) -> list[str]:
    lines = text.splitlines()
    if len(lines) < 2:
        return _split_text_by_chars(text, max_chars=max_chars)

    opener = lines[0]
    closer = lines[-1] if _is_matching_fence(lines[-1], _fence_marker(opener) or "") else lines[0][:3]
    body = lines[1:-1] if closer == lines[-1] else lines[1:]
    overhead = len(opener) + len(closer) + 2
    body_limit = max(40, max_chars - overhead)
    pieces: list[str] = []
    current: list[str] = []

    def flush_code() -> None:
        nonlocal current
        if current:
            pieces.append("\n".join([opener, *current, closer]))
            current = []

    for line in body:
        candidate = "\n".join([*current, line])
        if current and len(candidate) > body_limit:
            flush_code()
            candidate = line
        if len(candidate) > body_limit:
            flush_code()
            for line_piece in _split_text_by_chars(line, max_chars=body_limit):
                pieces.append("\n".join([opener, line_piece, closer]))
        else:
            current.append(line)
    flush_code()
    return pieces or [text]


def _split_text_by_chars(text: str, *, max_chars: int) -> list[str]:
    limit = max(40, max_chars)
    pieces: list[str] = []
    remaining = text
    while remaining:
        pieces.append(remaining[:limit].rstrip())
        remaining = remaining[limit:].lstrip()
    return [piece for piece in pieces if piece]


def _chunk_id(source_name: str, index: int, text: str) -> str:
    digest = hashlib.sha1(f"{source_name}:{index}:{text}".encode("utf-8")).hexdigest()[:12]
    return f"chunk_{index:04d}_{digest}"
