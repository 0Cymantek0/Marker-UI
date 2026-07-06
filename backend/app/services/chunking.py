"""Deterministic Markdown chunking for native Markdown-only converters."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
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
    char_start: int
    char_end: int
    heading_path: tuple[str, ...] = field(default_factory=tuple)
    kind: str = "text"
    content_types: tuple[str, ...] = field(default_factory=tuple)


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
        "chunking_strategy": "markdown_heading_blocks_v2",
        "source_format": "markdown",
        "source_sha256": payload["source"]["sha256"],
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
    source_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    line_offsets = _line_offsets(markdown)
    markdown_blocks = _markdown_blocks(markdown, line_offsets=line_offsets)
    chunk_blocks = _pack_chunk_blocks(
        [
            piece
            for block in markdown_blocks
            for piece in _split_block(
                block,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
                line_offsets=line_offsets,
            )
            if piece.text.strip()
        ],
        max_chars=max_chars,
        line_offsets=line_offsets,
    )
    for block in chunk_blocks:
        text = block.text.strip()
        if not text:
            continue
        index = len(chunks)
        chunk_id = _chunk_id(source_name, index, text)
        token_estimate = max(1, (len(text) + 3) // 4)
        heading_path = list(block.heading_path)
        content_types = list(block.content_types or (block.kind,))
        chunks.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "index": index,
                "text": text,
                "contextual_text": _contextual_text(text, heading_path),
                "heading_path": heading_path,
                "section_path": heading_path,
                "start_line": block.start_line,
                "end_line": block.end_line,
                "char_start": block.char_start,
                "char_end": block.char_end,
                "char_count": len(text),
                "token_estimate": token_estimate,
                "token_count": token_estimate,
                "content_types": content_types,
                "content_hash": f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
                "source_refs": [
                    {
                        "type": "markdown_line_span",
                        "source": source_name,
                        "start_line": block.start_line,
                        "end_line": block.end_line,
                        "char_start": block.char_start,
                        "char_end": block.char_end,
                        "heading_path": heading_path,
                        "content_types": content_types,
                    }
                ],
            }
        )
    for index, chunk in enumerate(chunks):
        chunk["previous_id"] = chunks[index - 1]["id"] if index > 0 else None
        chunk["next_id"] = chunks[index + 1]["id"] if index + 1 < len(chunks) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "chunk_kind": "semantic_markdown",
        "chunking_strategy": "markdown_heading_blocks_v2",
        "source": {
            "name": source_name,
            "sha256": source_sha256,
            "char_count": len(markdown),
        },
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def _markdown_blocks(markdown: str, *, line_offsets: list[tuple[int, int]]) -> list[MarkdownBlock]:
    lines = markdown.splitlines()
    blocks: list[MarkdownBlock] = []
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    block_start = 1

    def flush(end_line: int, *, kind: str = "text") -> None:
        nonlocal buffer, block_start
        text = "\n".join(buffer).strip()
        if text:
            char_start, char_end = _line_char_span(
                line_offsets,
                start_line=block_start,
                end_line=end_line,
            )
            blocks.append(
                MarkdownBlock(
                    text=text,
                    start_line=block_start,
                    end_line=end_line,
                    char_start=char_start,
                    char_end=char_end,
                    heading_path=tuple(title for _level, title in heading_stack),
                    kind=kind,
                    content_types=(kind,),
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


def _pack_chunk_blocks(
    blocks: list[MarkdownBlock],
    *,
    max_chars: int,
    line_offsets: list[tuple[int, int]],
) -> list[MarkdownBlock]:
    packed: list[MarkdownBlock] = []
    pending = deque(blocks)
    current: list[MarkdownBlock] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = "\n\n".join(block.text for block in current).strip()
        if text:
            packed.append(
                MarkdownBlock(
                    text=text,
                    start_line=current[0].start_line,
                    end_line=current[-1].end_line,
                    char_start=current[0].char_start,
                    char_end=current[-1].char_end,
                    heading_path=_common_heading_path(current),
                    kind=current[0].kind if len(current) == 1 else "mixed",
                    content_types=_content_types(current),
                )
            )
        current = []

    while pending:
        block = pending.popleft()
        if block.kind == "heading" and current:
            flush()
        projected_len = len("\n\n".join([*(item.text for item in current), block.text]))
        if current and projected_len > max_chars:
            if _is_standalone_heading(current) and block.kind == "text":
                heading_budget = max_chars - len(current[0].text) - 2
                if heading_budget > 0 and len(block.text) > heading_budget:
                    head_block, tail_block = _split_block_once(
                        block,
                        max_chars=heading_budget,
                        line_offsets=line_offsets,
                    )
                    current.append(head_block)
                    if tail_block is not None:
                        pending.appendleft(tail_block)
                    flush()
                    continue
                current.append(block)
                flush()
                continue
            flush()
        current.append(block)
        if block.kind in {"fenced_code", "table"}:
            flush()
    flush()
    return packed


def _is_standalone_heading(blocks: list[MarkdownBlock]) -> bool:
    return len(blocks) == 1 and blocks[0].kind == "heading"


def _common_heading_path(blocks: list[MarkdownBlock]) -> tuple[str, ...]:
    if not blocks:
        return ()
    common = list(blocks[0].heading_path)
    for block in blocks[1:]:
        next_path = list(block.heading_path)
        prefix_len = 0
        for left, right in zip(common, next_path):
            if left != right:
                break
            prefix_len += 1
        common = common[:prefix_len]
        if not common:
            break
    return tuple(common)


def _content_types(blocks: list[MarkdownBlock]) -> tuple[str, ...]:
    values: list[str] = []
    for block in blocks:
        for kind in block.content_types or (block.kind,):
            if kind not in values:
                values.append(kind)
    return tuple(values)


def _split_block(
    block: MarkdownBlock,
    *,
    max_chars: int,
    overlap_chars: int,
    line_offsets: list[tuple[int, int]],
) -> list[MarkdownBlock]:
    text = block.text
    if len(text) <= max_chars:
        return [block]
    if block.kind == "table":
        pieces = _split_table_block(text, max_chars=max_chars)
        return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)
    if block.kind == "fenced_code":
        pieces = _split_fenced_code_block(text, max_chars=max_chars)
        return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)
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
    return _locate_split_pieces(block, pieces, overlap_chars=overlap_chars, line_offsets=line_offsets)


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
    row_limit = max_chars - len(header_text) - 1
    if row_limit < 1:
        return _split_text_by_chars(text, max_chars=max_chars)
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
            for row_piece in _split_text_by_chars(row, max_chars=row_limit):
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
    body_limit = max_chars - overhead
    if body_limit < 1:
        return _split_text_by_chars(text, max_chars=max_chars)
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
    limit = max(1, max_chars)
    pieces: list[str] = []
    remaining = text
    while remaining:
        pieces.append(remaining[:limit].rstrip())
        remaining = remaining[limit:].lstrip()
    return [piece for piece in pieces if piece]


def _locate_split_pieces(
    block: MarkdownBlock,
    pieces: list[str],
    *,
    overlap_chars: int,
    line_offsets: list[tuple[int, int]],
) -> list[MarkdownBlock]:
    located: list[MarkdownBlock] = []
    search_from = 0
    for raw_piece in pieces:
        piece = raw_piece.strip()
        if not piece:
            continue
        start = block.text.find(piece, search_from)
        if start < 0:
            start = block.text.find(piece)
        if start < 0:
            start = 0
            end = len(block.text)
        else:
            end = start + len(piece)
        located.append(_block_piece(block, piece, start, end, line_offsets=line_offsets))
        search_from = max(start + 1, end - overlap_chars)
    return located


def _split_block_once(
    block: MarkdownBlock,
    *,
    max_chars: int,
    line_offsets: list[tuple[int, int]],
) -> tuple[MarkdownBlock, MarkdownBlock | None]:
    head_text, tail_text = _split_text_at_limit(block.text, max_chars=max_chars)
    head = _block_piece(block, head_text, 0, len(head_text), line_offsets=line_offsets)
    if not tail_text.strip():
        return head, None
    tail_start = block.text.find(tail_text, len(head_text))
    if tail_start < 0:
        tail_start = len(block.text) - len(tail_text)
    tail = _block_piece(
        block,
        tail_text,
        max(0, tail_start),
        max(0, tail_start) + len(tail_text),
        line_offsets=line_offsets,
    )
    return head, tail


def _block_piece(
    block: MarkdownBlock,
    text: str,
    rel_start: int,
    rel_end: int,
    *,
    line_offsets: list[tuple[int, int]],
) -> MarkdownBlock:
    stripped = text.strip()
    leading_trim = len(text) - len(text.lstrip())
    trailing_trim = len(text) - len(text.rstrip())
    piece_start = max(block.char_start, block.char_start + rel_start + leading_trim)
    piece_end = max(piece_start, block.char_start + rel_end - trailing_trim)
    start_line, end_line = _char_line_span(
        line_offsets,
        char_start=piece_start,
        char_end=piece_end,
    )
    return MarkdownBlock(
        text=stripped,
        start_line=start_line,
        end_line=end_line,
        char_start=piece_start,
        char_end=piece_end,
        heading_path=block.heading_path,
        kind=block.kind,
        content_types=block.content_types,
    )


def _split_text_at_limit(text: str, *, max_chars: int) -> tuple[str, str]:
    """Split text once, preferring whitespace before the hard limit."""

    if len(text) <= max_chars:
        return text, ""
    limit = max(1, max_chars)
    split_at = text.rfind(" ", 0, limit + 1)
    if split_at < max(1, limit // 2):
        split_at = limit
    head = text[:split_at].rstrip()
    tail = text[split_at:].lstrip()
    return head, tail


def _line_offsets(markdown: str) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    lines = markdown.splitlines(keepends=True)
    if not lines and markdown == "":
        return [(0, 0)]
    for line in lines:
        start = cursor
        cursor += len(line)
        end = cursor
        while end > start and line[end - start - 1] in "\r\n":
            end -= 1
        offsets.append((start, end))
    if markdown and markdown[-1] in "\r\n":
        offsets.append((cursor, cursor))
    return offsets


def _line_char_span(
    line_offsets: list[tuple[int, int]],
    *,
    start_line: int,
    end_line: int,
) -> tuple[int, int]:
    if not line_offsets:
        return 0, 0
    start_index = min(max(start_line - 1, 0), len(line_offsets) - 1)
    end_index = min(max(end_line - 1, start_index), len(line_offsets) - 1)
    return line_offsets[start_index][0], line_offsets[end_index][1]


def _char_line_span(
    line_offsets: list[tuple[int, int]],
    *,
    char_start: int,
    char_end: int,
) -> tuple[int, int]:
    if not line_offsets:
        return 1, 1
    start_pos = max(0, char_start)
    end_pos = max(start_pos, char_end - 1)
    start_line = 1
    end_line = len(line_offsets)
    for index, (line_start, line_end) in enumerate(line_offsets, start=1):
        if line_start <= start_pos <= line_end or start_pos < line_start:
            start_line = index
            break
    for index, (line_start, line_end) in enumerate(line_offsets, start=1):
        if line_start <= end_pos <= line_end or end_pos < line_start:
            end_line = index
            break
    return start_line, max(start_line, end_line)


def _contextual_text(text: str, heading_path: list[str]) -> str:
    if not heading_path:
        return text
    prefix = " > ".join(heading_path)
    return f"{prefix}\n\n{text}"


def _chunk_id(source_name: str, index: int, text: str) -> str:
    digest = hashlib.sha1(f"{source_name}:{index}:{text}".encode("utf-8")).hexdigest()[:12]
    return f"chunk_{index:04d}_{digest}"
