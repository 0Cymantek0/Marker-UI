"""LiteParse fast PDF converter.

Uses the LiteParse CLI so this remains isolated from binding API churn. OCR is
always disabled; scanned/complex files must route to Marker.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
import tempfile
import site
from pathlib import Path
from typing import Any

from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo
from app.conversion.table_evidence import attach_table_evidence


DEFAULT_LITEPARSE_MAX_PAGES = 1000


def _find_lit_executable() -> str | None:
    direct = shutil.which("lit")
    if direct:
        return direct
    exe_names = ("lit.exe", "lit")
    py_tag = f"Python{sys.version_info.major}{sys.version_info.minor}"
    script_dirs = [
        Path(sysconfig.get_path("scripts") or ""),
        Path(sys.executable).parent,
        Path(sys.executable).parent / "Scripts",
    ]
    user_base = getattr(site, "USER_BASE", None)
    if user_base:
        script_dirs.append(Path(user_base) / py_tag / "Scripts")
        script_dirs.append(Path(user_base) / "Scripts")
    for scripts_dir in script_dirs:
        for exe_name in exe_names:
            candidate = scripts_dir / exe_name
            if candidate.exists():
                return str(candidate)
    return None


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _liteparse_max_pages(config: dict[str, Any]) -> int:
    explicit = _coerce_positive_int(config.get("liteparse_max_pages"))
    if explicit:
        return explicit
    probe_data = config.get("probe_result")
    if isinstance(probe_data, dict):
        probed_pages = _coerce_positive_int(probe_data.get("page_count"))
        if probed_pages:
            return max(DEFAULT_LITEPARSE_MAX_PAGES, probed_pages)
    return DEFAULT_LITEPARSE_MAX_PAGES


def _liteparse_num_workers(config: dict[str, Any]) -> int | None:
    return _coerce_positive_int(config.get("liteparse_num_workers"))


def _convert_with_python_api(
    filepath: str,
    page_range: str | None,
    *,
    max_pages: int,
    num_workers: int | None,
) -> str:
    """Fallback for installs where the package exists but ``lit`` is off PATH."""
    try:
        from liteparse import LiteParse
    except Exception as exc:  # pragma: no cover - dependency absence path
        raise RuntimeError("LiteParse Python package is not installed") from exc

    parser = LiteParse(
        ocr_enabled=False,
        output_format="markdown",
        image_mode="off",
        extract_links=True,
        preserve_very_small_text=True,
        max_pages=max_pages,
        target_pages=page_range,
        num_workers=num_workers,
        quiet=True,
    )
    result = parser.parse(filepath)
    return result.text or ""


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
        page_range = config.get("page_range")
        max_pages = _liteparse_max_pages(config)
        num_workers = _liteparse_num_workers(config)
        execution_mode = "python_api"
        if lit:
            execution_mode = "cli"
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
                    "--preserve-small-text",
                    "--max-pages",
                    str(max_pages),
                    "--quiet",
                    "-o",
                    str(output_path),
                ]
                if page_range:
                    cmd.extend(["--target-pages", str(page_range)])
                if num_workers:
                    cmd.extend(["--num-workers", str(num_workers)])

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
        else:
            text = _convert_with_python_api(
                filepath,
                str(page_range) if page_range else None,
                max_pages=max_pages,
                num_workers=num_workers,
            )

        metadata = attach_table_evidence(
            {
                "liteparse": {
                    "ocr_enabled": False,
                    "image_mode": "off",
                    "extract_links": True,
                    "preserve_small_text": True,
                    "max_pages": max_pages,
                    "target_pages": str(page_range) if page_range else None,
                    "num_workers": num_workers,
                    "execution_mode": execution_mode,
                }
            },
            text or "",
        )

        return UniversalConversionResult(
            text=text or "",
            extension="md",
            images={},
            metadata=metadata,
        )
