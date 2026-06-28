"""SQLAlchemy model for document conversion jobs."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConversionJob(Base):
    """Tracks the lifecycle of a single document conversion."""

    __tablename__ = "conversion_jobs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    input_format: Mapped[str] = mapped_column(String(20), nullable=False, default="pdf")
    output_format: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="markdown",
    )
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-serialized conversion metadata (e.g. per-image understanding info).
    result_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-serialized cache of every output format rendered for this job:
    # ``{format: text}``. The primary ``output_format``/``result_text`` stays the
    # canonical entry; this holds the additional formats so preview tabs can
    # switch formats without reconverting. Self-heals on startup (additive col).
    formats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queue_backend: Mapped[str | None] = mapped_column(String(50), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return f"<ConversionJob(id={self.id!r}, status={self.status!r})>"
