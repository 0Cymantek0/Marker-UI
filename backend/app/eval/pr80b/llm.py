"""OpenRouter LLM specialist adapter with record/replay caching.

The live path is exercised only when explicitly requested; routine
tests and offline benchmark reruns replay recorded envelopes from the
committed cache so nothing in CI depends on credentials or network.
The cache stores only synthetic-corpus prompts/responses - no
secrets, no personal data.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app.eval.pr80b.scoring import (
    ABSENT,
    EMITTED,
    FLAGGED_CONFLICT,
    EmittedField,
    EmittedRow,
    SystemDocOutput,
)

SYSTEM_ID_PREFIX = "llm-openrouter"
CACHE_SCHEMA_VERSION = "marker.pr80b_llm_cache.v1"
API_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Fallback chain tried in order when a free-tier model is unavailable.
DEFAULT_MODEL_CHAIN = (
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
)

SYSTEM_PROMPT = """\
You are a document data-extraction specialist. Extract invoice fields \
from the provided plain-text invoice document and reply with ONLY a \
JSON object (no prose, no code fences) shaped exactly like:

{
  "invoice_number": string or null,
  "invoice_date": string or null,
  "currency": string or null,
  "po_number": string or null,
  "total_due": string or null,
  "items": [
    {"sku": string or null, "description": string or null,
     "quantity": string or null, "unit_price": string or null,
     "amount": string or null}
  ],
  "flags": ["<field>_conflict", ...]
}

Rules:
- Quote every value as a string (including numbers).
- invoice_date: normalize to YYYY-MM-DD. Accept ISO dates, US M/D/YYYY \
(US-first for ambiguous slash dates), and English "Month D, YYYY".
- currency: normalize to exactly one of USD, EUR, GBP ($ = USD, \
US Dollars = USD, euros/EUR-symbol = EUR, pounds/GBP-symbol = GBP).
- total_due, unit_price, amount: emit the number as printed, keeping \
its original separators (for example "3,750.00" or "2.045,00").
- Use null when the document does not state a value. NEVER invent a \
value: a missing field, an "N/A", or a structurally missing column \
means null.
- If the document states contradictory values for one field (or two \
rows for the same SKU that disagree), set that field to null and add \
"<field>_conflict" to flags (for rows: "items_<sku>_conflict").
- Line items are rows starting with LINEITEM; the columns are \
sku | description | quantity | unit_price | amount. A row with a \
missing column has that member as null. Ignore trailing annotation \
columns beyond the five canonical ones.
- Do not compute, sum, or infer values that are not printed.
"""

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "po_number": {"type": ["string", "null"]},
        "total_due": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                    "quantity": {"type": ["string", "null"]},
                    "unit_price": {"type": ["string", "null"]},
                    "amount": {"type": ["string", "null"]},
                },
                "required": [
                    "sku",
                    "description",
                    "quantity",
                    "unit_price",
                    "amount",
                ],
                "additionalProperties": False,
            },
        },
        "flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "invoice_number",
        "invoice_date",
        "currency",
        "po_number",
        "total_due",
        "items",
        "flags",
    ],
    "additionalProperties": False,
}

#: Transport contract: payload dict -> (http_status, body_text).
Transport = Callable[[dict[str, Any]], tuple[int, str]]


class CacheMissError(KeyError):
    """Raised in replay mode when no recorded envelope exists."""


def _urllib_transport(
    payload: dict[str, Any], *, api_key: str, timeout: float, base_url: str = API_URL
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
    """Decode a chat-completion body, tolerating SSE tails some gateways add.

    Certain OpenAI-compatible gateways append ``data: [DONE]`` fragments
    after the JSON object; parse the first complete JSON value and accept
    only whitespace or SSE terminator junk after it.
    """
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


def resolve_api_key() -> str | None:
    """Find the OpenRouter key without assuming env-var casing."""
    for name in ("OPENROUTER_API_KEY", "openrouter_api_key", "Openrouter_Api_Key"):
        value = os.environ.get(name)
        if value:
            return value
    lowered = {k.lower(): v for k, v in os.environ.items()}
    return lowered.get("openrouter_api_key")


def cache_key(model: str, user_text: str) -> str:
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(user_text.encode("utf-8"))
    return digest.hexdigest()


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


class OpenRouterClient:
    """One model chain, one cache file, injectable transport."""

    def __init__(
        self,
        models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
        *,
        api_key: str | None = None,
        base_url: str = API_URL,
        cache_path: Path | None = None,
        mode: str = "auto",
        transport: Transport | None = None,
        timeout: float = 90.0,
        max_retries: int = 4,
        retry_backoff: float = 4.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if mode not in {"auto", "live", "replay"}:
            raise ValueError(f"unknown cache mode {mode!r}")
        self.models = tuple(models)
        self.mode = mode
        self.base_url = base_url
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._sleep = sleep
        self._transport = transport
        self._api_key = api_key if api_key is not None else resolve_api_key()
        self._cache: dict[str, dict[str, Any]] = {}
        if self.cache_path is not None and self.cache_path.is_file():
            loaded = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if loaded.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
                raise ValueError("unsupported llm cache schema version")
            self._cache = loaded.get("responses", {})
        self.model_served: str | None = None

    # -- cache -----------------------------------------------------------
    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        payload = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "responses": self._cache,
        }
        tmp_path = self.cache_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp_path.replace(self.cache_path)

    # -- transport -------------------------------------------------------
    def _default_transport(self, payload: dict[str, Any]) -> tuple[int, str]:
        if self._api_key is None:
            return 0, f"no API key configured for {self.base_url}"

        def transport(request_payload: dict[str, Any]) -> tuple[int, str]:
            return _urllib_transport(
                request_payload,
                api_key=self._api_key,
                timeout=self.timeout,
                base_url=self.base_url,
            )

        return transport(payload)

    def _call_model(self, model: str, user_text: str) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "invoice_extraction",
                    "strict": True,
                    "schema": _JSON_SCHEMA,
                },
            },
            "temperature": 0,
        }
        attempts = 0
        last_error = "no attempt made"
        while attempts < self.max_retries:
            attempts += 1
            transport = self._transport or self._default_transport
            try:
                status, body = transport(payload)
            except Exception as exc:  # timeout / network
                last_error = f"transport error: {type(exc).__name__}: {exc}"
                self._sleep(self.retry_backoff * attempts)
                continue
            if status == 200:
                decoded = _decode_chat_body(body)
                if decoded is None:
                    last_error = "HTTP 200 with non-JSON body"
                    self._sleep(self.retry_backoff * attempts)
                    continue
                choice = (decoded.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    last_error = "HTTP 200 with empty content"
                    self._sleep(self.retry_backoff * attempts)
                    continue
                return {
                    "model_requested": model,
                    "model_served": decoded.get("model", model),
                    "content_raw": content,
                    "usage": decoded.get("usage"),
                    "error": None,
                    "attempts": attempts,
                }
            last_error = f"HTTP {status}: {body[:300]}"
            if status == 429 or status >= 500:
                self._sleep(self.retry_backoff * attempts)
                continue
            break  # non-retryable client error
        return {
            "model_requested": model,
            "model_served": None,
            "content_raw": None,
            "usage": None,
            "error": last_error,
            "attempts": attempts,
        }

    # -- public API --------------------------------------------------------
    def extract(self, doc_text: str) -> dict[str, Any]:
        """Get one extraction envelope for one document text.

        Tries the model chain in order; the first model that produces
        content wins and is pinned for the rest of the run so every
        document in one benchmark run is answered by the same model.
        """
        chain = (self.model_served,) if self.model_served else self.models
        for index, model in enumerate(chain):
            key = cache_key(model, doc_text)
            if self.mode != "live" and key in self._cache:
                envelope = dict(self._cache[key])
                envelope["from_cache"] = True
                if self.model_served is None:
                    self.model_served = model
                return envelope
            if self.mode == "replay":
                continue
            envelope = self._call_model(model, doc_text)
            if envelope["error"] is None or index == len(chain) - 1:
                envelope["from_cache"] = False
                self._cache[key] = dict(envelope)
                self._save_cache()
                if envelope["error"] is None and self.model_served is None:
                    self.model_served = model
                return envelope
        raise CacheMissError(
            "replay mode: no recorded envelope for this document under any "
            "model in the chain"
        )


def parse_content(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Parse the recorded raw content into the extraction mapping."""
    raw = envelope.get("content_raw")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _flagged_fields(parsed: dict[str, Any]) -> set[str]:
    flags = parsed.get("flags")
    if not isinstance(flags, list):
        return set()
    matched: set[str] = set()
    for flag in flags:
        if not isinstance(flag, str) or not flag.endswith("_conflict"):
            continue
        name = flag[: -len("_conflict")]
        if name in {"invoice_number", "invoice_date", "currency", "po_number", "total_due"}:
            matched.add(name)
    return matched


def envelope_to_output(envelope: dict[str, Any], doc_id: str) -> SystemDocOutput:
    """Map one recorded/live envelope onto the scoring surface."""
    model = envelope.get("model_served") or envelope.get("model_requested") or "unknown"
    system_id = f"{SYSTEM_ID_PREFIX}:{model}"
    parsed = parse_content(envelope)
    if parsed is None:
        return SystemDocOutput(
            system_id=system_id,
            doc_id=doc_id,
            fields={},
            rows=(),
            error=envelope.get("error") or "unparseable model content",
            raw={"envelope": {k: v for k, v in envelope.items() if k != "content_raw"}},
        )
    flagged = _flagged_fields(parsed)
    row_flags: set[str] = set()
    raw_flags = parsed.get("flags")
    if isinstance(raw_flags, list):
        for flag in raw_flags:
            if isinstance(flag, str) and flag.startswith("items_") and flag.endswith("_conflict"):
                row_flags.add(flag[len("items_") : -len("_conflict")])

    def scalar(name: str) -> EmittedField:
        value = parsed.get(name)
        if value is None:
            if name in flagged:
                return EmittedField(status=FLAGGED_CONFLICT, self_flagged=True)
            return EmittedField(status=ABSENT)
        return EmittedField(status=EMITTED, value=str(value))

    fields = {
        name: scalar(name)
        for name in ("invoice_number", "invoice_date", "currency", "po_number", "total_due")
    }
    rows: list[EmittedRow] = []
    items = parsed.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            sku = item.get("sku")
            row_flagged = sku is not None and str(sku) in row_flags

            def member(name: str) -> EmittedField:
                value = item.get(name)
                if value is None:
                    return EmittedField(
                        status=FLAGGED_CONFLICT if row_flagged else ABSENT,
                        self_flagged=row_flagged,
                    )
                return EmittedField(status=EMITTED, value=str(value))

            rows.append(
                EmittedRow(
                    sku=str(sku) if sku is not None else None,
                    fields={
                        name: member(name)
                        for name in ("description", "quantity", "unit_price", "amount")
                    },
                    status=FLAGGED_CONFLICT if row_flagged else EMITTED,
                    self_flagged=row_flagged,
                )
            )
    return SystemDocOutput(
        system_id=system_id,
        doc_id=doc_id,
        fields=fields,
        rows=tuple(rows),
        run_status="model_answered",
        invariant_findings=None,
        raw={"envelope": {k: v for k, v in envelope.items() if k != "content_raw"}},
    )
