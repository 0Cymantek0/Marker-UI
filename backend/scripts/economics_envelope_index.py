"""Compose + validate the economics evidence into one coverage index.

Reads the committed envelope artifacts (local, industrial, visual),
validates each against the fail-closed contract, and emits
``pr87-economics-envelope-index.json``: a dimension x profile coverage
matrix with per-artifact SHA-256 provenance and structural checks that
pin the invariant-57/58 closure claims — WAL amplification must be
measured on the industrial profile, database rows and cold starts on
both storage profiles, the visual OFF/ON comparison must exist with a
reproducible disposition, and no profile may report an invalid
envelope. The index is what the readiness ledger binds; a missing,
stale, or hand-weakened artifact fails the checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.eval.economics.contract import DIMENSION_SETS  # noqa: E402
from app.eval.economics.validate import validate_envelope  # noqa: E402

MEASUREMENTS = BACKEND.parent / "docs" / "reference" / "measurements"
INDEX_SCHEMA = "marker.economics_envelope_index.v1"

#: profile -> artifact file; the index is deliberately explicit so a
#: renamed or missing artifact fails instead of being silently skipped
PROFILE_ARTIFACTS = {
    "local": "pr87a-local-economics-envelope.json",
    "industrial": "pr87b-industrial-economics-envelope.json",
    "visual": "pr87c-visual-economics.json",
}


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def build_index(measurements_dir: Path) -> dict:
    artifacts: dict[str, dict] = {}
    problems: list[str] = []

    for profile, filename in PROFILE_ARTIFACTS.items():
        path = measurements_dir / filename
        if not path.is_file():
            problems.append(f"{profile} artifact missing: {filename}")
            continue
        parsed = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_envelope(parsed)
        artifacts[profile] = {
            "path": path.relative_to(measurements_dir.parent.parent).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dimension_set": parsed.get("dimension_set"),
            "run_mode": parsed.get("run_mode"),
            "git_sha": parsed.get("git_sha"),
            "validation_errors": errors,
            "statuses": {
                name: metric.get("status")
                for name, metric in (parsed.get("dimensions") or {}).items()
            },
        }
        if errors:
            problems.append(f"{profile} envelope failed validation: {errors[0]}")

    def _status(profile: str, dimension: str) -> str | None:
        entry = artifacts.get(profile)
        if entry is None:
            return None
        if dimension not in entry["statuses"]:
            # the artifact's dimension set does not include this dimension;
            # that is a category boundary, not silent omission (the contract
            # already requires every dimension of the declared set)
            return "not_in_set"
        return entry["statuses"][dimension]

    coverage: dict[str, dict[str, str | None]] = {}
    for dimension in DIMENSION_SETS["invariant_57"]:
        coverage[dimension] = {
            "local": _status("local", dimension),
            "industrial": _status("industrial", dimension),
        }
    # the visual experiment measures the visual-storage dimension of the
    # invariant-57 set as its invariant-58 storage_delta (OFF vs ON)
    coverage["visual_storage"]["visual"] = _status("visual", "storage_delta")
    for dimension in DIMENSION_SETS["invariant_58"]:
        coverage[dimension] = {"visual": _status("visual", dimension)}

    def _explicitly_stated(states: dict[str, str | None]) -> bool:
        # every profile that reports this dimension states it explicitly
        # (measured/derived/unavailable/not_applicable); "None" means the
        # artifact is missing entirely, which problems[] already catches
        return all(state is not None for state in states.values())

    checks = {
        "all_artifacts_valid": all(
            not entry["validation_errors"] for entry in artifacts.values()
        ) and len(artifacts) == len(PROFILE_ARTIFACTS),
        "every_dimension_explicitly_stated": all(
            _explicitly_stated(coverage[dimension]) for dimension in coverage
        ),
        "storage_dimensions_measured_where_implemented": (
            _is_measured(coverage["fts_storage"]["industrial"])
            and _is_measured(coverage["visual_storage"]["visual"])
        ),
        "vector_storage_absence_or_measurement_declared": all(
            state in ("measured", "derived", "not_applicable", "unavailable")
            for state in (
                coverage["vector_storage"]["local"],
                coverage["vector_storage"]["industrial"],
            )
        ),
        "wal_amplification_measured_on_industrial": _is_measured(
            coverage["wal_write_amplification"]["industrial"]
        ),
        "database_rows_measured_on_local": _is_measured(
            coverage["database_rows"]["local"]
        ),
        "database_rows_measured_on_industrial": _is_measured(
            coverage["database_rows"]["industrial"]
        ),
        "cold_start_measured_on_local": _is_measured(coverage["cold_start"]["local"]),
        "cold_start_measured_on_industrial": _is_measured(
            coverage["cold_start"]["industrial"]
        ),
        "review_burden_reported_on_local": _is_measured(
            coverage["review_burden"]["local"]
        ),
        "reprocessing_measured_on_local": _is_measured(
            coverage["reprocessing"]["local"]
        ),
        "reprocessing_measured_on_industrial": _is_measured(
            coverage["reprocessing"]["industrial"]
        ),
        "fts_storage_attributed_on_industrial": _is_measured(
            coverage["fts_storage"]["industrial"]
        ),
        "visual_storage_measured_by_visual_experiment": _is_measured(
            coverage["visual_storage"]["visual"]
        ),
        "visual_disabled_state_proved": _is_measured(
            coverage["disabled_state_proof"]["visual"]
        ),
        "visual_decision_recorded": _is_measured(coverage["decision"]["visual"]),
        "acl_complexity_measured": _is_measured(coverage["acl_complexity"]["visual"]),
    }

    return {
        "schema": INDEX_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "artifacts": artifacts,
        "coverage": coverage,
        "checks": checks,
        "problems": problems,
        "pass": all(checks.values()) and not problems,
    }


def _is_measured(status: str | None) -> bool:
    return status in ("measured", "derived")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--measurements", type=Path, default=MEASUREMENTS,
        help="directory holding the envelope artifacts",
    )
    parser.add_argument(
        "--output", type=Path,
        default=MEASUREMENTS / "pr87-economics-envelope-index.json",
    )
    args = parser.parse_args()

    index = build_index(args.measurements)
    print(json.dumps({"checks": index["checks"], "problems": index["problems"],
                      "pass": index["pass"]}, indent=2))
    if not index["pass"]:
        print("economics envelope index FAILED", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(index, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
