"""Filesystem policy helpers for local paths and agent output access."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from app.errors import InputNotAllowedError

_client_workspace_roots: ContextVar[list[Path] | None] = ContextVar(
    "marker_client_workspace_roots",
    default=None,
)


def workspace_roots() -> list[Path]:
    return _parse_roots(os.getenv("MARKER_WORKSPACE_ROOTS", ""))


@contextmanager
def scoped_client_workspace_roots(roots: list[Path] | None) -> Iterator[None]:
    """Apply MCP client-provided roots to local input checks for one request."""

    token = _client_workspace_roots.set([_resolve(root) for root in roots] if roots is not None else None)
    try:
        yield
    finally:
        _client_workspace_roots.reset(token)


def output_root() -> Path | None:
    raw = os.getenv("MARKER_OUTPUT_ROOT", "").strip()
    return _resolve(Path(raw).expanduser()) if raw else None


def assert_local_input_allowed(path: Path) -> None:
    resolved = _resolve(path)
    server_roots = workspace_roots()
    if server_roots and not _is_under_any(resolved, server_roots):
        raise InputNotAllowedError(
            f"Local input path is outside MARKER_WORKSPACE_ROOTS: {path}",
            details={"path": str(path), "workspace_roots": [str(root) for root in server_roots]},
        )
    client_roots = _client_workspace_roots.get()
    if client_roots is not None and not _is_under_any(resolved, client_roots):
        raise InputNotAllowedError(
            f"Local input path is outside MCP client roots: {path}",
            details={"path": str(path), "client_roots": [str(root) for root in client_roots]},
        )


def assert_output_write_allowed(path: Path) -> None:
    root = output_root()
    if root is None:
        return
    resolved = _resolve(path)
    if not _is_under(resolved, root):
        raise InputNotAllowedError(
            f"Output path is outside MARKER_OUTPUT_ROOT: {path}",
            details={"path": str(path), "output_root": str(root)},
        )


def assert_output_read_allowed(path: Path) -> None:
    root = output_root()
    if root is None:
        return
    resolved = _resolve(path)
    if not _is_under(resolved, root):
        raise InputNotAllowedError(
            f"Output read path is outside MARKER_OUTPUT_ROOT: {path}",
            details={"path": str(path), "output_root": str(root)},
        )


def _parse_roots(raw: str) -> list[Path]:
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        value = item.strip().strip('"')
        if value:
            roots.append(_resolve(Path(value).expanduser()))
    return roots


def _resolve(path: Path) -> Path:
    return path.resolve(strict=False)


def _is_under_any(path: Path, roots: list[Path]) -> bool:
    return any(_is_under(path, root) for root in roots)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
