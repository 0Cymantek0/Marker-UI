"""Tests for the LLM response cache (app.core.llm_cache)."""

from __future__ import annotations

import os
import time
import threading
import importlib

import pytest
import httpx

import app.core.llm_cache as lc


def test_cache_is_opt_in_by_default(monkeypatch):
    """Without MARKER_LLM_CACHE=1, disk cache stays disabled for privacy."""
    monkeypatch.delenv("MARKER_LLM_CACHE", raising=False)
    reloaded = importlib.reload(lc)
    try:
        assert reloaded.is_cache_enabled() is False
    finally:
        monkeypatch.setenv("MARKER_LLM_CACHE", "1")
        importlib.reload(lc)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Each test gets a fresh temp SQLite cache DB and a re-init connection.

    Cache is force-enabled so the env default (off) does not skip tests.
    """
    monkeypatch.setattr(lc, "_CACHE_ENABLED", True)
    cache_file = tmp_path / "llm_cache_test.db"
    monkeypatch.setattr(lc, "_CACHE_PATH", cache_file)
    # Drop any existing connection so init reopens at the new path.
    with lc._conn_lock:
        if lc._conn is not None:
            lc._conn.close()
        lc._conn = None
    lc.init_cache_db()
    yield
    with lc._conn_lock:
        if lc._conn is not None:
            lc._conn.close()
        lc._conn = None


def _gemini_request(model: str = "gemini-flash-lite-latest", contents=None) -> httpx.Request:
    body = (
        '{"contents": ' + (contents or '[]') +
        ', "generationConfig": {"temperature": 0}}'
    )
    return httpx.Request(
        "POST",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": "key-A", "content-type": "application/json"},
        content=body.encode("utf-8"),
    )


def _openai_request(model: str = "gpt-4", messages=None) -> httpx.Request:
    body = '{"model": "' + model + '", "messages": ' + (messages or "[]") + "}"
    return httpx.Request(
        "POST",
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-key-A", "content-type": "application/json"},
        content=body.encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Cacheability gating
# ---------------------------------------------------------------------------


def test_cache_key_for_generation_endpoint(isolated_cache):
    """A POST to :generateContent yields a non-None cache key."""
    req = _gemini_request()
    assert lc.cache_key(req) is not None


def test_no_cache_key_for_get_request(isolated_cache):
    """GET requests (e.g. model lists) are never cached."""
    req = httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/models")
    assert lc.cache_key(req) is None


def test_no_cache_key_for_non_generation_endpoint(isolated_cache):
    """POST to /v1/embeddings is not a generation endpoint."""
    req = httpx.Request(
        "POST", "https://api.openai.com/v1/embeddings",
        headers={"Authorization": "Bearer sk-key-A"},
        content=b'{"input": "hi"}',
    )
    assert lc.cache_key(req) is None


def test_no_cache_key_for_streaming_request(isolated_cache):
    """Streaming requests are not cacheable (partial bytes)."""
    req = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:streamGenerateContent",
        headers={"x-goog-api-key": "key-A"},
        content=b'{"contents": [], "stream": true}',
    )
    assert lc.cache_key(req) is None


# ---------------------------------------------------------------------------
# Auth stripping + model-agnostic keys
# ---------------------------------------------------------------------------


def test_auth_headers_stripped_from_cache_key(isolated_cache):
    """Same request with different API keys yields the SAME cache key."""
    body = b'{"contents": [{"text": "hello"}]}'
    req_a = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-A", "content-type": "application/json"},
        content=body,
    )
    req_b = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-B-different", "content-type": "application/json"},
        content=body,
    )
    assert lc.cache_key(req_a) == lc.cache_key(req_b)


def test_model_name_stripped_from_gemini_url_key(isolated_cache):
    """Same prompt, different sibling Gemini model -> same cache key
    (lets a same-provider model swap reuse cached responses)."""
    body = b'{"contents": [{"text": "hello"}]}'
    req_flash = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-A"},
        content=body,
    )
    req_pro = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent",
        headers={"x-goog-api-key": "key-A"},
        content=body,
    )
    assert lc.cache_key(req_flash) == lc.cache_key(req_pro)


def test_model_field_stripped_from_openai_body_key(isolated_cache):
    """Same prompt, different OpenAI model -> same cache key."""
    body = b'{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}'
    req_4 = httpx.Request(
        "POST", "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-key-A"}, content=body,
    )
    body_35 = b'{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]}'
    req_35 = httpx.Request(
        "POST", "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-key-A"}, content=body_35,
    )
    assert lc.cache_key(req_4) == lc.cache_key(req_35)


def test_different_prompt_yields_different_key(isolated_cache):
    """Different request bodies produce different cache keys."""
    body_a = b'{"contents": [{"text": "hello"}]}'
    body_b = b'{"contents": [{"text": "goodbye"}]}'
    req_a = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-A"}, content=body_a,
    )
    req_b = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-A"}, content=body_b,
    )
    assert lc.cache_key(req_a) != lc.cache_key(req_b)


def test_cross_provider_misses(isolated_cache):
    """Different host (Gemini vs OpenAI) yields a different cache key; a
    cross-provider retry does NOT replay the other provider's cached response."""
    body_gem = b'{"contents": [{"text": "hello"}]}'
    req_gem = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-A"}, content=body_gem,
    )
    body_oai = b'{"messages": [{"role": "user", "content": "hello"}]}'
    req_oai = httpx.Request(
        "POST", "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-key-A"}, content=body_oai,
    )
    assert lc.cache_key(req_gem) != lc.cache_key(req_oai)


# ---------------------------------------------------------------------------
# Put / get round-trip
# ---------------------------------------------------------------------------


def test_cache_put_then_get_returns_response(isolated_cache):
    """A 2xx response stored under a key is returned verbatim by cache_get."""
    req = _gemini_request(contents='[{"text": "hello"}]')
    key = lc.cache_key(req)
    assert key is not None

    resp = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"candidates": [{"content": {"parts": [{"text": "world"}]}}]}',
        request=req,
    )
    lc.cache_put(key, resp)

    cached = lc.cache_get(key)
    assert cached is not None
    assert cached.status_code == 200
    assert b'"world"' in cached.content


def test_cache_get_miss_returns_none(isolated_cache):
    """Unknown key returns None."""
    assert lc.cache_get("nonexistent-key") is None


def test_non_2xx_not_cached(isolated_cache):
    """4xx/5xx responses are never cached (would replay errors)."""
    req = _gemini_request(contents='[{"text": "hello"}]')
    key = lc.cache_key(req)
    assert key is not None

    err_resp = httpx.Response(429, content=b'{"error": "rate limited"}', request=req)
    lc.cache_put(key, err_resp)

    assert lc.cache_get(key) is None


def test_expired_entries_purged(isolated_cache, monkeypatch):
    """Entries past their TTL are deleted on read and on purge_expired()."""
    req = _gemini_request(contents='[{"text": "hello"}]')
    key = lc.cache_key(req)
    resp = httpx.Response(200, content=b'{"ok": true}', request=req)
    lc.cache_put(key, resp)

    # Force the stored TTL into the past.
    with lc._get_conn() as conn:
        conn.execute(
            "UPDATE llm_response_cache SET ttl = ? WHERE key = ?",
            (time.time() - 1, key),
        )
        conn.commit()

    assert lc.cache_get(key) is None  # expired -> miss + delete
    # Row gone.
    row = lc._get_conn().execute(
        "SELECT 1 FROM llm_response_cache WHERE key = ?", (key,)
    ).fetchone()
    assert row is None


def test_clear_cache_wipes_all(isolated_cache):
    """clear_cache removes every entry and returns the count."""
    req = _gemini_request(contents='[{"text": "hello"}]')
    key = lc.cache_key(req)
    lc.cache_put(key, httpx.Response(200, content=b'{"ok": true}', request=req))
    assert lc.cache_get(key) is not None

    deleted = lc.clear_cache()
    assert deleted >= 1
    assert lc.cache_get(key) is None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_put_is_safe(isolated_cache):
    """Two threads inserting the same key concurrently must not corrupt the DB
    (SQLite PRIMARY KEY conflict resolves via INSERT OR REPLACE)."""
    req = _gemini_request(contents='[{"text": "hello"}]')
    key = lc.cache_key(req)
    errors: list[Exception] = []

    def writer():
        try:
            lc.cache_put(key, httpx.Response(200, content=b'{"ok": true}', request=req))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors}"
    cached = lc.cache_get(key)
    assert cached is not None
    assert cached.status_code == 200


# ---------------------------------------------------------------------------
# End-to-end via the patched httpx send
# ---------------------------------------------------------------------------


def test_patched_send_replays_from_cache(isolated_cache, monkeypatch):
    """A second call to the same cacheable request returns the cached response
    WITHOUT hitting the network handler."""
    import app.core.api_manager as am
    from app.core.api_manager import setup_api_manager_monkeypatch

    _orig_client_send = httpx.Client.send
    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)
    setup_api_manager_monkeypatch()
    try:
        req = _gemini_request(contents='[{"text": "hello"}]')
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"candidates": [{"content": {"parts": [{"text": "world"}]}}]}',
            )

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            r1 = client.send(req)
            r2 = client.send(req)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.content == r2.content
        # Network hit once; second call replayed from cache.
        assert calls["n"] == 1
    finally:
        httpx.Client.send = _orig_client_send
