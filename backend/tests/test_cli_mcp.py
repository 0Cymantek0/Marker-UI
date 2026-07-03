from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import agent_api
from app.errors import OutputExistsError
from app.main import _app_state
from app.agent_api import AgentConversionOptions, convert_document, plan_conversion, read_output, self_test
from app.database import Base
from app.models.job import ConversionJob
from app.models.settings import Setting
from app.services.conversion_service import ConversionService
from app.services.marker_service import MarkerService
from app.services.task_manager import TaskManager
from app.utils.secrets import decrypt_value, encrypt_value
import app.services.task_manager as task_manager_module


@pytest.mark.asyncio
async def test_agent_api_converts_tsv_through_real_service(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\nbeta\t2\n", encoding="utf-8")

    result = await convert_document(
        local_file_path=str(source),
        output_dir=str(tmp_path / "out"),
        max_chars=5000,
        options=AgentConversionOptions(output_format="markdown"),
    )

    assert result["ok"] is True
    assert "| name | score |" in result["text_preview"]
    assert "| alpha | 1 |" in result["text_preview"]
    assert result["metadata"]["engine"]["engine"] == "text_data"
    assert Path(result["output"]["text_path"]).read_text(encoding="utf-8") == result["text_preview"]


@pytest.mark.asyncio
async def test_agent_api_plans_text_without_loading_marker(tmp_path: Path):
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")

    result = await plan_conversion(local_file_path=str(source))

    assert result["preliminary"] is False
    assert result["plan"]["engine"] == "text_data"
    assert result["plan"]["needs_marker_models"] is False


def test_cli_convert_command_writes_real_output(tmp_path: Path):
    source = tmp_path / "data.csv"
    source.write_text("city,value\nKolkata,10\nDhaka,20\nSylhet,30\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "convert",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--options-json",
            '{"text_data_max_rows": 2}',
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )

    data = json.loads(completed.stdout)
    out_path = Path(data["output"]["text_path"])
    assert out_path.is_file()
    assert "| city | value |" in out_path.read_text(encoding="utf-8")
    assert "Only first 2 rows shown" in out_path.read_text(encoding="utf-8")
    assert data["metadata"]["engine"]["engine"] == "text_data"


def test_cli_exposes_agent_productivity_knobs_directly(tmp_path: Path):
    source = tmp_path / "data.csv"
    source.write_text("city,value\nKolkata,10\nDhaka,20\nSylhet,30\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "convert",
            str(source),
            "--output-dir",
            str(tmp_path / "out-direct"),
            "--text-data-max-rows",
            "2",
            "--smart-router-level",
            "smart",
            "--no-router-enabled",
            "--decorative-max-text-density",
            "0.2",
            "--ocr-min-lines",
            "3",
            "--archive-max-files",
            "50",
            "--no-archive-recursive",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )

    data = json.loads(completed.stdout)
    out_path = Path(data["output"]["text_path"])
    text = out_path.read_text(encoding="utf-8")
    assert "| city | value |" in text
    assert "| Kolkata | 10 |" in text
    assert "Only first 2 rows shown" in text
    assert data["metadata"]["engine"]["engine"] == "text_data"

def test_read_output_reads_bounded_chunk_without_full_file_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "large.md"
    output.write_text("0123456789" * 100, encoding="utf-8")
    monkeypatch.setenv("MARKER_OUTPUT_ROOT", str(tmp_path))

    def fail_read_text(*args, **kwargs):
        raise AssertionError("read_output must not load full file with Path.read_text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    result = read_output(str(output), offset=10, limit=15)

    assert result["text"] == "012345678901234"
    assert result["text_chars"] == 1000
    assert result["has_more"] is True
    assert result["next_offset"] == 25


@pytest.mark.asyncio
async def test_self_test_reports_expected_tools_and_real_conversion():
    result = await self_test(include_conversion=True)

    assert result["capabilities_ok"] is True
    assert result["conversion_ok"] is True


@pytest.mark.asyncio
async def test_agent_api_settings_and_jobs_use_real_database_paths(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)

    secret = "dummy-openai-key-for-test-1234567890"
    saved = await agent_api.set_setting("openai_api_key", secret, category="llm")
    assert saved["value"] != secret
    assert "*" in saved["value"]

    row = await db_session.get(Setting, 1)
    assert row is not None
    assert row.value != secret
    assert decrypt_value(row.value) == secret

    listed = await agent_api.list_settings(category="llm")
    serialized = json.dumps(listed)
    assert "openai_api_key" in serialized
    assert secret not in serialized

    db_session.add(
        Setting(
            key="llm_providers",
            value=json.dumps(
                [
                    {
                        "id": "openai",
                        "api_key": encrypt_value("dummy-nested-key-for-test-123456"),
                        "fallback_api_keys": [encrypt_value("dummy-fallback-key-for-test-123456")],
                        "models": [{"model_id": "gpt-test", "max_output_tokens": 4096}],
                    }
                ]
            ),
            category="llm",
        )
    )
    await db_session.commit()
    providers_listed = await agent_api.list_settings(category="llm")
    providers_json = json.dumps(providers_listed)
    assert "dummy-nested-key" not in providers_json
    assert "dummy-fallback-key" not in providers_json
    assert "gAAAA" not in providers_json
    assert "max_output_tokens" in providers_json

    result_file = tmp_path / "finished.md"
    result_file.write_text("# Converted\n\nDone.", encoding="utf-8")
    db_session.add(
        ConversionJob(
            id="11111111-1111-4111-8111-111111111111",
            filename="finished.csv",
            original_name="finished.csv",
            status="completed",
            input_format="csv",
            output_format="markdown",
            config_json='{"converter_cls": "TableConverter"}',
            result_text=result_file.read_text(encoding="utf-8"),
            result_metadata_json='{"engine": {"engine": "text_data"}}',
            result_path=str(result_file),
            progress=100,
        )
    )
    await db_session.commit()

    history = await agent_api.list_jobs(page=1, page_size=5)
    assert history["total"] == 1
    assert history["jobs"][0]["converter"] == "TableConverter"
    assert history["jobs"][0]["conversion_metadata"]["engine"]["engine"] == "text_data"

    status = await agent_api.get_job_status(
        "11111111-1111-4111-8111-111111111111",
        include_result_text=True,
        max_chars=20,
    )
    assert status["status"] == "completed"
    assert status["result_text"].startswith("# Converted")


@pytest.mark.asyncio
async def test_agent_api_submit_job_uses_real_task_manager_and_conversion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source = tmp_path / "queued.tsv"
    source.write_text("name\tscore\nalpha\t1\nbeta\t2\n", encoding="utf-8")

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)
    monkeypatch.setattr(task_manager_module, "async_session_factory", session_factory)

    original_task_manager = _app_state.task_manager
    original_conversion_service = _app_state.conversion_service
    task_manager = TaskManager(max_workers=1)
    _app_state.task_manager = task_manager
    _app_state.conversion_service = ConversionService(MarkerService())
    try:
        submitted = await agent_api.submit_conversion_job(
            local_file_path=str(source),
            output_dir=str(tmp_path / "job-out"),
            options=AgentConversionOptions(output_format="markdown"),
        )
        assert submitted["job_id"]

        final = None
        for _ in range(50):
            final = await agent_api.get_job_status(
                submitted["job_id"],
                include_result_text=True,
                max_chars=5000,
            )
            if final["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.1)

        assert final is not None
        assert final["status"] == "completed"
        assert "| name | score |" in final["result_text"]
        assert Path(final["result_path"]).is_file()
    finally:
        task_manager.shutdown(wait=False)
        _app_state.task_manager = original_task_manager
        _app_state.conversion_service = original_conversion_service
        await engine.dispose()



@pytest.mark.asyncio
async def test_mcp_delete_job_output_schema_is_files_removed_list(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """UCM-004.1: marker_delete_job structured output must declare files_removed as list[str]."""
    import app.mcp_server as mcp_server

    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)

    result_file = tmp_path / "deleted.md"
    result_file.write_text("# gone", encoding="utf-8")
    db_session.add(
        ConversionJob(
            id="22222222-2222-4221-8222-222222222222",
            filename="deleted.csv",
            original_name="deleted.csv",
            status="completed",
            input_format="csv",
            output_format="markdown",
            config_json="{}",
            result_text="# gone",
            result_path=str(result_file),
            progress=100,
        )
    )
    await db_session.commit()

    tools = await mcp_server.mcp.list_tools()
    delete_tool = next(tool for tool in tools if tool.name == "marker_delete_job")
    files_removed_schema = delete_tool.outputSchema["properties"]["files_removed"]
    assert files_removed_schema["type"] == "array"
    assert files_removed_schema["items"]["type"] == "string"

    result = await agent_api.delete_job(
        "22222222-2222-4221-8222-222222222222",
        delete_files=True,
    )
    assert isinstance(result["files_removed"], list)
    assert any(Path(path).name == "deleted.md" for path in result["files_removed"])


@pytest.mark.asyncio
async def test_agent_cancel_job_marks_cancelled_without_deleting_row(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)

    cancelled_calls: list[str] = []

    async def fake_cancel(job_id: str) -> None:
        cancelled_calls.append(job_id)

    monkeypatch.setattr(agent_api, "_cancel_job_best_effort", fake_cancel)

    job_id = "33333333-3333-4333-8333-333333333333"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename="pending.csv",
            original_name="pending.csv",
            status="pending",
            input_format="csv",
            output_format="markdown",
            config_json="{}",
            progress=12,
        )
    )
    await db_session.commit()

    result = await agent_api.cancel_job(job_id)

    assert result == {"status": "cancelled", "job_id": job_id, "cancelled": True}
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.progress == 0
    assert cancelled_calls == [job_id]


@pytest.mark.asyncio
async def test_agent_cancel_job_does_not_cancel_completed_job(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)

    cancelled_calls: list[str] = []

    async def fake_cancel(job_id: str) -> None:
        cancelled_calls.append(job_id)

    monkeypatch.setattr(agent_api, "_cancel_job_best_effort", fake_cancel)

    job_id = "44444444-4444-4444-8444-444444444444"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename="done.csv",
            original_name="done.csv",
            status="completed",
            input_format="csv",
            output_format="markdown",
            config_json="{}",
            progress=100,
        )
    )
    await db_session.commit()

    result = await agent_api.cancel_job(job_id)

    assert result == {"status": "completed", "job_id": job_id, "cancelled": False}
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "completed"
    assert row.progress == 100
    assert cancelled_calls == []


def test_mcp_streamable_http_refuses_non_loopback_without_auth_token():
    from app.mcp_server import run

    try:
        run(transport="streamable-http", host="0.0.0.0", port=8765)
    except ValueError as exc:
        assert "MARKER_MCP_AUTH_TOKEN" in str(exc)
    else:  # pragma: no cover - run must refuse before starting server
        raise AssertionError("non-loopback streamable HTTP started without auth token")


def test_mcp_streamable_http_configures_bearer_auth_for_non_loopback(monkeypatch: pytest.MonkeyPatch):
    import app.mcp_server as mcp_server

    called = {}

    def fake_run(*, transport: str):
        called["transport"] = transport

    monkeypatch.setattr(mcp_server.mcp, "run", fake_run)

    mcp_server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8765,
        auth_token="test-token",
    )

    assert called == {"transport": "streamable-http"}
    assert mcp_server.mcp.settings.host == "0.0.0.0"
    assert mcp_server.mcp.settings.port == 8765
    assert mcp_server.mcp.settings.auth is not None
    assert mcp_server.mcp._token_verifier is not None
@pytest.mark.asyncio
async def test_mcp_tools_have_complete_input_metadata_and_output_schemas():
    import app.mcp_server as mcp_server

    tools = await mcp_server.mcp.list_tools()
    for tool in tools:
        assert tool.outputSchema["type"] == "object", tool.name
        assert tool.outputSchema.get("properties"), tool.name
        assert any(
            prop.get("description") and prop.get("examples")
            for prop in tool.outputSchema["properties"].values()
        ), tool.name
        for name, prop in tool.inputSchema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name} missing description"
            assert prop.get("examples"), f"{tool.name}.{name} missing examples"
            numeric_types = {prop.get("type")}
            numeric_types.update(item.get("type") for item in prop.get("anyOf", []))
            if {"integer", "number"} & numeric_types:
                assert any(key in prop for key in ("minimum", "maximum")), (
                    f"{tool.name}.{name} missing numeric bounds"
                )


@pytest.mark.asyncio
async def test_mcp_convert_schema_has_rich_descriptions_and_nullable_booleans():
    import app.mcp_server as mcp_server

    tools = await mcp_server.mcp.list_tools()
    schema = next(tool for tool in tools if tool.name == "marker_convert_file").inputSchema
    properties = schema["properties"]

    assert properties["local_file_path"]["description"]
    assert properties["max_chars"]["maximum"] == agent_api.MAX_READ_CHARS
    assert properties["router_enabled"].get("default") is None
    assert {item.get("type") for item in properties["router_enabled"].get("anyOf", [])} == {"boolean", "null"}
    assert properties["archive_recursive"].get("default") is None
    assert {item.get("type") for item in properties["archive_recursive"].get("anyOf", [])} == {"boolean", "null"}



def test_mcp_extra_options_omit_unspecified_booleans():
    import app.mcp_server as mcp_server

    assert mcp_server._image_understanding_extra_options(
        router_enabled=None,
        smart_router_level="",
        dedup_enabled=None,
        downscale_vlm_crops=None,
        batch_enabled=None,
        ocr_engine="",
        decorative_max_text_density=-1,
        ocr_min_text_density=-1,
        ocr_min_lines=0,
        dedup_max_distance=-1,
        vlm_crop_max_px=0,
        vlm_batch_size=0,
        max_batch_retries=-1,
    ) == {}
    assert mcp_server._agent_productivity_extra_options(archive_recursive=None) == {}


@pytest.mark.asyncio
async def test_agent_api_converts_same_file_without_clobbering_previous_output(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")
    output_dir = tmp_path / "out"

    first = await convert_document(
        local_file_path=str(source),
        output_dir=str(output_dir),
        max_chars=5000,
        options=AgentConversionOptions(output_format="markdown"),
    )
    first_path = Path(first["output"]["text_path"])
    first_path.write_text("sentinel", encoding="utf-8")

    second = await convert_document(
        local_file_path=str(source),
        output_dir=str(output_dir),
        max_chars=5000,
        options=AgentConversionOptions(output_format="markdown"),
    )
    second_path = Path(second["output"]["text_path"])
    second_manifest = Path(second["output"]["manifest_path"])

    assert first_path != second_path
    assert first_path.read_text(encoding="utf-8") == "sentinel"
    assert "| alpha | 1 |" in second_path.read_text(encoding="utf-8")
    assert second_manifest.is_file()
    manifest = json.loads(second_manifest.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "marker.output_manifest.v1"
    assert manifest["output"]["text_path"] == str(second_path.resolve())


@pytest.mark.asyncio
async def test_agent_api_explicit_output_path_refuses_existing_file(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")
    output_path = tmp_path / "out" / "fixed.md"
    output_path.parent.mkdir()
    output_path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(OutputExistsError):
        await convert_document(
            local_file_path=str(source),
            output_path=str(output_path),
            max_chars=5000,
            options=AgentConversionOptions(output_format="markdown"),
        )

    assert output_path.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.asyncio
async def test_agent_api_explicit_output_path_raises_typed_output_exists_error(tmp_path: Path):
    """UCM-002: existing output path raises OutputExistsError with stable code."""
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")
    output_path = tmp_path / "out" / "fixed.md"
    output_path.parent.mkdir()
    output_path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(OutputExistsError) as exc_info:
        await convert_document(
            local_file_path=str(source),
            output_path=str(output_path),
            max_chars=5000,
            options=AgentConversionOptions(output_format="markdown"),
        )
    assert exc_info.value.code == "OUTPUT_EXISTS"
    assert exc_info.value.exit_code == 11


def test_marker_pyproject_exposes_console_entrypoint():
    project_root = Path(__file__).resolve().parents[2]
    pyproject_path = project_root / "pyproject.toml"

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["marker"] == "app.cli:main"
    packages = data["tool"]["setuptools"]["packages"]["find"]
    assert packages["where"] == ["backend"]
    assert packages["include"] == ["app*"]



def test_repo_root_module_cli_self_test_works():
    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "backend")
    env["MARKER_PRELOAD_MODELS"] = "false"

    completed = subprocess.run(
        [sys.executable, "-m", "app.cli", "self-test", "--no-conversion", "--json"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )

    data = json.loads(completed.stdout)
    assert data["service"] == "marker_mcp"
    assert data["capabilities_ok"] is True



@pytest.mark.asyncio
async def test_mcp_server_lists_tools_self_tests_and_converts(tmp_path: Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    backend_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["MARKER_PRELOAD_MODELS"] = "false"
    env["ENCRYPTION_KEY"] = "dGVzdC1lbmNyeXB0aW9uLWtleS1mb3ItdW5pdHRlc3Q="

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.cli", "mcp"],
        cwd=backend_dir,
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            assert set(names).issuperset({
                "marker_convert_file",
                "marker_convert_local_file",
                "marker_convert_url",
                "marker_delete_job",
                "marker_delete_setting",
                "marker_get_job_status",
                "marker_get_setting",
                "marker_list_capabilities",
                "marker_list_jobs",
                "marker_list_settings",
                "marker_plan_conversion",
                "marker_plan_local_file",
                "marker_plan_url",
                "marker_read_output",
                "marker_self_test",
                "marker_set_setting",
                "marker_submit_job",
                "marker_submit_local_job",
                "marker_submit_url_job",
            })

            convert_tool = next(tool for tool in tools.tools if tool.name == "marker_convert_file")
            assert "text_data_max_rows" in convert_tool.inputSchema["properties"]
            assert "archive_max_files" in convert_tool.inputSchema["properties"]

            response = await session.call_tool("marker_self_test", {"include_conversion": True})
            payload = json.loads(response.content[0].text)
            assert payload["tools_ok"] is True
            assert payload["resources_ok"] is True
            assert payload["prompts_ok"] is True
            assert payload["conversion_ok"] is True

            source = tmp_path / "agent.csv"
            source.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
            convert_response = await session.call_tool(
                "marker_convert_file",
                {
                    "local_file_path": str(source),
                    "output_dir": str(tmp_path / "mcp-out"),
                    "max_chars": 12,
                    "paginate_output": True,
                    "disable_image_extraction": False,
                    "extra_options_json": '{"text_data_max_rows": 10}',
                },
            )
            converted = json.loads(convert_response.content[0].text)
            assert converted["ok"] is True
            assert converted["truncated"] is True
            assert converted["metadata"]["engine"]["engine"] == "text_data"
            assert Path(converted["output"]["manifest_path"]).is_file()

            read_response = await session.call_tool(
                "marker_read_output",
                {"output_path": converted["output"]["text_path"], "offset": 0, "limit": 200},
            )
            read_payload = json.loads(read_response.content[0].text)
            assert "| name | value |" in read_payload["text"]
            assert read_payload["has_more"] is False
