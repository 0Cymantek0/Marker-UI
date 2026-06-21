"""Lightweight file metadata sniffing for routing decisions.

``StreamInfo`` captures extension, MIME type, file size, and a small sample
of leading bytes — everything the router needs to pick a converter without
reading the entire file or importing heavy libraries.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

# How many leading bytes to read for magic-byte sniffing.  512 is enough for
# every Office ZIP-based format's PK header and most magic signatures.
_SAMPLE_SIZE = 512


@dataclass(frozen=True)
class StreamInfo:
    """Immutable snapshot of file metadata used for routing."""

    path: str
    extension: str  # lower-cased, with dot, e.g. ".pdf"
    mime_type: str  # e.g. "application/pdf", "application/octet-stream" fallback
    size: int  # bytes on disk (0 if stat fails)
    sample: bytes  # first _SAMPLE_SIZE bytes (empty if unreadable)

    @classmethod
    def from_path(cls, filepath: str | Path) -> StreamInfo:
        """Build a ``StreamInfo`` from a filesystem path.

        This is cheap: one stat + one partial read.  No heavy imports.
        """
        p = Path(filepath)
        extension = p.suffix.lower() if p.suffix else ""

        # MIME guess — stdlib only, no network.
        mime_type, _ = mimetypes.guess_type(p.name)
        if not mime_type:
            mime_type = "application/octet-stream"

        try:
            size = p.stat().st_size
        except OSError:
            size = 0

        sample = b""
        try:
            with open(p, "rb") as f:
                sample = f.read(_SAMPLE_SIZE)
        except OSError:
            pass

        return cls(
            path=str(p),
            extension=extension,
            mime_type=mime_type,
            size=size,
            sample=sample,
        )
