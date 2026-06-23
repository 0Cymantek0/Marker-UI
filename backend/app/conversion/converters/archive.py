"""ZIP archive converter."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import zipfile

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


_TEXT_LIKE_EXTS = frozenset({".txt", ".md", ".rst", ".log", ".csv", ".json", ".jsonl", ".xml", ".html", ".htm"})


class ArchiveConverter(BaseConverter):
    """Summarize ZIP contents and inline small text-like files."""

    engine_name = "archive"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    _EXTENSIONS = frozenset({".zip"})

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
        max_files = int(config.get("archive_max_files", 100))
        max_inline_bytes = int(config.get("archive_inline_bytes", 65536))
        lines = [f"# Archive: {Path(filepath).name}", "", "## Contents", ""]
        inlined = 0
        with zipfile.ZipFile(filepath) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            for info in infos[:max_files]:
                suspicious = " suspicious-name" if _is_suspicious_name(info.filename) else ""
                lines.append(f"- `{info.filename}` ({info.file_size} bytes){suspicious}")
            if len(infos) > max_files:
                lines.append(f"- ... {len(infos) - max_files} more file(s)")

            for info in infos[:max_files]:
                ext = Path(info.filename).suffix.lower()
                if ext not in _TEXT_LIKE_EXTS or info.file_size > max_inline_bytes:
                    continue
                with zf.open(info) as f:
                    data = f.read(max_inline_bytes + 1)
                if len(data) > max_inline_bytes:
                    continue
                text = _decode_bytes(data)
                lines.extend(["", f"## `{info.filename}`", "", "```", text.rstrip(), "```"])
                inlined += 1

        return UniversalConversionResult(
            text="\n".join(lines).strip(),
            extension="md",
            metadata={"engine_detail": {"format": "zip", "inlined_files": inlined}},
        )


def _is_suspicious_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return normalized.startswith("/") or "/../" in f"/{normalized}/" or normalized.startswith("../")


def _decode_bytes(data: bytes) -> str:
    try:
        from charset_normalizer import from_bytes

        match = from_bytes(data).best()
        if match is not None:
            return str(match)
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")
