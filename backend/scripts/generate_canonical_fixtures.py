"""Intentional regeneration tool for the canonical identity fixtures.

The golden corpus at ``backend/conformance/fixtures/canonical_vectors_v1.json``
carries committed expected outputs. The conformance suite compares
against those constants and never rewrites them; this script is the
only sanctioned way to regenerate them, so every change shows up as a
reviewable diff.

Usage (from the repository root or ``backend/``)::

    python backend/scripts/generate_canonical_fixtures.py           # report drift
    python backend/scripts/generate_canonical_fixtures.py --write   # rewrite

Run after an INTENTIONAL contract change (profile bump, framing
change, new cases). Never run to "fix" a failing conformance suite
without understanding why the outputs moved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from conformance.fixture_codec import (  # noqa: E402
    compute_expected,
    load_fixture_corpus,
)

FIXTURE_PATH = BACKEND_DIR / "conformance" / "fixtures" / "canonical_vectors_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite committed expected outputs (review the diff!)",
    )
    args = parser.parse_args()

    corpus = load_fixture_corpus(FIXTURE_PATH)
    positives = 0
    negatives = 0
    drift = 0

    for case in corpus["cases"]:
        if "expect_error" in case:
            # Rejection cases carry authored message fragments; the
            # conformance suite proves they raise. Nothing to compute.
            negatives += 1
            continue
        computed = compute_expected(case)
        if case.get("expect") != computed:
            drift += 1
            print(f"DRIFT {case['id']}")
            for key, value in computed.items():
                print(f"  {key}: {value}")
        case["expect"] = computed
        positives += 1

    print(f"{positives} golden cases, {negatives} rejection cases, {drift} drifting")

    if args.write:
        with open(FIXTURE_PATH, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(corpus, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"rewrote {FIXTURE_PATH}")
        return 0

    if drift:
        print("expected outputs drifted; rerun with --write if intentional")
        return 1
    print("committed expected outputs are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
