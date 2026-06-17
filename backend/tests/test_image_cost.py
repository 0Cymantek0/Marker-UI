"""Unit tests for VLM cost attribution (plan §6)."""

from __future__ import annotations

from app.utils.image_cost import estimate_cost, extract_usage, set_price


def test_estimate_cost_uses_model_price():
    # gpt-4o: 0.0025 in / 0.01 out per 1k.
    cost = estimate_cost("openai", "gpt-4o", prompt_tokens=1000, completion_tokens=1000)
    assert abs(cost - (0.0025 + 0.01)) < 1e-9


def test_estimate_cost_model_substring_match():
    cost = estimate_cost("openai", "gpt-4o-mini-2024", 1000, 1000)
    # mini rates: 0.00015 + 0.0006
    assert abs(cost - 0.00075) < 1e-9


def test_estimate_cost_provider_default_when_model_unknown():
    cost = estimate_cost("gemini", "some-future-model", 1000, 0)
    assert abs(cost - 0.000075) < 1e-9


def test_estimate_cost_ollama_is_free():
    assert estimate_cost("ollama", "llava", 100000, 100000) == 0.0


def test_set_price_override():
    set_price("mytestmodel", 1.0, 2.0)
    cost = estimate_cost("openai", "mytestmodel", 1000, 1000)
    assert abs(cost - 3.0) < 1e-9


def test_extract_usage_dict_shape():
    resp = {"usage": {"prompt_tokens": 120, "completion_tokens": 40}}
    assert extract_usage(resp) == (120, 40)


def test_extract_usage_object_shape():
    class _U:
        prompt_tokens = 7
        completion_tokens = 3

    class _R:
        usage = _U()

    assert extract_usage(_R()) == (7, 3)


def test_extract_usage_absent_returns_zero():
    assert extract_usage({"choices": []}) == (0, 0)
    assert extract_usage(object()) == (0, 0)
