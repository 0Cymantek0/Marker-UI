"""Tests for native non-Marker converters."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.audio.providers.base import RawTranscript
from app.conversion.converters.archive import ArchiveConverter
from app.conversion.converters.audio import AudioConverter
from app.conversion.converters.html import HtmlConverter
from app.conversion.converters.notebook import NotebookConverter
from app.conversion.converters.outlook_msg import OutlookMsgConverter
from app.conversion.converters.spreadsheet import SpreadsheetConverter
from app.conversion.converters.text_data import TextDataConverter
from app.conversion.converters.video import VideoConverter
from app.conversion.converters.xml_rss import XmlRssConverter
from app.services.conversion_service import ConversionService


def _fake_provider(transcribe_fn):
    """Wrap a legacy dict-returning fake into a provider returning a RawTranscript.

    The old tests stubbed ``_transcribe_audio`` to return a flat dict; the new
    provider-adapter seam expects a :class:`RawTranscript`. This adapter keeps
    the existing fakes working unchanged by routing their dict through
    :meth:`RawTranscript.from_provider_dict`.
    """

    class _FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(transcribe_fn(filepath, config, device=device))

    return _FakeProvider()


class _FakeMarkerService:
    def convert_file(self, filepath, options, device=None):
        return {"text": "marker fallback", "extension": "md", "images": {}, "metadata": {}}


def test_service_registers_every_advertised_native_engine() -> None:
    svc = ConversionService(_FakeMarkerService())

    for engine in [
        "archive",
        "audio",
        "html",
        "notebook",
        "outlook_msg",
        "spreadsheet",
        "text_data",
        "video",
        "xml_rss",
    ]:
        assert svc.registry.has(engine)


def test_text_data_converter_turns_csv_into_markdown_table(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,score\nAda,10\nLinus,9\n", encoding="utf-8")

    result = TextDataConverter().convert(str(path), {})

    assert "| name | score |" in result.text
    assert "| Ada | 10 |" in result.text
    assert result.metadata["engine_detail"]["format"] == "csv"


def test_text_data_converter_turns_tsv_into_markdown_table(tmp_path: Path) -> None:
    path = tmp_path / "data.tsv"
    path.write_text("name\tscore\tnote\nAda\t10\tuses | pipes\nLinus\t9\tline one\n", encoding="utf-8")

    result = TextDataConverter().convert(str(path), {})

    assert "| name | score | note |" in result.text
    assert "| Ada | 10 | uses \\| pipes |" in result.text
    assert "| Linus | 9 | line one |" in result.text
    assert result.metadata["engine_detail"] == {
        "format": "tsv",
        "rows": 3,
        "truncated": False,
    }


def test_conversion_service_derives_markdown_and_chunks_for_native_converter(tmp_path: Path) -> None:
    path = tmp_path / "scores.tsv"
    path.write_text("name\tscore\nAda\t10\nLinus\t9\n", encoding="utf-8")
    svc = ConversionService(_FakeMarkerService())
    config = {"output_formats": ["markdown", "chunks"], "output_format": "markdown"}

    assert svc.supports_multiple_formats(str(path), config) is True
    outputs = svc.convert_file_formats(str(path), config, ["markdown", "chunks"])

    assert list(outputs) == ["markdown", "chunks"]
    assert outputs["markdown"]["extension"] == "md"
    payload = json.loads(outputs["chunks"]["text"])
    assert outputs["chunks"]["extension"] == "json"
    assert payload["schema_version"] == "marker.chunks.v1"
    assert payload["chunk_kind"] == "semantic_markdown"
    assert "| Ada | 10 |" in payload["chunks"][-1]["text"]
    assert outputs["chunks"]["metadata"]["chunking"]["chunk_kind"] == "semantic_markdown"


def test_html_converter_drops_scripts_and_emits_markdown(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><title>Demo</title><script>bad()</script></head>"
        "<body><h1>Hello</h1><p><strong>World</strong></p></body></html>",
        encoding="utf-8",
    )

    result = HtmlConverter().convert(str(path), {})

    assert "# Hello" in result.text
    assert "**World**" in result.text
    assert "bad()" not in result.text


def test_xml_rss_converter_reads_feed_items(tmp_path: Path) -> None:
    path = tmp_path / "feed.rss"
    path.write_text(
        "<rss><channel><title>News</title><item><title>Item A</title>"
        "<link>https://example.com/a</link><description>Body</description>"
        "</item></channel></rss>",
        encoding="utf-8",
    )

    result = XmlRssConverter().convert(str(path), {})

    assert "# News" in result.text
    assert "## Item A" in result.text
    assert "https://example.com/a" in result.text


def test_notebook_converter_preserves_markdown_code_and_output(tmp_path: Path) -> None:
    path = tmp_path / "analysis.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["## Intro\n"]},
                    {
                        "cell_type": "code",
                        "source": ["print('ok')\n"],
                        "outputs": [{"text": ["ok\n"]}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = NotebookConverter().convert(str(path), {})

    assert "## Intro" in result.text
    assert "```python\nprint('ok')" in result.text
    assert "ok" in result.text


def test_spreadsheet_converter_reads_xlsx_sheets(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Scores"
    ws.append(["name", "score"])
    ws.append(["Ada", 10])
    wb.save(path)

    result = SpreadsheetConverter().convert(str(path), {})

    assert "## Sheet: Scores" in result.text
    assert "| name | score |" in result.text
    assert "| Ada | 10 |" in result.text


def test_spreadsheet_converter_reads_legacy_xls_sheets(tmp_path: Path) -> None:
    xlwt = pytest.importorskip("xlwt")
    path = tmp_path / "legacy.xls"
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Legacy Scores")
    for row_idx, row in enumerate([["name", "score", "active"], ["Ada", 10, True], ["Linus", 9.5, False]]):
        for col_idx, value in enumerate(row):
            ws.write(row_idx, col_idx, value)
    wb.save(str(path))

    result = SpreadsheetConverter().convert(str(path), {})

    assert "## Sheet: Legacy Scores" in result.text
    assert "| name | score | active |" in result.text
    assert "| Ada | 10 | True |" in result.text
    assert "| Linus | 9.5 | False |" in result.text
    assert result.metadata["engine_detail"]["format"] == "xls"
    assert result.metadata["engine_detail"]["sheets"] == [
        {
            "name": "Legacy Scores",
            "rows": 3,
            "columns": 3,
            "truncated": False,
        }
    ]


def test_outlook_msg_converter_renders_body_headers_and_safe_attachments(monkeypatch, tmp_path: Path) -> None:
    class FakeMessage:
        subject = "Quarterly Update"
        sender = "Ops <ops@example.com>"
        to = "Team <team@example.com>"
        cc = ""
        date = "2026-06-24 10:30:00+00:00"
        body = "Body line 1\r\nBody line 2"
        header = "X-Attach-Path: C:\\Users\\person\\secret\\report.xlsx"
        attachments = [
            SimpleNamespace(
                longFilename="C:\\Users\\person\\secret\\report.xlsx",
                shortFilename=None,
                mimetype="application/vnd.ms-excel",
                data=b"12345",
            )
        ]

        def __init__(self, filepath: str) -> None:
            self.filepath = filepath
            self.closed = False

        def close(self) -> None:
            self.closed = True

    import extract_msg

    monkeypatch.setattr(extract_msg, "Message", FakeMessage)
    path = tmp_path / "mail.msg"
    path.write_bytes(b"fake msg")

    result = OutlookMsgConverter().convert(str(path), {})

    assert "# Quarterly Update" in result.text
    assert "- **From:** Ops <ops@example.com>" in result.text
    assert "Body line 1\nBody line 2" in result.text
    assert "`report.xlsx`, application/vnd.ms-excel, 5 bytes" in result.text
    detail = result.metadata["engine_detail"]
    assert detail["format"] == "msg"
    assert detail["attachment_count"] == 1
    assert detail["attachments"][0]["filename"] == "report.xlsx"
    assert detail["attachments"][0]["unsafe_original_name_redacted"] is True
    assert "C:\\Users" not in detail["headers"]
    assert "[redacted-path]" in detail["headers"]


def test_audio_converter_renders_timestamped_local_transcript(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            assert filepath == str(path)
            return RawTranscript.from_provider_dict(
                {
                    "language": "en",
                    "duration": 2.5,
                    "model": "tiny.en",
                    "segments": [
                        {"start": 0.0, "end": 1.25, "text": "hello world", "confidence": 0.92},
                        {"start": 1.25, "end": 2.5, "text": "second line", "confidence": 0.4},
                    ],
                }
            )

    monkeypatch.setattr("app.audio.transcribe.build_provider", lambda pid: FakeProvider())
    monkeypatch.setattr(
        "app.audio.transcribe.probe_audio",
        lambda filepath: {"available": True, "codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
    )

    result = AudioConverter().convert(str(path), {})

    assert "# Audio Transcript: voice" in result.text
    assert "- **Language:** en" in result.text
    assert "- **Media:** pcm_s16le, 16000 Hz, 1 ch" in result.text
    assert "`00:00.000-00:01.250` hello world" in result.text
    assert "`00:01.250-00:02.500` second line" in result.text
    assert "## Source Map" in result.text
    assert "voice_seg_0001" in result.text
    assert "low_confidence" in result.text
    detail = result.metadata["engine_detail"]
    assert detail["format"] == "wav"
    assert detail["language"] == "en"
    assert detail["duration"] == 2.5
    assert detail["segment_count"] == 2
    assert detail["model"] == "tiny.en"
    assert detail["output_mode"] == "transcript"
    assert detail["media_info"]["codec"] == "pcm_s16le"
    transcript = result.metadata["audio"]["transcript"]
    assert transcript["segments"][0]["segment_id"] == "voice_seg_0001"
    assert transcript["segments"][1]["warnings"] == ["low_confidence"]


def test_audio_converter_enhanced_mode_requires_source_provenance(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "meeting.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(
                {
                    "language": "en",
                    "duration": 1.0,
                    "model": "tiny.en",
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "ship the table parser fix", "confidence": 0.88},
                    ],
                }
            )

    fake = FakeProvider()
    monkeypatch.setattr(
        "app.audio.transcribe.build_provider",
        lambda provider_id: fake,
    )
    monkeypatch.setattr(
        "app.audio.transcribe.probe_audio",
        lambda filepath: {"available": True, "codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
    )
    result = AudioConverter().convert(str(path), {"audio_output_mode": "meeting_notes"})

    assert "# Audio Document: meeting" in result.text
    assert "ship the table parser fix [meeting.wav 00:00.000-00:01.000 speaker_0 | `meeting_seg_0001`]" in result.text
    assert "## Original Transcript" in result.text
    assert result.metadata["engine_detail"]["output_mode"] == "meeting_notes"
    enhancement = result.metadata["audio"]["enhancement"]
    assert enhancement == {
        "mode": "meeting_notes",
        "template": "meeting_notes",
        "trigger": "output_mode",
        "text_enhancement_enabled": False,
        "text_enhancement_strength": 0,
        "structural_enhancement_enabled": False,
        "structural_enhancement_mode": "auto",
        "provider": "local_deterministic",
        "provenance_required": True,
        "source_refs_required": True,
        "source_refs_valid": True,
        "provenance_validation": {
            "valid": True,
            "required": True,
            "missing": [],
            "fallback_applied": False,
        },
    }


def test_audio_text_enhancement_toggle_uses_corrected_transcript_renderer(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "call.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(
                {
                    "language": "en",
                    "duration": 1.0,
                    "model": "tiny.en",
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "please send the follow up", "confidence": 0.88},
                    ],
                }
            )

    monkeypatch.setattr(
        "app.audio.transcribe.build_provider",
        lambda provider_id: FakeProvider(),
    )
    monkeypatch.setattr(
        "app.audio.transcribe.probe_audio",
        lambda filepath: {"available": True, "codec": "pcm_s16le", "sample_rate": 16000, "channels": 1},
    )

    result = AudioConverter().convert(
        str(path),
        {
            "audio_output_mode": "transcript",
            "audio_text_enhancement_enabled": True,
            "audio_text_enhancement_strength": 2,
        },
    )

    assert "# Enhanced Transcript: call" in result.text
    assert (
        "`00:00.000-00:01.000` Please send the follow up. _(call_seg_0001, speaker_0)_ "
        "[call.wav 00:00.000-00:01.000 speaker_0 | `call_seg_0001`]"
    ) in result.text
    assert "## Enhancement Audit" in result.text
    assert "## Original Transcript" in result.text
    assert result.metadata["engine_detail"]["output_mode"] == "enhanced"
    assert result.metadata["audio"]["enhancement"]["trigger"] == "text_enhancement"
    assert result.metadata["audio"]["enhancement"]["text_enhancement_strength"] == 2
    assert result.metadata["audio"]["enhancement"]["source_refs_valid"] is True


def test_audio_enhancement_falls_back_when_source_refs_are_missing(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "bad_refs.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(
                {
                    "duration": 1.0,
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "needs citation", "confidence": 0.9},
                    ],
                }
            )

    monkeypatch.setattr("app.audio.transcribe.build_provider", lambda provider_id: FakeProvider())
    monkeypatch.setattr("app.audio.transcribe.probe_audio", lambda filepath: {"available": True})
    monkeypatch.setattr(
        "app.conversion.converters.audio.render_text_enhanced_markdown",
        lambda transcript, *, title, strength: "# Bad Enhanced\n\n- no citations here",
    )

    result = AudioConverter().convert(
        str(path),
        {
            "audio_text_enhancement_enabled": True,
            "audio_enhancement_fallback_on_validation_failure": True,
        },
    )

    assert "# Audio Transcript: bad_refs" in result.text
    enhancement = result.metadata["audio"]["enhancement"]
    assert enhancement["trigger"] == "validation_fallback"
    assert enhancement["provenance_validation"]["fallback_applied"] is True


def test_audio_enhancement_can_fail_strictly_when_source_refs_are_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "strict_refs.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(
                {
                    "duration": 1.0,
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "needs citation", "confidence": 0.9},
                    ],
                }
            )

    monkeypatch.setattr("app.audio.transcribe.build_provider", lambda provider_id: FakeProvider())
    monkeypatch.setattr("app.audio.transcribe.probe_audio", lambda filepath: {"available": True})
    monkeypatch.setattr(
        "app.conversion.converters.audio.render_text_enhanced_markdown",
        lambda transcript, *, title, strength: "# Bad Enhanced\n\n- no citations here",
    )

    with pytest.raises(RuntimeError, match="failed provenance validation"):
        AudioConverter().convert(
            str(path),
            {
                "audio_text_enhancement_enabled": True,
                "audio_enhancement_fallback_on_validation_failure": False,
            },
        )


def test_audio_structural_enhancement_uses_requested_template(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "standup.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(
                {
                    "duration": 1.0,
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "today we decide to ship", "confidence": 0.9},
                    ],
                }
            )

    monkeypatch.setattr(
        "app.audio.transcribe.build_provider",
        lambda provider_id: FakeProvider(),
    )
    monkeypatch.setattr(
        "app.audio.transcribe.probe_audio",
        lambda filepath: {"available": True},
    )

    result = AudioConverter().convert(
        str(path),
        {
            "audio_output_mode": "transcript",
            "audio_structural_enhancement_enabled": True,
            "audio_structural_enhancement_mode": "meeting_notes",
        },
    )

    assert "- **Mode:** local deterministic meeting_notes" in result.text
    assert result.metadata["audio"]["enhancement"]["trigger"] == "structural_enhancement"
    assert result.metadata["audio"]["enhancement"]["template"] == "meeting_notes"


def test_audio_contradiction_detection_adds_review_findings(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    path = tmp_path / "decision.wav"
    path.write_bytes(b"RIFF fake wav")

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            return RawTranscript.from_provider_dict(
                {
                    "duration": 2.0,
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "The launch is approved today", "confidence": 0.9},
                        {"start": 1.0, "end": 2.0, "text": "The launch is not approved today", "confidence": 0.9},
                    ],
                }
            )

    monkeypatch.setattr(
        "app.audio.transcribe.build_provider",
        lambda provider_id: FakeProvider(),
    )
    monkeypatch.setattr(
        "app.audio.transcribe.probe_audio",
        lambda filepath: {"available": True},
    )

    result = AudioConverter().convert(
        str(path),
        {"audio_contradiction_detection": True},
    )

    assert "## Possible Contradictions" in result.text
    findings = result.metadata["audio"]["contradictions"]
    assert len(findings) == 1
    assert findings[0]["left"]["segment_id"] == "decision_seg_0001"
    assert findings[0]["right"]["segment_id"] == "decision_seg_0002"


def test_audio_benchmark_compare_rejected_before_transcription(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "compare.wav"
    path.write_bytes(b"RIFF fake wav")

    def fail_build_provider(provider_id):
        raise AssertionError("provider should not be built for unsupported benchmark mode")

    monkeypatch.setattr(
        "app.audio.transcribe.build_provider",
        fail_build_provider,
    )

    with pytest.raises(NotImplementedError, match="Audio provider comparison is not shipped"):
        AudioConverter().convert(
            str(path),
            {
                "audio_provider": "local_faster_whisper",
                "audio_benchmark_compare": True,
                "audio_compare_providers": ["local_faster_whisper"],
            },
        )


def test_audio_fusion_mode_rejected_before_transcription(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "fusion.wav"
    path.write_bytes(b"RIFF fake wav")

    def fail_build_provider(provider_id):
        raise AssertionError("provider should not be built for unsupported fusion mode")

    monkeypatch.setattr(
        "app.audio.transcribe.build_provider",
        fail_build_provider,
    )

    with pytest.raises(NotImplementedError, match="Audio context fusion is not shipped"):
        AudioConverter().convert(
            str(path),
            {
                "audio_provider": "local_faster_whisper",
                "audio_fusion_mode": "audio_first",
            },
        )


def test_audio_transcribe_passes_vocabulary_and_word_timestamp_options(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    class FakeWord:
        word = "Marker"
        start = 0.1
        end = 0.4
        probability = 0.8

    class FakeSegment:
        start = 0.0
        end = 0.5
        text = "Marker"
        no_speech_prob = 0.05
        words = [FakeWord()]

    class FakeInfo:
        language = "en"
        duration = 0.5

    class FakeWhisperModel:
        def __init__(self, model_name, device, compute_type):
            seen["model_name"] = model_name
            seen["device"] = device
            seen["compute_type"] = compute_type

        def transcribe(self, filepath, **kwargs):
            seen["filepath"] = filepath
            seen["kwargs"] = kwargs
            return [FakeSegment()], FakeInfo()

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF fake")

    from app.audio.providers.faster_whisper import FasterWhisperProvider

    raw = FasterWhisperProvider().transcribe(
        str(path),
        {
            "audio_model": "base.en",
            "audio_device": "cpu",
            "audio_compute_type": "int8",
            "audio_word_timestamps": True,
        },
        vocabulary_prompt="Vocabulary terms: Marker, LiteParse",
    )

    assert seen["model_name"] == "base.en"
    assert seen["kwargs"]["initial_prompt"] == "Vocabulary terms: Marker, LiteParse"
    assert seen["kwargs"]["word_timestamps"] is True
    assert raw.segments[0].words[0].word == "Marker"
    assert raw.segments[0].words[0].confidence == 0.8


def test_archive_converter_lists_zip_without_extracting(tmp_path: Path) -> None:
    path = tmp_path / "bundle.zip"
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w") as zf:
        zf.writestr("inner.txt", "nested hello")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("notes/readme.txt", "hello archive")
        zf.writestr("data/metrics.tsv", "metric\tvalue\nrevenue\t100\n")
        zf.write(nested, "nested/archive.zip")
        zf.writestr("scan.pdf", b"%PDF fake")
        zf.writestr("../sneaky.txt", "bad path")

    result = ArchiveConverter().convert(str(path), {})

    assert "`notes/readme.txt`" in result.text
    assert "hello archive" in result.text
    assert "`data/metrics.tsv`" in result.text
    assert "| metric | value |" in result.text
    assert "`nested/archive.zip`" in result.text
    assert "nested hello" in result.text
    assert "suspicious-name" in result.text
    detail = result.metadata["engine_detail"]
    assert detail["format"] == "zip"
    assert detail["converted_children"] == 3
    assert detail["skipped_children"] == 2
    assert any(item["path"] == "scan.pdf" and item["action"] == "skipped" for item in detail["manifest"])
    assert any(item["path"] == "../sneaky.txt" and item["reason"] == "suspicious archive path" for item in detail["manifest"])
    assert detail["archive_budget"]["used_uncompressed_bytes"] > 0


def test_archive_converter_enforces_global_budget_and_compression_ratio(tmp_path: Path) -> None:
    path = tmp_path / "guarded.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("first.txt", "alpha")
        zf.writestr("second.txt", "beta")
        zf.writestr("compressed.txt", "A" * 10_000)

    result = ArchiveConverter().convert(
        str(path),
        {
            "archive_max_total_uncompressed_bytes": 7,
            "archive_max_compression_ratio": 2.0,
        },
    )

    manifest = result.metadata["engine_detail"]["manifest"]
    assert any(
        item["path"] == "second.txt"
        and item["reason"] == "archive total uncompressed byte budget reached"
        for item in manifest
    )
    assert any(
        item["path"] == "compressed.txt"
        and item["reason"] == "archive child compression ratio exceeds limit"
        for item in manifest
    )


def test_archive_converter_preserves_namespaced_child_assets(monkeypatch, tmp_path: Path) -> None:
    from app.conversion.result import Asset, UniversalConversionResult
    import app.conversion.converters.archive as archive_mod

    path = tmp_path / "assets.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("reports/data.csv", "name,score\nAda,10\n")

    class FakeChildConverter:
        def convert(self, filepath, config, device=None):
            return UniversalConversionResult(
                text="# child",
                extension="md",
                images={"chart.png": b"png-bytes"},
                assets=[Asset(name="export/rows.csv", media_type="text/csv", data=b"a,b\n1,2\n")],
            )

    monkeypatch.setattr(archive_mod, "_child_converter_for_engine", lambda engine: FakeChildConverter())

    result = ArchiveConverter().convert(str(path), {})

    asset_names = [asset.name for asset in result.assets]
    assert "children/reports/data.csv/assets/export/rows.csv" in asset_names
    assert "children/reports/data.csv/assets/chart.png" in asset_names
    manifest_entry = result.metadata["engine_detail"]["manifest"][0]
    assert manifest_entry["asset_count"] == 2
    assert manifest_entry["assets"] == asset_names


def test_archive_converter_builds_multi_audio_document(monkeypatch, tmp_path: Path) -> None:
    from app.audio.providers.base import RawTranscript

    class FakeProvider:
        id = "local_faster_whisper"

        def transcribe(self, filepath, config, *, device=None, vocabulary_prompt=None):
            label = config["audio_source_label"]
            if label.endswith("part1.wav"):
                text = "project alpha kickoff today"
            else:
                text = "project alpha follow up tomorrow"
            return RawTranscript.from_provider_dict(
                {
                    "language": "en",
                    "duration": 1.2,
                    "model": "tiny.en",
                    "segments": [{"start": 0.0, "end": 1.2, "text": text, "confidence": 0.9}],
                }
            )

    monkeypatch.setattr("app.audio.transcribe.build_provider", lambda pid: FakeProvider())
    path = tmp_path / "audio_batch.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("calls/part1.wav", b"RIFF fake one")
        zf.writestr("calls/part2.wav", b"RIFF fake two")

    result = ArchiveConverter().convert(str(path), {})

    assert "## Audio Batch Document" in result.text
    assert "# Multi-Audio Document: audio_batch.zip" in result.text
    assert "project alpha kickoff today [calls/part1.wav 00:00.000-00:01.200 speaker_0 | `part1_seg_0001`]" in result.text
    assert "project alpha follow up tomorrow [calls/part2.wav 00:00.000-00:01.200 speaker_0 | `part2_seg_0001`]" in result.text
    batch = result.metadata["engine_detail"]["audio_batch"]
    assert batch["source_count"] == 2
    assert batch["segment_count"] == 2
    assert batch["relationship"]["label"] == "related_or_follow_up"


def test_video_converter_builds_visual_timeline_from_real_silent_video(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe unavailable")
    path = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=2",
            "-vf",
            "drawbox=x=0:y=0:w=80:h=90:color=red:t=fill",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = VideoConverter().convert(
        str(path),
        {"video_max_frames": 2, "video_frame_interval_s": 1.0, "video_frame_ocr": False},
    )

    assert "# Video Timeline: silent" in result.text
    assert "## Multimodal Timeline" in result.text
    assert "**Frame:** 160x90" in result.text
    assert "**Frame OCR:** unavailable (disabled)" in result.text
    assert "_No audio transcript available._" in result.text
    detail = result.metadata["engine_detail"]
    assert detail["format"] == "mp4"
    assert detail["width"] == 160
    assert detail["height"] == 90
    assert detail["frame_count"] >= 1
    assert detail["has_audio"] is False
    assert result.metadata["video"]["provenance"]["frames"] is True
    assert result.metadata["video"]["provenance"]["cloud"] is False
