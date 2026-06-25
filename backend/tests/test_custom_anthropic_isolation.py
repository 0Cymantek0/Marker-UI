"""UCM-004.5: custom Anthropic base URL must not mutate global os.environ.

Old behaviour set os.environ['ANTHROPIC_BASE_URL'] during build_marker_options
for custom_anthropic providers. That is process-global state, so two
concurrent jobs using different custom Anthropic providers would clobber each
other's base URL. The fix routes custom_anthropic through a subclass that
pins base_url on the service instance.
"""

from __future__ import annotations

import os

import pytest

from app.services.marker_service import LLM_SERVICE_MAP, build_marker_options


def _custom_anthropic_provider(provider_id: str, base_url: str) -> dict:
    return {
        "id": provider_id,
        "type": "custom_anthropic",
        "label": provider_id,
        "base_url": base_url,
        "api_key": f"key-{provider_id}",
        "models": [{"model_id": "claude-test"}],
    }


def test_custom_anthropic_llm_service_points_to_isolated_subclass():
    """The service map must route custom_anthropic to our per-instance subclass."""
    assert (
        LLM_SERVICE_MAP["custom_anthropic"]
        == "app.services.custom_anthropic_service.CustomAnthropicService"
    )


def test_build_marker_options_does_not_mutate_anthropic_env(tmp_path, monkeypatch):
    """build_marker_options must never touch os.environ for the base URL."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    before = dict(os.environ)

    opts = build_marker_options(
        {
            "providers": [_custom_anthropic_provider("anthro_a", "https://a.example/v1")],
            "active": {"provider_id": "anthro_a", "model_id": "claude-test"},
        },
        {"use_llm": True},
    )

    assert os.environ == before
    assert "ANTHROPIC_BASE_URL" not in os.environ
    # base_url flows through options instead.
    assert opts["base_url"] == "https://a.example/v1"
    assert opts["llm_service"] == LLM_SERVICE_MAP["custom_anthropic"]


def test_concurrent_custom_anthropic_providers_do_not_bleed_base_url(monkeypatch):
    """Two providers built in sequence must keep distinct base_urls with no env leak."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    opts_a = build_marker_options(
        {
            "providers": [
                _custom_anthropic_provider("anthro_a", "https://a.example/v1"),
                _custom_anthropic_provider("anthro_b", "https://b.example/v1"),
            ],
            "active": {"provider_id": "anthro_a", "model_id": "claude-test"},
        },
        {"use_llm": True},
    )
    opts_b = build_marker_options(
        {
            "providers": [
                _custom_anthropic_provider("anthro_a", "https://a.example/v1"),
                _custom_anthropic_provider("anthro_b", "https://b.example/v1"),
            ],
            "active": {"provider_id": "anthro_b", "model_id": "claude-test"},
        },
        {"use_llm": True},
    )

    assert opts_a["base_url"] == "https://a.example/v1"
    assert opts_b["base_url"] == "https://b.example/v1"
    assert "ANTHROPIC_BASE_URL" not in os.environ


def test_custom_anthropic_service_pins_base_url_on_client(monkeypatch):
    """The subclass must construct the Anthropic client with an explicit base_url."""
    captured: dict = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import app.services.custom_anthropic_service as cas_mod

    monkeypatch.setattr(cas_mod.anthropic, "Anthropic", FakeAnthropic)

    service = cas_mod.CustomAnthropicService(
        {
            "claude_api_key": "sk-test",
            "base_url": "https://gateway.example/v1",
        }
    )
    service.get_client()

    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "https://gateway.example/v1"
