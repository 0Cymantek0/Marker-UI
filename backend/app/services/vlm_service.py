"""VLMService — provider-agnostic Vision-Language-Model client for image
understanding + extraction (Phase 1, plan §6.5 + §6.10).

Design highlights:

* **Provider abstraction** — built on top of the existing ``LLMProvider`` /
  ``ModelConfig`` schemas. Per-provider OpenAI-compatible ``base_url`` mapping
  (Gemini / OpenAI / Anthropic / Ollama / Azure). Vertex is out of scope for
  Phase 1 and raises ``NotImplementedError`` at construction time.
* **Sync API** — ``classify`` and ``extract`` are blocking calls. They run
  inside TaskManager worker threads so blocking is fine and the call sites
  stay simple.
* **Never raises** — public methods always return a result object. Provider
  exceptions / network errors / malformed JSON are caught and surfaced via
  ``ClassificationResult`` / ``ExtractionResult`` fields. This keeps the
  document-processing pipeline resilient: one bad image never aborts a
  500-page conversion.
* **JSON-mode discipline + retry-once** — every VLM call requests
  ``response_format={"type": "json_object"}`` when supported. On malformed
  JSON or (for classify) an unknown ``image_type`` enum value, we retry once
  with a stricter prompt suffix. Second failure degrades gracefully.
* **Mermaid validator** — module-level ``validate_mermaid()`` does a cheap
  Python-only syntactic check (~90 % catch rate per decision #3). Diagram
  payloads that fail validation on both attempts are demoted to a
  ``DescriptionPayload`` shape so the document always gets *something*
  useful in place of the image.
* **Injectable HTTP client** — ``http_client`` is a constructor param so
  unit tests pass a ``MagicMock`` and assert on ``client.chat.completions.
  create``. The default (production) client is an httpx-backed adapter that
  speaks the OpenAI chat-completions shape.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from app.crypto import decrypt_value
from app.models.image_understanding import (
    ClassificationResult,
    ExtractionResult,
    ImageType,
)
from app.models.schemas import LLMProvider
from app.prompts.image_classify import build_classify_prompt
from app.prompts.image_extract import build_extract_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-provider OpenAI-compatible base URLs
# ---------------------------------------------------------------------------

_PROVIDER_BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai": "https://api.openai.com/v1",
    "custom_openai": "https://api.openai.com/v1",
    "claude": "https://api.anthropic.com/v1/",
    "ollama": "http://localhost:11434/v1/",
}

# ImageType values that carry a Mermaid field and therefore require
# post-extraction validation + demote-on-failure behaviour.
_DIAGRAM_TYPES: frozenset[ImageType] = frozenset(
    {
        ImageType.diagram_flow,
        ImageType.diagram_sequence,
        ImageType.diagram_state,
        ImageType.diagram_class,
        ImageType.diagram_architecture,
    }
)

# Stricter prompt suffix appended on a retry when the previous response was
# not parseable JSON (or not a known enum value).
_RETRY_SUFFIX = (
    " IMPORTANT: Your previous response was not valid JSON. "
    "Output ONLY a JSON object, nothing else."
)


# ---------------------------------------------------------------------------
# Default http_client — a tiny OpenAI-compatible adapter over httpx
# ---------------------------------------------------------------------------

class _Completions:
    """Shim that exposes ``create()`` matching OpenAI's chat.completions.create."""

    def __init__(self, parent: "_OpenAICompatClient") -> None:
        self._parent = parent

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._parent._post_chat(
            model=model,
            messages=messages,
            response_format=response_format,
            extra=kwargs,
        )


class _Chat:
    def __init__(self, parent: "_OpenAICompatClient") -> None:
        self.completions = _Completions(parent)


class _OpenAICompatClient:
    """Minimal synchronous OpenAI-compatible client built on httpx.

    Exposes the same ``client.chat.completions.create(...)`` surface as the
    official ``openai.OpenAI`` SDK, so the rest of the service (and the unit
    tests, which inject a ``MagicMock``) can treat all providers uniformly.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
    ) -> None:
        # Import inside __init__ so tests that inject a mock never require httpx.
        import httpx

        self._http = httpx.Client(timeout=timeout)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.chat = _Chat(self)

    def _post_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None,
        extra: dict[str, Any],
    ) -> Any:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            body["response_format"] = response_format
        # Pass through any other OpenAI-compatible kwargs (max_tokens etc.)
        body.update(extra)
        resp = self._http.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Mermaid validator (module-level helper)
# ---------------------------------------------------------------------------

_MERMAID_KEYWORDS: tuple[str, ...] = (
    "graph ",
    "graph\n",
    "graph TD",
    "graph LR",
    "graph TB",
    "graph BT",
    "graph RL",
    "flowchart",
    "sequenceDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "classDiagram",
    "erDiagram",
    "gitGraph",
    "pie",
    "journey",
)

_MERMAID_ARROWS: tuple[str, ...] = (
    "-->",
    "->>",
    "-->>",
    "->",
    "---",
    "-.-",
    "==>",
)


def validate_mermaid(mermaid: str) -> bool:
    """Cheap Python-only syntactic check for Mermaid source.

    Per decision #3 the goal is ~90 % coverage — catch obvious garbage
    without false-flagging valid diagrams. Real rendering validation is
    out of scope for v1.

    Checks (all must pass):
        1. ``20 <= len(mermaid) <= 5000`` — sane length bounds.
        2. Starts with one of the known Mermaid keywords.
        3. ``()``, ``[]``, ``{}`` brackets balanced across the whole string.
        4. Contains at least one Mermaid arrow token.

    Args:
        mermaid: Mermaid source string to check.

    Returns:
        ``True`` only if every check passes. ``False`` otherwise (a miss is
        logged at ``DEBUG`` level to aid debugging without being noisy).
    """
    if not isinstance(mermaid, str):
        logger.debug("validate_mermaid: non-string input rejected")
        return False

    if len(mermaid) < 20 or len(mermaid) > 5000:
        logger.debug("validate_mermaid: length %d out of bounds", len(mermaid))
        return False

    # Keyword prefix check — match ``graph`` family with whitespace or arrow
    # direction, plus the standalone diagram keywords.
    if not any(mermaid.startswith(kw) for kw in _MERMAID_KEYWORDS):
        logger.debug("validate_mermaid: unknown keyword prefix")
        return False

    # Bracket balance.
    for open_ch, close_ch in (("(", ")"), ("[", "]"), ("{", "}")):
        if mermaid.count(open_ch) != mermaid.count(close_ch):
            logger.debug(
                "validate_mermaid: unbalanced %r / %r", open_ch, close_ch
            )
            return False

    # Arrow presence.
    if not any(arrow in mermaid for arrow in _MERMAID_ARROWS):
        logger.debug("validate_mermaid: no arrow token found")
        return False

    return True


# ---------------------------------------------------------------------------
# VLMService
# ---------------------------------------------------------------------------

class VLMService:
    """Provider-agnostic VLM client for image classification + extraction.

    Construction:

        * ``provider`` — if ``None``, the service attempts to resolve the
          currently-active ``LLMProvider`` from the settings table. In
          production this runs inside a TaskManager worker thread (no
          running event loop), so we use a small sync helper. In tests the
          caller always passes an explicit provider.
        * ``model_id`` — if ``None``, falls back to the ``vlm_model``
          setting; if that is also unset, picks the first
          ``vision_capable`` model on the provider.
        * ``http_client`` — if ``None``, a default ``_OpenAICompatClient``
          is built using the provider's decrypted API key + provider-
          specific ``base_url``. Tests inject a ``MagicMock``.

    Public methods (both synchronous, both never raise):

        * :meth:`classify` — one VLM call → ``ClassificationResult``.
        * :meth:`extract` — one VLM call (or zero for decorative) →
          ``ExtractionResult``. Diagram types validate the Mermaid field
          and demote to a description on repeated failure.
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        model_id: str | None = None,
        http_client: Any = None,
    ) -> None:
        # ---- Provider resolution ---------------------------------------
        if provider is None:
            provider = self._resolve_provider_from_settings()
        if provider is None:
            raise RuntimeError(
                "VLMService requires an LLMProvider — none configured."
            )
        if provider.type == "vertex":
            # Vertex VLM is out of scope for Phase 1 (decision documented in
            # the plan). Fail fast at construction rather than silently
            # returning degraded results.
            raise NotImplementedError(
                "Vertex VLM is not supported in Phase 1. "
                "Use gemini / openai / claude / ollama / azure instead."
            )

        # ---- Model resolution -----------------------------------------
        self._provider: LLMProvider = provider
        self._model_id: str = model_id or self._resolve_model_id(provider)

        # ---- HTTP client ----------------------------------------------
        if http_client is None:
            http_client = self._build_default_client(provider)
        self._client: Any = http_client

        logger.debug(
            "VLMService initialised: provider=%s type=%s model=%s",
            provider.id,
            provider.type,
            self._model_id,
        )

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def classify(
        self,
        image_bytes: bytes,
        mime_type: str,
        heading_chain: str,
        surrounding_paragraphs: str,
    ) -> ClassificationResult:
        """Classify an image into one of the 17 ``ImageType`` values.

        Makes one VLM call (or two on retry). Never raises — on any failure
        returns ``ClassificationResult(image_type=ImageType.other,
        confidence=0.0, rationale="...")``.
        """
        system_prompt, user_prompt = build_classify_prompt(
            heading_chain, surrounding_paragraphs
        )
        data_url = _encode_data_url(image_bytes, mime_type)

        try:
            return self._classify_with_retry(
                system_prompt, user_prompt, data_url
            )
        except Exception as exc:  # noqa: BLE001 — provider errors caught here
            logger.warning(
                "classify(): provider call failed; returning fallback. error=%r",
                exc,
            )
            return ClassificationResult(
                image_type=ImageType.other,
                confidence=0.0,
                rationale=f"Classification failed; defaulting to other ({exc})",
            )

    def extract(
        self,
        image_bytes: bytes,
        mime_type: str,
        image_type: ImageType,
        heading_chain: str,
        surrounding_paragraphs: str,
    ) -> ExtractionResult:
        """Extract structured data from an image of a known ``ImageType``.

        Decorative images short-circuit (no VLM call). Diagram types
        validate the ``mermaid`` field via :func:`validate_mermaid` and
        demote to a :class:`DescriptionPayload` shape on repeated failure.
        Never raises — on any failure the error string lands in
        ``ExtractionResult.error``.
        """
        # Short-circuit: decorative images carry no information.
        if image_type == ImageType.decorative:
            return ExtractionResult(
                image_type=ImageType.decorative,
                payload={},
                raw_response="",
                confidence=1.0,
                error=None,
                context_window=None,
            )

        system_prompt, user_prompt = build_extract_prompt(
            image_type, heading_chain, surrounding_paragraphs
        )
        data_url = _encode_data_url(image_bytes, mime_type)

        try:
            return self._extract_with_retry(
                image_type, system_prompt, user_prompt, data_url
            )
        except Exception as exc:  # noqa: BLE001 — provider errors caught here
            logger.warning(
                "extract(): provider call failed for %s; returning error result. error=%r",
                image_type,
                exc,
            )
            return ExtractionResult(
                image_type=image_type,
                payload={},
                raw_response="",
                confidence=0.0,
                error=str(exc),
            )

    # -----------------------------------------------------------------
    # Internals — classify
    # -----------------------------------------------------------------

    def _classify_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        data_url: str,
    ) -> ClassificationResult:
        """Call the VLM up to twice. Never raises."""
        for attempt in (1, 2):
            suffix = _RETRY_SUFFIX if attempt == 2 else ""
            raw = self._call_vlm(
                system_prompt, user_prompt + suffix, data_url
            )
            parsed = _safe_json_load(raw)
            if parsed is None:
                logger.debug(
                    "classify attempt %d: JSON parse failed (raw=%r)",
                    attempt,
                    raw[:200],
                )
                continue

            image_type_str = parsed.get("image_type")
            try:
                image_type = ImageType(image_type_str)
            except ValueError:
                logger.debug(
                    "classify attempt %d: %r is not a valid ImageType",
                    attempt,
                    image_type_str,
                )
                continue

            return ClassificationResult(
                image_type=image_type,
                confidence=float(parsed.get("confidence", 0.0) or 0.0),
                rationale=str(parsed.get("rationale", "") or ""),
            )

        # Both attempts failed.
        return ClassificationResult(
            image_type=ImageType.other,
            confidence=0.0,
            rationale="Classification failed; defaulting to other",
        )

    # -----------------------------------------------------------------
    # Internals — extract
    # -----------------------------------------------------------------

    def _extract_with_retry(
        self,
        image_type: ImageType,
        system_prompt: str,
        user_prompt: str,
        data_url: str,
    ) -> ExtractionResult:
        """Call the VLM up to twice. Handles diagram Mermaid validation + demote."""
        last_raw = ""
        for attempt in (1, 2):
            suffix = _RETRY_SUFFIX if attempt == 2 else ""
            raw = self._call_vlm(
                system_prompt, user_prompt + suffix, data_url
            )
            last_raw = raw
            parsed = _safe_json_load(raw)
            if parsed is None:
                logger.debug(
                    "extract attempt %d (%s): JSON parse failed",
                    attempt,
                    image_type,
                )
                continue

            # Diagram path: validate Mermaid before returning.
            if image_type in _DIAGRAM_TYPES:
                mermaid = str(parsed.get("mermaid", "") or "")
                if not validate_mermaid(mermaid):
                    logger.debug(
                        "extract attempt %d (%s): Mermaid validation failed",
                        attempt,
                        image_type,
                    )
                    continue
                # Mermaid is valid — return as-is.
                return ExtractionResult(
                    image_type=image_type,
                    payload=parsed,
                    raw_response=raw,
                    confidence=0.9,
                    error=None,
                )

            # Non-diagram path — return parsed JSON as-is.
            return ExtractionResult(
                image_type=image_type,
                payload=parsed,
                raw_response=raw,
                confidence=0.9,
                error=None,
            )

        # Both attempts failed. Diagrams demote to a description payload;
        # everything else surfaces the JSON-parse error.
        if image_type in _DIAGRAM_TYPES and last_raw:
            logger.warning(
                "extract(%s): Mermaid validation failed twice — demoting to DescriptionPayload",
                image_type,
            )
            demoted = _demote_to_description(last_raw)
            return ExtractionResult(
                image_type=image_type,
                payload=demoted,
                raw_response=last_raw,
                confidence=0.5,
                error=None,
            )

        return ExtractionResult(
            image_type=image_type,
            payload={},
            raw_response=last_raw,
            confidence=0.0,
            error="JSON parse failed after retry",
        )

    # -----------------------------------------------------------------
    # Internals — VLM call shape
    # -----------------------------------------------------------------

    def _call_vlm(
        self,
        system_prompt: str,
        user_prompt: str,
        data_url: str,
    ) -> str:
        """Make one chat-completion call. Returns the raw content string.

        Uses the OpenAI-compatible ``chat.completions.create`` shape so the
        same code works for OpenAI, Gemini-OAI, Anthropic-OAI, Ollama, and
        Azure. Raises on provider error — caller catches.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ]
        resp = self._client.chat.completions.create(
            model=self._model_id,
            messages=messages,
            response_format={"type": "json_object"},
        )
        # OpenAI-shaped response: resp.choices[0].message.content
        # ``resp`` may be a dict (httpx-backed adapter) or an object (openai SDK / mock).
        return _extract_content(resp)

    # -----------------------------------------------------------------
    # Internals — provider / model / client resolution
    # -----------------------------------------------------------------

    @staticmethod
    def _resolve_provider_from_settings() -> LLMProvider | None:
        """Best-effort lookup of the active LLMProvider from the settings DB.

        Used only when ``provider=None`` is passed to ``__init__``. Runs from
        a sync worker thread (no running event loop). Returns ``None`` if
        anything goes wrong — the caller will then raise a clear error.
        """
        try:
            import asyncio

            from app.database import async_session_factory
            from app.routes.settings import get_active_llm, get_llm_providers

            async def _load() -> LLMProvider | None:
                async with async_session_factory() as session:
                    active = await get_active_llm(session)
                    providers = await get_llm_providers(session)
                    for p in providers:
                        if p.id == active.provider_id:
                            # ``get_llm_providers`` returns masked keys. For
                            # production callers the calling route should
                            # instead pass a freshly-decrypted provider; this
                            # path is a convenience fallback only.
                            return p
                    return None

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                # We're inside an event loop — can't ``asyncio.run`` here.
                logger.warning(
                    "VLMService: cannot resolve provider from settings "
                    "inside a running event loop — pass provider explicitly."
                )
                return None

            return asyncio.run(_load())
        except Exception as exc:  # noqa: BLE001 — best-effort fallback
            logger.warning(
                "VLMService: failed to resolve provider from settings: %r",
                exc,
            )
            return None

    @staticmethod
    def _resolve_model_id(provider: LLMProvider) -> str:
        """Pick the model to use. Order: ``vlm_model`` setting → first
        ``vision_capable`` model on the provider → first model on the provider.
        """
        # 1. vlm_model setting (best-effort, sync DB read skipped in tests).
        try:
            vlm_model = _read_setting_sync("vlm_model")
            if vlm_model:
                return vlm_model
        except Exception:  # noqa: BLE001 — setting may be absent
            pass

        # 2. First vision_capable model.
        for m in provider.models:
            if m.vision_capable:
                return m.model_id

        # 3. First model on the provider (last resort).
        if provider.models:
            return provider.models[0].model_id

        raise RuntimeError(
            f"VLMService: provider {provider.id!r} has no models configured."
        )

    @staticmethod
    def _build_default_client(provider: LLMProvider) -> _OpenAICompatClient:
        """Build the production httpx-backed OpenAI-compat client."""
        base_url = provider.base_url or _PROVIDER_BASE_URLS.get(
            provider.type, _PROVIDER_BASE_URLS["openai"]
        )
        if provider.type == "azure":
            # Azure uses deployment-prefixed URLs; caller must supply base_url.
            if not provider.base_url:
                raise RuntimeError(
                    "Azure VLM provider requires base_url to be set "
                    "(e.g. https://<resource>.openai.azure.com)"
                )
            base_url = provider.base_url.rstrip("/")
            base_url = f"{base_url}/openai/deployments/{{deployment}}"

        api_key = provider.api_key or ""
        # Decrypt if it looks like a Fernet token (no-op on plaintext).
        if api_key:
            try:
                api_key = decrypt_value(api_key)
            except Exception:  # noqa: BLE001 — already plaintext
                pass

        return _OpenAICompatClient(base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _encode_data_url(image_bytes: bytes, mime_type: str) -> str:
    """Wrap raw image bytes as a base64 data URL."""
    encoded = base64.b64encode(image_bytes).decode()
    return f"data:{mime_type};base64,{encoded}"


def _safe_json_load(raw: str) -> dict[str, Any] | None:
    """Parse ``raw`` as JSON; return ``None`` on any failure."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _extract_content(resp: Any) -> str:
    """Pull the assistant content string from an OpenAI-shaped response.

    Handles both the dict shape returned by httpx and the object shape
    returned by the OpenAI SDK / ``MagicMock``.
    """
    # Dict path (httpx adapter).
    if isinstance(resp, dict):
        try:
            choices = resp.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            return str(content) if content is not None else ""
        except (AttributeError, IndexError, KeyError):
            return ""

    # Object path (openai SDK / MagicMock).
    try:
        return str(resp.choices[0].message.content)
    except (AttributeError, IndexError, TypeError):
        return ""


def _demote_to_description(raw_response: str) -> dict[str, Any]:
    """Build a DescriptionPayload-shaped dict when a diagram fails to convert.

    Per plan §6.10 the demoted payload preserves the raw Mermaid snippet so
    a human reader can still see what the VLM tried to produce.
    """
    snippet = raw_response
    if len(snippet) > 800:
        snippet = snippet[:800] + "..."
    return {
        "alt_text": "Diagram could not be converted to Mermaid",
        "details": [f"Raw VLM response: {snippet}"],
    }


def _read_setting_sync(key: str) -> str | None:
    """Read a single Setting value synchronously.

    Used by ``_resolve_model_id`` to look up the ``vlm_model`` override. Runs
    in a worker thread (no event loop). Returns ``None`` if the setting is
    absent or the lookup fails for any reason.
    """
    try:
        import asyncio

        from app.database import async_session_factory
        from app.models.settings import Setting
        from sqlalchemy import select

        async def _load() -> str | None:
            async with async_session_factory() as session:
                row = (
                    await session.execute(
                        select(Setting).where(Setting.key == key)
                    )
                ).scalar_one_or_none()
                return row.value if row else None

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            return None

        return asyncio.run(_load())
    except Exception:  # noqa: BLE001 — best-effort
        return None
