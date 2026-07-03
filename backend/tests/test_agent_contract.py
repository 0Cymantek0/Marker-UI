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
        "extra_options",
    }.issubset(option_names)


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
