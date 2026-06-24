"""Audio media preflight helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def probe_audio(filepath: str | Path) -> dict[str, Any]:
    """Return ffprobe audio metadata without exposing local paths.

    Missing ffprobe or corrupt media is reported in the returned dict rather
    than raised; transcription can still try the file if the STT backend can
    decode it.
    """
    if not shutil.which("ffprobe"):
        return {"available": False, "error": "ffprobe_not_available"}
    path = Path(filepath)
    try:
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
    except Exception as exc:  # noqa: BLE001 - preflight must not abort STT.
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    streams = payload.get("streams") or []
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    fmt = payload.get("format") or {}
    duration = audio_stream.get("duration") or fmt.get("duration") or 0
    bit_rate = audio_stream.get("bit_rate") or fmt.get("bit_rate") or 0
    return {
        "available": True,
        "format": fmt.get("format_name"),
        "duration_s": _coerce_float(duration),
        "bit_rate": _coerce_int(bit_rate),
        "codec": audio_stream.get("codec_name"),
        "sample_rate": _coerce_int(audio_stream.get("sample_rate")),
        "channels": _coerce_int(audio_stream.get("channels")),
        "has_audio": bool(audio_stream),
        "stream_count": len(streams),
    }


def _coerce_float(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

