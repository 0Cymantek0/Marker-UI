from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_copy_sources_exist() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    missing: list[str] = []

    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        parts = stripped.split()
        sources = parts[1:-1]
        if "--from=" in stripped:
            continue
        for source in sources:
            if source.startswith("--"):
                continue
            if not (REPO_ROOT / source).exists():
                missing.append(source)

    assert missing == []


def test_python_version_contract_is_311_everywhere() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11"' in pyproject

    checked_files = [
        "start.ps1",
        "start.sh",
        "docs/installation/windows.md",
        "docs/installation/source.md",
        "docs/installation/linux-macos.md",
        "docs/development/backend.md",
    ]
    stale: list[str] = []
    for relative in checked_files:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        if re.search(r"Python 3\.10\+|python 3\.10|3,\s*10", text):
            stale.append(relative)
    assert stale == []
