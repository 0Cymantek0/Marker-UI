"""Safe HTTP(S) downloader for source_url conversion inputs."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse, urlunparse

import aiofiles
import httpx

from app.conversion.formats import CONTENT_TYPE_EXTENSION_MAP
from app.core.config import MAX_UPLOAD_SIZE, SOURCE_URL_ALLOWLIST


MAX_URL_REDIRECTS = 5


class SafeUrlFetchError(Exception):
    """Typed URL fetch failure suitable for REST and CLI/MCP mapping."""

    def __init__(self, detail: str, *, status_code: int = 400, category: str = "fetch") -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.category = category


@dataclass(frozen=True)
class DownloadedSource:
    original_name: str
    suffix: str
    safe_url: str


AuditHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]


async def download_source_url(
    raw_url: str,
    destination: Path,
    *,
    allowed_extensions: set[str],
    max_bytes: int = MAX_UPLOAD_SIZE,
    max_redirects: int = MAX_URL_REDIRECTS,
    timeout: float = 30.0,
    allowlist: tuple[str, ...] | None = None,
    audit_hook: AuditHook | None = None,
) -> DownloadedSource:
    """Download a public HTTP(S) document after SSRF and size checks."""

    current_url = raw_url
    await _audit(audit_hook, "url_fetch.started", {"url": _safe_source_url(raw_url)})
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for redirect_index in range(max_redirects + 1):
            assert_safe_source_url(current_url, allowlist=allowlist)
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SafeUrlFetchError("source_url redirect missing Location header")
                    current_url = urljoin(current_url, location)
                    await _audit(
                        audit_hook,
                        "url_fetch.redirect",
                        {"url": _safe_source_url(current_url), "redirect_index": redirect_index + 1},
                    )
                    continue
                if response.status_code >= 400:
                    raise SafeUrlFetchError(f"source_url returned HTTP {response.status_code}")
                original_name, suffix = extension_for_download(
                    current_url,
                    response.headers,
                    allowed_extensions=allowed_extensions,
                )
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = None
                    if declared_length is not None and declared_length > max_bytes:
                        raise SafeUrlFetchError(
                            f"Downloaded file exceeds maximum size of {max_bytes} bytes.",
                            status_code=413,
                            category="blocked",
                        )
                total = 0
                too_large = False
                async with aiofiles.open(destination, "wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            too_large = True
                            break
                        await handle.write(chunk)
                if too_large:
                    destination.unlink(missing_ok=True)
                    raise SafeUrlFetchError(
                        f"Downloaded file exceeds maximum size of {max_bytes} bytes.",
                        status_code=413,
                        category="blocked",
                    )
                safe_url = _safe_source_url(current_url)
                await _audit(
                    audit_hook,
                    "url_fetch.completed",
                    {"url": safe_url, "bytes": total, "suffix": suffix},
                )
                return DownloadedSource(original_name=original_name, suffix=suffix, safe_url=safe_url)
    raise SafeUrlFetchError("source_url exceeded redirect limit", category="blocked")


def assert_safe_source_url(raw_url: str, *, allowlist: tuple[str, ...] | None = None) -> None:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise SafeUrlFetchError("source_url must be an http(s) URL", category="unsafe")
    if parsed.username or parsed.password:
        raise SafeUrlFetchError("source_url must not contain credentials", category="unsafe")
    effective_allowlist = SOURCE_URL_ALLOWLIST if allowlist is None else allowlist
    if effective_allowlist and not _host_allowed(hostname, effective_allowlist):
        raise SafeUrlFetchError("source_url host is not in MARKER_SOURCE_URL_ALLOWLIST", category="blocked")
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SafeUrlFetchError("source_url host could not be resolved", category="unsafe") from exc
    for _family, _socktype, _proto, _canonname, sockaddr in addresses:
        host = sockaddr[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise SafeUrlFetchError("source_url resolved to an invalid address", category="unsafe") from exc
        if _is_blocked_ip(ip):
            raise SafeUrlFetchError("source_url resolves to a private or local network address", category="unsafe")


def extension_for_download(
    url: str,
    headers: httpx.Headers,
    *,
    allowed_extensions: set[str],
) -> tuple[str, str]:
    content_type = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    header_name = _filename_from_content_disposition(headers.get("content-disposition"))
    path_name = Path(urlparse(url).path).name
    filename = header_name or path_name or "download"
    ext_from_type = CONTENT_TYPE_EXTENSION_MAP.get(content_type)
    ext_from_name = Path(filename).suffix.lower()
    suffix = ext_from_type or ext_from_name
    if suffix not in allowed_extensions:
        raise SafeUrlFetchError(
            f"Unsupported downloaded content type or extension '{content_type or suffix}'",
            category="blocked",
        )
    stem = Path(filename).stem or "download"
    safe_stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)[:80] or "download"
    return f"{safe_stem}{suffix}", suffix


def _filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() in {"filename", "filename*"}:
            filename = raw.strip().strip('"')
            if "''" in filename:
                filename = filename.split("''", 1)[1]
            return Path(filename.replace("\\", "/")).name
    return None


def _safe_source_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _host_allowed(hostname: str, allowlist: tuple[str, ...]) -> bool:
    for entry in allowlist:
        normalized = entry.strip().lower()
        if not normalized:
            continue
        if normalized.startswith("*.") and hostname.endswith(normalized[1:]):
            return True
        if hostname == normalized or hostname.endswith(f".{normalized}"):
            return True
    return False


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _audit(audit_hook: AuditHook | None, event: str, payload: dict[str, Any]) -> None:
    if audit_hook is None:
        return
    result = audit_hook(event, payload)
    if hasattr(result, "__await__"):
        await result
