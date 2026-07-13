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
from app.core.config import MAX_UPLOAD_SIZE, SOURCE_URL_ALLOWLIST, SOURCE_URL_REQUIRE_ALLOWLIST


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
    require_allowlist: bool = SOURCE_URL_REQUIRE_ALLOWLIST,
    allow_cross_host_redirects: bool = False,
    audit_hook: AuditHook | None = None,
) -> DownloadedSource:
    """Download a public HTTP(S) document after SSRF and size checks."""

    current_url = raw_url
    resolved_ips: list[str] = []
    await _audit(audit_hook, "url_fetch.started", {"url": _safe_source_url(raw_url)})
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for redirect_index in range(max_redirects + 1):
            resolved_ips = assert_safe_source_url(
                current_url,
                allowlist=allowlist,
                require_allowlist=require_allowlist,
            )
            async with client.stream("GET", current_url) as response:
                peer_ip = _assert_response_peer_safe(response, resolved_ips)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SafeUrlFetchError("source_url redirect missing Location header")
                    next_url = urljoin(current_url, location)
                    resolved_ips = assert_safe_source_url(
                        next_url,
                        allowlist=allowlist,
                        require_allowlist=require_allowlist,
                    )
                    if not allow_cross_host_redirects and _hostname(next_url) != _hostname(current_url):
                        raise SafeUrlFetchError(
                            "source_url redirect to a different host is not allowed",
                            category="blocked",
                        )
                    current_url = next_url
                    await _audit(
                        audit_hook,
                        "url_fetch.redirect",
                        {
                            "url": _safe_source_url(current_url),
                            "redirect_index": redirect_index + 1,
                            "resolved_ips": resolved_ips,
                            "peer_ip": peer_ip,
                        },
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
                    {
                        "url": safe_url,
                        "bytes": total,
                        "suffix": suffix,
                        "resolved_ips": resolved_ips,
                        "peer_ip": peer_ip,
                    },
                )
                return DownloadedSource(original_name=original_name, suffix=suffix, safe_url=safe_url)
    raise SafeUrlFetchError("source_url exceeded redirect limit", category="blocked")


def assert_safe_source_url(
    raw_url: str,
    *,
    allowlist: tuple[str, ...] | None = None,
    require_allowlist: bool = False,
) -> list[str]:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise SafeUrlFetchError("source_url must be an http(s) URL", category="unsafe")
    if parsed.username or parsed.password:
        raise SafeUrlFetchError("source_url must not contain credentials", category="unsafe")
    effective_allowlist = SOURCE_URL_ALLOWLIST if allowlist is None else allowlist
    if require_allowlist and not effective_allowlist:
        raise SafeUrlFetchError(
            "source_url requires MARKER_SOURCE_URL_ALLOWLIST in this deployment",
            category="blocked",
        )
    if effective_allowlist and not _host_allowed(hostname, effective_allowlist):
        raise SafeUrlFetchError("source_url host is not in MARKER_SOURCE_URL_ALLOWLIST", category="blocked")
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SafeUrlFetchError("source_url host could not be resolved", category="unsafe") from exc
    resolved_ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in addresses:
        host = sockaddr[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise SafeUrlFetchError("source_url resolved to an invalid address", category="unsafe") from exc
        if _is_blocked_ip(ip):
            raise SafeUrlFetchError("source_url resolves to a private or local network address", category="unsafe")
        resolved_ips.append(str(ip))
    return resolved_ips


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


def _assert_response_peer_safe(response: httpx.Response, resolved_ips: list[str]) -> str:
    """Ensure the established peer matches the pre-vetted DNS answer.

    httpx/httpcore exposes the active network stream after connection. Checking
    it closes the DNS-rebinding gap where a host resolves safely during the
    preflight check but connects to a private/local address during fetch.
    """

    peer_host = _response_peer_host(response)
    if not peer_host:
        raise SafeUrlFetchError("source_url peer address unavailable for safety check", category="unsafe")
    try:
        peer_ip = ipaddress.ip_address(peer_host)
    except ValueError as exc:
        raise SafeUrlFetchError("source_url connected to an invalid peer address", category="unsafe") from exc
    if _is_blocked_ip(peer_ip):
        raise SafeUrlFetchError("source_url connected to a private or local network address", category="unsafe")
    allowed_ips = {ipaddress.ip_address(value) for value in resolved_ips}
    if peer_ip not in allowed_ips:
        raise SafeUrlFetchError(
            "source_url peer address changed after DNS safety check",
            category="unsafe",
        )
    return str(peer_ip)


def _response_peer_host(response: httpx.Response) -> str | None:
    stream = getattr(response, "extensions", {}).get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return None
    server_addr = stream.get_extra_info("server_addr")
    if isinstance(server_addr, (tuple, list)) and server_addr:
        return str(server_addr[0])
    if isinstance(server_addr, str):
        return server_addr
    sock = stream.get_extra_info("socket")
    if sock is not None and hasattr(sock, "getpeername"):
        try:
            peer = sock.getpeername()
        except OSError:
            return None
        if isinstance(peer, (tuple, list)) and peer:
            return str(peer[0])
        if isinstance(peer, str):
            return peer
    return None


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


def _hostname(raw_url: str) -> str:
    return (urlparse(raw_url).hostname or "").lower()


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
        not ip.is_global
        or ip.is_private
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
