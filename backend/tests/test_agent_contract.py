"""Tests for shared agent contract schemas (UCM-001)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent_contract import (
    AUDIO_OUTPUT_MODES,
    CONTRACT_SCHEMA_VERSION,
    ConversionOptionsModel,
    ConvertResultModel,
    OPTION_METADATA,
    OutputManifestModel,
    export_json_schemas,
)


def test_export_json_schemas_contains_core_models_and_metadata():
    schemas = export_json_schemas()

    assert schemas["schema_version"] == CONTRACT_SCHEMA_VERSION
    models = schemas["models"]
    for name in (
        "ConversionOptionsModel",
        "PlanRequestModel",
        "PlanResultModel",
        "ConvertRequestModel",
        "ConvertResultModel",
        "SubmitJobRequestModel",
        "JobStatusModel",
        "OutputManifestModel",
        "MarkerErrorModel",
        "BatchRequestModel",
        "BatchResultModel",
    ):
        assert name in models
        assert models[name]["type"] == "object"

    option_names = {item["name"] for item in schemas["option_metadata"]}
    assert {
        "output_format",
        "allow_cloud_vlm",
        "ocr_engine",
        "hybrid_ocr_profile",
        "router_enabled",
        "vlm_batch_size",
        "text_data_max_rows",
        "chunking_strategy",
        "chunk_max_tokens",
        "allow_chunking_fallback",
        "archive_recursive",
        "archive_max_total_uncompressed_bytes",
        "archive_max_compression_ratio",
        "extra_options",
    }.issubset(option_names)

    option_properties = models["ConversionOptionsModel"]["properties"]
    assert option_properties["smart_router_level"]["anyOf"][0]["enum"] == [
        "disabled",
        "smart",
        "beeg_brain",
    ]
    assert option_properties["vlm_batch_size"]["anyOf"][0]["maximum"] == 64
    assert option_properties["chunking_strategy"]["anyOf"][0]["enum"] == [
        "markdown_heading_blocks_v2",
        "unstructured_by_title",
    ]
    assert option_properties["chunk_max_tokens"]["anyOf"][0]["minimum"] == 16
    assert option_properties["allow_chunking_fallback"]["default"] is False
    assert option_properties["archive_max_depth"]["anyOf"][0]["minimum"] == 0
    assert option_properties["archive_max_compression_ratio"]["anyOf"][0]["minimum"] == 1.0


def test_option_metadata_cli_flags_exist_on_convert_parser():
    """Schema metadata must not advertise CLI flags argparse cannot parse."""
    import argparse

    from app.cli import _build_parser

    parser = _build_parser()
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    convert_parser = subparsers.choices["convert"]
    convert_flags = {
        option
        for action in convert_parser._actions
        for option in action.option_strings
    }
    global_flags = {
        option
        for action in parser._actions
        for option in action.option_strings
    }

    missing = [
        (metadata.name, metadata.cli_flag)
        for metadata in OPTION_METADATA
        if metadata.cli_flag
        and metadata.cli_flag.startswith("--")
        and "/" not in metadata.cli_flag
        and metadata.cli_flag not in convert_flags
        and metadata.cli_flag not in global_flags
    ]

    assert missing == []


def test_option_metadata_covers_conversion_options_model_fields():
    """Every first-class agent option needs exported docs/metadata."""
    option_fields = set(ConversionOptionsModel.model_fields)
    metadata_names = {metadata.name for metadata in OPTION_METADATA}

    assert sorted(option_fields - metadata_names) == []
    assert sorted(metadata_names - option_fields) == ["audio_config"]


def test_audio_diarization_metadata_is_capability_gated():
    metadata = {item.name: item for item in OPTION_METADATA}

    description = metadata["audio_diarization"].description
    assert "rejected unless" in description
    assert "supports diarization" in description


def test_conversion_options_validate_known_enums_and_extra_options():
    opts = ConversionOptionsModel(
        output_format="markdown",
        image_handling_mode="both",
        conversion_profile="high_accuracy",
        ocr_engine="hybrid_ocr",
        hybrid_ocr_profile="low_vram",
        extra_options={"text_data_max_rows": 10},
    )

    assert opts.output_format == "markdown"
    assert opts.ocr_engine == "hybrid_ocr"
    assert opts.hybrid_ocr_profile == "low_vram"
    assert opts.extra_options == {"text_data_max_rows": 10}

    for mode in ("interview_qna", "action_decision_log"):
        audio_opts = ConversionOptionsModel(audio_output_mode=mode)
        assert audio_opts.audio_output_mode == mode
        assert mode in AUDIO_OUTPUT_MODES

    with pytest.raises(ValueError):
        ConversionOptionsModel(output_format="docx")
    with pytest.raises(ValueError):
        ConversionOptionsModel(ocr_engine="glm_ocr")


def test_conversion_options_validate_agent_productivity_fields():
    opts = ConversionOptionsModel(
        text_data_max_rows=10,
        chunking_strategy="unstructured_by_title",
        chunk_max_tokens=512,
        allow_chunking_fallback=True,
        archive_recursive=False,
        archive_max_files=25,
        archive_max_total_uncompressed_bytes=4096,
        archive_max_compression_ratio=50.0,
        archive_max_depth=0,
        router_enabled=False,
        smart_router_level="beeg_brain",
        decorative_max_text_density=0.05,
        ocr_min_lines=1,
        vlm_crop_max_px=512,
        vlm_batch_size=4,
        max_batch_retries=0,
    )

    assert opts.archive_recursive is False
    assert opts.chunking_strategy == "unstructured_by_title"
    assert opts.chunk_max_tokens == 512
    assert opts.allow_chunking_fallback is True
    assert opts.archive_max_total_uncompressed_bytes == 4096
    assert opts.archive_max_compression_ratio == 50.0
    assert opts.router_enabled is False
    assert opts.smart_router_level == "beeg_brain"
    assert opts.vlm_batch_size == 4

    with pytest.raises(ValueError):
        ConversionOptionsModel(text_data_max_rows=0)
    with pytest.raises(ValueError):
        ConversionOptionsModel(chunking_strategy="fixed_windows")
    with pytest.raises(ValueError):
        ConversionOptionsModel(chunk_max_tokens=15)
    with pytest.raises(ValueError):
        ConversionOptionsModel(smart_router_level="huge")
    with pytest.raises(ValueError):
        ConversionOptionsModel(vlm_batch_size=65)


def test_convert_result_and_manifest_models_accept_current_output_shape(tmp_path: Path):
    text_path = tmp_path / "out.md"
    manifest_path = tmp_path / "out.marker.json"
    text_path.write_text("hello", encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")

    result = ConvertResultModel(
        ok=True,
        source={"name": "input.tsv"},
        output={
            "text_path": str(text_path),
            "manifest_path": str(manifest_path),
            "asset_paths": [],
            "media_type": "text/markdown",
        },
        text_preview="hello",
        text_chars=5,
        truncated=False,
    )
    assert result.output.manifest_path == str(manifest_path)

    manifest = OutputManifestModel(
        created_at="2026-06-26T00:00:00+00:00",
        source={"name": "input.tsv", "source_url": None},
        output={
            "final_path": str(text_path),
            "text_path": str(text_path),
            "manifest_path": str(manifest_path),
            "media_type": "text/markdown",
            "text_chars": 5,
            "text_sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "asset_count": 0,
            "assets": [],
        },
        conversion={"config": {}, "metadata": {}},
    )
    assert manifest.schema_version == "marker.output_manifest.v1"


def test_agent_contract_import_does_not_load_marker_modules():
    backend_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir)
    code = (
        "import json, sys; "
        "import app.agent_contract as c; "
        "schemas = c.export_json_schemas(); "
        "print(json.dumps({'schema_version': schemas['schema_version'], "
        "'marker_loaded': any(name == 'marker' or name.startswith('marker.') for name in sys.modules)}))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload == {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "marker_loaded": False,
    }
