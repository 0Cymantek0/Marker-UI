from __future__ import annotations

from pathlib import Path


def test_runtime_image_installs_ffmpeg_for_video_converter() -> None:
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "ffmpeg" in text
    assert "apt-get install" in text
