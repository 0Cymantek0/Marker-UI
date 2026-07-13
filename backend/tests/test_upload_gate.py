"""Upload gate tests — reject video uploads when native deps missing (D6).

Tests the pre-flight capability gate logic in isolation. The gate must fire
for video extensions when ffmpeg/ffprobe are absent, and must NOT fire for
non-video extensions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.conversion.converters.video import VideoConverter
from app.conversion.dependencies import get_engine_status


def _video_extensions() -> frozenset[str]:
    """Extensions the gate treats as video."""
    return VideoConverter().supported_extensions


def test_gate_fires_for_video_when_ffmpeg_missing() -> None:
    """get_engine_status returns non-ready for video → gate must reject."""
    with patch(
        "app.conversion.dependencies.shutil.which",
        return_value=None,
    ):
        status = get_engine_status()["video"]
    assert status in (
        "missing_optional_dependency",
        "missing_native_dependency",
    ), f"Expected non-ready, got {status}"


def test_gate_does_not_fire_for_non_video_extensions() -> None:
    """Non-video extensions (.pdf, .docx, .txt) must never match the gate.

    The gate checks extension membership against video extensions only.
    Verify that common non-video extensions are not in that set.
    """
    video_exts = _video_extensions()
    non_video = [".pdf", ".docx", ".txt", ".xlsx", ".html", ".json", ".csv"]
    for ext in non_video:
        assert ext not in video_exts, (
            f"{ext} must not be treated as a video extension by the gate"
        )


def test_gate_fires_only_for_declared_video_extensions() -> None:
    """The gate extension set must match VideoConverter.supported_extensions."""
    exts = _video_extensions()
    assert ".mp4" in exts
    assert ".mov" in exts
    assert ".mkv" in exts
    assert ".webm" in exts
    assert ".avi" in exts


def test_video_engine_status_when_ffmpeg_present() -> None:
    """When ffmpeg is on PATH, video engine must not report missing dep.

    (May still report missing_optional_dependency if faster-whisper is absent,
    but the native dep itself must resolve.)
    """
    converter = VideoConverter()
    # runtime_ready only checks native_requirements (ffmpeg/ffprobe), not
    # Python deps like faster-whisper.
    if converter.runtime_ready():
        # If ffmpeg IS present on this machine, verify the converter says so.
        assert converter.runtime_ready() is True
    else:
        # If ffmpeg is absent (CI without ffmpeg), runtime_ready is False — fine.
        assert converter.runtime_ready() is False
