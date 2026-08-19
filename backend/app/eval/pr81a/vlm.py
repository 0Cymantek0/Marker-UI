"""PR81A hosted VLM lane: multimodal answer/rerank client with replay cache.

Mirrors the PR80B hosted-LLM conventions exactly — injectable transport,
``auto``/``live``/``replay`` modes, a versioned JSON replay cache that
never contains API-key material, a model chain with first-success
pinning, temperature 0, strict JSON responses — extended in one
dimension: user content is a multimodal part list, so page images ride
along as ``data:image/png;base64`` URLs and the cache key binds the
sha256 of every content part, not just text.

The VLM is a *measured candidate*, never an authority: every parsed
output is downstream-scored against committed gold, and the replay
cache exists precisely so routine verification needs no key, no network,
and no GPU.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

SYSTEM_ID_PREFIX = "vlm-openrouter"
CACHE_SCHEMA_VERSION = "marker.pr81a_vlm_cache.v1"
#: base (not endpoint) URL: the transport appends ``/chat/completions``
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: free-tier multimodal chain, tried in order; first success is pinned
#: (ids verified against the live OpenRouter catalog at benchmark time)
DEFAULT_MODEL_CHAIN: tuple[str, ...] = (
    "google/gemma-4-26b-a4b-it:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-31b-it:free",
)

ANSWER_SYSTEM_PROMPT = (
    "You answer questions strictly from the evidence you are given "
    "(a rendered document page image, a text transcript, or both). "
    "Reply with compact JSON only: {\"answer\": \"...\"}. The answer must "
    "be the shortest exact value that answers the question (a number, a "
    "label, a date, or a short name as printed in the evidence). "
    "If the evidence does not contain the answer, reply "
    "{\"answer\": null}. Never guess and never explain."
)

RERANK_SYSTEM_PROMPT = (
    "You are shown one contact sheet image containing several labeled "
    "document page thumbnails (A, B, C, ...). Score how well each page "
    "satisfies the information need in the question, from 0 (useless) to "
    "10 (exactly on point). Judge by what is visible on each page. "
    "Reply with compact JSON only: {\"scores\": {\"A\": 0-10, ...}} for "
    "every label shown. No explanation."
)

Transport = Callable[[dict], tuple[int, str]]


class CacheMissError(KeyError):
    """Replay mode miss on every model in the chain."""


class VlmError(RuntimeError):
    """Client-level failure; carries an honest error, never a guess."""


@dataclass(frozen=True)
class VlmEnvelope:
    content_raw: str | None
    error: str | None
    model_requested: str
    model_served: str | None
    attempts: int
    usage: dict
    from_cache: bool = False


def resolve_api_key() -> str | None:
    for name in ("OPENROUTER_API_KEY", "openrouter_api_key", "Openrouter_Api_Key"):
        value = os.environ.get(name)
        if value:
            return value
    for name, value in os.environ.items():
        if name.lower() == "openrouter_api_key" and value:
            return value
    return None


def cache_key(model: str, system: str, parts: Sequence[Mapping]) -> str:
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(system.encode("utf-8"))
    digest.update(b"\0")
    for part in parts:
        digest.update(json.dumps(part, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _image_part(png_bytes: bytes) -> dict:
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def _text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def _urllib_transport(payload: dict, *, api_key: str, timeout: float, base_url: str) -> tuple[int, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
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


def _decode_chat_body(body: str) -> dict | None:
    body = body.strip()
    # tolerate SSE terminator junk after a JSON body (PR80B hardening)
    if body.endswith("data: [DONE]"):
        body = body[: body.rfind("data: [DONE]")].strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    # some gateways answer non-streamed requests with an SSE chunk
    # stream anyway (observed on this gateway's kr/* routes): accumulate
    # content deltas into one synthetic completion
    if body.startswith("data:"):
        content_parts: list[str] = []
        model: str | None = None
        usage: dict = {}
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            model = model or chunk.get("model")
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                message = choice.get("message") or {}
                if message.get("content"):
                    content_parts.append(message["content"])
        if content_parts:
            return {
                "model": model,
                "choices": [
                    {"message": {"role": "assistant", "content": "".join(content_parts)}}
                ],
                "usage": usage,
            }
    return None


class VlmClient:
    """OpenAI-compatible multimodal chat client with record/replay."""

    def __init__(
        self,
        models: Sequence[str] = DEFAULT_MODEL_CHAIN,
        *,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
        cache_path: Path | None = None,
        mode: str = "auto",
        transport: Transport | None = None,
        timeout: float = 120.0,
        max_retries: int = 4,
        retry_backoff: float = 4.0,
        sleep: Callable[[float], None] = time.sleep,
        inter_call_delay: float = 1.5,
    ) -> None:
        if mode not in {"auto", "live", "replay"}:
            raise ValueError(f"invalid mode: {mode!r}")
        if not models:
            raise ValueError("model chain must not be empty")
        self.models = tuple(models)
        self.api_key = api_key
        self.base_url = base_url
        self.cache_path = Path(cache_path) if cache_path else None
        self.mode = mode
        self._transport = transport
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._sleep = sleep
        self.inter_call_delay = inter_call_delay
        self.model_served: str | None = None
        # the chain model that first succeeded; later requests keep using
        # it so cache keys stay stable while ``model_served`` records the
        # identity the gateway actually reported
        self._pinned_request_model: str | None = None
        self.usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
        self.calls = {"live": 0, "cache": 0}

    # -- cache ------------------------------------------------------------

    def _load_cache(self) -> dict:
        if self.cache_path is None or not self.cache_path.is_file():
            return {
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "gateway_origin": self._origin_description(),
                "model_chain": list(self.models),
                "responses": {},
            }
        data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        if data.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
            raise VlmError("unsupported vlm cache schema version")
        return data

    def _origin_description(self) -> str:
        host = urlparse(self.base_url).hostname or self.base_url
        return f"OpenAI-compatible gateway: {host}"

    def _write_cache(self, cache: dict) -> None:
        if self.cache_path is None:
            return
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(self.cache_path)

    # -- transport ----------------------------------------------------------

    def _send(self, payload: dict) -> VlmEnvelope:
        api_key = self.api_key or resolve_api_key()
        if self._transport is None:
            if not api_key:
                return VlmEnvelope(
                    content_raw=None,
                    error="no API key configured",
                    model_requested=payload["model"],
                    model_served=None,
                    attempts=0,
                    usage={},
                )
            transport = lambda p: _urllib_transport(
                p, api_key=api_key, timeout=self.timeout, base_url=self.base_url
            )
        else:
            transport = self._transport
        attempts = 0
        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            attempts = attempt
            try:
                status, body = transport(payload)
            except Exception as exc:
                last_error = f"transport exception: {exc}"
                self._sleep(self.retry_backoff * attempt)
                continue
            if status == 200:
                decoded = _decode_chat_body(body)
                if decoded is None:
                    last_error = "status 200 with non-JSON body"
                    self._sleep(self.retry_backoff * attempt)
                    continue
                choices = decoded.get("choices") or []
                if not choices or not (choices[0].get("message") or {}).get("content"):
                    last_error = "status 200 with empty content"
                    self._sleep(self.retry_backoff * attempt)
                    continue
                usage = decoded.get("usage") or {}
                self.usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                self.usage_totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                return VlmEnvelope(
                    content_raw=choices[0]["message"]["content"],
                    error=None,
                    model_requested=payload["model"],
                    model_served=decoded.get("model"),
                    attempts=attempts,
                    usage=usage,
                )
            if status in (429,) or status >= 500:
                last_error = f"status {status}"
                self._sleep(self.retry_backoff * attempt)
                continue
            if status == 404:
                # free-tier providers surface "Not Found" when routing
                # fails; return immediately so the chain falls through
                # to the next model without burning retries
                return VlmEnvelope(
                    content_raw=None,
                    error=f"status 404 (model routing unavailable): {body[:120]}",
                    model_requested=payload["model"],
                    model_served=None,
                    attempts=attempts,
                    usage={},
                )
            # other 4xx: fail fast, retrying will not help
            return VlmEnvelope(
                content_raw=None,
                error=f"status {status}: {body[:200]}",
                model_requested=payload["model"],
                model_served=None,
                attempts=attempts,
                usage={},
            )
        return VlmEnvelope(
            content_raw=None,
            error=f"retries exhausted: {last_error}",
            model_requested=payload["model"],
            model_served=None,
            attempts=attempts,
            usage={},
        )

    # -- public API ---------------------------------------------------------

    def chat(self, *, system: str, parts: Sequence[Mapping]) -> tuple[VlmEnvelope, dict | None]:
        """One multimodal chat call against the (pinned) first model.

        Returns ``(envelope, parsed_json)``; ``parsed_json`` is ``None``
        whenever the response is absent or not strict JSON.
        """
        cache = self._load_cache()
        models = [self._pinned_request_model] if self._pinned_request_model else list(self.models)
        last_envelope: VlmEnvelope | None = None
        for model in models:
            key = cache_key(model, system, parts)
            if self.mode in {"auto", "replay"}:
                cached = cache.get("responses", {}).get(key)
                if cached is not None:
                    envelope = VlmEnvelope(
                        content_raw=cached.get("content_raw"),
                        error=cached.get("error"),
                        model_requested=cached.get("model_requested", model),
                        model_served=cached.get("model_served"),
                        attempts=cached.get("attempts", 1),
                        usage=cached.get("usage", {}),
                        from_cache=True,
                    )
                    self.calls["cache"] += 1
                    return envelope, _parse_json(envelope.content_raw)
            if self.mode == "replay":
                continue
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": list(parts)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 512,
            }
            envelope = self._send(payload)
            self.calls["live"] += 1
            last_envelope = envelope
            if envelope.error is None:
                self.model_served = envelope.model_served or model
                self._pinned_request_model = model
                cache.setdefault("responses", {})[key] = {
                    "attempts": envelope.attempts,
                    "content_raw": envelope.content_raw,
                    "error": envelope.error,
                    "model_requested": envelope.model_requested,
                    "model_served": envelope.model_served,
                    "usage": envelope.usage,
                }
                cache["model_chain"] = list(self.models)
                cache["gateway_origin"] = self._origin_description()
                self._write_cache(cache)
                # free-tier pacing: keep the provider side of the chain
                # healthy without the caller knowing the schedule
                self._sleep(self.inter_call_delay)
                return envelope, _parse_json(envelope.content_raw)
        if last_envelope is not None:
            return last_envelope, None
        raise CacheMissError("no cached response for any model in the chain")

    def answer(self, question: str, *, page_png: bytes | None, page_text: str | None) -> tuple[VlmEnvelope, dict | None]:
        """Answer one question from the given evidence (image and/or text)."""
        parts: list[dict] = []
        prompt = f"Question: {question}"
        if page_text:
            prompt += f"\n\nText transcript of the page:\n{page_text}"
        parts.append(_text_part(prompt))
        if page_png:
            parts.append(_image_part(page_png))
        return self.chat(system=ANSWER_SYSTEM_PROMPT, parts=parts)

    def rerank(self, query: str, montage_png: bytes, labels: Sequence[str]) -> tuple[VlmEnvelope, dict | None]:
        """Score labeled thumbnails on one contact sheet."""
        parts = [
            _text_part(f"Question: {query}\nLabels shown: {', '.join(labels)}"),
            _image_part(montage_png),
        ]
        return self.chat(system=RERANK_SYSTEM_PROMPT, parts=parts)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _parse_json(content: str | None) -> dict | None:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
