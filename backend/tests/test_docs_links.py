from __future__ import annotations

import argparse
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


def test_cli_guide_batch_json_flag_matches_parser() -> None:
    from app.cli import _build_parser

    cli_guide = (REPO_ROOT / "docs" / "usage" / "cli.md").read_text(encoding="utf-8")
    parser = _build_parser()
    top_subparser = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    batch_parser = top_subparser.choices["batch"]
    batch_flags = {
        option
        for action in batch_parser._actions
        for option in action.option_strings
    }

    assert "--request-json" in batch_flags
    assert "--manifest" not in batch_flags
    assert "batch --request-json" in cli_guide
    assert "batch --manifest" not in cli_guide


def test_cli_guide_documents_first_class_audio_flags() -> None:
    cli_guide = (REPO_ROOT / "docs" / "usage" / "cli.md").read_text(encoding="utf-8")

    for flag in (
        "--audio-provider",
        "--audio-diarization",
        "--audio-speaker-alias",
        "--audio-text-enhancement",
        "--audio-structural-enhancement",
        "--audio-contradiction-detection",
        "--audio-allow-cloud-stt",
    ):
        assert flag in cli_guide


def test_mcp_guide_documents_url_open_world_and_audio_controls() -> None:
    mcp_guide = (REPO_ROOT / "docs" / "usage" / "mcp.md").read_text(encoding="utf-8")

    for text in (
        "openWorldHint=true",
        "openWorldHint=false",
        "audio_provider",
        "audio_allow_cloud_stt",
        "audio_speaker_aliases_json",
        "audio_contradiction_detection",
    ):
        assert text in mcp_guide


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
