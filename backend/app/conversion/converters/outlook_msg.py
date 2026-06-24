"""Outlook MSG converter using local extract-msg parsing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


_LOCAL_PATH_RE = re.compile(
    r"(?i)\b(?:[a-z]:\\|\\\\[^\\\s]+\\|/users/|/home/|/var/|/tmp/)[^\s<>\"]+"
)


class OutlookMsgConverter(BaseConverter):
    """Convert Outlook .msg email metadata and body to Markdown."""

    engine_name = "outlook_msg"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    _EXTENSIONS = frozenset({".msg"})

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
        import extract_msg

        msg = extract_msg.Message(filepath)
        try:
            fields = {
                "subject": _clean_text(getattr(msg, "subject", "")),
                "from": _clean_text(getattr(msg, "sender", "")),
                "to": _clean_text(getattr(msg, "to", "")),
                "cc": _clean_text(getattr(msg, "cc", "")),
                "date": _clean_text(getattr(msg, "date", "")),
            }
            body = _clean_text(getattr(msg, "body", ""))
            header = _redact_private_paths(_clean_text(getattr(msg, "header", "")))
            attachments = _attachment_metadata(getattr(msg, "attachments", []) or [])
        finally:
            close = getattr(msg, "close", None)
            if callable(close):
                close()

        title = fields["subject"] or Path(filepath).stem
        lines = [f"# {title}", ""]
        for label, key in [
            ("From", "from"),
            ("To", "to"),
            ("Cc", "cc"),
            ("Date", "date"),
            ("Subject", "subject"),
        ]:
            if fields[key]:
                lines.append(f"- **{label}:** {_escape_inline(fields[key])}")

        lines.extend(["", "## Body", "", body or "_No plain-text body found._"])
        if attachments:
            lines.extend(["", "## Attachments", ""])
            for item in attachments:
                label = item["filename"]
                suffix = f", {item['mime_type']}" if item.get("mime_type") else ""
                size = item.get("size")
                size_suffix = f", {size} bytes" if isinstance(size, int) else ""
                lines.append(f"- `{label}`{suffix}{size_suffix}")

        metadata = {
            "engine_detail": {
                "format": "msg",
                "headers": header,
                "attachments": attachments,
                "attachment_count": len(attachments),
            }
        }
        return UniversalConversionResult(
            text="\n".join(lines).strip(),
            extension="md",
            metadata=metadata,
        )


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                return value.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace").strip()
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _escape_inline(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")


def _safe_attachment_name(raw_name: Any, index: int) -> tuple[str, bool]:
    raw = _clean_text(raw_name)
    if not raw:
        return f"attachment_{index}", False
    normalized = raw.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].strip().strip(".")
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name)
    if not name:
        name = f"attachment_{index}"
    if len(name) > 120:
        name = name[:117] + "..."
    return name, name != raw


def _redact_private_paths(value: str) -> str:
    return _LOCAL_PATH_RE.sub("[redacted-path]", value)


def _attachment_metadata(attachments: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        raw_name = (
            getattr(attachment, "longFilename", None)
            or getattr(attachment, "shortFilename", None)
            or getattr(attachment, "name", None)
        )
        filename, redacted = _safe_attachment_name(raw_name, index)
        data = getattr(attachment, "data", None)
        result.append(
            {
                "filename": filename,
                "mime_type": _clean_text(getattr(attachment, "mimetype", "")),
                "size": len(data) if isinstance(data, (bytes, bytearray)) else None,
                "unsafe_original_name_redacted": redacted,
            }
        )
    return result
