"""XML, RSS, and Atom feed converter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from defusedxml import ElementTree

from app.conversion.converters.text_data import decode_text_file
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


class XmlRssConverter(BaseConverter):
    """Convert XML/RSS/Atom to readable Markdown."""

    engine_name = "xml_rss"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    _EXTENSIONS = frozenset({".xml", ".rss", ".atom"})

    @property
    def supported_extensions(self) -> frozenset[str]:
        return self._EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension in self._EXTENSIONS

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        raw = decode_text_file(filepath)
        root = ElementTree.fromstring(raw.encode("utf-8"))
        root_name = _local_name(root.tag)

        if root_name == "rss" or root.find("./channel") is not None:
            text = _render_rss(root, Path(filepath).stem)
            format_name = "rss"
        elif root_name == "feed":
            text = _render_atom(root, Path(filepath).stem)
            format_name = "atom"
        else:
            max_nodes = int(config.get("xml_max_nodes", 200))
            lines = [f"# {Path(filepath).stem}", ""]
            lines.extend(_render_xml_tree(root, max_nodes=max_nodes))
            text = "\n".join(lines).strip()
            format_name = "xml"

        return UniversalConversionResult(
            text=text,
            extension="md",
            metadata={"engine_detail": {"format": format_name, "root": root_name}},
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el: Any, path: str) -> str:
    found = el.find(path)
    return "".join(found.itertext()).strip() if found is not None else ""


def _render_rss(root: Any, fallback_title: str) -> str:
    channel = root.find("./channel") or root
    title = _text(channel, "./title") or fallback_title
    lines = [f"# {title}", ""]
    for item in channel.findall("./item"):
        item_title = _text(item, "./title") or "Untitled item"
        link = _text(item, "./link")
        desc = _text(item, "./description")
        lines.append(f"## {item_title}")
        if link:
            lines.append(f"[{link}]({link})")
        if desc:
            lines.append(desc)
        lines.append("")
    return "\n".join(lines).strip()


def _render_atom(root: Any, fallback_title: str) -> str:
    ns = {"a": root.tag.split("}", 1)[0].lstrip("{")} if root.tag.startswith("{") else {}
    prefix = "a:" if ns else ""
    title = _text(root, f"./{prefix}title") or fallback_title
    lines = [f"# {title}", ""]
    for entry in root.findall(f"./{prefix}entry", ns):
        entry_title = _text(entry, f"./{prefix}title") or "Untitled entry"
        summary = _text(entry, f"./{prefix}summary") or _text(entry, f"./{prefix}content")
        link_el = entry.find(f"./{prefix}link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        lines.append(f"## {entry_title}")
        if link:
            lines.append(f"[{link}]({link})")
        if summary:
            lines.append(summary)
        lines.append("")
    return "\n".join(lines).strip()


def _render_xml_tree(root: Any, *, max_nodes: int) -> list[str]:
    lines: list[str] = []
    seen = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal seen
        if seen >= max_nodes:
            return
        seen += 1
        indent = "  " * depth
        text = " ".join("".join(node.itertext()).split())
        attrs = " ".join(f'{k}="{v}"' for k, v in sorted(node.attrib.items()))
        suffix = f" ({attrs})" if attrs else ""
        value = f": {text[:200]}" if text and len(list(node)) == 0 else ""
        lines.append(f"{indent}- `{_local_name(node.tag)}`{suffix}{value}")
        for child in list(node):
            walk(child, depth + 1)

    walk(root, 0)
    if seen >= max_nodes:
        lines.append(f"\n_Only first {max_nodes} XML nodes shown._")
    return lines
