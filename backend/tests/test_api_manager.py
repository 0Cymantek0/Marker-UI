"""Unit tests for the api_manager live secret replacement system."""

import threading
import time

import pytest
import httpx

import app.core.api_manager as am
from app.core.api_manager import (
    update_secret_cache,
    get_secret,
    setup_api_manager_monkeypatch,
    _resolve_request,
)

# Preserve original send methods to restore after tests
_orig_client_send = httpx.Client.send
_orig_async_client_send = httpx.AsyncClient.send


@pytest.fixture(autouse=True)
def setup_and_teardown_monkeypatch(monkeypatch):
    """Apply interceptor monkeypatch and clean up after each test.

    Also no-ops the backoff/cooldown sleeps so the suite stays fast, disables
    the LLM response cache (Phase 2a tests cover it separately), and clears
    cooldown state between each test. Tests that assert on sleep timing
    reinstall their own spy via ``monkeypatch``.
    """
    import app.core.llm_cache as llm_cache

    monkeypatch.setattr(llm_cache, "_CACHE_ENABLED", False)
    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    async def _noop_async(_s: float) -> None:
        return None

    monkeypatch.setattr(am, "_sleep_async", _noop_async)
    with am._cooldown_lock:
        am._provider_cooldown_until.clear()

    setup_api_manager_monkeypatch()
    yield
    httpx.Client.send = _orig_client_send
    httpx.AsyncClient.send = _orig_async_client_send
    with am._cooldown_lock:
        am._provider_cooldown_until.clear()


def test_secrets_cache_crud():
    """Verify in-memory secrets cache updates and retrieval."""
    update_secret_cache("test_api_key", "secret-xyz-789")
    assert get_secret("test_api_key") == "secret-xyz-789"
    assert get_secret("nonexistent_key") == ""


def test_resolve_request_headers():
    """Verify that headers containing 'secret:<key>' placeholders are rewritten."""
    update_secret_cache("gemini_api_key", "real-gemini-key")
    update_secret_cache("openai_api_key", "real-openai-key")

    req = httpx.Request(
        "GET",
        "https://example.com/api",
        headers={
            "x-goog-api-key": "secret:gemini_api_key",
            "Authorization": "Bearer secret:openai_api_key",
            "x-normal-header": "just-some-value",
        },
    )

    _resolve_request(req)

    assert req.headers["x-goog-api-key"] == "real-gemini-key"
    assert req.headers["Authorization"] == "Bearer real-openai-key"
    assert req.headers["x-normal-header"] == "just-some-value"


def test_resolve_request_url():
    """Verify that URL path/query containing 'secret:<key>' placeholders are rewritten."""
    update_secret_cache("vertex_project_id", "my-gcp-project-id")

    req = httpx.Request(
        "GET",
        "https://us-central1-aiplatform.googleapis.com/v1/projects/secret:vertex_project_id/locations",
    )

    _resolve_request(req)

    assert str(req.url) == "https://us-central1-aiplatform.googleapis.com/v1/projects/my-gcp-project-id/locations"


def test_resolve_request_body():
    """Verify that request body string containing placeholders is rewritten."""
    update_secret_cache("claude_api_key", "real-claude-key")

    payload = '{"api_key": "secret:claude_api_key", "max_tokens": 1}'
    req = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        content=payload.encode("utf-8"),
    )

    _resolve_request(req)

    assert b"real-claude-key" in req.content
    assert b"secret:claude_api_key" not in req.content
    assert req.headers["content-length"] == str(len(req.content))


def test_sync_client_intercepts():
    """Verify sync Client.send intercepts and replaces placeholders."""
    update_secret_cache("openai_api_key", "secret-key-sync-value")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-key-sync-value"
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        client.get("https://api.openai.com/v1/models", headers={"Authorization": "Bearer secret:openai_api_key"})


@pytest.mark.asyncio
async def test_async_client_intercepts():
    """Verify async AsyncClient.send intercepts and replaces placeholders."""
    update_secret_cache("gemini_api_key", "secret-key-async-value")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "secret-key-async-value"
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": "secret:gemini_api_key"})


# ---------------------------------------------------------------------------
# Model-call logging (readable "a model was used" lines for server + UI console)
# ---------------------------------------------------------------------------


def test_llm_host_call_is_logged_with_status(caplog):
    """A call to a known LLM host logs a readable start + done line with status."""
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.INFO, logger="app.core.api_manager"):
        with httpx.Client(transport=transport) as client:
            client.get("https://api.openai.com/v1/chat/completions")

    messages = [r.getMessage() for r in caplog.records]
    assert any("model call >" in m and "api.openai.com" in m for m in messages)
    assert any("model call OK" in m and "HTTP 200" in m for m in messages)


def test_llm_host_error_status_logged_as_warning(caplog):
    """A 503 from an LLM host logs the failure marker at WARNING."""
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.INFO, logger="app.core.api_manager"):
        with httpx.Client(transport=transport) as client:
            client.get("https://generativelanguage.googleapis.com/v1beta/models")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("model call FAIL" in m and "HTTP 503" in m for m in warnings)


def test_non_llm_host_call_is_not_logged(caplog):
    """Routine traffic to a non-LLM host (e.g. model download) must NOT log a
    'model call' line — only LLM hosts are tracked, to keep the console clean."""
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"weights")

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.INFO, logger="app.core.api_manager"):
        with httpx.Client(transport=transport) as client:
            client.get("https://huggingface.co/some/model/resolve/main/model.safetensors")

    assert not any("model call" in r.getMessage() for r in caplog.records)


def test_model_call_log_never_contains_secret(caplog):
    """The model-call log must log host + status only — never the API key."""
    import logging

    update_secret_cache("openai_api_key", "sk-super-secret-xyz")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.INFO, logger="app.core.api_manager"):
        with httpx.Client(transport=transport) as client:
            client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": "Bearer secret:openai_api_key"},
            )

    for r in caplog.records:
        assert "sk-super-secret-xyz" not in r.getMessage()


def test_model_call_log_lines_are_cp1252_safe(caplog):
    """Windows consoles default to cp1252; fancy glyphs (▶ ✓ → …) raise
    UnicodeEncodeError in the StreamHandler and crash logging mid-job. Every
    model-call log line must be encodable as cp1252."""
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "x"})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.INFO, logger="app.core.api_manager"):
        with httpx.Client(transport=transport) as client:
            client.get("https://api.openai.com/v1/chat/completions")

    msgs = [r.getMessage() for r in caplog.records if "model call" in r.getMessage()]
    assert msgs, "expected at least one model-call log line"
    for m in msgs:
        m.encode("cp1252")  # raises UnicodeEncodeError if a glyph sneaks back in


# ---------------------------------------------------------------------------
# Per-provider concurrency cap
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_concurrency_state():
    """Reset the module-level concurrency maps before and after each test."""
    with am._concurrency_lock:
        am._provider_concurrency.clear()
        am._provider_hosts.clear()
        am._sync_semaphores.clear()
        am._async_semaphores.clear()
    with am._cache_lock:
        am._provider_keys.clear()
    with am._cooldown_lock:
        am._provider_cooldown_until.clear()
    yield
    with am._concurrency_lock:
        am._provider_concurrency.clear()
        am._provider_hosts.clear()
        am._sync_semaphores.clear()
        am._async_semaphores.clear()
    with am._cache_lock:
        am._provider_keys.clear()
    with am._cooldown_lock:
        am._provider_cooldown_until.clear()


def test_sync_concurrency_cap_limits_in_flight(clean_concurrency_state):
    """A provider with concurrency=N must never have more than N sync calls
    in flight at once, even when more threads fire simultaneously."""
    provider_id = "gemini"
    limit = 2
    total_threads = 6

    # Register the provider so the request host maps to it (keyless path).
    with am._concurrency_lock:
        am._provider_concurrency[provider_id] = limit
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    in_flight = 0
    peak = 0
    state_lock = threading.Lock()
    release_gate = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        with state_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Hold the connection so concurrent callers pile up against the gate.
        release_gate.wait(timeout=5)
        with state_lock:
            in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    def worker():
        with httpx.Client(transport=transport) as client:
            client.get("https://generativelanguage.googleapis.com/v1beta/models")

    threads = [threading.Thread(target=worker) for _ in range(total_threads)]
    for t in threads:
        t.start()

    # Give the gated threads time to reach peak saturation, then release.
    time.sleep(0.5)
    observed_peak = peak
    release_gate.set()
    for t in threads:
        t.join(timeout=5)

    assert observed_peak <= limit, f"peak in-flight {observed_peak} exceeded cap {limit}"
    assert observed_peak == limit, "expected the gate to saturate to its full cap"


def test_sync_unlimited_provider_not_gated(clean_concurrency_state):
    """A provider with no concurrency set must not be throttled."""
    provider_id = "openai"
    total_threads = 5

    with am._concurrency_lock:
        # No entry in _provider_concurrency => unlimited.
        am._provider_hosts["api.openai.com"] = provider_id

    in_flight = 0
    peak = 0
    state_lock = threading.Lock()
    release_gate = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        with state_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        release_gate.wait(timeout=5)
        with state_lock:
            in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    def worker():
        with httpx.Client(transport=transport) as client:
            client.get("https://api.openai.com/v1/models")

    threads = [threading.Thread(target=worker) for _ in range(total_threads)]
    for t in threads:
        t.start()

    time.sleep(0.5)
    observed_peak = peak
    release_gate.set()
    for t in threads:
        t.join(timeout=5)

    assert observed_peak == total_threads, (
        f"unlimited provider should run all {total_threads} at once, saw {observed_peak}"
    )


def test_concurrency_gate_released_on_error(clean_concurrency_state):
    """The gate must be released even when the underlying send raises, so a
    failing burst does not permanently starve the provider's capacity."""
    provider_id = "claude"
    limit = 1

    with am._concurrency_lock:
        am._provider_concurrency[provider_id] = limit
        am._provider_hosts["api.anthropic.com"] = provider_id

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)

    # Two sequential failing calls; if the gate leaked on the first, the second
    # would block forever and the semaphore value would be wrong.
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            with httpx.Client(transport=transport) as client:
                client.get("https://api.anthropic.com/v1/messages")

    assert call_count == 2
    # Semaphore fully released => can be acquired `limit` times without blocking.
    with am._concurrency_lock:
        sem = am._sync_semaphores.get(provider_id)
    assert sem is not None
    assert sem.acquire(blocking=False) is True
    sem.release()


# ---------------------------------------------------------------------------
# Live model hot-swap
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_override_state():
    """Reset model-override + provider maps before and after a test."""
    with am._override_lock:
        am._model_overrides.clear()
    with am._concurrency_lock:
        am._provider_hosts.clear()
        am._provider_concurrency.clear()
        am._sync_semaphores.clear()
    with am._cache_lock:
        am._provider_keys.clear()
    yield
    with am._override_lock:
        am._model_overrides.clear()
    with am._concurrency_lock:
        am._provider_hosts.clear()
        am._provider_concurrency.clear()
        am._sync_semaphores.clear()
    with am._cache_lock:
        am._provider_keys.clear()


def test_model_override_rewrites_gemini_url(clean_override_state):
    """A live override swaps the model name in the Gemini URL path mid-flight."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id
    am.set_model_override(provider_id, "gemini-3-flash-preview", "gemini-2.0-flash")

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        client.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3-flash-preview:generateContent",
            json={"contents": []},
        )

    assert seen_urls, "request never reached the transport"
    assert "gemini-2.0-flash:generateContent" in seen_urls[0]
    assert "gemini-3-flash-preview" not in seen_urls[0]


def test_model_override_rewrites_openai_body(clean_override_state):
    """For body-carried models (OpenAI/Claude), the override rewrites the JSON."""
    provider_id = "openai"
    with am._concurrency_lock:
        am._provider_hosts["api.openai.com"] = provider_id
    am.set_model_override(provider_id, "gpt-4o-mini", "gpt-4o")

    seen_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        client.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": []},
        )

    assert seen_bodies, "request never reached the transport"
    assert b'"gpt-4o"' in seen_bodies[0]
    assert b"gpt-4o-mini" not in seen_bodies[0]


def test_model_override_only_affects_its_provider(clean_override_state):
    """An override for one provider must not rewrite another provider's call."""
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = "gemini"
        am._provider_hosts["api.openai.com"] = "openai"
    am.set_model_override("gemini", "gemini-3-flash-preview", "gemini-2.0-flash")

    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    # OpenAI call references the gemini model name in its body; must be untouched.
    with httpx.Client(transport=transport) as client:
        client.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gemini-3-flash-preview", "messages": []},
        )

    assert seen and b"gemini-3-flash-preview" in seen[0]


def test_clear_model_override_stops_rewrite(clean_override_state):
    """After clearing, requests pass through with the original model intact."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id
    am.set_model_override(provider_id, "gemini-3-flash-preview", "gemini-2.0-flash")
    am.clear_model_override(provider_id)

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        client.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3-flash-preview:generateContent",
            json={"contents": []},
        )

    assert seen_urls and "gemini-3-flash-preview:generateContent" in seen_urls[0]


def test_set_provider_concurrency_live_rebuilds_semaphore(clean_override_state):
    """Changing concurrency live drops the old semaphore so the new cap applies."""
    provider_id = "gemini"
    am.set_provider_concurrency(provider_id, 4)
    with am._concurrency_lock:
        assert am._provider_concurrency[provider_id] == 4
    # Seed a stale semaphore, then lower the cap; it must be discarded.
    with am._concurrency_lock:
        am._sync_semaphores[provider_id] = threading.Semaphore(4)
    am.set_provider_concurrency(provider_id, 2)
    with am._concurrency_lock:
        assert am._provider_concurrency[provider_id] == 2
        assert provider_id not in am._sync_semaphores  # rebuilt lazily at new cap
    # Setting to None removes the cap entirely.
    am.set_provider_concurrency(provider_id, None)
    with am._concurrency_lock:
        assert provider_id not in am._provider_concurrency


# ---------------------------------------------------------------------------
# "Suggest a model swap" signal (only when key rotation can't recover)
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_stuck_state():
    with am._stuck_lock:
        am._stuck_counter.clear()
    with am._concurrency_lock:
        am._provider_hosts.clear()
    with am._cache_lock:
        am._provider_keys.clear()
    yield
    with am._stuck_lock:
        am._stuck_counter.clear()
    with am._concurrency_lock:
        am._provider_hosts.clear()
    with am._cache_lock:
        am._provider_keys.clear()


def test_swap_suggested_after_threshold_consecutive_rate_limits(clean_stuck_state, caplog):
    """With a single key (no rotation possible), N consecutive 429s emit a
    'model swap suggested' line at every threshold crossing (3, 6, 9, ...) —
    not just once, so a sustained storm keeps nudging the user."""
    import logging

    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.WARNING, logger="app.core.api_manager"):
        # STUCK_THRESHOLD + 2 = 5 calls -> streak hits 5 -> emit at 3 only.
        for _ in range(am._STUCK_THRESHOLD + 2):
            with httpx.Client(transport=transport) as client:
                client.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "gemini-3-flash-preview:generateContent",
                    json={"contents": []},
                )

    suggestions = [m.getMessage() for m in caplog.records if "model swap suggested" in m.getMessage()]
    assert len(suggestions) == 1, f"expected one suggestion at streak=3, got {len(suggestions)}"
    assert provider_id in suggestions[0]


def test_no_swap_suggestion_below_threshold(clean_stuck_state, caplog):
    """A couple of rate limits (below threshold) must NOT nudge the user."""
    import logging

    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.WARNING, logger="app.core.api_manager"):
        for _ in range(am._STUCK_THRESHOLD - 1):
            with httpx.Client(transport=transport) as client:
                client.get("https://generativelanguage.googleapis.com/v1beta/models")

    assert not any("model swap suggested" in m.getMessage() for m in caplog.records)


def test_success_resets_rate_limit_streak(clean_stuck_state, caplog):
    """A successful call resets the streak, so intermittent 429s never accumulate
    to a suggestion."""
    import logging

    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        # Fail, fail, succeed, repeat - never N-in-a-row.
        if state["calls"] % 3 == 0:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(429, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    with caplog.at_level(logging.WARNING, logger="app.core.api_manager"):
        for _ in range(12):
            with httpx.Client(transport=transport) as client:
                client.get("https://generativelanguage.googleapis.com/v1beta/models")

    assert not any("model swap suggested" in m.getMessage() for m in caplog.records)


# ---------------------------------------------------------------------------
# Phase 1: exponential backoff, Retry-After, cooldown, stuck re-emit, 504
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_backoff_state():
    """Reset backoff/cooldown/stuck state for backoff-focused tests."""
    with am._cooldown_lock:
        am._provider_cooldown_until.clear()
    with am._stuck_lock:
        am._stuck_counter.clear()
    with am._concurrency_lock:
        am._provider_hosts.clear()
    with am._cache_lock:
        am._provider_keys.clear()
    yield
    with am._cooldown_lock:
        am._provider_cooldown_until.clear()
    with am._stuck_lock:
        am._stuck_counter.clear()
    with am._concurrency_lock:
        am._provider_hosts.clear()
    with am._cache_lock:
        am._provider_keys.clear()


def test_parse_retry_after_seconds(clean_backoff_state):
    """Retry-After as integer seconds is parsed and capped at _BACKOFF_CAP."""
    res = httpx.Response(429, headers={"Retry-After": "5"})
    assert am._parse_retry_after(res) == 5.0

    res = httpx.Response(429, headers={"Retry-After": "999"})
    assert am._parse_retry_after(res) == am._BACKOFF_CAP


def test_parse_retry_after_missing_returns_none(clean_backoff_state):
    res = httpx.Response(429)
    assert am._parse_retry_after(res) is None


def test_compute_backoff_exponential(clean_backoff_state):
    """Without Retry-After, backoff follows base * factor^attempt, capped."""
    assert am._compute_backoff(0, None) == 2.0   # 2 * 2^0
    assert am._compute_backoff(1, None) == 4.0   # 2 * 2^1
    assert am._compute_backoff(2, None) == 8.0   # 2 * 2^2
    assert am._compute_backoff(3, None) == 16.0  # 2 * 2^3
    assert am._compute_backoff(10, None) == am._BACKOFF_CAP  # capped


def test_compute_backoff_retry_after_wins(clean_backoff_state):
    """Retry-After larger than computed backoff is honored (capped)."""
    assert am._compute_backoff(0, 10.0) == 10.0
    assert am._compute_backoff(1, 10.0) == 10.0  # computed 4 < 10
    assert am._compute_backoff(0, 999.0) == am._BACKOFF_CAP  # capped


def test_retry_after_header_honored(clean_backoff_state, monkeypatch):
    """A 429 with Retry-After: 5 followed by a 200 recovers on the same key."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    sleeps: list[float] = []
    monkeypatch.setattr(am, "_sleep_sync", lambda s: sleeps.append(s))

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, json={"err": "limited"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    assert res.status_code == 200
    assert state["calls"] == 2  # initial + 1 backoff retry
    assert sleeps == [5.0]  # Retry-After honored


def test_exponential_backoff_sequence(clean_backoff_state, monkeypatch):
    """Four consecutive 429s without Retry-After sleep 2,4,8,16 then give up."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    sleeps: list[float] = []
    monkeypatch.setattr(am, "_sleep_sync", lambda s: sleeps.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"err": "limited"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    # Backoff exhausted: 4 sleeps (2,4,8,16), 5 total calls (1 initial + 4 retries).
    assert sleeps == [2.0, 4.0, 8.0, 16.0]
    assert res.status_code == 429  # last response returned


def test_backoff_recovers_on_third_attempt(clean_backoff_state, monkeypatch):
    """Two 429s then a 200: backoff recovers, no rotation needed."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= 2:
            return httpx.Response(429, json={"err": "limited"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    assert res.status_code == 200
    assert state["calls"] == 3
    # Cooldown cleared on success.
    assert provider_id not in am._provider_cooldown_until


def test_cooldown_set_after_backoff_exhausts(clean_backoff_state, monkeypatch):
    """When backoff exhausts on a single-key provider, cooldown is set."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"err": "limited"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    assert provider_id in am._provider_cooldown_until


def test_cooldown_blocks_subsequent_calls(clean_backoff_state, monkeypatch):
    """During cooldown, a new call returns a synthetic 429 (with Retry-After)
    instead of hitting the network — so marker's retry loop sleeps rather than
    hammering the throttled endpoint."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    # Simulate an already-active cooldown with a large remaining window.
    with am._cooldown_lock:
        am._provider_cooldown_until[provider_id] = time.time() + 100.0

    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    # Network handler should never be reached — cooldown short-circuits.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be hit during cooldown")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    assert res.status_code == 429
    assert res.headers.get("Retry-After") is not None


def test_success_clears_cooldown(clean_backoff_state, monkeypatch):
    """A 2xx response clears any active cooldown for the provider. The
    short-circuit only fires while the window is positive, so an expired
    cooldown lets the call through and the 2xx clears the stale entry."""
    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id
    # Already-expired cooldown: remaining == 0, so the call proceeds to network.
    with am._cooldown_lock:
        am._provider_cooldown_until[provider_id] = time.time() - 1.0

    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    assert res.status_code == 200
    assert provider_id not in am._provider_cooldown_until


def test_stuck_signal_re_emits_at_every_threshold(clean_backoff_state, caplog, monkeypatch):
    """Streak 6 emits twice (at 3 and 6), streak 9 emits three times."""
    import logging

    provider_id = "gemini"
    with am._concurrency_lock:
        am._provider_hosts["generativelanguage.googleapis.com"] = provider_id

    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"err": "limited"})

    transport = httpx.MockTransport(handler)
    target = am._STUCK_THRESHOLD * 3  # 9
    with caplog.at_level(logging.WARNING, logger="app.core.api_manager"):
        for _ in range(target):
            with httpx.Client(transport=transport) as client:
                client.post("https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")

    suggestions = [m for m in caplog.records if "model swap suggested" in m.getMessage()]
    assert len(suggestions) == 3, f"expected 3 re-emits (at 3,6,9), got {len(suggestions)}"


def test_504_triggers_rotation(clean_backoff_state, monkeypatch):
    """A 504 response triggers fallback-key rotation (was previously a bug:
    504 was in _RATE_LIMIT_STATUSES but not in the rotation trigger set)."""
    provider_id = "openai"
    with am._concurrency_lock:
        am._provider_hosts["api.openai.com"] = provider_id
    with am._cache_lock:
        am._provider_keys[provider_id] = ["key-primary", "key-fallback"]
        am._active_key_index[provider_id] = 0

    monkeypatch.setattr(am, "_sleep_sync", lambda _s: None)

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        auth = request.headers.get("Authorization", "")
        if "key-primary" in auth:
            return httpx.Response(504, json={"err": "gateway timeout"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer key-primary"},
            json={"model": "gpt-4"},
        )

    assert res.status_code == 200
    assert state["calls"] >= 2  # initial 504 + rotated retry
    # Active key advanced to the fallback.
    assert am._active_key_index[provider_id] == 1


def test_backoff_then_rotation_recovers(clean_backoff_state, monkeypatch):
    """Backoff exhausts (4 attempts, all 429 on primary key), then rotation to a
    fallback key succeeds — full recovery."""
    provider_id = "openai"
    with am._concurrency_lock:
        am._provider_hosts["api.openai.com"] = provider_id
    with am._cache_lock:
        am._provider_keys[provider_id] = ["key-primary", "key-fallback"]
        am._active_key_index[provider_id] = 0

    sleeps: list[float] = []
    monkeypatch.setattr(am, "_sleep_sync", lambda s: sleeps.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if "key-primary" in auth:
            return httpx.Response(429, json={"err": "limited"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        res = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer key-primary"},
            json={"model": "gpt-4"},
        )

    assert res.status_code == 200
    # Backoff ran 4 sleeps before rotation took over.
    assert len(sleeps) == am._BACKOFF_MAX_ATTEMPTS
    # Fallback key is now active.
    assert am._active_key_index[provider_id] == 1
    # Cooldown cleared on recovery.
    assert provider_id not in am._provider_cooldown_until

