"""LiteParse fast PDF converter.

Uses the LiteParse CLI so this remains isolated from binding API churn. OCR is
always disabled; scanned/complex files must route to Marker.
"""

from __future__ import annotations

import shutil
import subprocess
import sysconfig
import tempfile
import site
from pathlib import Path
from typing import Any

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


def _find_lit_executable() -> str | None:
    direct = shutil.which("lit")
    if direct:
        return direct
    exe_names = ("lit.exe", "lit")
    script_dirs = [Path(sysconfig.get_path("scripts") or "")]
    user_base = getattr(site, "USER_BASE", None)
    if user_base:
        script_dirs.append(Path(user_base) / "Python311" / "Scripts")
        script_dirs.append(Path(user_base) / "Scripts")
    for scripts_dir in script_dirs:
        for exe_name in exe_names:
            candidate = scripts_dir / exe_name
            if candidate.exists():
                return str(candidate)
    return None


class LiteParsePdfConverter(BaseConverter):
    """CPU-only, text-layer PDF converter for clean digital PDFs."""

    engine_name = "liteparse_pdf"
    priority = 100
    requires_marker_models = False
    requires_gpu = False

    _EXTENSIONS = frozenset({".pdf"})

    @property
    def supported_extensions(self) -> frozenset[str]:
        return self._EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension == ".pdf"

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        lit = _find_lit_executable()
        if not lit:
            raise RuntimeError("LiteParse CLI 'lit' is not installed")

        page_range = config.get("page_range")
        with tempfile.TemporaryDirectory(prefix="liteparse-") as tmpdir:
            output_path = Path(tmpdir) / "output.md"
            cmd = [
                lit,
                "parse",
                filepath,
                "--format",
                "markdown",
                "--no-ocr",
                "--image-mode",
                "off",
                "-o",
                str(output_path),
            ]
            if page_range:
                cmd.extend(["--target-pages", str(page_range)])

            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=int(config.get("liteparse_timeout", 120)),
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"LiteParse failed: {stderr[:500]}")
            text = output_path.read_text(encoding="utf-8") if output_path.exists() else proc.stdout

        return UniversalConversionResult(
            text=text or "",
            extension="md",
            images={},
            metadata={
                "liteparse": {
                    "ocr_enabled": False,
                    "image_mode": "off",
                }
            },
        )
