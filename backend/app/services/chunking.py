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
        for piece in _split_block(block.text, max_chars=max_chars, overlap_chars=overlap_chars):
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

    def flush(end_line: int) -> None:
        nonlocal buffer, block_start
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(
                MarkdownBlock(
                    text=text,
                    start_line=block_start,
                    end_line=end_line,
                    heading_path=tuple(title for _level, title in heading_stack),
                )
            )
        buffer = []

    for line_no, line in enumerate(lines, start=1):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush(line_no - 1)
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_stack = [(lvl, text) for lvl, text in heading_stack if lvl < level]
            heading_stack.append((level, title))
            block_start = line_no
            buffer = [line]
            flush(line_no)
            block_start = line_no + 1
            continue
        if not line.strip():
            flush(line_no - 1)
            block_start = line_no + 1
            continue
        if not buffer:
            block_start = line_no
        buffer.append(line)
    flush(len(lines))
    return blocks


def _split_block(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
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


def _chunk_id(source_name: str, index: int, text: str) -> str:
    digest = hashlib.sha1(f"{source_name}:{index}:{text}".encode("utf-8")).hexdigest()[:12]
    return f"chunk_{index:04d}_{digest}"
