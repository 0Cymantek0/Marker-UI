"""Tests for safe source_url fetching (UCM-005)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import Headers

from app.services.safe_url_fetcher import (
    SafeUrlFetchError,
    assert_safe_source_url,
    download_source_url,
    extension_for_download,
)


def test_assert_safe_source_url_rejects_private_network_resolution(monkeypatch):
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 80))],
    )

    with pytest.raises(SafeUrlFetchError) as exc_info:
        assert_safe_source_url("https://example.com/file.pdf")

    assert exc_info.value.category == "unsafe"
    assert "private or local network" in exc_info.value.detail


def test_assert_safe_source_url_enforces_allowlist(monkeypatch):
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    assert_safe_source_url("https://docs.allowed.example/file.pdf", allowlist=("allowed.example",))
    with pytest.raises(SafeUrlFetchError) as exc_info:
        assert_safe_source_url("https://blocked.example/file.pdf", allowlist=("allowed.example",))
    assert "allowlist" in exc_info.value.detail.lower()


def test_extension_for_download_prefers_content_type_and_sanitizes_filename():
    filename, suffix = extension_for_download(
        "https://example.com/download.bin",
        Headers(
            {
                "content-type": "text/csv; charset=utf-8",
                "content-disposition": 'attachment; filename="../Bad Name.csv"',
            }
        ),
        allowed_extensions={".csv"},
    )

    assert filename == "Bad_Name.csv"
    assert suffix == ".csv"


@pytest.mark.asyncio
async def test_download_source_url_writes_file_and_audits(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )
    fake_client = _FakeClient(
        [
            _FakeResponse(
                200,
                {"content-type": "application/pdf", "content-length": "8"},
                [b"%PDF", b"-1.4"],
            )
        ]
    )
    monkeypatch.setattr("app.services.safe_url_fetcher.httpx.AsyncClient", lambda **kwargs: fake_client)
    events: list[tuple[str, dict]] = []

    downloaded = await download_source_url(
        "https://example.com/doc.pdf?query=hidden",
        tmp_path / "download",
        allowed_extensions={".pdf"},
        audit_hook=lambda event, payload: events.append((event, payload)),
    )

    assert downloaded.original_name == "doc.pdf"
    assert downloaded.suffix == ".pdf"
    assert downloaded.safe_url == "https://example.com/doc.pdf"
    assert (tmp_path / "download").read_bytes() == b"%PDF-1.4"
    assert [event for event, _payload in events] == ["url_fetch.started", "url_fetch.completed"]
    assert events[0][1]["url"] == "https://example.com/doc.pdf"


@pytest.mark.asyncio
async def test_download_source_url_blocks_oversized_content_length(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.httpx.AsyncClient",
        lambda **kwargs: _FakeClient([_FakeResponse(200, {"content-type": "application/pdf", "content-length": "9"}, [])]),
    )

    with pytest.raises(SafeUrlFetchError) as exc_info:
        await download_source_url(
            "https://example.com/doc.pdf",
            tmp_path / "download",
            allowed_extensions={".pdf"},
            max_bytes=8,
        )

    assert exc_info.value.status_code == 413
    assert not (tmp_path / "download").exists()


@pytest.mark.asyncio
async def test_download_source_url_ignores_invalid_content_length_and_uses_stream_cap(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "app.services.safe_url_fetcher.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(
            [_FakeResponse(200, {"content-type": "application/pdf", "content-length": "not-a-number"}, [b"12345"])]
        ),
    )

    with pytest.raises(SafeUrlFetchError) as exc_info:
        await download_source_url(
            "https://example.com/doc.pdf",
            tmp_path / "download",
            allowed_extensions={".pdf"},
            max_bytes=4,
        )

    assert exc_info.value.status_code == 413
    assert not (tmp_path / "download").exists()


@pytest.mark.asyncio
async def test_download_source_url_rechecks_redirect_target(monkeypatch, tmp_path: Path):
    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "example.com":
            return [(0, 0, 0, "", ("93.184.216.34", 443))]
        return [(0, 0, 0, "", ("127.0.0.1", 80))]

    monkeypatch.setattr("app.services.safe_url_fetcher.socket.getaddrinfo", fake_getaddrinfo)
    fake_client = _FakeClient(
        [
            _FakeResponse(302, {"location": "http://localhost/private.pdf"}, []),
        ]
    )
    monkeypatch.setattr("app.services.safe_url_fetcher.httpx.AsyncClient", lambda **kwargs: fake_client)

    with pytest.raises(SafeUrlFetchError) as exc_info:
        await download_source_url(
            "https://example.com/doc.pdf",
            tmp_path / "download",
            allowed_extensions={".pdf"},
        )

    assert "private or local network" in exc_info.value.detail
    assert fake_client.urls == ["https://example.com/doc.pdf"]


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str], chunks: list[bytes]) -> None:
        self.status_code = status_code
        self.headers = Headers(headers)
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def aiter_bytes(self, chunk_size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def stream(self, method: str, url: str):
        self.urls.append(url)
        return self._responses.pop(0)
