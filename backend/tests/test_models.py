"""Tests for SQLAlchemy models (ConversionJob, Setting)."""

from datetime import datetime, timezone

import pytest

from app.models.job import ConversionJob
from app.models.job_event import JobEvent
from app.models.settings import Setting


# ---------------------------------------------------------------------------
# ConversionJob
# ---------------------------------------------------------------------------


class TestConversionJob:
    """Verify ConversionJob has all expected columns and defaults."""

    EXPECTED_COLUMNS = {
        "id",
        "filename",
        "original_name",
        "status",
        "input_format",
        "output_format",
        "progress",
        "queue_backend",
        "queued_at",
        "started_at",
        "lease_owner",
        "lease_expires_at",
        "retry_count",
        "max_retries",
        "idempotency_key",
        "error_message",
        "result_text",
        "result_metadata_json",
        "result_path",
        "config_json",
        "created_at",
        "updated_at",
        "completed_at",
    }

    def test_table_name(self):
        assert ConversionJob.__tablename__ == "conversion_jobs"

    def test_has_all_columns(self):
        mapper = ConversionJob.__table__
        col_names = {c.name for c in mapper.columns}
        assert col_names == self.EXPECTED_COLUMNS

    def test_instantiation_with_defaults(self):
        job = ConversionJob(
            id="test-id-1",
            filename="test.pdf",
            original_name="report.pdf",
            status="pending",
            input_format="pdf",
        )
        assert job.id == "test-id-1"
        assert job.filename == "test.pdf"
        assert job.original_name == "report.pdf"
        assert job.status == "pending"
        assert job.input_format == "pdf"
        # SQLAlchemy Column defaults only apply on INSERT, not instantiation
        assert job.output_format in ("markdown", None)
        assert job.progress in (0, None)
        assert job.error_message is None
        assert job.result_text is None
        assert job.result_path is None
        assert job.config_json is None
        assert job.completed_at is None
        assert job.queue_backend is None
        assert job.idempotency_key is None

    def test_instantiation_with_all_fields(self):
        now = datetime.now(timezone.utc)
        job = ConversionJob(
            id="test-id-2",
            filename="doc.pdf",
            original_name="doc.pdf",
            status="completed",
            input_format="pdf",
            output_format="json",
            progress=100,
            error_message=None,
            result_text="# Hello",
            result_path="/tmp/out",
            config_json='{"converter_cls": "PdfConverter"}',
            created_at=now,
            completed_at=now,
        )
        assert job.status == "completed"
        assert job.progress == 100
        assert job.result_text == "# Hello"
        assert job.result_path == "/tmp/out"
        assert job.config_json == '{"converter_cls": "PdfConverter"}'
        assert job.created_at == now
        assert job.completed_at == now


class TestJobEvent:
    """Verify durable queue event model shape."""

    EXPECTED_COLUMNS = {
        "id",
        "job_id",
        "event_type",
        "status",
        "message",
        "payload_json",
        "created_at",
    }

    def test_table_name(self):
        assert JobEvent.__tablename__ == "job_events"

    def test_has_all_columns(self):
        col_names = {c.name for c in JobEvent.__table__.columns}
        assert col_names == self.EXPECTED_COLUMNS

    def test_instantiation(self):
        event = JobEvent(
            job_id="job-1",
            event_type="queue.enqueued",
            status="pending",
            payload_json="{}",
        )
        assert event.job_id == "job-1"
        assert event.event_type == "queue.enqueued"


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------


class TestSetting:
    """Verify Setting model has key/value/category columns."""

    EXPECTED_COLUMNS = {"id", "key", "value", "category", "created_at", "updated_at"}

    def test_table_name(self):
        assert Setting.__tablename__ == "settings"

    def test_has_all_columns(self):
        col_names = {c.name for c in Setting.__table__.columns}
        assert col_names == self.EXPECTED_COLUMNS

    def test_instantiation_defaults(self):
        s = Setting(key="model_name", value="gpt-4o")
        assert s.key == "model_name"
        assert s.value == "gpt-4o"
        # SQLAlchemy Column default only applies on INSERT
        assert s.category in ("general", None)

    def test_instantiation_with_category(self):
        s = Setting(key="openai_api_key", value="encrypted", category="llm")
        assert s.key == "openai_api_key"
        assert s.value == "encrypted"
        assert s.category == "llm"
