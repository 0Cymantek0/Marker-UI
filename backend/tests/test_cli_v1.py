"""CLI v1 expansion subprocess tests (UCM-007)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(
    args: list[str],
    *,
    cwd: Path,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MARKER_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'marker-cli-v1.db'}"
    env.pop("MARKER_WORKSPACE_ROOTS", None)
    env.pop("MARKER_OUTPUT_ROOT", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )


def test_cli_v1_help_lists_new_groups_and_global_flags(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    completed = _run_cli(["--help"], cwd=backend_root, tmp_path=tmp_path)

    assert completed.returncode == 0
    assert "doctor" in completed.stdout
    assert "schema" in completed.stdout
    assert "batch" in completed.stdout
    assert "server" in completed.stdout
    assert "--no-input" in completed.stdout
    assert "--dry-run" in completed.stdout
    assert "--version" in completed.stdout


def test_cli_version_flag_uses_environment_version_metadata(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    completed = _run_cli(
        ["--version"],
        cwd=backend_root,
        tmp_path=tmp_path,
        extra_env={"MARKER_VERSION": "7.6.5-test", "MARKER_COMMIT_SHA": "abc123"},
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "marker 7.6.5-test (abc123)"
    assert completed.stderr == ""


def test_schema_export_and_mcp_init_config_emit_stable_json(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    schema = _run_cli(["schema", "export", "--json"], cwd=backend_root, tmp_path=tmp_path)
    config = _run_cli(["mcp", "init-config", "--client", "codex", "--json"], cwd=backend_root, tmp_path=tmp_path)

    assert schema.returncode == 0
    schema_payload = json.loads(schema.stdout)
    assert schema_payload["schema_version"] == "marker.agent_contract.v1"
    assert "BatchRequestModel" in schema_payload["models"]
    assert config.returncode == 0
    config_payload = json.loads(config.stdout)
    assert config_payload["mcpServers"]["marker"]["args"] == ["-m", "app.cli", "mcp", "start"]


def test_grouped_jobs_and_server_status_use_json_contracts(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    jobs = _run_cli(["jobs", "list", "--json"], cwd=backend_root, tmp_path=tmp_path)
    server = _run_cli(["server", "status", "--json"], cwd=backend_root, tmp_path=tmp_path)

    assert jobs.returncode == 0
    jobs_payload = json.loads(jobs.stdout)
    assert jobs_payload["page"] == 1
    assert jobs_payload["jobs"] == []
    assert server.returncode == 0
    server_payload = json.loads(server.stdout)
    assert server_payload["schema_version"] == "marker.server_status.v1"
    assert server_payload["managed_by_cli"] is False


def test_grouped_output_read_alias_reads_marker_manifest_output(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "scores.tsv"
    out_dir = tmp_path / "out"
    source.write_text("city\tvalue\nKolkata\t10\n", encoding="utf-8")

    converted = _run_cli(
        ["convert", str(source), "--output-dir", str(out_dir), "--json"],
        cwd=backend_root,
        tmp_path=tmp_path,
    )
    assert converted.returncode == 0
    output_path = json.loads(converted.stdout)["output"]["text_path"]

    read = _run_cli(["output", "read", output_path, "--limit", "200", "--json"], cwd=backend_root, tmp_path=tmp_path)

    assert read.returncode == 0
    payload = json.loads(read.stdout)
    assert "| Kolkata | 10 |" in payload["text"]


def test_batch_partial_failure_returns_exit_10_and_records_item_errors(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    good = tmp_path / "good.tsv"
    missing = tmp_path / "missing.tsv"
    good.write_text("a\tb\n1\t2\n", encoding="utf-8")

    completed = _run_cli(
        ["batch", str(good), str(missing), "--output-dir", str(tmp_path / "out"), "--json"],
        cwd=backend_root,
        tmp_path=tmp_path,
    )

    assert completed.returncode == 10
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "marker.batch_result.v1"
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1
    assert payload["failures"][0]["error"]["code"] == "INPUT_NOT_FOUND"


def test_batch_resume_skips_existing_explicit_output_path(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "good.tsv"
    output = tmp_path / "out.md"
    request = tmp_path / "batch.json"
    source.write_text("a\tb\n1\t2\n", encoding="utf-8")
    output.write_text("already done", encoding="utf-8")
    request.write_text(
        json.dumps({"items": [{"local_file_path": str(source), "output_path": str(output)}], "resume": True}),
        encoding="utf-8",
    )

    completed = _run_cli(["batch", "--request-json", str(request), "--json"], cwd=backend_root, tmp_path=tmp_path)

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["succeeded"] == 0
    assert payload["skipped"] == 1
    assert payload["results"][0]["skipped"] is True


def test_parse_time_json_errors_use_marker_error_schema(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]

    completed = _run_cli(["convert", "--bad-flag", "--json"], cwd=backend_root, tmp_path=tmp_path)

    assert completed.returncode == 2
    payload = json.loads(completed.stderr)
    assert payload["schema_version"] == "marker.error.v1"
    assert payload["error"]["code"] == "USAGE_ERROR"
