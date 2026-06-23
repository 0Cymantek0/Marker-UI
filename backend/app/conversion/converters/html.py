"""HTML to Markdown converter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import markdownify
from bs4 import BeautifulSoup

from app.conversion.converters.text_data import decode_text_file
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


class HtmlConverter(BaseConverter):
    """Convert HTML documents to clean Markdown without Marker models."""

    engine_name = "html"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    _EXTENSIONS = frozenset({".html", ".htm"})

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
        html = decode_text_file(filepath)
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else Path(filepath).stem
        root = soup.body or soup
        markdown = markdownify.markdownify(str(root), heading_style="ATX").strip()
        if title and not markdown.lstrip().startswith("#"):
            markdown = f"# {title}\n\n{markdown}".strip()
        return UniversalConversionResult(
            text=markdown,
            extension="md",
            metadata={"engine_detail": {"format": "html", "title": title}},
        )
