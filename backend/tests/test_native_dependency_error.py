"""Verify that VideoConverter raises a typed NativeDependencyMissingError (not
bare RuntimeError) when ffmpeg/ffprobe are absent, and that the error round-trips
through the Marker taxonomy with the stable NATIVE_DEPENDENCY_MISSING code.

Covers defect D2 from the issue #8 investigation:
    video.py line 125 raised RuntimeError("ffmpeg and ffprobe are required for
    local video conversion") — a stdlib exception, not a MarkerError subclass.
    from_exception() mapped it to InternalError (generic), giving the user
    "Internal error" instead of an actionable "install ffmpeg" message.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.conversion.converters.video import VideoConverter
from app.errors import (
    CODE_INTERNAL_ERROR,
    CODE_NATIVE_DEPENDENCY_MISSING,
    InternalError,
    NativeDependencyMissingError,
    from_exception,
)


def test_video_raises_typed_error_when_ffmpeg_missing(tmp_path) -> None:
    """When ffmpeg is absent, convert() must raise NativeDependencyMissingError,
    not bare RuntimeError. The payload must carry the stable code."""
    fake_video = tmp_path / "test.mp4"
    fake_video.write_bytes(b"not a real video")

    converter = VideoConverter()
    with patch("shutil.which", return_value=None):
        with pytest.raises(NativeDependencyMissingError) as exc_info:
            converter.convert(str(fake_video), {})

    payload = exc_info.value.to_payload()
    assert payload["error"]["code"] == CODE_NATIVE_DEPENDENCY_MISSING
    assert "ffmpeg" in payload["error"]["message"].lower()
    assert payload["error"]["retryable"] is False
    # Details must list which native deps are missing.
    details = payload["error"]["details"]
    assert "missing" in details
    assert "ffmpeg" in details["missing"]
    assert "ffprobe" in details["missing"]


def test_from_exception_sniffs_ffmpeg_runtime_error() -> None:
    """A bare RuntimeError mentioning ffmpeg must map to NativeDependencyMissingError,
    not the generic InternalError. This is the defensive path for any converter
    that has not yet been migrated to raise the typed error directly."""
    exc = RuntimeError("ffmpeg and ffprobe are required for local video conversion")
    mapped = from_exception(exc)
    assert isinstance(mapped, NativeDependencyMissingError)
    assert mapped.code == CODE_NATIVE_DEPENDENCY_MISSING
    assert mapped.exit_code != InternalError().exit_code


def test_from_exception_still_maps_generic_runtime_to_internal() -> None:
    """Regression guard: an unrelated RuntimeError (no ffmpeg mention) must still
    fall through to InternalError so existing mappings are unaffected."""
    mapped = from_exception(RuntimeError("something else broke"))
    assert isinstance(mapped, InternalError)
    assert mapped.code == CODE_INTERNAL_ERROR


def test_native_dependency_missing_registered_in_error_classes() -> None:
    """The error class must be in the ERROR_CLASSES registry so introspection
    (schema export, CLI --list-errors) discovers it."""
    from app.errors import ERROR_CLASSES

    assert CODE_NATIVE_DEPENDENCY_MISSING in ERROR_CLASSES
    assert ERROR_CLASSES[CODE_NATIVE_DEPENDENCY_MISSING] is NativeDependencyMissingError


# --- PR-2: native_requirements contract on BaseConverter + VideoConverter --------

def test_video_converter_declares_native_requirements() -> None:
    """VideoConverter must declare ffmpeg + ffprobe as native requirements."""
    from app.conversion.converters.video import VideoConverter

    converter = VideoConverter()
    commands = {req.command for req in converter.native_requirements}
    assert "ffmpeg" in commands
    assert "ffprobe" in commands
    assert len(converter.native_requirements) >= 2


def test_video_runtime_ready_true_when_present() -> None:
    """runtime_ready() must return True when all native deps are on PATH."""
    from app.conversion.converters.video import VideoConverter

    converter = VideoConverter()
    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "app.conversion.native_requirements._detect_version",
            return_value="5.1.2",
        ),
    ):
        assert converter.runtime_ready() is True


def test_video_runtime_ready_false_when_missing() -> None:
    """runtime_ready() must return False when any native dep is absent."""
    from app.conversion.converters.video import VideoConverter

    converter = VideoConverter()
    with patch("shutil.which", return_value=None):
        assert converter.runtime_ready() is False


def test_missing_requirements_returns_structured_list() -> None:
    """missing_requirements() returns list of dicts with command + status."""
    from app.conversion.converters.video import VideoConverter

    converter = VideoConverter()
    with patch("shutil.which", return_value=None):
        missing = converter.missing_requirements()
    assert len(missing) == 2
    commands = {entry["command"] for entry in missing}
    assert commands == {"ffmpeg", "ffprobe"}
    for entry in missing:
        assert entry["status"] == "missing"
        assert entry["available"] is False


def test_native_requirement_resolve_present() -> None:
    """NativeRequirement.resolve() reports present when binary exists."""
    from app.conversion.native_requirements import NativeRequirement

    req = NativeRequirement(command="ffmpeg", min_version="5.0")
    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "app.conversion.native_requirements._detect_version",
            return_value="5.1.2",
        ),
    ):
        result = req.resolve()
    assert result["status"] == "present"
    assert result["available"] is True
    assert result["detected_version"] == "5.1.2"


def test_native_requirement_resolve_wrong_version() -> None:
    """NativeRequirement.resolve() reports wrong_version when too old."""
    from app.conversion.native_requirements import NativeRequirement

    req = NativeRequirement(command="ffmpeg", min_version="6.0")
    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "app.conversion.native_requirements._detect_version",
            return_value="4.2.0",
        ),
    ):
        result = req.resolve()
    assert result["status"] == "wrong_version"
    assert result["available"] is False


def test_base_converter_default_native_requirements_empty() -> None:
    """BaseConverter subclasses without native_requirements default to empty."""
    from app.conversion.registry import BaseConverter
    from app.conversion.converters.text_data import TextDataConverter

    converter = TextDataConverter()
    assert converter.native_requirements == ()
    assert converter.runtime_ready() is True
    assert converter.missing_requirements() == []
