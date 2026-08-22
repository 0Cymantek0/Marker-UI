"""Git identity helpers for evidence/source freshness binding.

Content identity is the working-tree blob SHA (``git hash-object``), not
the index SHA, so unstaged edits to evidence files are still detected as
stale. Files must also be tracked: untracked evidence has no stable
identity and can never support a ``proven`` status.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitMetaError(RuntimeError):
    """Raised when git identity cannot be established."""


class GitMeta:
    """Batched git content-identity resolver for one repository root."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def _run(self, *args: str, stdin: str | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            input=stdin,
        )
        if proc.returncode != 0:
            raise GitMetaError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").strip()

    def content_shas(self, paths: list[str]) -> dict[str, str]:
        """Working-tree blob SHA for each tracked file (``git hash-object``).

        Missing/unreadable files are omitted from the result; callers
        treat an omitted path as dangling evidence.
        """

        repo_relative = [self._to_repo_relative(path) for path in paths]
        stdin = "\n".join(repo_relative)
        out = self._run("hash-object", "--stdin-paths", stdin=stdin + "\n" if stdin else "")
        shas = [line.strip() for line in out.splitlines()]
        if len(shas) != len(repo_relative):
            raise GitMetaError(
                f"git hash-object returned {len(shas)} hashes for {len(repo_relative)} paths"
            )
        return {path: sha for path, sha in zip(paths, shas)}

    def tracked(self, paths: list[str]) -> dict[str, bool]:
        listed = self._run("ls-files", "--", *[self._to_repo_relative(p) for p in paths])
        tracked_set = {line.strip() for line in listed.splitlines() if line.strip()}
        return {path: self._to_repo_relative(path) in tracked_set for path in paths}

    def _to_repo_relative(self, path: str) -> str:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                return str(candidate.resolve().relative_to(self.repo_root)).replace("\\", "/")
            except ValueError as exc:
                raise GitMetaError(f"path outside repository: {path}") from exc
        return str(candidate).replace("\\", "/")


class StaticResolver:
    """Deterministic resolver for tests: precomputed content SHAs."""

    def __init__(self, shas: dict[str, str], tracked: set[str] | None = None) -> None:
        self._shas = shas
        self._tracked = tracked if tracked is not None else set(shas)

    def head(self) -> str:
        return "0" * 40

    def content_shas(self, paths: list[str]) -> dict[str, str]:
        return {path: self._shas[path] for path in paths if path in self._shas}

    def tracked(self, paths: list[str]) -> dict[str, bool]:
        return {path: path in self._tracked for path in paths}
