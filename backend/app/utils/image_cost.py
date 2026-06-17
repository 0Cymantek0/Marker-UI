"""Per-provider VLM cost attribution (plan §6 — close the ``cost_usd=0`` gap).

The badge metadata always reported ``cost_usd=0`` because nothing ever measured
the spend. This module turns a call's token usage into a US-dollar estimate from
a small, override-able price table, so the per-image badge can show what an
extraction actually cost.

Prices are **directional** (USD per 1K tokens, mixed input/output) sourced from
``vlm-landscape.md`` (June 2026) — NOT re-fetched live, and they drift. They are
deliberately conservative and easy to override via :func:`set_price`. A model
not in the table falls back to a provider-type default, then a global default,
so an estimate is always produced rather than silently zero.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# (input_usd_per_1k, output_usd_per_1k). Directional, June 2026 vlm-landscape.
_DEFAULT = (0.005, 0.015)

_PROVIDER_DEFAULTS: dict[str, tuple[float, float]] = {
    "gemini": (0.000075, 0.0003),   # Flash-Lite tier
    "openai": (0.0025, 0.01),       # gpt-4o tier
    "custom_openai": (0.0025, 0.01),
    "claude": (0.003, 0.015),       # Sonnet tier
    "ollama": (0.0, 0.0),           # local, no marginal cost
    "azure": (0.0025, 0.01),
}

# Per-model overrides keyed by a lowercased substring match on the model id.
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gemini-2.0-flash": (0.000075, 0.0003),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "gemini-1.5-pro": (0.00125, 0.005),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-sonnet": (0.003, 0.015),
}


def set_price(model_substring: str, input_per_1k: float, output_per_1k: float) -> None:
    """Override / add a per-model price (USD per 1K tokens). For tuning + tests."""
    _MODEL_PRICES[model_substring.lower()] = (float(input_per_1k), float(output_per_1k))


def _rates(provider_type: str, model_id: str) -> tuple[float, float]:
    """Resolve (input, output) per-1K rates: model match -> provider -> global."""
    mid = (model_id or "").lower()
    for needle, rate in _MODEL_PRICES.items():
        if needle in mid:
            return rate
    return _PROVIDER_DEFAULTS.get(provider_type, _DEFAULT)


def estimate_cost(
    provider_type: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate USD cost for one call from its token usage. Never raises."""
    try:
        in_rate, out_rate = _rates(provider_type, model_id)
        cost = (prompt_tokens / 1000.0) * in_rate + (
            completion_tokens / 1000.0
        ) * out_rate
        return round(max(0.0, cost), 6)
    except Exception as exc:  # noqa: BLE001 — cost is best-effort metadata
        logger.debug("estimate_cost failed: %r", exc)
        return 0.0


def extract_usage(resp: Any) -> tuple[int, int]:
    """Pull (prompt_tokens, completion_tokens) from an OpenAI-shaped response.

    Handles the dict shape (httpx adapter) and the object shape (SDK / mock).
    Returns ``(0, 0)`` when usage is absent.
    """
    usage: Any = None
    if isinstance(resp, dict):
        usage = resp.get("usage")
    else:
        usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0

    def _get(key: str) -> int:
        if isinstance(usage, dict):
            val = usage.get(key)
        else:
            val = getattr(usage, key, None)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    return _get("prompt_tokens"), _get("completion_tokens")
