from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


EXPECTED_README_DOC_LINKS = {
    "docs/usage/cli-and-mcp.md",
    "docs/usage/cli.md",
    "docs/usage/mcp.md",
    "docs/enterprise/security.md",
    "docs/enterprise/deployment.md",
    "docs/reference/json-schemas.md",
    "docs/reference/errors.md",
    "docs/reference/output-manifest.md",
}


MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def test_readme_links_to_enterprise_cli_mcp_docs() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for link in EXPECTED_README_DOC_LINKS:
        assert f"({link})" in readme
        assert (REPO_ROOT / link).is_file()


def test_markdown_links_point_to_existing_local_files() -> None:
    docs = [REPO_ROOT / "README.md", *(REPO_ROOT / "docs").rglob("*.md")]
    failures: list[str] = []

    for doc in docs:
        text = _strip_code_spans_and_blocks(doc.read_text(encoding="utf-8"))
        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = raw_target.strip()
            if _is_external_or_in_page(target):
                continue
            path_part = target.split("#", 1)[0].replace("%20", " ").strip()
            if not path_part:
                continue
            candidate = (doc.parent / path_part).resolve()
            if not _is_inside_repo(candidate) or not candidate.exists():
                failures.append(f"{doc.relative_to(REPO_ROOT)} -> {target}")

    assert failures == []


def _is_external_or_in_page(target: str) -> bool:
    lowered = target.lower()
    return (
        lowered.startswith(("http://", "https://", "mailto:", "file:"))
        or lowered.startswith("#")
    )


def _is_inside_repo(path: Path) -> bool:
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


def _strip_code_spans_and_blocks(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]+`", "", text)
