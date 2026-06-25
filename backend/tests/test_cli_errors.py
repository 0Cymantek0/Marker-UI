"""CLI subprocess tests for the MarkerError contract (UCM-002)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_cli_missing_input_json_returns_stable_error_and_exit_3(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    missing = tmp_path / "does-not-exist.pdf"

    completed = _run_cli(
        ["convert", str(missing), "--output-dir", str(tmp_path / "out"), "--json"],
        cwd=backend_root,
    )

    assert completed.returncode == 3
    payload = json.loads(completed.stderr)
    assert payload == {
        "ok": False,
        "schema_version": "marker.error.v1",
        "error": {
            "code": "INPUT_NOT_FOUND",
            "message": payload["error"]["message"],
            "hint": "Check the path or pass --source-url for remote files.",
            "details": {"path": str(missing)},
            "retryable": False,
        },
    }
    # primary result still on stdout (empty here), error on stderr only
    assert completed.stdout == ""


def test_cli_unsupported_suffix_non_json_returns_exit_4(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    bad = tmp_path / "thing.xyz"
    bad.write_text("nope", encoding="utf-8")

    completed = _run_cli(
        ["convert", str(bad), "--output-dir", str(tmp_path / "out")],
        cwd=backend_root,
    )

    assert completed.returncode == 4
    assert completed.stderr.startswith("Error: Unsupported file type")
    assert "USAGE" not in completed.stderr


def test_cli_unsupported_suffix_json_returns_exit_4_payload(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    bad = tmp_path / "thing.xyz"
    bad.write_text("nope", encoding="utf-8")

    completed = _run_cli(
        ["convert", str(bad), "--output-dir", str(tmp_path / "out"), "--json"],
        cwd=backend_root,
    )

    assert completed.returncode == 4
    payload = json.loads(completed.stderr)
    assert payload["schema_version"] == "marker.error.v1"
    assert payload["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert payload["error"]["details"]["suffix"] == ".xyz"
    assert payload["error"]["retryable"] is False


def test_cli_usage_error_missing_input_and_url_returns_exit_2(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    # argparse requires a subcommand; no positional input and no source url:
    # convert_document raises UsageError only after parser dispatch, but the
    # parser also accepts no positional (nargs='?'). Providing neither path nor
    # url reaches the agent seam and yields USAGE_ERROR exit 2.
    completed = _run_cli(["convert", "--json"], cwd=backend_root)

    assert completed.returncode == 2
    payload = json.loads(completed.stderr)
    assert payload["error"]["code"] == "USAGE_ERROR"


def test_cli_output_exists_json_returns_exit_11(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "fixed.md"
    target.write_text("sentinel", encoding="utf-8")

    completed = _run_cli(
        [
            "convert",
            str(source),
            "--output-path",
            str(target),
            "--json",
        ],
        cwd=backend_root,
    )

    assert completed.returncode == 11
    payload = json.loads(completed.stderr)
    assert payload["error"]["code"] == "OUTPUT_EXISTS"
    assert payload["error"]["hint"] is not None
    assert target.read_text(encoding="utf-8") == "sentinel"


def test_cli_debug_emits_stack_trace_and_still_exits_clean(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    missing = tmp_path / "missing.pdf"

    completed = _run_cli(
        ["--debug", "convert", str(missing), "--json"],
        cwd=backend_root,
    )

    assert completed.returncode == 3
    assert "Traceback (most recent call last)" in completed.stderr
    # JSON payload still present (after the trace)
    assert "marker.error.v1" in completed.stderr


def test_cli_job_status_missing_json_returns_exit_3(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    completed = _run_cli(
        ["job-status", "nonexistent-job-id", "--json"],
        cwd=backend_root,
    )

    assert completed.returncode == 3
    payload = json.loads(completed.stderr)
    assert payload["error"]["code"] == "INPUT_NOT_FOUND"
    assert payload["error"]["details"]["job_id"] == "nonexistent-job-id"


@pytest.mark.parametrize(
    "code,exc_cls",
    [
        ("INPUT_NOT_FOUND", "FileNotFoundError"),
        ("OUTPUT_EXISTS", "FileExistsError"),
    ],
)
def test_exit_code_constants_match_codes(code: str, exc_cls: str):
    from app import errors as e

    assert e.ERROR_CLASSES[code].code == code
    assert e.EXIT_CODE_BY_CODE[code] >= 1
