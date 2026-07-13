"""Tests for Pydantic schemas."""

from datetime import datetime, timezone
from types import SimpleNamespace


from app.models.schemas import (
    ConversionResponse,
    HistoryResponse,
    JobStatusResponse,
    LLMConfig,
    SettingsBatchUpdateRequest,
    SettingsResponse,
    SettingsUpdateRequest,
)


# ---------------------------------------------------------------------------
# ConversionResponse
# ---------------------------------------------------------------------------


class TestConversionResponse:
    def test_create(self):
        r = ConversionResponse(
            job_id="abc",
            status="pending",
            filename="test.pdf",
            output_format="markdown",
        )
        assert r.job_id == "abc"
        assert r.status == "pending"
        assert r.filename == "test.pdf"
        assert r.output_format == "markdown"

    def test_from_attributes(self):
        obj = SimpleNamespace(
            job_id="j1", status="completed", filename="f.pdf", output_format="json"
        )
        r = ConversionResponse.model_validate(obj, from_attributes=True)
        assert r.job_id == "j1"


# ---------------------------------------------------------------------------
# JobStatusResponse
# ---------------------------------------------------------------------------


class TestJobStatusResponse:
    def test_minimal(self):
        r = JobStatusResponse(
            job_id="j1", status="pending", progress=0, filename="a.pdf", output_format="markdown"
        )
        assert r.error_message is None
        assert r.converter is None
        assert r.created_at is None

    def test_full(self):
        now = datetime.now(timezone.utc)
        r = JobStatusResponse(
            job_id="j1",
            status="completed",
            progress=100,
            error_message=None,
            result_text="# Result",
            created_at=now,
            completed_at=now,
            filename="a.pdf",
            output_format="markdown",
            converter="PdfConverter",
        )
        assert r.progress == 100
        assert r.converter == "PdfConverter"

    def test_from_attributes(self):
        obj = SimpleNamespace(
            job_id="j2",
            status="failed",
            progress=50,
            error_message="boom",
            result_text=None,
            created_at=None,
            completed_at=None,
            filename="x.pdf",
            output_format="html",
            converter="TableConverter",
        )
        r = JobStatusResponse.model_validate(obj, from_attributes=True)
        assert r.error_message == "boom"


# ---------------------------------------------------------------------------
# HistoryResponse
# ---------------------------------------------------------------------------


class TestHistoryResponse:
    def test_empty(self):
        r = HistoryResponse(jobs=[], total=0)
        assert r.jobs == []
        assert r.total == 0

    def test_with_jobs(self):
        j = JobStatusResponse(
            job_id="j1", status="pending", progress=0, filename="a.pdf", output_format="markdown"
        )
        r = HistoryResponse(jobs=[j], total=1)
        assert len(r.jobs) == 1
        assert r.total == 1


# ---------------------------------------------------------------------------
# SettingsResponse
# ---------------------------------------------------------------------------


class TestSettingsResponse:
    def test_create(self):
        r = SettingsResponse(key="model_name", value="gpt-4o", category="llm")
        assert r.key == "model_name"
        assert r.value == "gpt-4o"
        assert r.category == "llm"

    def test_from_attributes(self):
        obj = SimpleNamespace(key="timeout", value="60", category="general")
        r = SettingsResponse.model_validate(obj, from_attributes=True)
        assert r.value == "60"


# ---------------------------------------------------------------------------
# SettingsUpdateRequest
# ---------------------------------------------------------------------------


class TestSettingsUpdateRequest:
    def test_defaults(self):
        r = SettingsUpdateRequest(key="base_url", value="http://localhost")
        assert r.category == "general"

    def test_custom_category(self):
        r = SettingsUpdateRequest(key="openai_api_key", value="sk-123", category="llm")
        assert r.category == "llm"


# ---------------------------------------------------------------------------
# SettingsBatchUpdateRequest
# ---------------------------------------------------------------------------


class TestSettingsBatchUpdateRequest:
    def test_create(self):
        items = [
            SettingsUpdateRequest(key="k1", value="v1"),
            SettingsUpdateRequest(key="k2", value="v2", category="llm"),
        ]
        r = SettingsBatchUpdateRequest(settings=items)
        assert len(r.settings) == 2


# ---------------------------------------------------------------------------
# LLMConfig
# ---------------------------------------------------------------------------


class TestLLMConfig:
    def test_defaults(self):
        c = LLMConfig()
        assert c.llm_service == "no_llm"
        assert c.gemini_api_key is None
        assert c.openai_api_key is None
        assert c.claude_api_key is None
        assert c.ollama_base_url is None
        assert c.timeout == 60
        assert c.max_retries == 3

    def test_custom_values(self):
        c = LLMConfig(
            llm_service="openai",
            openai_api_key="sk-test",
            openai_model="gpt-4o-mini",
        )
        assert c.llm_service == "openai"
        assert c.openai_api_key == "sk-test"
        assert c.openai_model == "gpt-4o-mini"

    def test_from_attributes(self):
        obj = SimpleNamespace(
            llm_service="gemini",
            gemini_api_key="ai-xxx",
            gemini_model_name="gemini-2.0-flash",
            openai_api_key="",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-4o",
            claude_api_key="",
            claude_model_name="claude-3-5-sonnet-20241022",
            ollama_base_url="http://localhost:11434",
            ollama_model="llama3.1",
            azure_api_key="",
            azure_endpoint="",
            azure_api_version="2024-02-01",
            azure_deployment_name="",
            vertex_project_id="",
            vertex_location="us-central1",
            timeout=60,
            max_retries=3,
            retry_wait_time=10.0,
            max_output_tokens=8192,
        )
        c = LLMConfig.model_validate(obj, from_attributes=True)
        assert c.llm_service == "gemini"
        assert c.gemini_api_key == "ai-xxx"

    def test_all_fields_present(self):
        expected = {
            "llm_service",
            "gemini_api_key",
            "gemini_model_name",
            "openai_api_key",
            "openai_base_url",
            "openai_model",
            "claude_api_key",
            "claude_model_name",
            "ollama_base_url",
            "ollama_model",
            "azure_api_key",
            "azure_endpoint",
            "azure_api_version",
            "azure_deployment_name",
            "vertex_project_id",
            "vertex_location",
            "timeout",
            "max_retries",
            "retry_wait_time",
            "max_output_tokens",
        }
        assert set(LLMConfig.model_fields.keys()) == expected
