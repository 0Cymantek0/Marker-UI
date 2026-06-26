"""Tests for workspace/output path policy (UCM-006)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agent_api import AgentConversionOptions, convert_document, read_output
from app.errors import InputNotAllowedError
from app.services.output_writer import write_conversion_output
from app.mcp_server import _path_from_root_uri
from app.services.policy import assert_local_input_allowed, scoped_client_workspace_roots, workspace_roots


def test_workspace_roots_parse_os_pathsep(monkeypatch, tmp_path: Path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", f"{one}{os.pathsep}{two}")

    assert workspace_roots() == [one.resolve(), two.resolve()]


def test_local_input_policy_allows_path_under_workspace_root(monkeypatch, tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "doc.tsv"
    source.write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(root))

    assert_local_input_allowed(source)


def test_local_input_policy_denies_path_outside_workspace_root(monkeypatch, tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.tsv"
    outside.write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(root))

    with pytest.raises(InputNotAllowedError) as exc_info:
        assert_local_input_allowed(outside)

    assert exc_info.value.code == "INPUT_NOT_ALLOWED"
    assert "MARKER_WORKSPACE_ROOTS" in exc_info.value.message


def test_client_workspace_roots_deny_path_outside_client_root(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MARKER_WORKSPACE_ROOTS", raising=False)
    client_root = tmp_path / "client"
    client_root.mkdir()
    outside = tmp_path / "outside.tsv"
    outside.write_text("a\tb\n1\t2\n", encoding="utf-8")

    with scoped_client_workspace_roots([client_root]):
        with pytest.raises(InputNotAllowedError) as exc_info:
            assert_local_input_allowed(outside)

    assert "MCP client roots" in exc_info.value.message


def test_client_workspace_roots_allow_path_inside_client_and_server_roots(monkeypatch, tmp_path: Path):
    server_root = tmp_path / "workspace"
    client_root = server_root / "project"
    client_root.mkdir(parents=True)
    source = client_root / "doc.tsv"
    source.write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(server_root))

    with scoped_client_workspace_roots([client_root]):
        assert_local_input_allowed(source)


def test_mcp_file_root_uri_parses_to_local_path():
    parsed = _path_from_root_uri("file:///tmp/marker-workspace")

    assert parsed == Path("/tmp/marker-workspace")


def test_read_output_policy_denies_path_outside_output_root(monkeypatch, tmp_path: Path):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("nope", encoding="utf-8")
    monkeypatch.setenv("MARKER_OUTPUT_ROOT", str(output_root))

    with pytest.raises(InputNotAllowedError) as exc_info:
        read_output(str(outside))

    assert "MARKER_OUTPUT_ROOT" in exc_info.value.message


def test_read_output_policy_denies_unregistered_path_without_output_root(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MARKER_OUTPUT_ROOT", raising=False)
    outside = tmp_path / "outside.md"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(InputNotAllowedError) as exc_info:
        read_output(str(outside))

    assert "registered Marker output" in exc_info.value.message


def test_read_output_policy_allows_marker_manifest_output_without_output_root(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MARKER_OUTPUT_ROOT", raising=False)
    written = write_conversion_output(
        {"text": "hello", "extension": "md"},
        source_name="doc.tsv",
        output_base=tmp_path,
        output_format="markdown",
    )

    payload = read_output(str(written.text_path))

    assert payload["text"] == "hello"


def test_read_output_policy_allows_path_under_output_root(monkeypatch, tmp_path: Path):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    inside = output_root / "inside.md"
    inside.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("MARKER_OUTPUT_ROOT", str(output_root))

    payload = read_output(str(inside))

    assert payload["text"] == "hello"


@pytest.mark.asyncio
async def test_convert_document_denies_output_dir_outside_output_root_before_conversion(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    output_root = tmp_path / "outputs"
    bad_output = tmp_path / "bad-output"
    workspace.mkdir()
    output_root.mkdir()
    bad_output.mkdir()
    source = workspace / "doc.tsv"
    source.write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setenv("MARKER_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("MARKER_OUTPUT_ROOT", str(output_root))

    with pytest.raises(InputNotAllowedError) as exc_info:
        await convert_document(
            local_file_path=str(source),
            output_dir=str(bad_output),
            options=AgentConversionOptions(output_format="markdown"),
        )

    assert "MARKER_OUTPUT_ROOT" in exc_info.value.message
