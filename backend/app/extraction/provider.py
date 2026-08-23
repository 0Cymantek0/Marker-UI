"""Provider-neutral specialist transport contract.

One boundary, two implementations:

* :class:`OpenAICompatProvider` — an OpenAI-compatible chat-completions
  client with an injectable transport, bounded retries, and typed
  outcomes. 401/403 fail fast (no retry storm), 429/5xx/transport
  faults retry a bounded number of times with injectable backoff, and
  a malformed 200 is reported as data, never improvised into a
  response. The request payload NEVER carries tools: the specialist
  cannot gain capabilities by being asked nicely inside a document.
* :class:`ReplayProvider` — deterministic recorded responses keyed by
  ``(model, prompt)``. A miss is an explicit typed outcome, never an
  invented response. Tests and offline benchmarks replay through this
  provider so nothing in CI depends on credentials or a network.

No model id is hardcoded: callers pass the exact model they selected
(from configuration or a catalog), honoring the repository's
model-selection rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

PROVIDER_OK = "ok"
PROVIDER_AUTH_ERROR = "auth_error"
PROVIDER_CLIENT_ERROR = "client_error"
PROVIDER_RATE_LIMITED = "rate_limited"
PROVIDER_SERVER_ERROR = "server_error"
PROVIDER_TRANSPORT_ERROR = "transport_error"
PROVIDER_BAD_RESPONSE = "bad_response"
PROVIDER_CACHE_MISS = "cache_miss"

#: Transport contract: request payload -> (http_status, body_text).
Transport = Callable[[dict[str, Any]], tuple[int, str]]

#: Env var names for the live provider (never hardcoded values).
API_KEY_ENV_VARS = ("MARKER_SPECIALIST_API_KEY", "OPENAI_COMPAT_API_KEY")
BASE_URL_ENV_VAR = "MARKER_SPECIALIST_BASE_URL"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: Body snippet length kept in error details (bounded, secret-free).
_ERROR_SNIPPET_CHARS = 300


def resolve_api_key() -> str | None:
    """Find the specialist API key without assuming env-var casing."""
    for name in API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    lowered = {k.lower(): v for k, v in os.environ.items()}
    for name in API_KEY_ENV_VARS:
        candidate = lowered.get(name.lower())
        if candidate:
            return candidate
    return None


def replay_key(model: str, user_text: str) -> str:
    """Deterministic replay-cache key over (model, prompt)."""
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(user_text.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class ModelIdentity:
    """Stable identity of the trained specialist being consulted.

    ``family`` records shared model lineage so downstream consumers can
    never mistake two responses from the same family for independent
    producers.
    """

    provider: str
    model: str
    family: str

    @property
    def producer_id(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class ProviderResult:
    """One typed provider outcome; ``content`` is untrusted data."""

    status: str
    content: str | None = None
    model_served: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    from_cache: bool = False
    error: str | None = None


class SpecialistProvider(Protocol):
    """The provider-neutral boundary a specialist lane depends on."""

    @property
    def model_identity(self) -> ModelIdentity: ...

    def complete(
        self, system: str, user: str, response_schema: Mapping[str, Any]
    ) -> ProviderResult: ...


def _urllib_transport(
    payload: dict[str, Any], *, api_key: str, timeout: float, base_url: str
) -> tuple[int, str]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _decode_chat_body(body: str) -> dict[str, Any] | None:
    """Decode a chat-completion body, tolerating SSE tails some gateways add."""
    try:
        decoded, end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    trailing = body[end:].strip()
    if trailing and trailing != "data: [DONE]":
        return None
    return decoded


def _usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if not isinstance(usage, Mapping):
        return None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        int(prompt) if isinstance(prompt, int) else None,
        int(completion) if isinstance(completion, int) else None,
    )


class OpenAICompatProvider:
    """OpenAI-compatible live provider with injectable transport."""

    def __init__(
        self,
        *,
        model: str,
        family: str | None = None,
        provider: str = "openai-compatible",
        transport: Transport | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if not model:
            raise ValueError("an explicit model id is required")
        self.model_identity = ModelIdentity(
            provider=provider, model=model, family=family or model
        )
        self._transport = transport
        self._api_key = api_key if api_key is not None else resolve_api_key()
        self._base_url = base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._sleep = sleep

    def _default_transport(self, payload: dict[str, Any]) -> tuple[int, str]:
        if self._api_key is None:
            return 0, f"no API key configured for {self._base_url}"
        return _urllib_transport(
            payload,
            api_key=self._api_key,
            timeout=self._timeout,
            base_url=self._base_url,
        )

    def complete(
        self, system: str, user: str, response_schema: Mapping[str, Any]
    ) -> ProviderResult:
        payload: dict[str, Any] = {
            "model": self.model_identity.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "specialist_extraction",
                    "strict": True,
                    "schema": dict(response_schema),
                },
            },
            "temperature": 0,
        }
        attempts = 0
        last_error = "no attempt made"
        last_status: int | None = None
        started = time.perf_counter()
        while attempts < self._max_retries:
            attempts += 1
            transport = self._transport or self._default_transport
            try:
                status, body = transport(payload)
            except Exception as exc:  # timeout / network — typed, bounded retry
                last_status = None
                last_error = f"transport error: {type(exc).__name__}: {exc}"
                if attempts < self._max_retries:
                    self._sleep(self._retry_backoff * attempts)
                continue
            last_status = status
            if status == 200:
                decoded = _decode_chat_body(body)
                if decoded is None:
                    last_error = "HTTP 200 with non-JSON body"
                    if attempts < self._max_retries:
                        self._sleep(self._retry_backoff * attempts)
                    continue
                choice = (decoded.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    last_error = "HTTP 200 with empty content"
                    if attempts < self._max_retries:
                        self._sleep(self._retry_backoff * attempts)
                    continue
                prompt_tokens, completion_tokens = _usage_tokens(
                    decoded.get("usage")
                )
                return ProviderResult(
                    status=PROVIDER_OK,
                    content=content,
                    model_served=decoded.get("model", self.model_identity.model),
                    attempts=attempts,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    error=None,
                )
            last_error = f"HTTP {status}: {body[:_ERROR_SNIPPET_CHARS]}"
            if status == 429 or status >= 500:
                if attempts < self._max_retries:
                    self._sleep(self._retry_backoff * attempts)
                continue
            break  # non-retryable client error (401/403/404/…): fail fast
        return ProviderResult(
            status=self._exhaustion_status(last_status, last_error),
            attempts=attempts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=last_error,
        )

    @staticmethod
    def _exhaustion_status(last_status: int | None, last_error: str) -> str:
        """Map the final failure to a typed outcome (never improvised)."""
        if last_status == 429:
            return PROVIDER_RATE_LIMITED
        if last_status is not None and last_status >= 500:
            return PROVIDER_SERVER_ERROR
        if last_status in (401, 403):
            return PROVIDER_AUTH_ERROR
        if last_status is not None and last_status >= 400:
            return PROVIDER_CLIENT_ERROR
        if "non-JSON body" in last_error or "empty content" in last_error:
            return PROVIDER_BAD_RESPONSE
        return PROVIDER_TRANSPORT_ERROR


class ReplayProvider:
    """Deterministic recorded-response provider for tests and benchmarks.

    ``responses`` is either a mapping of :func:`replay_key` hex digests
    to recorded content strings, or a callable ``(model, user_text) ->
    content | None`` for harnesses that derive responses from the
    committed PR80B cache. A miss is ALWAYS a typed ``cache_miss``
    outcome — the lane reports it honestly instead of inventing a
    response, and no network is ever touched.
    """

    def __init__(
        self,
        responses: Mapping[str, str] | Callable[[str, str], str | None],
        *,
        model: str,
        family: str | None = None,
    ) -> None:
        self.model_identity = ModelIdentity(
            provider="replay", model=model, family=family or model
        )
        self._responses = responses

    def _lookup(self, user_text: str) -> str | None:
        if callable(self._responses):
            return self._responses(self.model_identity.model, user_text)
        key = replay_key(self.model_identity.model, user_text)
        return self._responses.get(key)

    def complete(
        self, system: str, user: str, response_schema: Mapping[str, Any]
    ) -> ProviderResult:
        content = self._lookup(user)
        if content is None:
            return ProviderResult(
                status=PROVIDER_CACHE_MISS,
                attempts=1,
                error=(
                    "replay cache miss: no recorded response for this "
                    "(model, prompt); refusing to invent one"
                ),
            )
        return ProviderResult(
            status=PROVIDER_OK,
            content=content,
            model_served=self.model_identity.model,
            attempts=1,
            latency_ms=0,
            from_cache=True,
        )
