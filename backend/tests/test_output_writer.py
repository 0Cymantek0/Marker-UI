"""Tests for shared conversion output writer (UCM-003)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.errors import OutputExistsError
from app.services.output_writer import (
    OUTPUT_MANIFEST_SCHEMA_VERSION,
    write_conversion_output,
)


def test_write_conversion_output_creates_manifest_and_collision_safe_file(tmp_path: Path):
    result = {
        "text": "# Report\n\nbody",
        "extension": "md",
        "images": {},
        "metadata": {"engine": {"engine": "text_data"}},
    }

    first = write_conversion_output(
        result,
        source_name="report.tsv",
        output_base=tmp_path,
        conversion_config={"output_format": "markdown", "api_key": "secret-value"},
    )
    second = write_conversion_output(
        result,
        source_name="report.tsv",
        output_base=tmp_path,
        conversion_config={"output_format": "markdown"},
    )

    assert first.text_path != second.text_path
    assert first.text_path.name == "report.md"
    assert second.text_path.name == "report-1.md"
    assert first.manifest_path.is_file()

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == OUTPUT_MANIFEST_SCHEMA_VERSION
    assert manifest["output"]["text_path"] == str(first.text_path.resolve())
    assert manifest["output"]["manifest_path"] == str(first.manifest_path.resolve())
    assert manifest["output"]["text_chars"] == len(result["text"])
    assert manifest["output"]["text_sha256"]
    assert manifest["conversion"]["metadata"]["engine"] == {"engine": "text_data"}
    assert manifest["conversion"]["config"]["api_key"] == "****"


def test_write_conversion_output_refuses_existing_explicit_output_path(tmp_path: Path):
    output_path = tmp_path / "fixed.md"
    output_path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(OutputExistsError):
        write_conversion_output(
            {"text": "new", "extension": "md"},
            source_name="source.tsv",
            output_base=tmp_path,
            output_path=output_path,
        )

    assert output_path.read_text(encoding="utf-8") == "sentinel"


def test_write_conversion_output_bundles_assets_and_sanitizes_paths(tmp_path: Path):
    result = {
        "text": "hello",
        "extension": "md",
        "images": {"../unsafe image.png": b"img"},
        "assets": [
            {
                "name": "../sheets/Sheet 1.csv",
                "media_type": "text/csv",
                "data": b"col\nval\n",
            }
        ],
        "metadata": {},
    }

    written = write_conversion_output(
        result,
        source_name="input.tsv",
        output_base=tmp_path,
        layout="directory_if_assets",
        job_id="job-1",
    )

    assert written.final_path.is_dir()
    assert written.text_path == written.final_path / "input.md"
    assert written.manifest_path == written.final_path / "input.marker.json"
    assert (written.final_path / "unsafe_image.png").read_bytes() == b"img"
    assert (written.final_path / "sheets" / "Sheet_1.csv").read_bytes() == b"col\nval\n"

    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    names = sorted(asset["name"] for asset in manifest["output"]["assets"])
    assert names == ["sheets/Sheet_1.csv", "unsafe_image.png"]
    assert all(asset["sha256"] for asset in manifest["output"]["assets"])
