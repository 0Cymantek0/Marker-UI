"""Deterministic Markdown chunking for native Markdown-only converters."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "marker.chunks.v1"
DEFAULT_MAX_CHARS = 1800
DEFAULT_OVERLAP_CHARS = 160
MARKDOWN_CHUNKING_STRATEGY = "markdown_heading_blocks_v2"
UNSTRUCTURED_CHUNKING_STRATEGY = "unstructured_by_title"


@dataclass(frozen=True)
class SourceSpan:
    start_line: int
    end_line: int
    char_start: int
    char_end: int
    page_numbers: tuple[int, ...] = field(default_factory=tuple)


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
    asset_refs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    source_spans: tuple[SourceSpan, ...] = field(default_factory=tuple)
    page_numbers: tuple[int, ...] = field(default_factory=tuple)


def build_chunks_envelope(
    markdown: str,
    *,
    source_name: str,
    metadata: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    strategy: str = MARKDOWN_CHUNKING_STRATEGY,
) -> dict[str, Any]:
    """Return a legacy conversion envelope containing JSON chunk payload."""

    payload = chunk_markdown_with_strategy(
        markdown,
        source_name=source_name,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        strategy=strategy,
    )
    resolved_strategy = str(payload.get("chunking_strategy") or MARKDOWN_CHUNKING_STRATEGY)
    chunk_metadata = {
        "schema_version": SCHEMA_VERSION,
        "chunk_kind": "semantic_markdown",
        "chunking_strategy": resolved_strategy,
        "requested_strategy": _normalize_strategy(strategy),
        "source_format": "markdown",
        "source_sha256": payload["source"]["sha256"],
        "chunk_count": payload["chunk_count"],
        "max_chars": max_chars,
        "overlap_chars": overlap_chars,
    }
    if payload.get("chunking_fallback_reason"):
        chunk_metadata["fallback_reason"] = payload["chunking_fallback_reason"]
    return {
        "text": json.dumps(payload, ensure_ascii=False, indent=2),
        "extension": "json",
        "images": {},
        "metadata": {
            **(metadata or {}),
            "chunking": chunk_metadata,
        },
    }


def chunk_markdown_with_strategy(
    markdown: str,
    *,
    source_name: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    strategy: str = MARKDOWN_CHUNKING_STRATEGY,
) -> dict[str, Any]:
    normalized = _normalize_strategy(strategy)
    if normalized == UNSTRUCTURED_CHUNKING_STRATEGY:
        try:
            return chunk_markdown_unstructured_by_title(
                markdown,
                source_name=source_name,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
        except Exception as exc:  # noqa: BLE001 - optional strategy must degrade.
            payload = chunk_markdown(
                markdown,
                source_name=source_name,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
            payload["chunking_strategy_requested"] = UNSTRUCTURED_CHUNKING_STRATEGY
            payload["chunking_fallback_reason"] = f"{type(exc).__name__}: {exc}"
            return payload
    return chunk_markdown(
        markdown,
        source_name=source_name,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
    )


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
        stable_id = _stable_chunk_id(source_sha256, block, text)
        token_estimate = max(1, (len(text) + 3) // 4)
        heading_path = list(block.heading_path)
        content_types = list(block.content_types or (block.kind,))
        source_refs = _source_refs(
            block,
            source_name=source_name,
            heading_path=heading_path,
            content_types=content_types,
        )
        asset_refs = list(block.asset_refs)
        page_numbers = _page_numbers_for_refs(source_refs) or list(block.page_numbers)
        metadata: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "stable_id": stable_id,
            "content_types": content_types,
        }
        if asset_refs:
            metadata["asset_refs"] = asset_refs
        if page_numbers:
            page_range = _format_page_range(page_numbers)
            metadata["page_numbers"] = page_numbers
            metadata["page_range"] = page_range
        chunks.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "stable_id": stable_id,
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
                "asset_refs": asset_refs,
                "metadata": metadata,
                "content_hash": f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
                "source_refs": source_refs,
            }
        )
        if page_numbers:
            chunks[-1]["page_numbers"] = page_numbers
            chunks[-1]["page_range"] = _format_page_range(page_numbers)
    for index, chunk in enumerate(chunks):
        chunk["previous_id"] = chunks[index - 1]["id"] if index > 0 else None
        chunk["next_id"] = chunks[index + 1]["id"] if index + 1 < len(chunks) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "chunk_kind": "semantic_markdown",
        "chunking_strategy": MARKDOWN_CHUNKING_STRATEGY,
        "source": {
            "name": source_name,
            "sha256": source_sha256,
            "char_count": len(markdown),
        },
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def chunk_markdown_unstructured_by_title(
    markdown: str,
    *,
    source_name: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> dict[str, Any]:
    """Chunk Markdown with Unstructured's title-aware element chunker.

    This is explicit opt-in because Unstructured normalizes Markdown syntax
    (notably tables) differently than the default Markdown-preserving splitter.
    """

    max_chars = max(200, int(max_chars or DEFAULT_MAX_CHARS))
    overlap_chars = max(0, min(int(overlap_chars or 0), max_chars // 3))
    partition_md = importlib.import_module("unstructured.partition.md").partition_md
    chunk_by_title = importlib.import_module("unstructured.chunking.title").chunk_by_title
    elements = partition_md(text=markdown)
    raw_chunks = chunk_by_title(
        elements,
        max_characters=max_chars,
        new_after_n_chars=max(1, max_chars - overlap_chars),
        combine_text_under_n_chars=0,
    )
    line_offsets = _line_offsets(markdown)
    source_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    chunks: list[dict[str, Any]] = []
    search_start_line = 1
    for raw_chunk in raw_chunks:
        text = str(raw_chunk).strip()
        if not text:
            continue
        index = len(chunks)
        chunk_id = _chunk_id(source_name, index, text)
        char_start, char_end, start_line, end_line = _locate_unstructured_chunk_span(
            markdown,
            text,
            line_offsets=line_offsets,
            search_start_line=search_start_line,
        )
        search_start_line = max(search_start_line, end_line + 1)
        metadata = _public_unstructured_metadata(raw_chunk)
        content_types = ["unstructured_composite"]
        if metadata.get("text_as_html"):
            content_types.append("table")
        token_estimate = max(1, (len(text) + 3) // 4)
        stable_id = _stable_chunk_id(
            source_sha256,
            MarkdownBlock(
                text=text,
                start_line=start_line,
                end_line=end_line,
                char_start=char_start,
                char_end=char_end,
                kind="unstructured_composite",
                content_types=tuple(content_types),
            ),
            text,
        )
        chunk_metadata = {
            "schema_version": SCHEMA_VERSION,
            "stable_id": stable_id,
            "content_types": content_types,
        }
        chunks.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "stable_id": stable_id,
                "index": index,
                "text": text,
                "contextual_text": text,
                "heading_path": [],
                "section_path": [],
                "start_line": start_line,
                "end_line": end_line,
                "char_start": char_start,
                "char_end": char_end,
                "char_count": len(text),
                "token_estimate": token_estimate,
                "token_count": token_estimate,
                "content_types": content_types,
                "content_hash": f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
                "metadata": chunk_metadata,
                "element_metadata": metadata,
                "source_refs": [
                    {
                        "type": "markdown_line_span",
                        "source": source_name,
                        "start_line": start_line,
                        "end_line": end_line,
                        "char_start": char_start,
                        "char_end": char_end,
                        "heading_path": [],
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
        "chunking_strategy": UNSTRUCTURED_CHUNKING_STRATEGY,
        "source": {
            "name": source_name,
            "sha256": source_sha256,
            "char_count": len(markdown),
        },
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def _normalize_strategy(strategy: str | None) -> str:
    value = str(strategy or "").strip().lower().replace("-", "_")
    if value in {"", "markdown", "markdown_heading", "markdown_heading_blocks"}:
        return MARKDOWN_CHUNKING_STRATEGY
    if value in {MARKDOWN_CHUNKING_STRATEGY, UNSTRUCTURED_CHUNKING_STRATEGY, "unstructured"}:
        return UNSTRUCTURED_CHUNKING_STRATEGY if value == "unstructured" else value
    return MARKDOWN_CHUNKING_STRATEGY


def _public_unstructured_metadata(chunk: object) -> dict[str, Any]:
    metadata = getattr(chunk, "metadata", None)
    if metadata is None or not hasattr(metadata, "to_dict"):
        return {}
    raw = metadata.to_dict()
    public: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "orig_elements":
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            public[key] = value
        elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
            public[key] = value
    return public


def _source_refs(
    block: MarkdownBlock,
    *,
    source_name: str,
    heading_path: list[str],
    content_types: list[str],
) -> list[dict[str, Any]]:
    spans = block.source_spans or (
        SourceSpan(
            block.start_line,
            block.end_line,
            block.char_start,
            block.char_end,
            block.page_numbers,
        ),
    )
    refs: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int, tuple[int, ...]]] = set()
    for span in spans:
        key = (span.start_line, span.end_line, span.char_start, span.char_end, span.page_numbers)
        if key in seen:
            continue
        seen.add(key)
        ref = {
            "type": "markdown_line_span",
            "source": source_name,
            "start_line": span.start_line,
            "end_line": span.end_line,
            "char_start": span.char_start,
            "char_end": span.char_end,
            "heading_path": heading_path,
            "content_types": content_types,
        }
        if span.page_numbers:
            ref["page_numbers"] = list(span.page_numbers)
            ref["page_range"] = _format_page_range(span.page_numbers)
        refs.append(ref)
    return refs


def _page_numbers_for_refs(refs: list[dict[str, Any]]) -> list[int]:
    values: set[int] = set()
    for ref in refs:
        for page in ref.get("page_numbers") or []:
            values.add(int(page))
    return sorted(values)


def _format_page_range(page_numbers: list[int] | tuple[int, ...]) -> str:
    pages = sorted({int(page) for page in page_numbers if int(page) > 0})
    if not pages:
        return ""
    ranges: list[str] = []
    start = prev = pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = page
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _locate_unstructured_chunk_span(
    markdown: str,
    text: str,
    *,
    line_offsets: list[tuple[int, int]],
    search_start_line: int = 1,
) -> tuple[int, int, int, int]:
    lines = markdown.splitlines()
    wanted = [_normalize_markdown_line_text(line) for line in text.splitlines() if line.strip()]
    wanted = [line for line in wanted if line]
    if not wanted:
        return 0, 0, 1, 1

    start_line = _find_normalized_line(lines, wanted[0], start=max(search_start_line - 1, 0))
    if start_line <= 0 and search_start_line > 1:
        start_line = _find_normalized_line(lines, wanted[0], start=0)
    end_line = _find_normalized_line(lines, wanted[-1], start=max(start_line - 1, 0))
    if start_line <= 0:
        start_line = 1
    if end_line <= 0:
        end_line = start_line
    char_start, char_end = _line_char_span(
        line_offsets,
        start_line=start_line,
        end_line=end_line,
    )
    return char_start, char_end, start_line, max(start_line, end_line)


def _find_normalized_line(lines: list[str], needle: str, *, start: int) -> int:
    for index in range(start, len(lines)):
        candidate = _normalize_markdown_line_text(lines[index])
        if candidate == needle or (needle and needle in candidate):
            return index + 1
    return -1


def _normalize_markdown_line_text(line: str) -> str:
    stripped = line.strip()
    heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
    if heading:
        return heading.group(1).strip()
    if stripped.startswith("|") and stripped.endswith("|"):
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        return " ".join(cell for cell in cells if cell)
    return re.sub(r"\s+", " ", stripped)


def _block_content_types(text: str, kind: str) -> tuple[str, ...]:
    values = [kind]
    if kind == "html_block":
        lowered = text.lower()
        if "<table" in lowered:
            values.append("table")
    for asset in _asset_refs(text):
        asset_type = str(asset.get("type") or "")
        if asset_type and asset_type not in values:
            values.append(asset_type)
    if kind == "list" and re.search(r"^\s*[-*+]\s+\[[ xX]\]", text, re.MULTILINE):
        values.append("task_list")
    return tuple(values)


def _asset_refs(text: str) -> tuple[dict[str, Any], ...]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_ref(kind: str, target: str, *, label: str = "", title: str = "") -> None:
        target = target.strip()
        if not target:
            return
        key = (kind, target, label)
        if key in seen:
            return
        seen.add(key)
        ref: dict[str, Any] = {"type": kind, "target": target}
        if kind in {"image", "html_image"}:
            ref["url"] = target
        else:
            ref["href"] = target
        if label:
            ref["alt" if kind in {"image", "html_image"} else "label"] = label
        if title:
            ref["title"] = title
        refs.append(ref)

    for match in re.finditer(r"!\[([^\]]*)\]\(([^)]*)\)", text):
        target, title = _parse_markdown_link_destination(match.group(2))
        add_ref("image", target, label=match.group(1).strip(), title=title)
    for match in re.finditer(r"(?<!!)\[([^\]]+)\]\(([^)]*)\)", text):
        target, title = _parse_markdown_link_destination(match.group(2))
        add_ref("link", target, label=match.group(1).strip(), title=title)
    for match in re.finditer(r"<(https?://[^>\s]+)>", text):
        add_ref("link", match.group(1))
    for match in re.finditer(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", text, flags=re.IGNORECASE):
        alt = _html_attr(match.group(0), "alt")
        title = _html_attr(match.group(0), "title")
        add_ref("html_image", match.group(1), label=alt, title=title)
    for match in re.finditer(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", text, flags=re.IGNORECASE | re.DOTALL):
        label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        add_ref("html_link", match.group(1), label=label, title=_html_attr(match.group(0), "title"))
    return tuple(refs)


def _parse_markdown_link_destination(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if not value:
        return "", ""
    title = ""
    if value.startswith("<") and ">" in value:
        end = value.find(">")
        target = value[1:end]
        rest = value[end + 1 :].strip()
    else:
        parts = value.split(None, 1)
        target = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
    if len(rest) >= 2 and rest[0] in {"'", '"'} and rest[-1] == rest[0]:
        title = rest[1:-1]
    return target, title


def _html_attr(tag: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}=[\"']([^\"']*)[\"']", tag, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _html_block_end_pattern(line: str):
    stripped = line.strip()
    if not stripped.startswith("<"):
        return None
    if stripped.startswith("<!--"):
        return lambda candidate: "-->" in candidate
    if stripped.startswith("<![CDATA["):
        return lambda candidate: "]]>" in candidate
    if stripped.startswith("<?"):
        return lambda candidate: "?>" in candidate
    match = re.match(
        r"^</?([A-Za-z][\w:-]*)(?:\s|>|/>)",
        stripped,
    )
    if not match:
        return None
    tag = match.group(1).lower()
    block_tags = {
        "address",
        "article",
        "aside",
        "blockquote",
        "canvas",
        "details",
        "dialog",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
        "video",
    }
    if tag not in block_tags:
        return None
    if stripped.endswith("/>") or tag == "hr":
        return lambda _candidate: True
    close = re.compile(rf"</{re.escape(tag)}\s*>", flags=re.IGNORECASE)
    return lambda candidate: bool(close.search(candidate))


def _is_blockquote_line(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}>", line))


def _list_marker(line: str) -> str | None:
    match = re.match(r"^(\s{0,3})(?:[-*+]|\d{1,9}[.)])\s+", line)
    return match.group(0) if match else None


def _is_list_continuation(line: str) -> bool:
    return bool(re.match(r"^\s{2,}\S", line))


def _is_indented_code_line(line: str) -> bool:
    return bool(re.match(r"^(?: {4}|\t)\S", line))


def _parse_page_marker(line: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*<!--\s*pages?\s*:\s*([^>]+?)\s*-->\s*$", line, flags=re.IGNORECASE)
    if not match:
        return None
    pages: set[int] = set()
    for part in match.group(1).split(","):
        token = part.strip()
        if not token:
            continue
        if re.match(r"^\d+\s*-\s*\d+$", token):
            left, right = [int(value.strip()) for value in token.split("-", 1)]
            start, end = sorted((left, right))
            pages.update(range(max(1, start), end + 1))
            continue
        if token.isdigit() and int(token) > 0:
            pages.add(int(token))
    return tuple(sorted(pages))


def _markdown_blocks(markdown: str, *, line_offsets: list[tuple[int, int]]) -> list[MarkdownBlock]:
    lines = markdown.splitlines()
    blocks: list[MarkdownBlock] = []
    heading_stack: list[tuple[int, str]] = []
    current_page_numbers: tuple[int, ...] = ()
    buffer: list[str] = []
    block_start = 1

    def flush(end_line: int, *, kind: str = "text") -> None:
        nonlocal buffer, block_start
        text = _trim_block_text("\n".join(buffer), kind)
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
                    content_types=_block_content_types(text, kind),
                    asset_refs=_asset_refs(text),
                    page_numbers=current_page_numbers,
                )
            )
        buffer = []

    line_no = 1
    while line_no <= len(lines):
        line = lines[line_no - 1]
        page_marker = _parse_page_marker(line)
        if page_marker is not None:
            flush(line_no - 1)
            current_page_numbers = page_marker
            block_start = line_no + 1
            line_no += 1
            continue

        if line_no == 1 and line.strip() in {"---", "+++"}:
            closing = line.strip()
            closing_line = None
            for candidate_line in range(line_no + 1, len(lines) + 1):
                if lines[candidate_line - 1].strip() == closing:
                    closing_line = candidate_line
                    break
            if closing_line is not None:
                block_start = line_no
                buffer = lines[line_no - 1 : closing_line]
                flush(closing_line, kind="front_matter")
                block_start = closing_line + 1
                line_no = closing_line + 1
                continue

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

        setext = re.match(r"^\s{0,3}(=+|-+)\s*$", line)
        if setext and len(buffer) == 1 and buffer[0].strip() and block_start == line_no - 1:
            level = 1 if setext.group(1).startswith("=") else 2
            title = buffer[0].strip()
            heading_stack = [(lvl, text) for lvl, text in heading_stack if lvl < level]
            heading_stack.append((level, title))
            buffer = [buffer[0], line]
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

        html_end = _html_block_end_pattern(line)
        if html_end:
            flush(line_no - 1)
            block_start = line_no
            html_lines = [line]
            if html_end(line):
                buffer = html_lines
                flush(line_no, kind="html_block")
                block_start = line_no + 1
                line_no += 1
                continue
            line_no += 1
            while line_no <= len(lines):
                if html_end(lines[line_no - 1]):
                    html_lines.append(lines[line_no - 1])
                    line_no += 1
                    break
                html_lines.append(lines[line_no - 1])
                line_no += 1
            buffer = html_lines
            flush(line_no - 1, kind="html_block")
            block_start = line_no
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
        if _is_blockquote_line(line):
            flush(line_no - 1)
            block_start = line_no
            quote_lines = [line]
            line_no += 1
            while line_no <= len(lines):
                candidate = lines[line_no - 1]
                if _is_blockquote_line(candidate):
                    quote_lines.append(candidate)
                    line_no += 1
                    continue
                if not candidate.strip() and line_no < len(lines) and _is_blockquote_line(lines[line_no]):
                    quote_lines.append(candidate)
                    line_no += 1
                    continue
                break
            buffer = quote_lines
            flush(line_no - 1, kind="blockquote")
            block_start = line_no
            continue
        if _is_indented_code_line(line):
            flush(line_no - 1)
            block_start = line_no
            code_lines = [line]
            line_no += 1
            while line_no <= len(lines):
                candidate = lines[line_no - 1]
                if _is_indented_code_line(candidate):
                    code_lines.append(candidate)
                    line_no += 1
                    continue
                if not candidate.strip() and line_no < len(lines) and _is_indented_code_line(lines[line_no]):
                    code_lines.append(candidate)
                    line_no += 1
                    continue
                break
            buffer = code_lines
            flush(line_no - 1, kind="indented_code")
            block_start = line_no
            continue
        if _list_marker(line):
            flush(line_no - 1)
            block_start = line_no
            list_lines = [line]
            line_no += 1
            while line_no <= len(lines):
                candidate = lines[line_no - 1]
                if _list_marker(candidate) or _is_list_continuation(candidate):
                    list_lines.append(candidate)
                    line_no += 1
                    continue
                if not candidate.strip() and line_no < len(lines):
                    next_line = lines[line_no]
                    if _list_marker(next_line) or _is_list_continuation(next_line):
                        list_lines.append(candidate)
                        line_no += 1
                        continue
                break
            buffer = list_lines
            flush(line_no - 1, kind="list")
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
                    asset_refs=_asset_refs(text),
                    source_spans=_source_spans_for_blocks(current),
                    page_numbers=_page_numbers_for_blocks(current),
                )
            )
        current = []

    while pending:
        block = pending.popleft()
        if block.kind == "heading" and current:
            flush()
        if current and block.page_numbers != current[-1].page_numbers and (block.page_numbers or current[-1].page_numbers):
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


def _page_numbers_for_blocks(blocks: list[MarkdownBlock]) -> tuple[int, ...]:
    values: set[int] = set()
    for block in blocks:
        values.update(block.page_numbers)
        for span in block.source_spans:
            values.update(span.page_numbers)
    return tuple(sorted(values))


def _source_spans_for_blocks(blocks: list[MarkdownBlock]) -> tuple[SourceSpan, ...]:
    if blocks and not any(block.source_spans for block in blocks) and _same_page_numbers(blocks):
        return (
            SourceSpan(
                blocks[0].start_line,
                blocks[-1].end_line,
                blocks[0].char_start,
                blocks[-1].char_end,
                blocks[0].page_numbers,
            ),
        )
    spans: list[SourceSpan] = []
    for block in blocks:
        if block.source_spans:
            spans.extend(
                span
                if span.page_numbers
                else SourceSpan(span.start_line, span.end_line, span.char_start, span.char_end, block.page_numbers)
                for span in block.source_spans
            )
        else:
            spans.append(
                SourceSpan(
                    block.start_line,
                    block.end_line,
                    block.char_start,
                    block.char_end,
                    block.page_numbers,
                )
            )
    return tuple(spans)


def _same_page_numbers(blocks: list[MarkdownBlock]) -> bool:
    if not blocks:
        return True
    first = blocks[0].page_numbers
    return all(block.page_numbers == first for block in blocks)


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
        return _split_table_block(block, max_chars=max_chars, line_offsets=line_offsets)
    if block.kind == "fenced_code":
        return _split_fenced_code_block(block, max_chars=max_chars, line_offsets=line_offsets)
    if block.kind in {"blockquote", "front_matter", "html_block", "indented_code", "list"}:
        return _split_line_preserving_block(block, max_chars=max_chars, line_offsets=line_offsets)
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


def _split_line_preserving_block(
    block: MarkdownBlock,
    *,
    max_chars: int,
    line_offsets: list[tuple[int, int]],
) -> list[MarkdownBlock]:
    lines = block.text.splitlines()
    pieces: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            pieces.append("\n".join(current))
            current = []

    for line in lines:
        candidate = "\n".join([*current, line])
        if current and len(candidate) > max_chars:
            flush()
            candidate = line
        if len(candidate) > max_chars:
            flush()
            pieces.extend(_split_text_by_chars(line, max_chars=max_chars))
        else:
            current.append(line)
    flush()
    return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)


def _split_table_block(
    block: MarkdownBlock,
    *,
    max_chars: int,
    line_offsets: list[tuple[int, int]],
) -> list[MarkdownBlock]:
    text = block.text
    lines = text.splitlines()
    if len(lines) <= 2:
        pieces = _split_text_by_chars(text, max_chars=max_chars)
        return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)

    header = lines[:2]
    rows = lines[2:]
    header_text = "\n".join(header)
    row_limit = max_chars - len(header_text) - 1
    if row_limit < 1:
        pieces = _split_text_by_chars(text, max_chars=max_chars)
        return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)
    pieces: list[MarkdownBlock] = []
    current_rows: list[tuple[int, str]] = []

    def flush_rows() -> None:
        nonlocal current_rows
        if current_rows:
            piece_text = "\n".join([*header, *(row for _index, row in current_rows)])
            first_index = current_rows[0][0]
            last_index = current_rows[-1][0]
            pieces.append(
                _synthetic_wrapped_block(
                    block,
                    piece_text,
                    start_line=block.start_line + 2 + first_index,
                    end_line=block.start_line + 2 + last_index,
                    line_offsets=line_offsets,
                    source_spans=_table_source_spans(
                        block,
                        row_start_line=block.start_line + 2 + first_index,
                        row_end_line=block.start_line + 2 + last_index,
                        line_offsets=line_offsets,
                    ),
                )
            )
            current_rows = []

    for row_index, row in enumerate(rows):
        candidate = "\n".join([*header, *(item for _index, item in current_rows), row])
        if current_rows and len(candidate) > max_chars:
            flush_rows()
            candidate = "\n".join([*header, row])
        if len(candidate) > max_chars:
            flush_rows()
            row_line = block.start_line + 2 + row_index
            row_start, _row_end = _line_char_span(line_offsets, start_line=row_line, end_line=row_line)
            cursor = 0
            for row_piece in _split_text_by_chars(row, max_chars=row_limit):
                rel_start = row.find(row_piece, cursor)
                if rel_start < 0:
                    rel_start = cursor
                rel_end = rel_start + len(row_piece)
                cursor = rel_end
                pieces.append(
                    _synthetic_wrapped_block(
                        block,
                        "\n".join([*header, row_piece]),
                        start_line=row_line,
                        end_line=row_line,
                        line_offsets=line_offsets,
                        source_spans=(
                            _table_header_source_span(block, line_offsets=line_offsets),
                            SourceSpan(
                                row_line,
                                row_line,
                                row_start + rel_start,
                                row_start + rel_end,
                                block.page_numbers,
                            ),
                        ),
                    )
                )
        else:
            current_rows.append((row_index, row))
    flush_rows()
    return pieces or [block]


def _split_fenced_code_block(
    block: MarkdownBlock,
    *,
    max_chars: int,
    line_offsets: list[tuple[int, int]],
) -> list[MarkdownBlock]:
    text = block.text
    lines = text.splitlines()
    if len(lines) < 2:
        pieces = _split_text_by_chars(text, max_chars=max_chars)
        return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)

    opener = lines[0]
    opener_marker = _fence_marker(opener) or ""
    has_closer = bool(_is_matching_fence(lines[-1], opener_marker))
    closer = lines[-1] if has_closer else lines[0][:3]
    body = lines[1:-1] if has_closer else lines[1:]
    overhead = len(opener) + len(closer) + 2
    body_limit = max_chars - overhead
    if body_limit < 1:
        pieces = _split_text_by_chars(text, max_chars=max_chars)
        return _locate_split_pieces(block, pieces, overlap_chars=0, line_offsets=line_offsets)
    pieces: list[MarkdownBlock] = []
    current: list[tuple[int, str]] = []

    def flush_code() -> None:
        nonlocal current
        if current:
            piece_text = "\n".join([opener, *(line for _index, line in current), closer])
            first_index = current[0][0]
            last_index = current[-1][0]
            body_start_line = block.start_line + 1 + first_index
            body_end_line = block.start_line + 1 + last_index
            pieces.append(
                _synthetic_wrapped_block(
                    block,
                    piece_text,
                    start_line=body_start_line,
                    end_line=body_end_line,
                    line_offsets=line_offsets,
                    source_spans=_fenced_code_source_spans(
                        block,
                        body_start_line=body_start_line,
                        body_end_line=body_end_line,
                        line_offsets=line_offsets,
                        include_closer=has_closer,
                    ),
                )
            )
            current = []

    for body_index, line in enumerate(body):
        candidate = "\n".join([*(item for _index, item in current), line])
        if current and len(candidate) > body_limit:
            flush_code()
            candidate = line
        if len(candidate) > body_limit:
            flush_code()
            body_line = block.start_line + 1 + body_index
            line_start, _line_end = _line_char_span(line_offsets, start_line=body_line, end_line=body_line)
            cursor = 0
            for line_piece in _split_text_by_chars(line, max_chars=body_limit):
                rel_start = line.find(line_piece, cursor)
                if rel_start < 0:
                    rel_start = cursor
                rel_end = rel_start + len(line_piece)
                cursor = rel_end
                pieces.append(
                    _synthetic_wrapped_block(
                        block,
                        "\n".join([opener, line_piece, closer]),
                        start_line=body_line,
                        end_line=body_line,
                        line_offsets=line_offsets,
                        source_spans=(
                            *_fenced_code_source_spans(
                                block,
                                body_start_line=body_line,
                                body_end_line=body_line,
                                line_offsets=line_offsets,
                                include_closer=has_closer,
                            )[:1],
                            SourceSpan(
                                body_line,
                                body_line,
                                line_start + rel_start,
                                line_start + rel_end,
                                block.page_numbers,
                            ),
                            *_fenced_code_source_spans(
                                block,
                                body_start_line=body_line,
                                body_end_line=body_line,
                                line_offsets=line_offsets,
                                include_closer=has_closer,
                            )[2:],
                        ),
                    )
                )
        else:
            current.append((body_index, line))
    flush_code()
    return pieces or [block]


def _synthetic_wrapped_block(
    block: MarkdownBlock,
    text: str,
    *,
    start_line: int,
    end_line: int,
    line_offsets: list[tuple[int, int]],
    source_spans: tuple[SourceSpan | tuple[int, int, int, int], ...],
) -> MarkdownBlock:
    char_start, char_end = _line_char_span(
        line_offsets,
        start_line=start_line,
        end_line=end_line,
    )
    content_types = _content_types([block])
    for asset in _asset_refs(text):
        asset_type = str(asset.get("type") or "")
        if asset_type and asset_type not in content_types:
            content_types = (*content_types, asset_type)
    return MarkdownBlock(
        text=text.strip(),
        start_line=start_line,
        end_line=end_line,
        char_start=char_start,
        char_end=char_end,
        heading_path=block.heading_path,
        kind=block.kind,
        content_types=content_types,
        asset_refs=_asset_refs(text),
        source_spans=_coerce_source_spans(source_spans, page_numbers=block.page_numbers),
        page_numbers=block.page_numbers,
    )


def _coerce_source_spans(
    source_spans: tuple[SourceSpan | tuple[int, int, int, int], ...],
    *,
    page_numbers: tuple[int, ...],
) -> tuple[SourceSpan, ...]:
    spans: list[SourceSpan] = []
    for span in source_spans:
        if isinstance(span, SourceSpan):
            spans.append(
                span
                if span.page_numbers
                else SourceSpan(span.start_line, span.end_line, span.char_start, span.char_end, page_numbers)
            )
        else:
            spans.append(SourceSpan(*span, page_numbers))
    return tuple(spans)


def _table_header_source_span(
    block: MarkdownBlock,
    *,
    line_offsets: list[tuple[int, int]],
) -> SourceSpan:
    header_start, header_end = _line_char_span(
        line_offsets,
        start_line=block.start_line,
        end_line=block.start_line + 1,
    )
    return SourceSpan(block.start_line, block.start_line + 1, header_start, header_end, block.page_numbers)


def _table_source_spans(
    block: MarkdownBlock,
    *,
    row_start_line: int,
    row_end_line: int,
    line_offsets: list[tuple[int, int]],
) -> tuple[SourceSpan, ...]:
    header_start, header_end = _line_char_span(
        line_offsets,
        start_line=block.start_line,
        end_line=block.start_line + 1,
    )
    row_start, row_end = _line_char_span(
        line_offsets,
        start_line=row_start_line,
        end_line=row_end_line,
    )
    if row_start_line == block.start_line + 2:
        return (SourceSpan(block.start_line, row_end_line, header_start, row_end, block.page_numbers),)
    return (
        SourceSpan(block.start_line, block.start_line + 1, header_start, header_end, block.page_numbers),
        SourceSpan(row_start_line, row_end_line, row_start, row_end, block.page_numbers),
    )


def _fenced_code_source_spans(
    block: MarkdownBlock,
    *,
    body_start_line: int,
    body_end_line: int,
    line_offsets: list[tuple[int, int]],
    include_closer: bool = True,
) -> tuple[SourceSpan, ...]:
    opener_start, opener_end = _line_char_span(
        line_offsets,
        start_line=block.start_line,
        end_line=block.start_line,
    )
    body_start, body_end = _line_char_span(
        line_offsets,
        start_line=body_start_line,
        end_line=body_end_line,
    )
    spans = [
        SourceSpan(block.start_line, block.start_line, opener_start, opener_end, block.page_numbers),
        SourceSpan(body_start_line, body_end_line, body_start, body_end, block.page_numbers),
    ]
    if include_closer:
        closer_start, closer_end = _line_char_span(
            line_offsets,
            start_line=block.end_line,
            end_line=block.end_line,
        )
        spans.append(SourceSpan(block.end_line, block.end_line, closer_start, closer_end, block.page_numbers))
    return tuple(spans)


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
    stripped = _trim_block_text(text, block.kind)
    leading_trim = 0 if _preserves_leading_space(block.kind) else len(text) - len(text.lstrip())
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
        content_types=_block_content_types(stripped, block.kind),
        asset_refs=_asset_refs(stripped),
        page_numbers=block.page_numbers,
    )


def _preserves_leading_space(kind: str) -> bool:
    return kind in {"blockquote", "fenced_code", "indented_code", "list", "table"}


def _trim_block_text(text: str, kind: str) -> str:
    if _preserves_leading_space(kind):
        return text.rstrip()
    return text.strip()


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


def _stable_chunk_id(source_sha256: str, block: MarkdownBlock, text: str) -> str:
    material = (
        f"{source_sha256}:{block.start_line}:{block.end_line}:"
        f"{block.char_start}:{block.char_end}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    )
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
    return f"stable_{digest}"
