"""Native runtime dependency contract for converters.

A ``NativeRequirement`` declares a system binary a converter needs at runtime
(e.g. ``ffmpeg``, ``tesseract``), plus an optional minimum version.  The
capability endpoint resolves each requirement against ``PATH`` and reports
``present`` / ``missing`` / ``wrong_version`` so the agent seam (CLI/MCP/REST)
can surface actionable deployment diagnostics — not a generic ``InternalError``.

Converters declare dependencies as a class attribute::

    class VideoConverter(BaseConverter):
        native_requirements = (
            NativeRequirement(command="ffmpeg", min_version="5.0"),
            NativeRequirement(command="ffprobe", min_version="5.0"),
        )

The registry collects these at registration time so callers can introspect
requirements without instantiating converters (which may be expensive).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeRequirement:
    """A system binary a converter needs at runtime.

    Attributes:
        command: binary name resolved via ``shutil.which``.
        min_version: optional dotted minimum version (e.g. ``"5.0"``).
        install_hint: human-readable one-liner shown when this is missing.
    """

    command: str
    min_version: str | None = None
    install_hint: str = ""

    def resolve(self) -> dict[str, Any]:
        """Check ``PATH`` for the binary and parse its version.

        Returns a dict with ``status`` of ``present``, ``missing``, or
        ``wrong_version`` plus the detected version (if any).
        """
        path = shutil.which(self.command)
        if path is None:
            return {
                "command": self.command,
                "available": False,
                "detected_version": None,
                "min_version": self.min_version,
                "status": "missing",
                "install_hint": self.install_hint,
            }
        detected = _detect_version(self.command)
        status = "present"
        if self.min_version and detected is not None:
            if not _version_ge(detected, self.min_version):
                status = "wrong_version"
        return {
            "command": self.command,
            "available": status == "present",
            "detected_version": detected,
            "min_version": self.min_version,
            "status": status,
            "install_hint": self.install_hint,
        }


def _detect_version(command: str) -> str | None:
    """Best-effort parse of ``<command> -version`` / ``--version`` output.

    ``ffmpeg`` prints its banner to stderr; most other tools use stdout.
    We scan both streams for the first ``N.N[.N]`` pattern.
    """
    for flag in ("-version", "--version"):
        try:
            proc = subprocess.run(
                [command, flag],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
        for stream in (proc.stdout, proc.stderr):
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", stream)
            if match:
                return match.group(1)
    return None


def _version_ge(detected: str, minimum: str) -> bool:
    """Tuple-compare dotted versions: ``"5.1.2" >= "5.0"`` → ``True``."""

    def tupleify(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split(".") if p.isdigit())

    return tupleify(detected) >= tupleify(minimum)
