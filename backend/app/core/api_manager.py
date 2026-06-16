"""Live API interceptor and in-memory secure secrets cache for Marker UI."""

import asyncio
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse
import httpx

logger = logging.getLogger(__name__)

# Pattern to search for placeholder secret references in request headers, url, or body
SECRET_REF_PATTERN = re.compile(r"secret:([a-zA-Z0-9_-]+_api_key|[a-zA-Z0-9_-]+_project_id)")

# Thread-safe in-memory cache for decrypted API keys/secrets
_secrets_cache: dict[str, str] = {}
_provider_keys: dict[str, list[str]] = {}  # provider_id -> [decrypted_keys...]
_active_key_index: dict[str, int] = {}     # provider_id -> active index (default 0)
_cache_lock = threading.Lock()

# --- Per-provider concurrency control --------------------------------------
# Marker fires a burst of parallel LLM calls (e.g. LLMTableProcessor runs 8
# table requests at once). An overloaded endpoint answers with 504
# DEADLINE_EXCEEDED. A per-provider semaphore caps how many calls are in flight
# at any instant, across every job and worker thread, smoothing that burst.
_provider_concurrency: dict[str, int] = {}          # provider_id -> max in-flight (>=1)
_provider_hosts: dict[str, str] = {}                # lowercased host -> provider_id
_sync_semaphores: dict[str, threading.Semaphore] = {}
_async_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}
_concurrency_lock = threading.Lock()

# --- Live model hot-swap ----------------------------------------------------
# When a running job hits repeated rate limits on a preview/overloaded model,
# the user can swap to a sibling model of the SAME provider without losing
# work. The converter already running has its model name baked in, so we
# rewrite the outgoing request at the httpx layer: provider_id -> (old, new).
# Only same-provider swaps work here (identical host/auth/wire format); a
# different provider would need a different client entirely.
_model_overrides: dict[str, tuple[str, str]] = {}
_override_lock = threading.Lock()

# --- "Suggest a model swap" signal ------------------------------------------
# Key rotation already recovers from a single key's rate limit by trying the
# next key. We only want to nudge the user to swap models when recovery is
# genuinely stuck: every key is rate-limited, or there is one key, or the model
# itself is overloaded (repeated 504). So we count ONLY final, post-rotation
# rate-limit/timeout outcomes per provider and reset on any success. When the
# streak crosses the threshold we emit ONE log line the UI listens for.
_STUCK_THRESHOLD = 3
_stuck_counter: dict[str, int] = {}
_stuck_lock = threading.Lock()
# Status codes that mean "rate limited / overloaded" rather than a config error.
_RATE_LIMIT_STATUSES = (429, 503, 504)


def reset_stuck_counter(provider_id: str) -> None:
    """Forget the rate-limit streak for a provider (e.g. after a model swap)."""
    with _stuck_lock:
        _stuck_counter.pop(provider_id, None)


def _note_call_outcome(provider_id: str | None, *, ok: bool, rate_limited: bool) -> None:
    """Track consecutive unrecovered rate-limit/timeout outcomes per provider.

    Emits a single 'model swap suggested' log line (which the frontend watches)
    when the streak first crosses the threshold. A success resets the streak.
    """
    if not provider_id:
        return
    with _stuck_lock:
        if ok:
            _stuck_counter.pop(provider_id, None)
            return
        if not rate_limited:
            return  # auth/other errors are not a "swap model" situation
        streak = _stuck_counter.get(provider_id, 0) + 1
        _stuck_counter[provider_id] = streak
        crossed = streak == _STUCK_THRESHOLD
    if crossed:
        # ASCII-only: Windows consoles default to cp1252 (see memory).
        logger.warning(
            "model swap suggested for provider %s: %d consecutive rate-limit/timeout responses",
            provider_id,
            _STUCK_THRESHOLD,
        )



def set_model_override(provider_id: str, old_model: str, new_model: str) -> None:
    """Install a live old->new model rewrite for a provider's in-flight calls."""
    with _override_lock:
        if not new_model or old_model == new_model:
            _model_overrides.pop(provider_id, None)
        else:
            _model_overrides[provider_id] = (old_model, new_model)
    # New model => fresh start; forget the previous model's rate-limit streak.
    reset_stuck_counter(provider_id)


def clear_model_override(provider_id: str) -> None:
    """Remove any live model rewrite for a provider."""
    with _override_lock:
        _model_overrides.pop(provider_id, None)


def set_provider_concurrency(provider_id: str, limit: int | None) -> None:
    """Update a provider's concurrency cap live and reset its semaphores so the
    new limit applies to subsequent calls. None/<=0 means unlimited."""
    with _concurrency_lock:
        if limit and limit > 0:
            _provider_concurrency[provider_id] = limit
        else:
            _provider_concurrency.pop(provider_id, None)
        # Drop cached semaphores; they are lazily rebuilt at the new limit.
        _sync_semaphores.pop(provider_id, None)
        for key in [k for k in _async_semaphores if k[0] == provider_id]:
            _async_semaphores.pop(key, None)


def _apply_model_override(request: httpx.Request, provider_id: str | None) -> None:
    """Rewrite the model name in a request when a live override is set.

    Gemini carries the model in the URL path
    (``/v1beta/models/<model>:generateContent``); OpenAI/Claude/Ollama carry it
    in the JSON body as ``"model": "<model>"``. A literal old->new replace
    covers both without parsing provider-specific schemas.
    """
    if not provider_id:
        return
    with _override_lock:
        mapping = _model_overrides.get(provider_id)
    if not mapping:
        return
    old_model, new_model = mapping

    url_str = str(request.url)
    if old_model in url_str:
        request.url = httpx.URL(url_str.replace(old_model, new_model))

    if hasattr(request, "_content") and request._content and old_model.encode("utf-8") in request._content:
        try:
            content_str = request._content.decode("utf-8", errors="ignore")
            request._content = content_str.replace(old_model, new_model).encode("utf-8")
            request.headers["content-length"] = str(len(request._content))
        except Exception as e:  # noqa: BLE001 - never break a request over a rewrite
            logger.error("Failed to apply model override in request body: %s", e)



def _identify_provider(request: httpx.Request) -> str | None:
    """Best-effort map a request to a configured provider id.

    Tries the resolved API key first (covers keyed providers), then falls back
    to the request host (covers keyless providers like Ollama). Runs after
    `_resolve_request`, so the real key is present in the request.
    """
    matched = _find_key_in_request(request)
    if matched:
        return matched[0]

    host = (request.url.host or "").lower()
    if host:
        with _concurrency_lock:
            return _provider_hosts.get(host)
    return None


def _acquire_sync_gate(provider_id: str | None) -> threading.Semaphore | None:
    """Acquire (blocking) the sync concurrency gate for a provider, if capped.

    Returns the semaphore so the caller can release it, or None when the
    provider is unlimited / unknown.
    """
    if not provider_id:
        return None
    with _concurrency_lock:
        limit = _provider_concurrency.get(provider_id)
        if not limit or limit <= 0:
            return None
        sem = _sync_semaphores.get(provider_id)
        if sem is None:
            sem = threading.Semaphore(limit)
            _sync_semaphores[provider_id] = sem
    sem.acquire()
    return sem


def _acquire_async_gate(provider_id: str | None) -> asyncio.Semaphore | None:
    """Acquire the async concurrency gate for a provider on the current loop.

    asyncio.Semaphore is bound to one event loop, so cache per (provider, loop).
    Returns the semaphore (already acquired) or None when unlimited / unknown.
    """
    if not provider_id:
        return None
    with _concurrency_lock:
        limit = _provider_concurrency.get(provider_id)
        if not limit or limit <= 0:
            return None
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        return None
    key = (provider_id, loop_id)
    with _concurrency_lock:
        sem = _async_semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            _async_semaphores[key] = sem
    return sem

# Original httpx.Client.send and httpx.AsyncClient.send methods
_orig_client_send = httpx.Client.send
_orig_async_client_send = httpx.AsyncClient.send


async def load_secrets_from_db() -> None:
    """Load and decrypt all secrets from DB into the in-memory cache on startup."""
    from app.crypto import decrypt_value
    from app.database import async_session_factory
    from app.models.settings import Setting
    from sqlalchemy import select
    from app.routes.settings import ALLOWED_LLM_HOSTS, init_llm_providers_if_missing
    import json

    logger.info("Initializing in-memory secrets cache from DB...")
    async with async_session_factory() as session:
        try:
            await init_llm_providers_if_missing(session)

            stmt = select(Setting).where(Setting.key == "llm_providers")
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            providers = []
            if row:
                try:
                    providers = json.loads(row.value)
                except Exception:
                    pass

            with _cache_lock:
                _secrets_cache.clear()
                _provider_keys.clear()

                with _concurrency_lock:
                    _provider_concurrency.clear()
                    _provider_hosts.clear()
                    _sync_semaphores.clear()
                    _async_semaphores.clear()

                for p in providers:
                    pid = p.get("id")
                    if not pid:
                        continue

                    decrypted_keys = []

                    # Primary key
                    api_key = p.get("api_key")
                    if api_key:
                        api_key = decrypt_value(api_key)
                        decrypted_keys.append(api_key)
                        _secrets_cache[f"provider_{pid}_key_0_api_key"] = api_key

                    # Fallback keys
                    fallbacks = p.get("fallback_api_keys", [])
                    for i, fb in enumerate(fallbacks):
                        if fb:
                            fb = decrypt_value(fb)
                            decrypted_keys.append(fb)
                            _secrets_cache[f"provider_{pid}_key_{i+1}_api_key"] = fb

                    _provider_keys[pid] = decrypted_keys
                    _active_key_index[pid] = 0

                    # Per-provider concurrency cap (None/<=0 => unlimited).
                    concurrency = p.get("concurrency")
                    base_url = p.get("base_url")
                    with _concurrency_lock:
                        if isinstance(concurrency, int) and concurrency > 0:
                            _provider_concurrency[pid] = concurrency
                        if base_url:
                            host = urlparse(base_url).hostname
                            if host:
                                _provider_hosts[host.lower()] = pid

                    # Add base URL hostnames dynamically to allowed list for SSRF protection
                    if base_url:
                        parsed_host = urlparse(base_url).hostname
                        if parsed_host:
                            ALLOWED_LLM_HOSTS.add(parsed_host.lower())
                            logger.debug("Added %s to ALLOWED_LLM_HOSTS", parsed_host.lower())

            logger.info("Secrets cache initialized successfully.")
        except Exception as e:
            logger.error("Failed to load secrets from DB: %s", e, exc_info=True)


def update_secret_cache(key: str, decrypted_value: str) -> None:
    """Update a specific secret in the in-memory cache."""
    with _cache_lock:
        _secrets_cache[key] = decrypted_value
        logger.debug("Updated secret cache for key: %s", key)


def get_secret(key: str) -> str:
    """Retrieve a decrypted secret from the in-memory cache."""
    with _cache_lock:
        return _secrets_cache.get(key, "")


def _resolve_string(val: str) -> str:
    """Replace any 'secret:<key>' matches with their cached decrypted value."""
    def replace_match(match: re.Match[str]) -> str:
        secret_key = match.group(1)
        # Check provider pattern: provider_{provider_id}_key_{index}_api_key
        m = re.match(r"provider_([a-zA-Z0-9_-]+)_key_(\d+)_api_key", secret_key)
        if m:
            provider_id = m.group(1)
            with _cache_lock:
                keys = _provider_keys.get(provider_id, [])
                active_idx = _active_key_index.get(provider_id, 0)
                if active_idx < len(keys):
                    return keys[active_idx]
                elif keys:
                    return keys[0]

        secret_val = get_secret(secret_key)
        if secret_val:
            return secret_val
        return match.group(0)

    return SECRET_REF_PATTERN.sub(replace_match, val)


def _resolve_request(request: httpx.Request) -> None:
    """Intercept request headers, url, and content, substituting secret references."""
    for name, value in list(request.headers.items()):
        if "secret:" in value:
            request.headers[name] = _resolve_string(value)

    url_str = str(request.url)
    if "secret:" in url_str:
        request.url = httpx.URL(_resolve_string(url_str))

    if hasattr(request, "_content") and request._content and b"secret:" in request._content:
        try:
            content_str = request._content.decode("utf-8", errors="ignore")
            resolved_content = _resolve_string(content_str)
            request._content = resolved_content.encode("utf-8")
            request.headers["content-length"] = str(len(request._content))
            logger.debug("Substituted secret references in request body")
        except Exception as e:
            logger.error("Failed to resolve secrets in request body: %s", e)


def _find_key_in_request(request: httpx.Request) -> tuple[str, int, str] | None:
    """Scan the request object to identify which provider key was used."""
    with _cache_lock:
        for provider_id, keys in _provider_keys.items():
            for idx, key in enumerate(keys):
                if not key:
                    continue
                # Check headers
                for val in request.headers.values():
                    if key in val:
                        return provider_id, idx, key
                # Check URL
                if key in str(request.url):
                    return provider_id, idx, key
                # Check body
                if hasattr(request, "_content") and request._content and key.encode("utf-8") in request._content:
                    return provider_id, idx, key
    return None


def _replace_key_in_request(request: httpx.Request, old_key: str, new_key: str) -> None:
    """Replace an API key value with a fallback key inside the request."""
    for name, value in list(request.headers.items()):
        if old_key in value:
            request.headers[name] = value.replace(old_key, new_key)

    url_str = str(request.url)
    if old_key in url_str:
        request.url = httpx.URL(url_str.replace(old_key, new_key))

    if hasattr(request, "_content") and request._content and old_key.encode("utf-8") in request._content:
        try:
            content_str = request._content.decode("utf-8", errors="ignore")
            resolved_content = content_str.replace(old_key, new_key)
            request._content = resolved_content.encode("utf-8")
            request.headers["content-length"] = str(len(request._content))
        except Exception as e:
            logger.error("Failed to replace key in request body: %s", e)


def _handle_rotation_sync(
    client: httpx.Client,
    request: httpx.Request,
    error_or_res: Any,
    *args: Any,
    **kwargs: Any,
) -> httpx.Response | None:
    """Determine if fallback API key rotation is possible and retry the request synchronously."""
    matched = _find_key_in_request(request)
    if not matched:
        return None if isinstance(error_or_res, Exception) else error_or_res

    provider_id, key_index, key_value = matched
    with _cache_lock:
        keys_list = _provider_keys.get(provider_id, [])
        curr_active = _active_key_index.get(provider_id, 0)
        if key_index == curr_active:
            next_index = key_index + 1
            if next_index < len(keys_list):
                _active_key_index[provider_id] = next_index
                logger.warning("Rotating API key for provider %s to index %d due to error/timeout", provider_id, next_index)

        new_active = _active_key_index.get(provider_id, 0)
        if new_active < len(keys_list):
            new_key = keys_list[new_active]
            _replace_key_in_request(request, key_value, new_key)
            try:
                res = _orig_client_send(client, request, *args, **kwargs)
                if res.status_code in (401, 403, 429, 503):
                    return _handle_rotation_sync(client, request, res, *args, **kwargs)
                return res
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                return _handle_rotation_sync(client, request, e, *args, **kwargs)

    return None if isinstance(error_or_res, Exception) else error_or_res


async def _handle_rotation_async(
    client: httpx.AsyncClient,
    request: httpx.Request,
    error_or_res: Any,
    *args: Any,
    **kwargs: Any,
) -> httpx.Response | None:
    """Determine if fallback API key rotation is possible and retry the request asynchronously."""
    matched = _find_key_in_request(request)
    if not matched:
        return None if isinstance(error_or_res, Exception) else error_or_res

    provider_id, key_index, key_value = matched
    with _cache_lock:
        keys_list = _provider_keys.get(provider_id, [])
        curr_active = _active_key_index.get(provider_id, 0)
        if key_index == curr_active:
            next_index = key_index + 1
            if next_index < len(keys_list):
                _active_key_index[provider_id] = next_index
                logger.warning("Rotating API key for provider %s to index %d due to error/timeout", provider_id, next_index)

        new_active = _active_key_index.get(provider_id, 0)
        if new_active < len(keys_list):
            new_key = keys_list[new_active]
            _replace_key_in_request(request, key_value, new_key)
            try:
                res = await _orig_async_client_send(client, request, *args, **kwargs)
                if res.status_code in (401, 403, 429, 503):
                    return await _handle_rotation_async(client, request, res, *args, **kwargs)
                return res
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                return await _handle_rotation_async(client, request, e, *args, **kwargs)

    return None if isinstance(error_or_res, Exception) else error_or_res


def _is_llm_host(request: httpx.Request) -> bool:
    """True if the request targets a known LLM provider host.

    Gates model-call logging so routine traffic (model downloads from
    huggingface, etc.) does not spam the console. Imported lazily to avoid a
    circular import with the settings module.
    """
    try:
        from app.routes.settings import ALLOWED_LLM_HOSTS

        host = (request.url.host or "").lower()
        return host in ALLOWED_LLM_HOSTS
    except Exception:  # noqa: BLE001 - never let logging gate break a request
        return False


def _log_model_call_start(request: httpx.Request) -> bool:
    """Log that a model call is starting. Returns True if it was an LLM host
    (so the caller knows to log the matching completion line)."""
    if not _is_llm_host(request):
        return False
    logger.info("model call > %s %s", request.method, request.url.host)
    return True


def _log_model_call_done(
    request: httpx.Request, status_code: int, elapsed_ms: int
) -> None:
    """Log the outcome of a model call (host, status, duration). No secrets."""
    marker = "OK" if 200 <= status_code < 300 else "FAIL"
    log = logger.info if 200 <= status_code < 300 else logger.warning
    log(
        "model call %s %s -> HTTP %d (%dms)",
        marker,
        request.url.host,
        status_code,
        elapsed_ms,
    )


def patched_client_send(self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
    """Patched synchronous send method with auto key rotation retry."""
    _resolve_request(request)
    is_llm = _log_model_call_start(request)
    provider_id = _identify_provider(request) if is_llm else None
    if is_llm:
        _apply_model_override(request, provider_id)
    gate = _acquire_sync_gate(provider_id) if is_llm else None
    t0 = time.perf_counter()
    try:
        res = _orig_client_send(self, request, *args, **kwargs)
        if is_llm:
            _log_model_call_done(
                request, res.status_code, int((time.perf_counter() - t0) * 1000)
            )
        if res.status_code in (401, 403, 429, 503):
            rotated = _handle_rotation_sync(self, request, res, *args, **kwargs)
            if rotated:
                if is_llm:
                    _note_call_outcome(
                        provider_id,
                        ok=200 <= rotated.status_code < 300,
                        rate_limited=rotated.status_code in _RATE_LIMIT_STATUSES,
                    )
                return rotated
        if is_llm:
            _note_call_outcome(
                provider_id,
                ok=200 <= res.status_code < 300,
                rate_limited=res.status_code in _RATE_LIMIT_STATUSES,
            )
        return res
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        if is_llm:
            logger.warning(
                "model call FAIL %s -> %s (%dms)",
                request.url.host,
                type(e).__name__,
                int((time.perf_counter() - t0) * 1000),
            )
        rotated = _handle_rotation_sync(self, request, e, *args, **kwargs)
        if rotated:
            if is_llm:
                _note_call_outcome(
                    provider_id,
                    ok=200 <= rotated.status_code < 300,
                    rate_limited=rotated.status_code in _RATE_LIMIT_STATUSES,
                )
            return rotated
        if is_llm:
            # A timeout that rotation could not recover counts as overloaded.
            _note_call_outcome(provider_id, ok=False, rate_limited=True)
        raise
    finally:
        if gate is not None:
            gate.release()


async def patched_async_client_send(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
    """Patched asynchronous send method with auto key rotation retry."""
    _resolve_request(request)
    is_llm = _log_model_call_start(request)
    provider_id = _identify_provider(request) if is_llm else None
    if is_llm:
        _apply_model_override(request, provider_id)
    gate = _acquire_async_gate(provider_id) if is_llm else None
    if gate is not None:
        await gate.acquire()
    t0 = time.perf_counter()
    try:
        res = await _orig_async_client_send(self, request, *args, **kwargs)
        if is_llm:
            _log_model_call_done(
                request, res.status_code, int((time.perf_counter() - t0) * 1000)
            )
        if res.status_code in (401, 403, 429, 503):
            rotated = await _handle_rotation_async(self, request, res, *args, **kwargs)
            if rotated:
                if is_llm:
                    _note_call_outcome(
                        provider_id,
                        ok=200 <= rotated.status_code < 300,
                        rate_limited=rotated.status_code in _RATE_LIMIT_STATUSES,
                    )
                return rotated
        if is_llm:
            _note_call_outcome(
                provider_id,
                ok=200 <= res.status_code < 300,
                rate_limited=res.status_code in _RATE_LIMIT_STATUSES,
            )
        return res
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        if is_llm:
            logger.warning(
                "model call FAIL %s -> %s (%dms)",
                request.url.host,
                type(e).__name__,
                int((time.perf_counter() - t0) * 1000),
            )
        rotated = await _handle_rotation_async(self, request, e, *args, **kwargs)
        if rotated:
            if is_llm:
                _note_call_outcome(
                    provider_id,
                    ok=200 <= rotated.status_code < 300,
                    rate_limited=rotated.status_code in _RATE_LIMIT_STATUSES,
                )
            return rotated
        if is_llm:
            _note_call_outcome(provider_id, ok=False, rate_limited=True)
        raise
    finally:
        if gate is not None:
            gate.release()


def setup_api_manager_monkeypatch() -> None:
    """Apply monkeypatches to httpx Client/AsyncClient send methods."""
    logger.info("Applying httpx client send monkeypatches for live secret substitution...")
    httpx.Client.send = patched_client_send  # type: ignore[assignment]
    httpx.AsyncClient.send = patched_async_client_send  # type: ignore[assignment]

