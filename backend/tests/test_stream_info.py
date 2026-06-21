"""Tests for StreamInfo — file metadata sniffing."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversion.stream_info import StreamInfo


class TestStreamInfo:
    """StreamInfo construction and field correctness."""

    def test_from_path_pdf(self, tmp_path: Path) -> None:
        """PDF file produces correct extension, mime, size, and sample."""
        pdf = tmp_path / "document.pdf"
        content = b"%PDF-1.4 fake content here"
        pdf.write_bytes(content)

        info = StreamInfo.from_path(str(pdf))

        assert info.extension == ".pdf"
        assert info.mime_type == "application/pdf"
        assert info.size == len(content)
        assert info.sample == content  # < 512 bytes, so full content
        assert info.path == str(pdf)

    def test_from_path_docx(self, tmp_path: Path) -> None:
        """DOCX file gets correct extension and generic mime."""
        docx = tmp_path / "report.docx"
        docx.write_bytes(b"PK\x03\x04 fake zip")

        info = StreamInfo.from_path(docx)

        assert info.extension == ".docx"
        assert "openxml" in info.mime_type or "zip" in info.mime_type or "octet" in info.mime_type

    def test_from_path_no_extension(self, tmp_path: Path) -> None:
        """File with no extension gets empty string."""
        noext = tmp_path / "README"
        noext.write_bytes(b"hello")

        info = StreamInfo.from_path(noext)

        assert info.extension == ""
        assert info.size == 5

    def test_from_path_uppercase_extension(self, tmp_path: Path) -> None:
        """Extension is lowercased."""
        upper = tmp_path / "Photo.JPG"
        upper.write_bytes(b"\xFF\xD8\xFF")

        info = StreamInfo.from_path(upper)

        assert info.extension == ".jpg"

    def test_from_path_nonexistent_file(self) -> None:
        """Non-existent file returns size=0 and empty sample without crashing."""
        info = StreamInfo.from_path("/nonexistent/file.pdf")

        assert info.extension == ".pdf"
        assert info.size == 0
        assert info.sample == b""

    def test_from_path_large_file_reads_only_sample(self, tmp_path: Path) -> None:
        """Large file reads only the first 512 bytes."""
        large = tmp_path / "big.bin"
        large.write_bytes(b"A" * 10000)

        info = StreamInfo.from_path(large)

        assert len(info.sample) == 512
        assert info.size == 10000

    def test_frozen_dataclass(self, tmp_path: Path) -> None:
        """StreamInfo is immutable (frozen dataclass)."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hi")
        info = StreamInfo.from_path(f)

        with pytest.raises(AttributeError):
            info.extension = ".pdf"  # type: ignore[misc]
