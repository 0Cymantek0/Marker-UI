"""Generate Markitdown sidecar outputs for the format benchmark.

This script is intentionally outside app runtime. It imports Markitdown from the
local read-only reference checkout (or an explicitly supplied source path),
converts the manual format fixtures, and writes ``<sample_id>.md`` plus metadata
sidecars for ``run_format_benchmark.py --markitdown-output-dir``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.benchmark.format_corpus import manual_format_benchmark_cases


DEFAULT_MARKITDOWN_SRC = (
    ROOT / "references" / "markitdown" / "packages" / "markitdown" / "src"
)
_PRIVATE_PATH_RE = re.compile(
    r"([A-Za-z]:\\Users\\[^\s`'\"<>]+|/Users/[^\s`'\"<>]+|/home/[^\s`'\"<>]+)",
    re.IGNORECASE,
)


class _MarkitdownLike(Protocol):
    def convert_local(self, path: str) -> Any:
        ...


@dataclass
class GeneratedMarkitdownOutput:
    sample_id: str
    status: str
    output_path: str | None = None
    metadata_path: str | None = None
    elapsed_s: float | None = None
    error: str | None = None


def _repo_relative(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return resolved.name


def _sanitize_markdown(markdown: str) -> str:
    """Remove machine-local paths from generated sidecar text."""

    def replace(match: re.Match[str]) -> str:
        path = match.group(0)
        return _repo_relative(path)

    return _PRIVATE_PATH_RE.sub(replace, markdown)


def _load_markitdown(markitdown_src: str | Path) -> type:
    src = Path(markitdown_src)
    if not src.is_dir():
        raise RuntimeError(f"Markitdown source directory not found: {_repo_relative(src)}")
    sys.path.insert(0, str(src))
    try:
        from markitdown import MarkItDown
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Markitdown dependency missing. Install Markitdown reference dependencies "
            "(at least base dependencies plus docx/pptx/xlsx extras) before generating sidecars."
        ) from exc
    return MarkItDown


def generate_outputs(
    *,
    fixture_dir: str | Path,
    output_dir: str | Path,
    markitdown: _MarkitdownLike,
) -> list[GeneratedMarkitdownOutput]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: list[GeneratedMarkitdownOutput] = []

    for case in manual_format_benchmark_cases(fixture_dir):
        start = time.perf_counter()
        output_path = out / f"{case.sample_id}.md"
        metadata_path = out / f"{case.sample_id}.metadata.json"
        try:
            converted = markitdown.convert_local(str(case.source_path))
            markdown = _sanitize_markdown(str(getattr(converted, "markdown", "") or ""))
            elapsed_s = round(time.perf_counter() - start, 3)
            output_path.write_text(markdown, encoding="utf-8")
            metadata = {
                "engine": {
                    "engine": "markitdown",
                    "label": "Markitdown",
                    "needs_cloud": False,
                },
                "source_path": _repo_relative(case.source_path),
                "elapsed_s": elapsed_s,
            }
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            results.append(
                GeneratedMarkitdownOutput(
                    sample_id=case.sample_id,
                    status="ok",
                    output_path=_repo_relative(output_path),
                    metadata_path=_repo_relative(metadata_path),
                    elapsed_s=elapsed_s,
                )
            )
        except Exception as exc:
            elapsed_s = round(time.perf_counter() - start, 3)
            error_path = out / f"{case.sample_id}.error.txt"
            error = f"{type(exc).__name__}: {exc}"
            error_path.write_text(error, encoding="utf-8")
            results.append(
                GeneratedMarkitdownOutput(
                    sample_id=case.sample_id,
                    status="error",
                    output_path=None,
                    metadata_path=None,
                    elapsed_s=elapsed_s,
                    error=error,
                )
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        default=str(BACKEND / "tests" / "fixtures" / "manual_real_docs"),
        help="Directory containing manual source fixtures.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(
            BACKEND
            / "tests"
            / "fixtures"
            / "manual_real_docs"
            / "markitdown_outputs"
        ),
        help="Directory for generated Markitdown sidecars.",
    )
    parser.add_argument(
        "--markitdown-src",
        default=str(DEFAULT_MARKITDOWN_SRC),
        help="Local Markitdown src directory.",
    )
    args = parser.parse_args()

    try:
        markitdown_cls = _load_markitdown(args.markitdown_src)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    results = generate_outputs(
        fixture_dir=args.fixture_dir,
        output_dir=args.output_dir,
        markitdown=markitdown_cls(),
    )
    summary_path = Path(args.output_dir) / "generation_summary.json"
    summary = [result.__dict__ for result in results]
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"summary={_repo_relative(summary_path)}")
    return 0 if all(result.status == "ok" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
