"""Local video timeline converter."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from app.audio.pipeline import format_timestamp_ms
from app.audio.transcribe import transcribe_audio_file
from app.conversion.native_requirements import NativeRequirement
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo
from app.errors import NativeDependencyMissingError


_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})


class VideoConverter(BaseConverter):
    """Build a local multimodal timeline from video frames plus audio."""

    engine_name = "video"
    priority = 10
    requires_marker_models = False
    requires_gpu = False
    native_requirements = (
        NativeRequirement(
            command="ffmpeg",
            min_version="5.0",
            install_hint="apt-get install ffmpeg (Debian/Ubuntu) or brew install ffmpeg (macOS)",
        ),
        NativeRequirement(
            command="ffprobe",
            min_version="5.0",
            install_hint="Ships with the ffmpeg package on most distros.",
        ),
    )

    @property
    def supported_extensions(self) -> frozenset[str]:
        return _VIDEO_EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension in _VIDEO_EXTENSIONS

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        missing = self.missing_requirements()
        if missing:
            commands = [entry["command"] for entry in missing]
            raise NativeDependencyMissingError(
                f"Video conversion requires native binaries that are not "
                f"available (missing: {', '.join(commands)})",
                hint="Install the ffmpeg package — it ships both ffmpeg and "
                     "ffprobe on most distros (apt-get install ffmpeg / "
                     "brew install ffmpeg).",
                details={"missing": commands, "engine": self.engine_name},
                retryable=False,
            )
        path = Path(filepath)
        probe = _probe_video(path)
        max_frames = int(config.get("video_max_frames", 8))
        interval_s = float(config.get("video_frame_interval_s", 2.0))
        title = path.stem
        warnings: list[str] = []

        with tempfile.TemporaryDirectory(prefix="marker-video-") as temp_dir:
            temp = Path(temp_dir)
            transcript_data = None
            if probe.get("has_audio"):
                audio_path = temp / "audio.wav"
                if _extract_audio(path, audio_path):
                    transcript = transcribe_audio_file(
                        str(audio_path),
                        config,
                        device=device,
                        source_label=path.name,
                        source_id=f"{_safe_stem(path)}_audio",
                    )
                    transcript_data = transcript.to_dict()
                else:
                    warnings.append("audio_demux_failed")
            else:
                warnings.append("no_audio_stream")

            frame_dir = temp / "frames"
            frame_dir.mkdir()
            frame_paths = _extract_frames(path, frame_dir, interval_s=interval_s, max_frames=max_frames)
            frame_analyses = [
                _analyze_frame(
                    frame_path,
                    timestamp_ms=int(round(index * interval_s * 1000)),
                    config=config,
                )
                for index, frame_path in enumerate(frame_paths)
            ]
            if not frame_analyses:
                warnings.append("no_frames_extracted")

        text = _render_video_markdown(
            title=title,
            probe=probe,
            transcript=transcript_data,
            frames=frame_analyses,
            warnings=warnings,
        )
        return UniversalConversionResult(
            text=text,
            extension="md",
            metadata={
                "engine_detail": {
                    "format": path.suffix.lower().lstrip("."),
                    "duration": probe.get("duration_s"),
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "video_codec": probe.get("video_codec"),
                    "audio_codec": probe.get("audio_codec"),
                    "frame_count": len(frame_analyses),
                    "has_audio": bool(probe.get("has_audio")),
                    "warnings": warnings,
                },
                "video": {
                    "probe": probe,
                    "transcript": transcript_data,
                    "frames": frame_analyses,
                    "provenance": {
                        "audio": transcript_data is not None,
                        "frames": len(frame_analyses) > 0,
                        "ocr": any(frame.get("ocr_text") for frame in frame_analyses),
                        "cloud": False,
                    },
                },
            },
        )


def _safe_stem(path: Path) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in path.stem).strip("_").lower() or "video"


def _probe_video(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    duration = (
        video_stream.get("duration")
        or (payload.get("format") or {}).get("duration")
        or 0
    )
    return {
        "duration_s": round(float(duration or 0), 6),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "has_audio": bool(audio_stream),
        "stream_count": len(streams),
    }


def _extract_audio(path: Path, destination: Path) -> bool:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and destination.is_file() and destination.stat().st_size > 44


def _extract_frames(path: Path, frame_dir: Path, *, interval_s: float, max_frames: int) -> list[Path]:
    interval_s = max(0.25, interval_s)
    max_frames = max(1, max_frames)
    output_pattern = str(frame_dir / "frame_%04d.png")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-vf",
            f"fps=1/{interval_s}",
            "-frames:v",
            str(max_frames),
            output_pattern,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return sorted(frame_dir.glob("frame_*.png"))[:max_frames]


def _analyze_frame(frame_path: Path, *, timestamp_ms: int, config: dict[str, Any]) -> dict[str, Any]:
    with Image.open(frame_path) as img:
        rgb = img.convert("RGB")
        stat = ImageStat.Stat(rgb)
        mean = tuple(round(channel, 3) for channel in stat.mean)
        brightness = round(sum(mean) / 3.0, 3)
        ocr_text, ocr_error = _ocr_frame(rgb, enabled=bool(config.get("video_frame_ocr", True)))
        return {
            "timestamp_ms": timestamp_ms,
            "timestamp": format_timestamp_ms(timestamp_ms),
            "width": rgb.width,
            "height": rgb.height,
            "mean_rgb": mean,
            "brightness": brightness,
            "brightness_label": _brightness_label(brightness),
            "dominant_color": _dominant_color(mean),
            "ocr_text": ocr_text,
            "ocr_error": ocr_error,
            "provenance": ["frame", *(['ocr'] if ocr_text else [])],
        }


def _ocr_frame(image: Image.Image, *, enabled: bool) -> tuple[str, str | None]:
    if not enabled:
        return "", "disabled"
    if not shutil.which("tesseract"):
        return "", "tesseract_not_available"
    try:
        import pytesseract

        text = pytesseract.image_to_string(image).strip()
        return text, None if text else "no_text_recovered"
    except Exception as exc:  # noqa: BLE001 - OCR must not kill video conversion.
        return "", f"{type(exc).__name__}: {exc}"


def _brightness_label(value: float) -> str:
    if value < 64:
        return "dark"
    if value > 192:
        return "bright"
    return "mid"


def _dominant_color(mean_rgb: tuple[float, float, float]) -> str:
    red, green, blue = mean_rgb
    if max(mean_rgb) - min(mean_rgb) < 20:
        return "neutral"
    if red >= green and red >= blue:
        return "red"
    if green >= red and green >= blue:
        return "green"
    return "blue"


def _render_video_markdown(
    *,
    title: str,
    probe: dict[str, Any],
    transcript: dict[str, Any] | None,
    frames: list[dict[str, Any]],
    warnings: list[str],
) -> str:
    lines = [f"# Video Timeline: {title}", ""]
    lines.append(f"- **Duration:** {format_timestamp_ms(int(float(probe.get('duration_s') or 0) * 1000))}")
    lines.append(f"- **Resolution:** {probe.get('width')}x{probe.get('height')}")
    lines.append(f"- **Video codec:** {probe.get('video_codec') or 'unknown'}")
    lines.append(f"- **Audio codec:** {probe.get('audio_codec') or 'none'}")
    if warnings:
        lines.append(f"- **Warnings:** {', '.join(warnings)}")
    lines.extend(["", "## Multimodal Timeline", ""])
    transcript_segments = (transcript or {}).get("segments") or []
    for frame in frames:
        ts = frame["timestamp"]
        lines.append(f"### {ts}")
        lines.append(
            f"- **Frame:** {frame['width']}x{frame['height']}, "
            f"{frame['brightness_label']} {frame['dominant_color']} frame "
            f"(brightness {frame['brightness']})"
        )
        if frame.get("ocr_text"):
            lines.append(f"- **Frame OCR:** {frame['ocr_text']}")
        elif frame.get("ocr_error"):
            lines.append(f"- **Frame OCR:** unavailable ({frame['ocr_error']})")
        nearby = _segments_near_timestamp(transcript_segments, int(frame["timestamp_ms"]))
        for segment in nearby:
            lines.append(
                f"- **Audio:** {segment.get('text')} "
                f"[{format_timestamp_ms(int(segment.get('start_ms') or 0))}-"
                f"{format_timestamp_ms(int(segment.get('end_ms') or 0))} | `{segment.get('segment_id')}`]"
            )
        lines.append("- **Provenance:** frame" + (", ocr" if frame.get("ocr_text") else "") + (", audio" if nearby else ""))
        lines.append("")
    lines.extend(["## Audio Transcript", ""])
    if transcript_segments:
        for segment in transcript_segments:
            lines.append(
                f"- `{format_timestamp_ms(int(segment.get('start_ms') or 0))}-"
                f"{format_timestamp_ms(int(segment.get('end_ms') or 0))}` "
                f"{segment.get('text')} _({segment.get('segment_id')})_"
            )
    else:
        lines.append("_No audio transcript available._")
    return "\n".join(lines).strip()


def _segments_near_timestamp(segments: list[dict[str, Any]], timestamp_ms: int) -> list[dict[str, Any]]:
    window_ms = 1500
    nearby: list[dict[str, Any]] = []
    for segment in segments:
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or 0)
        if start_ms - window_ms <= timestamp_ms <= end_ms + window_ms:
            nearby.append(segment)
    return nearby

