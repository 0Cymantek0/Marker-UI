from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import agent_api
from app.errors import OutputExistsError, UnsupportedFormatError, UsageError
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


@contextmanager
def mcp_profile(profile: str):
    import app.mcp_server as mcp_server

    previous = mcp_server.MCP_ACTIVE_TOOL_PROFILE
    mcp_server.configure_mcp_tool_profile(profile)
    try:
        yield mcp_server
    finally:
        mcp_server.configure_mcp_tool_profile(previous)


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
async def test_agent_api_converts_tsv_to_chunks_json(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\nbeta\t2\n", encoding="utf-8")

    result = await convert_document(
        local_file_path=str(source),
        output_dir=str(tmp_path / "out"),
        max_chars=5000,
        options=AgentConversionOptions(output_format="chunks"),
    )

    output_path = Path(result["output"]["text_path"])
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert output_path.suffix == ".json"
    assert result["output"]["media_type"] == "application/json"
    assert payload["schema_version"] == "marker.chunks.v1"
    assert payload["chunk_kind"] == "semantic_markdown"
    assert payload["chunk_count"] == len(payload["chunks"])
    assert "| alpha | 1 |" in payload["chunks"][-1]["text"]
    assert result["metadata"]["chunking"]["chunk_kind"] == "semantic_markdown"


def test_agent_capabilities_and_config_include_frontend_audio_modes():
    caps = agent_api.capabilities()

    for mode in ("interview_qna", "action_decision_log"):
        assert mode in caps["audio_output_modes"]
        config = agent_api.build_conversion_config(AgentConversionOptions(audio_output_mode=mode))
        assert config["audio_output_mode"] == mode


def test_agent_build_conversion_config_preserves_advanced_audio_options():
    config = agent_api.build_conversion_config(
        AgentConversionOptions(
            audio_provider="local_faster_whisper",
            audio_language="en",
            audio_vad_filter=False,
            audio_diarization=True,
            audio_min_speakers=2,
            audio_speaker_aliases={"speaker_0": "Alice"},
            audio_vocabulary_pack_ids=["team"],
            audio_text_enhancement_enabled=True,
            audio_text_enhancement_strength=3,
            audio_structural_enhancement_enabled=True,
            audio_structural_enhancement_mode="meeting_notes",
            audio_contradiction_detection=True,
            audio_allow_cloud_stt=True,
            audio_benchmark_compare=True,
            audio_compare_providers=["local_faster_whisper"],
        )
    )

    assert config["audio_provider"] == "local_faster_whisper"
    assert config["audio_language"] == "en"
    assert config["audio_vad_filter"] is False
    assert config["audio_diarization"] is True
    assert config["audio_min_speakers"] == 2
    assert config["audio_speaker_aliases"] == {"speaker_0": "Alice"}
    assert config["audio_vocabulary_pack_ids"] == ["team"]
    assert config["audio_text_enhancement_enabled"] is True
    assert config["audio_text_enhancement_strength"] == 3
    assert config["audio_structural_enhancement_enabled"] is True
    assert config["audio_structural_enhancement_mode"] == "meeting_notes"
    assert config["audio_contradiction_detection"] is True
    assert config["audio_allow_cloud_stt"] is True
    assert config["audio_benchmark_compare"] is True
    assert config["audio_compare_providers"] == ["local_faster_whisper"]


def test_agent_capabilities_expose_minimal_non_admin_tools():
    caps = agent_api.capabilities()

    assert "marker_cancel_job" in caps["tools"]
    assert "marker_delete_job" not in caps["tools"]


def test_agent_surface_registry_drives_agent_capabilities_and_mcp_profiles():
    import app.agent_surface as agent_surface
    import app.mcp_server as mcp_server

    caps = agent_api.capabilities()

    assert caps["tools"] == list(agent_surface.DEFAULT_AGENT_TOOL_NAMES)
    assert mcp_server.MCP_V1_TOOL_NAMES == list(agent_surface.MCP_V1_TOOL_NAMES)
    assert mcp_server.MCP_MINIMAL_TOOL_NAMES == list(agent_surface.MCP_MINIMAL_TOOL_NAMES)
    assert mcp_server.MCP_FULL_TOOL_NAMES == list(agent_surface.MCP_FULL_TOOL_NAMES)
    assert mcp_server.MCP_ADMIN_TOOL_NAMES == list(agent_surface.MCP_ADMIN_TOOL_NAMES)
    assert set(agent_surface.DEFAULT_AGENT_TOOL_NAMES).issubset(agent_surface.MCP_ALL_TOOL_NAMES)
    assert mcp_server.MCP_ALL_TOOL_NAMES == list(agent_surface.MCP_ALL_TOOL_NAMES)
    assert set(agent_surface.MCP_TOOL_SPEC_BY_NAME) == set(agent_surface.MCP_ALL_TOOL_NAMES)
    assert all(spec.scopes for spec in agent_surface.MCP_TOOL_SPECS)
    assert set(agent_surface.MCP_RESOURCE_SPEC_BY_URI) == set(agent_surface.MCP_RESOURCE_URIS)
    assert all(spec.scopes for spec in agent_surface.MCP_RESOURCE_SPECS)


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


@pytest.mark.asyncio
async def test_mcp_v2_convert_uses_source_object(tmp_path: Path):
    import app.mcp_server as mcp_server

    source = tmp_path / "data.csv"
    source.write_text("city,value\nKolkata,10\n", encoding="utf-8")

    result = await mcp_server.marker_convert(
        None,
        mcp_server.SourceInput(kind="local_path", path=str(source)),
        output_dir=str(tmp_path / "out"),
        max_chars=5000,
        output_format="markdown",
    )

    out_path = Path(result["output"]["text_path"])
    assert out_path.is_file()
    assert "| Kolkata | 10 |" in out_path.read_text(encoding="utf-8")
    assert result["resource_links"]["manifest"].startswith("marker://outputs/")


def test_cli_convert_accepts_request_json_file(tmp_path: Path):
    source = tmp_path / "data.csv"
    source.write_text("city,value\nKolkata,10\nDhaka,20\n", encoding="utf-8")
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "out" / "fixed.md"
    request_path.write_text(
        json.dumps(
            {
                "local_file_path": str(source),
                "output_path": str(output_path),
                "max_chars": 5000,
                "options": {"output_format": "markdown", "extra_options": {"text_data_max_rows": 1}},
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "convert",
            "--request-json",
            str(request_path),
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )

    data = json.loads(completed.stdout)
    assert Path(data["output"]["text_path"]) == output_path
    assert output_path.is_file()
    assert "Only first 1 rows shown" in output_path.read_text(encoding="utf-8")


def test_cli_convert_accepts_stdin_json_with_overwrite(tmp_path: Path):
    source = tmp_path / "data.csv"
    source.write_text("city,value\nKolkata,10\n", encoding="utf-8")
    output_path = tmp_path / "out" / "fixed.md"
    output_path.parent.mkdir()
    output_path.write_text("sentinel", encoding="utf-8")
    request = {
        "local_file_path": str(source),
        "output_path": str(output_path),
        "overwrite": True,
        "max_chars": 5000,
        "options": {"output_format": "markdown"},
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "convert",
            "--stdin-json",
            "--json",
        ],
        input=json.dumps(request),
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )

    data = json.loads(completed.stdout)
    assert Path(data["output"]["text_path"]) == output_path
    assert "sentinel" not in output_path.read_text(encoding="utf-8")
    assert "| Kolkata | 10 |" in output_path.read_text(encoding="utf-8")


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
    assert result["chunk_kind"] == "offset_text"
    assert result["is_semantic_chunk"] is False


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

    fetched = await agent_api.get_setting("openai_api_key", category="llm")
    assert fetched["key"] == "openai_api_key"
    with pytest.raises(agent_api.InputNotFoundError):
        await agent_api.get_setting("openai_api_key", category="ui")

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

    await agent_api.delete_setting("llm_providers", category="ui")
    still_present = await agent_api.get_setting("llm_providers", category="llm")
    assert still_present["key"] == "llm_providers"

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
async def test_agent_submit_rejects_deferred_audio_provider_before_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source = tmp_path / "call.wav"
    source.write_bytes(b"RIFF fake wav")

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'agent-audio.db'}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)
    monkeypatch.setattr(agent_api, "_db_tables_ready", False)

    try:
        with pytest.raises(UsageError, match="not shipped yet"):
            await agent_api.submit_conversion_job(
                local_file_path=str(source),
                options=AgentConversionOptions(
                    audio_provider="openai",
                    audio_allow_cloud_stt=True,
                ),
            )

        async with session_factory() as session:
            rows = (await session.execute(select(ConversionJob))).scalars().all()
        assert rows == []
    finally:
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

    mcp_server.configure_mcp_tool_profile("admin")
    try:
        tools = await mcp_server.mcp.list_tools()
    finally:
        mcp_server.configure_mcp_tool_profile("minimal")
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


@pytest.mark.asyncio
async def test_agent_delete_job_rejects_live_job_without_force(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    @asynccontextmanager
    async def session_factory():
        yield db_session

    monkeypatch.setattr(agent_api, "_db_session_factory", session_factory)

    job_id = "55555555-5555-4555-8555-555555555555"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename="running.csv",
            original_name="running.csv",
            status="processing",
            input_format="csv",
            output_format="markdown",
            config_json="{}",
            progress=50,
        )
    )
    await db_session.commit()

    with pytest.raises(agent_api.UsageError) as exc_info:
        await agent_api.delete_job(job_id)

    assert "cancel it first or pass force=true" in str(exc_info.value)
    row = await db_session.get(ConversionJob, job_id)
    assert row is not None
    assert row.status == "processing"


@pytest.mark.asyncio
async def test_agent_delete_job_force_deletes_live_job(
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

    job_id = "66666666-6666-4666-8666-666666666666"
    db_session.add(
        ConversionJob(
            id=job_id,
            filename="running.csv",
            original_name="running.csv",
            status="processing",
            input_format="csv",
            output_format="markdown",
            config_json="{}",
            progress=50,
        )
    )
    await db_session.commit()

    result = await agent_api.delete_job(job_id, force=True, delete_files=False)

    assert result == {"status": "deleted", "job_id": job_id, "files_removed": []}
    assert await db_session.get(ConversionJob, job_id) is None
    assert cancelled_calls == [job_id]


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
async def test_mcp_default_tool_profile_is_minimal():
    import app.mcp_server as mcp_server

    mcp_server.configure_mcp_tool_profile("minimal")
    tools = await mcp_server.mcp.list_tools()
    names = [tool.name for tool in tools]
    caps = await mcp_server.marker_list_capabilities()

    assert len(names) <= 10
    assert names == mcp_server.MCP_MINIMAL_TOOL_NAMES
    assert caps["tools"] == names
    assert "marker_convert" in names
    assert "marker_convert_file" not in names
    assert "marker_delete_job" not in names
    assert "marker_set_setting" not in names
    assert "marker_delete_setting" not in names


@pytest.mark.asyncio
async def test_mcp_full_and_admin_profiles_gate_destructive_tools(monkeypatch: pytest.MonkeyPatch):
    import app.mcp_server as mcp_server

    monkeypatch.delenv("MARKER_MCP_ENABLE_SETTINGS_WRITE", raising=False)
    mcp_server.configure_mcp_tool_profile("full")
    try:
        full_names = [tool.name for tool in await mcp_server.mcp.list_tools()]
        assert "marker_convert_url" in full_names
        assert "marker_delete_job" not in full_names
        assert "marker_set_setting" not in full_names
        assert "marker_delete_setting" not in full_names

        mcp_server.configure_mcp_tool_profile("admin")
        admin_names = [tool.name for tool in await mcp_server.mcp.list_tools()]
        assert "marker_delete_job" in admin_names
        assert "marker_set_setting" not in admin_names
        assert "marker_delete_setting" not in admin_names

        monkeypatch.setenv("MARKER_MCP_ENABLE_SETTINGS_WRITE", "true")
        mcp_server.configure_mcp_tool_profile("admin")
        write_names = [tool.name for tool in await mcp_server.mcp.list_tools()]
        assert "marker_set_setting" in write_names
        assert "marker_delete_setting" in write_names
    finally:
        mcp_server.configure_mcp_tool_profile("minimal")


@pytest.mark.asyncio
async def test_mcp_tool_profile_env_fallback(monkeypatch: pytest.MonkeyPatch):
    import app.mcp_server as mcp_server

    monkeypatch.setenv("MARKER_MCP_TOOL_PROFILE", "full")
    mcp_server.configure_mcp_tool_profile(None)
    try:
        names = [tool.name for tool in await mcp_server.mcp.list_tools()]
        assert "marker_convert_url" in names
        assert "marker_delete_job" not in names
    finally:
        mcp_server.configure_mcp_tool_profile("minimal")


def test_mcp_inspect_honors_tool_profile():
    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "backend")
    env["MARKER_PRELOAD_MODELS"] = "false"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "mcp",
            "inspect",
            "--tool-profile",
            "admin",
            "--json",
        ],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )

    data = json.loads(completed.stdout)
    assert data["mcp"]["tool_profile"] == "admin"
    assert "marker_delete_job" in data["tools"]


@pytest.mark.asyncio
async def test_mcp_tools_have_complete_input_metadata_and_output_schemas():
    import app.mcp_server as mcp_server

    mcp_server.configure_mcp_tool_profile("admin")
    try:
        tools = await mcp_server.mcp.list_tools()
    finally:
        mcp_server.configure_mcp_tool_profile("minimal")
    tools_by_name = {tool.name: tool for tool in tools}
    chunk_reader = tools_by_name["marker_read_output_chunk"]
    assert "offset" in (chunk_reader.description or "").lower()
    assert "semantic" in (chunk_reader.description or "").lower()
    assert chunk_reader.outputSchema["properties"]["chunk_kind"]["examples"] == ["offset_text"]
    assert chunk_reader.outputSchema["properties"]["is_semantic_chunk"]["examples"] == [False]

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

    mcp_server.configure_mcp_tool_profile("full")
    try:
        tools = await mcp_server.mcp.list_tools()
    finally:
        mcp_server.configure_mcp_tool_profile("minimal")
    schema = next(tool for tool in tools if tool.name == "marker_convert_file").inputSchema
    properties = schema["properties"]

    assert properties["local_file_path"]["description"]
    assert properties["overwrite"]["description"]
    assert properties["overwrite"].get("default") is False
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
async def test_mcp_settings_resource_requires_settings_read_scope(monkeypatch):
    import app.mcp_server as mcp_server
    import app.security.auth as auth
    from mcp.server.fastmcp.exceptions import ResourceError

    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(scopes=["capabilities:read"]))

    with pytest.raises(ResourceError, match="settings:read"):
        await mcp_server.mcp.read_resource("marker://settings")


@pytest.mark.asyncio
async def test_mcp_jobs_resource_requires_jobs_read_scope(monkeypatch):
    import app.mcp_server as mcp_server
    import app.security.auth as auth
    from mcp.server.fastmcp.exceptions import ResourceError

    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(scopes=["capabilities:read"]))

    with pytest.raises(ResourceError, match="jobs:read"):
        await mcp_server.mcp.read_resource("marker://jobs")


@pytest.mark.asyncio
async def test_mcp_output_manifest_resource_requires_outputs_read_scope(monkeypatch):
    import app.mcp_server as mcp_server
    import app.security.auth as auth

    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(scopes=["capabilities:read"]))

    with pytest.raises(ValueError, match="outputs:read"):
        await mcp_server.mcp.read_resource("marker://outputs/example.md/manifest")


@pytest.mark.asyncio
async def test_mcp_job_output_resource_requires_jobs_and_outputs_scopes(monkeypatch):
    import app.mcp_server as mcp_server
    import app.security.auth as auth

    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(scopes=["jobs:read"]))

    with pytest.raises(ValueError, match="outputs:read"):
        await mcp_server.mcp.read_resource("marker://jobs/job-1/output")


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
async def test_agent_api_rejects_native_structured_output_format(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")

    with pytest.raises(UnsupportedFormatError) as exc_info:
        await convert_document(
            local_file_path=str(source),
            output_dir=str(tmp_path / "out"),
            max_chars=5000,
            options=AgentConversionOptions(output_format="json"),
        )

    assert "not supported for engine 'text_data'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_agent_api_rejects_unknown_engine_override() -> None:
    with pytest.raises(UsageError) as exc_info:
        await plan_conversion(
            filename="scores.tsv",
            size=12,
            options=AgentConversionOptions(engine_override="does_not_exist"),
        )

    assert "Unknown engine_override" in str(exc_info.value)
    assert "text_data" in exc_info.value.details["known_engines"]


@pytest.mark.asyncio
async def test_agent_api_rejects_incompatible_engine_override() -> None:
    with pytest.raises(UsageError) as exc_info:
        await plan_conversion(
            filename="image.png",
            size=12,
            options=AgentConversionOptions(engine_override="liteparse_pdf"),
        )

    assert "incompatible" in str(exc_info.value)
    assert exc_info.value.details["extension"] == ".png"
    assert exc_info.value.details["compatible_extensions"] == [".pdf"]


@pytest.mark.asyncio
async def test_agent_api_allows_compatible_engine_override_and_auto_sentinel(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")

    explicit = await convert_document(
        local_file_path=str(source),
        output_dir=str(tmp_path / "out-explicit"),
        options=AgentConversionOptions(engine_override="text_data"),
    )
    automatic = await plan_conversion(
        filename="scores.tsv",
        size=source.stat().st_size,
        options=AgentConversionOptions(engine_override="auto"),
    )

    assert explicit["metadata"]["engine"]["engine"] == "text_data"
    assert automatic["plan"]["engine"] == "text_data"


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
async def test_agent_api_explicit_output_path_overwrite_replaces_existing_file(tmp_path: Path):
    source = tmp_path / "scores.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")
    output_path = tmp_path / "out" / "fixed.md"
    output_path.parent.mkdir()
    output_path.write_text("sentinel", encoding="utf-8")

    result = await convert_document(
        local_file_path=str(source),
        output_path=str(output_path),
        overwrite=True,
        max_chars=5000,
        options=AgentConversionOptions(output_format="markdown"),
    )

    assert Path(result["output"]["text_path"]) == output_path
    written_text = output_path.read_text(encoding="utf-8")
    assert "sentinel" not in written_text
    assert "| alpha | 1 |" in written_text


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


def test_mcp_client_config_codex_source_mode_emits_parseable_toml(tmp_path: Path):
    from app.cli import _mcp_client_config

    config = _mcp_client_config(
        "codex",
        mode="source",
        cwd=str(tmp_path),
        server_name="marker_docs",
        tool_profile="minimal",
    )

    assert config["format"] == "toml"
    parsed = tomllib.loads(config["config"])
    server = parsed["mcp_servers"]["marker_docs"]
    assert server["command"] == "python"
    assert server["args"] == ["-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"]
    assert server["cwd"] == str(tmp_path.resolve())
    assert server["env"]["MARKER_PRELOAD_MODELS"] == "false"


def test_mcp_client_config_installed_and_http_modes_parse_as_json():
    from app.cli import _mcp_client_config

    clients = ["claude", "gemini", "cursor", "zed", "cline", "continue", "windsurf", "antigravity"]
    for client in clients:
        config = _mcp_client_config(
            client,
            mode="installed",
            server_name="marker",
            tool_profile="full",
        )
        assert config["format"] == "json"
        json.dumps(config["config"])

    installed = _mcp_client_config("gemini", mode="installed", tool_profile="full")
    server = installed["config"]["mcpServers"]["marker"]
    assert server["command"] == "marker"
    assert server["args"] == ["mcp", "start", "--tool-profile", "full"]
    assert "cwd" not in server

    http = _mcp_client_config(
        "cursor",
        mode="http",
        url="https://marker.example/mcp",
        auth_token="token-123",
    )
    http_server = http["config"]["mcpServers"]["marker"]
    assert http_server["url"] == "https://marker.example/mcp"
    assert http_server["headers"]["Authorization"] == "Bearer token-123"


def test_mcp_client_config_opencode_uses_command_array(tmp_path: Path):
    from app.cli import _mcp_client_config

    config = _mcp_client_config("opencode", mode="source", cwd=str(tmp_path))
    server = config["config"]["mcp"]["marker"]
    assert server["type"] == "local"
    assert server["command"] == ["python", "-m", "app.cli", "mcp", "start", "--tool-profile", "minimal"]
    assert server["cwd"] == str(tmp_path.resolve())


def test_cli_settings_get_and_delete_accept_documented_category(monkeypatch: pytest.MonkeyPatch, capsys):
    from app import cli

    calls: list[tuple[str, str, str | None]] = []

    async def fake_get_setting(key: str, *, category: str | None = None) -> dict[str, str]:
        calls.append(("get", key, category))
        return {"key": key, "value": "********", "category": category or "general"}

    async def fake_delete_setting(key: str, *, category: str | None = None) -> dict[str, str]:
        calls.append(("delete", key, category))
        return {"status": "deleted", "key": key}

    monkeypatch.setattr(cli, "get_setting", fake_get_setting)
    monkeypatch.setattr(cli, "delete_setting", fake_delete_setting)

    assert cli.main(["settings", "get", "openai_api_key", "--category", "llm", "--json"]) == 0
    get_payload = json.loads(capsys.readouterr().out)
    assert get_payload["category"] == "llm"

    assert cli.main(["settings", "delete", "openai_api_key", "--category", "llm", "--json"]) == 0
    delete_payload = json.loads(capsys.readouterr().out)
    assert delete_payload["status"] == "deleted"
    assert calls == [
        ("get", "openai_api_key", "llm"),
        ("delete", "openai_api_key", "llm"),
    ]


def test_cli_config_alias_get_and_delete_accept_documented_category(monkeypatch: pytest.MonkeyPatch):
    from app import cli

    calls: list[tuple[str, str, str | None]] = []

    async def fake_get_setting(key: str, *, category: str | None = None) -> dict[str, str]:
        calls.append(("get", key, category))
        return {"key": key, "value": "********", "category": category or "general"}

    async def fake_delete_setting(key: str, *, category: str | None = None) -> dict[str, str]:
        calls.append(("delete", key, category))
        return {"status": "deleted", "key": key}

    monkeypatch.setattr(cli, "get_setting", fake_get_setting)
    monkeypatch.setattr(cli, "delete_setting", fake_delete_setting)

    assert cli.main(["config", "get", "openai_api_key", "--category", "llm", "--json"]) == 0
    assert cli.main(["config", "delete", "openai_api_key", "--category", "llm", "--json"]) == 0
    assert calls == [
        ("get", "openai_api_key", "llm"),
        ("delete", "openai_api_key", "llm"),
    ]



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
    env["MARKER_MCP_ENABLE_SETTINGS_WRITE"] = "true"
    env["ENCRYPTION_KEY"] = "dGVzdC1lbmNyeXB0aW9uLWtleS1mb3ItdW5pdHRlc3Q="

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.cli", "mcp", "start", "--tool-profile", "admin"],
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
