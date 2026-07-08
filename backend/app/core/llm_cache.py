"""Persistent LLM response cache.

Sits at the httpx layer (inside ``api_manager.patched_*_send``) so a retry
that re-runs a job from scratch replays already-done LLM calls from disk
instead of re-hitting a throttled endpoint. A same-provider model swap
reuses cached responses (the model name is stripped from the cache key); a
cross-provider retry misses the cache and re-does the work (different host).

Only 2xx responses to LLM generation endpoints are cached. Streaming
requests, auth errors, and non-generation traffic are never cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx

from app.core.config import DATA_DIR

logger = logging.getLogger(__name__)

# Env-gated: opt-in so existing jobs keep their current (uncached) behaviour
# unless explicitly enabled. ``MARKER_LLM_CACHE=1`` turns it on.
_CACHE_ENABLED = os.environ.get("MARKER_LLM_CACHE", "0") == "1"
_CACHE_PATH: Path = DATA_DIR / "llm_cache.db"
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Per-key write lock prevents duplicate concurrent inserts for the same key.
_write_lock = threading.Lock()
# Module-level connection guard (SQLite is thread-safe with check_same_thread).
_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()

# Headers that must never be cached (auth, hop-by-hop, dynamic, length-varying).
_STRIP_HEADERS_RE = re.compile(
    r"^(authorization|x-api-key|x-goog-api-key|api-key|cookie|set-cookie|"
    r"date|expires|server|x-request-id|x-correlation-id|content-length|"
    r"transfer-encoding|content-encoding|host|connection)$",
    re.IGNORECASE,
)

# Generation endpoint path patterns we cache. Excludes model-list, embeddings,
# and anything that isn't a content-generation call.
_CACHEABLE_PATH_RE = re.compile(
    r":(generateContent|streamGenerateContent|countTokens)$|"
    r"/(chat/completions|messages|completions|generate)$"
)


def is_cache_enabled() -> bool:
    """True when the LLM response cache is enabled (env ``MARKER_LLM_CACHE=1``)."""
    return _CACHE_ENABLED


def init_cache_db() -> None:
    """Create the cache table if missing. Called once on app startup."""
    if not _CACHE_ENABLED:
        return
    global _conn
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _conn_lock:
        if _conn is None:
            _conn = sqlite3.connect(str(_CACHE_PATH), check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_response_cache (
                key TEXT PRIMARY KEY,
                status INTEGER NOT NULL,
                headers TEXT NOT NULL,
                body BLOB NOT NULL,
                created_at REAL NOT NULL,
                ttl REAL NOT NULL
            )
            """
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_response_cache(created_at)"
        )
        _conn.commit()
    logger.info("LLM response cache initialized at %s", _CACHE_PATH)


def _get_conn() -> sqlite3.Connection:
    """Lazily open the connection (safe to call from any thread)."""
    global _conn
    if _conn is None:
        init_cache_db()
    assert _conn is not None
    return _conn


def _strip_auth_headers(headers: httpx.Headers) -> dict[str, str]:
    """Return a copy of headers with auth/hop-by-hop entries removed."""
    return {
        k: v for k, v in headers.items()
        if not _STRIP_HEADERS_RE.match(k)
    }


def _normalize_url(url: httpx.URL) -> str:
    """Strip the model name from a Gemini URL path so a same-provider model
    swap produces the same cache key. OpenAI/Claude/Ollama carry the model in
    the body, so their URL is already model-agnostic."""
    path = url.path
    # Gemini: /v1beta/models/<model>:generateContent -> /v1beta/models:generateContent
    path = re.sub(r"/models/[^/:]+:", "/models/<model>:", path)
    # Query string is excluded from the key; gemini carries only the key there.
    return f"{url.host}{path}"


def _normalize_body(content: bytes) -> bytes:
    """Remove model/stream fields and sort keys so identical prompts hash the
    same regardless of which sibling model or streaming flag was used."""
    if not content:
        return b""
    try:
        data = json.loads(content.decode("utf-8"))
        if isinstance(data, dict):
            data.pop("model", None)
            data.pop("stream", None)
            data.pop("gemini_model_name", None)  # marker's gemini service field
            return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except Exception:  # noqa: BLE001 - non-JSON body: hash as-is
        pass
    return content


def _is_cacheable(request: httpx.Request) -> bool:
    """Only cache POSTs to LLM generation endpoints (not model lists, not GETs)."""
    if request.method != "POST":
        return False
    path = request.url.path
    return bool(_CACHEABLE_PATH_RE.search(path))


def _is_streaming_request(request: httpx.Request) -> bool:
    """Streaming responses are not cacheable (partial bytes)."""
    if hasattr(request, "_content") and request._content:
        try:
            data = json.loads(request._content.decode("utf-8"))
            if isinstance(data, dict) and data.get("stream"):
                return True
        except Exception:  # noqa: BLE001
            pass
    # Gemini streamGenerateContent is streaming
    return ":streamGenerateContent" in request.url.path


def cache_key(request: httpx.Request) -> str | None:
    """Compute the cache key for a request, or None if not cacheable."""
    if not _CACHE_ENABLED or not _is_cacheable(request) or _is_streaming_request(request):
        return None
    host_path = _normalize_url(request.url)
    body = _normalize_body(request._content) if hasattr(request, "_content") and request._content else b""
    # Auth headers stripped: the key is identical regardless of which key was used.
    auth_stripped = _strip_auth_headers(request.headers)
    header_str = json.dumps(auth_stripped, sort_keys=True, separators=(",", ":"))
    key_material = f"{request.method}\n{host_path}\n{header_str}\n".encode("utf-8") + body
    return hashlib.sha256(key_material).hexdigest()


def cache_get(key: str) -> httpx.Response | None:
    """Return a cached response if present and not expired, else None."""
    if not _CACHE_ENABLED:
        return None
    try:
        row = _get_conn().execute(
            "SELECT status, headers, body, ttl FROM llm_response_cache WHERE key = ?",
            (key,),
        ).fetchone()
    except sqlite3.Error as e:
        logger.error("LLM cache read failed: %s", e)
        return None
    if not row:
        return None
    status, headers_json, body, ttl = row
    if time.time() > ttl:
        # Expired: best-effort delete, fall through to a fresh request.
        try:
            _get_conn().execute("DELETE FROM llm_response_cache WHERE key = ?", (key,))
            _get_conn().commit()
        except sqlite3.Error:
            pass
        return None
    try:
        headers = json.loads(headers_json)
    except Exception:  # noqa: BLE001
        headers = {}
    logger.info("LLM cache HIT key=%s", key[:12])
    return httpx.Response(status_code=status, headers=headers, content=body)


def cache_put(key: str, response: httpx.Response) -> None:
    """Store a 2xx response. No-op for non-2xx or streaming responses."""
    if not _CACHE_ENABLED:
        return
    if not (200 <= response.status_code < 300):
        return
    # Skip SSE / streaming responses (partial bytes, not replayable).
    if "text/event-stream" in response.headers.get("content-type", ""):
        return
    body = response.content
    if not body:
        return
    headers = _strip_auth_headers(response.headers)
    headers_json = json.dumps(headers, separators=(",", ":"))
    now = time.time()
    ttl = now + _DEFAULT_TTL_SECONDS
    with _write_lock:
        try:
            _get_conn().execute(
                "INSERT OR REPLACE INTO llm_response_cache "
                "(key, status, headers, body, created_at, ttl) VALUES (?, ?, ?, ?, ?, ?)",
                (key, response.status_code, headers_json, body, now, ttl),
            )
            _get_conn().commit()
            logger.info("LLM cache PUT key=%s (%d bytes)", key[:12], len(body))
        except sqlite3.Error as e:
            logger.error("LLM cache write failed: %s", e)


def clear_cache() -> int:
    """Wipe all cached entries. Returns the count deleted."""
    if not _CACHE_ENABLED:
        return 0
    with _write_lock:
        cur = _get_conn().execute("DELETE FROM llm_response_cache")
        _get_conn().commit()
        return cur.rowcount or 0


def purge_expired() -> int:
    """Delete expired entries. Called periodically from a background sweep."""
    if not _CACHE_ENABLED:
        return 0
    now = time.time()
    with _write_lock:
        cur = _get_conn().execute(
            "DELETE FROM llm_response_cache WHERE ttl < ?", (now,)
        )
        _get_conn().commit()
        return cur.rowcount or 0
