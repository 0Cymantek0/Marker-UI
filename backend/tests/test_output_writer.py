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
    assert manifest["output"]["text_path"] == "report.md"
    assert manifest["output"]["manifest_path"] == "report.marker.json"
    assert manifest["output"]["final_path"] == "report.md"
    assert manifest["output"]["text_chars"] == len(result["text"])
    assert manifest["output"]["text_sha256"]
    assert manifest["conversion"]["metadata"]["engine"] == {"engine": "text_data"}
    assert manifest["conversion"]["config"]["api_key"] == "****"
    assert not Path(manifest["output"]["text_path"]).is_absolute()


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
    assert manifest["output"]["text_path"] == "input.md"
    assert manifest["output"]["manifest_path"] == "input.marker.json"
    assert manifest["output"]["final_path"] == "."
    assert all(not Path(asset["path"]).is_absolute() for asset in manifest["output"]["assets"])


def test_write_conversion_output_deduplicates_image_asset_names(tmp_path: Path):
    result = {
        "text": "hello",
        "extension": "md",
        "images": {
            "figure.png": b"first",
            "nested/figure.png": b"second",
        },
        "metadata": {},
    }

    written = write_conversion_output(
        result,
        source_name="input.tsv",
        output_base=tmp_path,
        layout="directory_if_assets",
    )

    assert (written.final_path / "figure.png").read_bytes() == b"first"
    assert (written.final_path / "figure-1.png").read_bytes() == b"second"
    assert sorted(entry["name"] for entry in written.asset_entries) == ["figure-1.png", "figure.png"]


def test_write_conversion_output_deduplicates_nested_asset_names(tmp_path: Path):
    result = {
        "text": "hello",
        "extension": "md",
        "assets": [
            {"name": "tables/data.csv", "media_type": "text/csv", "data": b"first"},
            {"name": "tables/data.csv", "media_type": "text/csv", "data": b"second"},
        ],
        "metadata": {},
    }

    written = write_conversion_output(
        result,
        source_name="input.tsv",
        output_base=tmp_path,
        layout="directory_if_assets",
    )

    assert (written.final_path / "tables" / "data.csv").read_bytes() == b"first"
    assert (written.final_path / "tables" / "data-1.csv").read_bytes() == b"second"
    assert sorted(entry["name"] for entry in written.asset_entries) == [
        "tables/data-1.csv",
        "tables/data.csv",
    ]


def test_write_conversion_output_deduplicates_image_and_asset_collision(tmp_path: Path):
    result = {
        "text": "hello",
        "extension": "md",
        "images": {"shared.bin": b"image"},
        "assets": [
            {"name": "shared.bin", "media_type": "application/octet-stream", "data": b"asset"},
        ],
        "metadata": {},
    }

    written = write_conversion_output(
        result,
        source_name="input.tsv",
        output_base=tmp_path,
        layout="directory_if_assets",
    )

    assert (written.final_path / "shared.bin").read_bytes() == b"image"
    assert (written.final_path / "shared-1.bin").read_bytes() == b"asset"
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    entries = sorted(manifest["output"]["assets"], key=lambda item: item["name"])
    assert [entry["name"] for entry in entries] == ["shared-1.bin", "shared.bin"]
    assert [entry["relative_path"] for entry in entries] == ["shared-1.bin", "shared.bin"]
    assert len({entry["path"] for entry in entries}) == 2
    assert all(not Path(entry["path"]).is_absolute() for entry in entries)


def test_write_conversion_output_uses_result_media_type_for_chunks_explicit_path(tmp_path: Path):
    output_path = tmp_path / "fixed"
    result = {
        "text": '{"schema_version":"marker.chunks.v1","chunks":[]}',
        "extension": "json",
        "images": {},
        "metadata": {"chunking": {"schema_version": "marker.chunks.v1"}},
    }

    written = write_conversion_output(
        result,
        source_name="source.tsv",
        output_base=tmp_path,
        output_path=output_path,
        output_format="chunks",
    )

    assert written.text_path == output_path
    assert written.media_type == "application/json"
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    assert manifest["output"]["media_type"] == "application/json"


def test_write_conversion_output_names_primary_chunks_artifact_distinctly(tmp_path: Path):
    result = {
        "text": '{"schema_version":"marker.chunks.v1","chunks":[]}',
        "extension": "json",
        "images": {},
        "metadata": {"chunking": {"schema_version": "marker.chunks.v1"}},
    }

    written = write_conversion_output(
        result,
        source_name="source.tsv",
        output_base=tmp_path,
        output_format="chunks",
        conversion_config={"output_format": "chunks"},
    )

    assert written.text_path.name == "source.chunks.json"
    assert written.manifest_path.name == "source.chunks.marker.json"
    assert written.media_type == "application/json"
    manifest = json.loads(written.manifest_path.read_text(encoding="utf-8"))
    assert manifest["output"]["text_path"] == "source.chunks.json"
    assert manifest["output"]["media_type"] == "application/json"
