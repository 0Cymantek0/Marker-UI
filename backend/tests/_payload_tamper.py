"""Cross-platform payload tamper helpers for kernel tests.

Production marks staged objects owner-read-only as a tamper hint, so a
test that deliberately rewrites or removes bytes must first clear that
hint. ``os.chmod(path, stat.S_IWRITE)`` alone is write-only (0o200) on
POSIX: it drops the owner read bit and makes every later integrity read
fail with ``PermissionError`` instead of exercising the intended
hash-mismatch classification. These helpers always keep the file
readable while enabling deliberate mutation, mirroring the production
``_clear_readonly`` semantics in ``app.kernel.payloads``.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

__all__ = ["corrupt_object", "make_unreadable", "unlink_object"]


def _make_writable(path: Path) -> None:
    """Clear the read-only tamper hint while keeping the file readable."""
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def corrupt_object(store: object, blob_key: str, data: bytes) -> None:
    """Overwrite one object's bytes, keeping it readable afterwards.

    The tampered object must stay readable so verification classifies it
    by hash/length mismatch (corrupt), never by an accidental read
    failure.
    """
    path = store.object_path(blob_key)  # type: ignore[attr-defined]
    _make_writable(path)
    path.write_bytes(data)


def unlink_object(store: object, blob_key: str) -> None:
    """Remove one object's bytes outright (missing, not corrupt)."""
    path = store.object_path(blob_key)  # type: ignore[attr-defined]
    _make_writable(path)
    path.unlink()


@contextmanager
def make_unreadable(path: Path) -> Iterator[None]:
    """Make an existing regular file temporarily unreadable by its owner.

    The condition must be real, not monkeypatched:

    * POSIX — strip every permission bit. Skipped when running as root,
      because root bypasses permission checks and the file would remain
      readable.
    * Windows — ``os.chmod`` cannot remove read access, so hold the file
      open through ``CreateFileW`` with a zero share mode; every other
      open then fails with a sharing violation until the handle closes.

    Behaviour differs by platform only in the physical mechanism; both
    make ``read_bytes()`` raise ``OSError`` for the duration.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        GENERIC_READ = 0x80000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE = ctypes.c_void_p(-1).value

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        handle: int | None = kernel32.CreateFileW(
            str(path), GENERIC_READ, 0, None, OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle in (None, INVALID_HANDLE):
            raise OSError(f"cannot hold exclusive handle on {path}")
        try:
            yield
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    else:
        if os.geteuid() == 0:  # type: ignore[attr-defined]
            pytest.skip("root bypasses POSIX permission bits")
        original = stat.S_IMODE(os.stat(path).st_mode)
        os.chmod(path, 0o000)
        try:
            yield
        finally:
            os.chmod(path, original)
