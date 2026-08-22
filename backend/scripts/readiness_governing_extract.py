#!/usr/bin/env python
"""Regenerate / verify the committed release-governing masterplan extract.

The working master plan lives under the gitignored ``planning/`` tree, so a
clean checkout has no governing source for the readiness integrity audit
(``readiness_audit.py --mode integrity``). To keep that command hermetic
without publishing work-in-progress planning material, the repository commits
a verbatim extract of the single governing section — V3.2 amendment 23C, the
62 readiness invariants — at
``backend/readiness/governing/masterplan-amendment-23c.md``.

That committed extract IS the release-governing authority for integrity
audits: the 62-entry inventory JSON cannot silently drift from it, and the
extract itself can only change by rerunning this script against a real
master plan.

Usage (repository root, from a machine that has the planning tree):

  python backend/scripts/readiness_governing_extract.py \
      --source planning/v2/marker-ui-v2-Masterplan.md --write

  python backend/scripts/readiness_governing_extract.py \
      --source planning/v2/marker-ui-v2-Masterplan.md --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from readiness.inventory import _ITEM_RE, _SECTION_HEADER  # noqa: E402

EXTRACT_PATH = REPO_ROOT / "backend" / "readiness" / "governing" / "masterplan-amendment-23c.md"


def extract_section_text(source: Path) -> str:
    """Return the governing section verbatim, header through last content line."""
    lines = source.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == _SECTION_HEADER)
    except StopIteration:
        raise SystemExit(f"[error] governing section not found in {source}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = lines[start:end]
    while section and not section[-1].strip():
        section.pop()
    return "\n".join(section) + "\n"


def render_extract(section_text: str, source: Path) -> str:
    """Wrap the verbatim section in provenance front matter for reviewers."""
    numbered = sum(1 for line in section_text.splitlines() if _ITEM_RE.match(line))
    provenance = (
        "<!-- Release-governing extract. DO NOT EDIT BY HAND.\n"
        "     Regenerate with:\n"
        "       python backend/scripts/readiness_governing_extract.py \\\n"
        f"         --source {source.as_posix()} --write\n"
        "     Everything below the provenance block is the VERBATIM governing\n"
        "     section from the master plan; integrity audits parse only that\n"
        "     section, so any hand edit shows up as inventory drift. -->\n\n"
        f"<!-- extracted-items: {numbered} -->\n\n"
    )
    return provenance + section_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="local master plan path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the committed extract")
    group.add_argument("--check", action="store_true", help="verify the extract matches the source")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_file():
        print(f"[error] source master plan not found: {source}")
        return 1

    section = extract_section_text(source)
    rendered = render_extract(section, source)

    if args.write:
        EXTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXTRACT_PATH.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"[ok] wrote {EXTRACT_PATH}")
        return 0

    if not EXTRACT_PATH.is_file():
        print(f"[error] committed extract missing: {EXTRACT_PATH}")
        return 1
    committed = EXTRACT_PATH.read_text(encoding="utf-8")
    if committed == rendered:
        print("[ok] committed extract matches the source governing section")
        return 0
    print(
        "[error] committed extract drifted from the source governing section; "
        "regenerate with --write and review the diff"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
