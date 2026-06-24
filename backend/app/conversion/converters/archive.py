"""ZIP archive converter."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import tempfile
import zipfile

from app.audio.pipeline import AudioTranscript, build_multi_audio_document, slug_source_id
from app.conversion.converters.html import HtmlConverter
from app.conversion.converters.audio import AudioConverter
from app.conversion.converters.notebook import NotebookConverter
from app.conversion.converters.outlook_msg import OutlookMsgConverter
from app.conversion.converters.spreadsheet import SpreadsheetConverter
from app.conversion.converters.text_data import TextDataConverter
from app.conversion.converters.video import VideoConverter
from app.conversion.converters.xml_rss import XmlRssConverter
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.router import ConversionRouter
from app.conversion.stream_info import StreamInfo


_TEXT_LIKE_EXTS = frozenset({".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm"})


class ArchiveConverter(BaseConverter):
    """Summarize ZIP contents and recursively convert safe deterministic children."""

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
        max_child_bytes = int(config.get("archive_max_child_bytes", 2 * 1024 * 1024))
        max_depth = int(config.get("archive_max_depth", 2))
        max_converted_children = int(config.get("archive_max_converted_children", 25))
        depth = int(config.get("_archive_depth", 0))
        recursive = bool(config.get("archive_recursive", True))
        lines = [f"# Archive: {Path(filepath).name}", "", "## Contents", ""]
        converted = 0
        inlined = 0
        manifest: list[dict[str, Any]] = []
        audio_transcripts: list[AudioTranscript] = []
        with zipfile.ZipFile(filepath) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            for info in infos[:max_files]:
                suspicious = " suspicious-name" if _is_suspicious_name(info.filename) else ""
                lines.append(f"- `{info.filename}` ({info.file_size} bytes){suspicious}")
            if len(infos) > max_files:
                lines.append(f"- ... {len(infos) - max_files} more file(s)")

            if recursive:
                lines.extend(["", "## Converted Children", ""])
            for info in infos[:max_files]:
                if converted >= max_converted_children:
                    manifest.append(_manifest_entry(info, "skipped", "archive child conversion limit reached", depth))
                    continue
                entry, child_result = _convert_child(
                    zf,
                    info,
                    config,
                    max_child_bytes=max_child_bytes,
                    max_depth=max_depth,
                    depth=depth,
                )
                manifest.append(entry)
                child_text = child_result.text if child_result else None
                if child_result:
                    transcript = _audio_transcript_from_result(child_result)
                    if transcript is not None:
                        audio_transcripts.append(transcript)
                if child_text:
                    lines.extend(["", f"### `{info.filename}`", "", child_text.strip()])
                    converted += 1
                    if Path(info.filename).suffix.lower() in _TEXT_LIKE_EXTS:
                        inlined += 1
                elif not recursive and Path(info.filename).suffix.lower() in _TEXT_LIKE_EXTS and info.file_size <= max_inline_bytes:
                    with zf.open(info) as f:
                        data = f.read(max_inline_bytes + 1)
                    if len(data) <= max_inline_bytes:
                        text = _decode_bytes(data)
                        lines.extend(["", f"## `{info.filename}`", "", "```", text.rstrip(), "```"])
                        inlined += 1
            audio_batch_metadata: dict[str, Any] | None = None
            if len(audio_transcripts) > 1:
                batch_text, audio_batch_metadata = build_multi_audio_document(
                    audio_transcripts,
                    title=f"Multi-Audio Document: {Path(filepath).name}",
                    context=config.get("audio_context"),
                )
                lines.extend(["", "## Audio Batch Document", "", batch_text])

        return UniversalConversionResult(
            text="\n".join(lines).strip(),
            extension="md",
            metadata={
                "engine_detail": {
                    "format": "zip",
                    "inlined_files": inlined,
                    "converted_children": sum(1 for item in manifest if item["action"] == "converted"),
                    "skipped_children": sum(1 for item in manifest if item["action"] == "skipped"),
                    "failed_children": sum(1 for item in manifest if item["action"] == "failed"),
                    "manifest": manifest,
                    "audio_batch": audio_batch_metadata,
                }
            },
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


def _manifest_entry(info: zipfile.ZipInfo, action: str, reason: str, depth: int, engine: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": info.filename,
        "size": info.file_size,
        "action": action,
        "reason": reason,
        "depth": depth,
    }
    if engine:
        entry["engine"] = engine
    return entry


def _convert_child(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    config: dict[str, Any],
    *,
    max_child_bytes: int,
    max_depth: int,
    depth: int,
) -> tuple[dict[str, Any], UniversalConversionResult | None]:
    if not bool(config.get("archive_recursive", True)):
        return _manifest_entry(info, "skipped", "recursive archive conversion disabled", depth), None
    if depth >= max_depth:
        return _manifest_entry(info, "skipped", "archive depth limit reached", depth), None
    if _is_suspicious_name(info.filename):
        return _manifest_entry(info, "skipped", "suspicious archive path", depth), None
    if info.file_size > max_child_bytes:
        return _manifest_entry(info, "skipped", "child exceeds archive_max_child_bytes", depth), None

    ext = Path(info.filename).suffix.lower()
    data = zf.read(info)
    if len(data) > max_child_bytes:
        return _manifest_entry(info, "skipped", "child exceeds archive_max_child_bytes", depth), None

    with tempfile.TemporaryDirectory(prefix="marker-archive-child-") as temp_dir:
        child_path = Path(temp_dir) / f"child{ext or '.bin'}"
        child_path.write_bytes(data)
        stream = StreamInfo.from_path(child_path)
        plan = ConversionRouter.plan(stream, {**config, "_archive_depth": depth + 1})
        if plan.needs_marker_models or plan.needs_gpu or plan.engine in {"marker_pdf", "liteparse_pdf", "mixed_pdf"}:
            return _manifest_entry(info, "skipped", "child requires non-deterministic or PDF/image engine", depth, plan.engine), None
        converter = _child_converter_for_engine(plan.engine)
        if converter is None:
            return _manifest_entry(info, "skipped", f"no archive child converter for {plan.engine}", depth, plan.engine), None
        try:
            child_config = {**config, "_archive_depth": depth + 1}
            if plan.engine == "audio":
                child_config["audio_source_label"] = info.filename
                child_config["audio_source_id"] = slug_source_id(info.filename)
            result = converter.convert(str(child_path), child_config)
        except Exception as exc:
            return _manifest_entry(info, "failed", f"{type(exc).__name__}: {exc}", depth, plan.engine), None
    return _manifest_entry(info, "converted", "converted by child router", depth, plan.engine), result


def _child_converter_for_engine(engine: str) -> BaseConverter | None:
    if engine == "archive":
        return ArchiveConverter()
    return {
        "html": HtmlConverter(),
        "audio": AudioConverter(),
        "notebook": NotebookConverter(),
        "outlook_msg": OutlookMsgConverter(),
        "spreadsheet": SpreadsheetConverter(),
        "text_data": TextDataConverter(),
        "video": VideoConverter(),
        "xml_rss": XmlRssConverter(),
    }.get(engine)


def _audio_transcript_from_result(result: UniversalConversionResult) -> AudioTranscript | None:
    raw = ((result.metadata or {}).get("audio") or {}).get("transcript")
    if not isinstance(raw, dict):
        return None
    from app.audio.pipeline import AudioSegment

    segments = tuple(
        AudioSegment(
            segment_id=str(item.get("segment_id")),
            source_id=str(item.get("source_id")),
            source_label=str(item.get("source_label")),
            start_ms=int(item.get("start_ms") or 0),
            end_ms=int(item.get("end_ms") or 0),
            speaker=str(item.get("speaker") or "speaker_0"),
            text=str(item.get("text") or ""),
            confidence=item.get("confidence"),
            warnings=tuple(item.get("warnings") or ()),
            words=tuple(item.get("words") or ()),
        )
        for item in raw.get("segments") or []
        if isinstance(item, dict)
    )
    return AudioTranscript(
        source_id=str(raw.get("source_id") or "audio"),
        source_label=str(raw.get("source_label") or "audio"),
        language=raw.get("language"),
        duration_ms=int(raw.get("duration_ms") or 0),
        model=raw.get("model"),
        provider=str(raw.get("provider") or "local_faster_whisper"),
        segments=segments,
        warnings=tuple(raw.get("warnings") or ()),
        risk_summary=dict(raw.get("risk_summary") or {}),
        media_info=dict(raw.get("media_info") or {}),
        vocabulary_hits=tuple(raw.get("vocabulary_hits") or ()),
    )
