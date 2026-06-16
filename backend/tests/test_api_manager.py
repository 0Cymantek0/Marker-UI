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
def setup_and_teardown_monkeypatch():
    """Apply interceptor monkeypatch and clean up after each test."""
    setup_api_manager_monkeypatch()
    yield
    # Restore original methods
    httpx.Client.send = _orig_client_send
    httpx.AsyncClient.send = _orig_async_client_send


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
    """Reset the module-level concurrency maps before and after a test."""
    with am._concurrency_lock:
        am._provider_concurrency.clear()
        am._provider_hosts.clear()
        am._sync_semaphores.clear()
        am._async_semaphores.clear()
    with am._cache_lock:
        am._provider_keys.clear()
    yield
    with am._concurrency_lock:
        am._provider_concurrency.clear()
        am._provider_hosts.clear()
        am._sync_semaphores.clear()
        am._async_semaphores.clear()
    with am._cache_lock:
        am._provider_keys.clear()


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

