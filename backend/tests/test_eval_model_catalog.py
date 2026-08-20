"""Model catalog tests: validation, capability selection, env indirection.

The catalog is configuration for evaluation clients — it must never
carry secret values or endpoint credentials, only env var NAMES, and
must fail closed on every structural inconsistency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.model_catalog import (
    CATALOG_SCHEMA_VERSION,
    DEFAULT_CATALOG_PATH,
    ModelCatalogError,
    load_catalog,
)


def _write(tmp_path, payload) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _minimal() -> dict:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "providers": [
            {
                "id": "gw",
                "transport": "openai_chat",
                "base_url_env": "MARKER_LLM_BASE_URL",
                "api_key_env": "MARKER_LLM_API_KEY",
                "base_url_default": None,
            }
        ],
        "models": [
            {
                "id": "a/one",
                "provider": "gw",
                "context_window": 1000,
                "max_output": 100,
                "vision": True,
                "tier": "frontier",
            },
            {
                "id": "a/two",
                "provider": "gw",
                "context_window": 2000,
                "max_output": 200,
                "vision": False,
                "tier": "economy",
                "thinking": {"format": "openai", "can_disable": True, "levels": ["low", "high"]},
                "tags": ["fast"],
            },
        ],
    }


class TestLoader:
    def test_minimal_catalog_loads(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        assert set(catalog.models) == {"a/one", "a/two"}
        assert catalog.models["a/one"].vision is True
        assert catalog.models["a/two"].thinking_levels == ("low", "high")

    def test_wrong_schema_rejected(self, tmp_path):
        payload = _minimal()
        payload["schema_version"] = "bogus.v0"
        with pytest.raises(ModelCatalogError, match="schema"):
            load_catalog(_write(tmp_path, payload))

    def test_unknown_transport_rejected(self, tmp_path):
        payload = _minimal()
        payload["providers"][0]["transport"] = "grpc_magic"
        with pytest.raises(ModelCatalogError, match="transport"):
            load_catalog(_write(tmp_path, payload))

    def test_unknown_provider_reference_rejected(self, tmp_path):
        payload = _minimal()
        payload["models"][0]["provider"] = "nowhere"
        with pytest.raises(ModelCatalogError, match="unknown provider"):
            load_catalog(_write(tmp_path, payload))

    def test_duplicate_model_rejected(self, tmp_path):
        payload = _minimal()
        payload["models"].append(dict(payload["models"][0]))
        with pytest.raises(ModelCatalogError, match="duplicate"):
            load_catalog(_write(tmp_path, payload))

    def test_non_positive_context_window_rejected(self, tmp_path):
        payload = _minimal()
        payload["models"][0]["context_window"] = 0
        with pytest.raises(ModelCatalogError, match="context_window"):
            load_catalog(_write(tmp_path, payload))

    def test_non_https_default_url_rejected(self, tmp_path):
        payload = _minimal()
        payload["providers"][0]["base_url_default"] = "http://insecure"
        with pytest.raises(ModelCatalogError, match="base_url_default"):
            load_catalog(_write(tmp_path, payload))

    def test_lowercase_env_name_rejected(self, tmp_path):
        payload = _minimal()
        payload["providers"][0]["api_key_env"] = "api_key"
        with pytest.raises(ModelCatalogError, match="api_key_env"):
            load_catalog(_write(tmp_path, payload))


class TestResolve:
    def test_explicit_ids_preserve_order(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        env = {"MARKER_LLM_BASE_URL": "http://gw", "MARKER_LLM_API_KEY": "sk-x"}
        selection = catalog.resolve("a/two,a/one", env=env)
        assert [m.id for m in selection.models] == ["a/two", "a/one"]
        assert selection.base_url == "http://gw"
        assert selection.api_key_env == "MARKER_LLM_API_KEY"

    def test_unknown_id_fails_closed(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        with pytest.raises(ModelCatalogError, match="unknown model id"):
            catalog.resolve("a/three", env={})

    def test_capability_selector_filters(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        selection = catalog.resolve("@vision", env={"MARKER_LLM_BASE_URL": "http://gw"})
        assert [m.id for m in selection.models] == ["a/one"]

    def test_tier_selector(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        selection = catalog.resolve("@tier:economy", env={"MARKER_LLM_BASE_URL": "http://gw"})
        assert [m.id for m in selection.models] == ["a/two"]

    def test_conjunctive_selector(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        # a/two is economy but not reasoning -> empty conjunction fails closed
        with pytest.raises(ModelCatalogError, match="no catalog model"):
            catalog.resolve("@tier:economy&reasoning", env={"MARKER_LLM_BASE_URL": "http://gw"})
        with pytest.raises(ModelCatalogError, match="no catalog model"):
            catalog.resolve("@tier:frontier&tag:fast", env={})

    def test_tag_selector(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        selection = catalog.resolve("@tag:fast", env={"MARKER_LLM_BASE_URL": "http://gw"})
        assert [m.id for m in selection.models] == ["a/two"]

    def test_valueless_unknown_capability_rejected(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        with pytest.raises(ModelCatalogError, match="value"):
            catalog.resolve("@hyperspace", env={})

    def test_missing_base_url_env_fails_closed(self, tmp_path):
        catalog = load_catalog(_write(tmp_path, _minimal()))
        with pytest.raises(ModelCatalogError, match="MARKER_LLM_BASE_URL"):
            catalog.resolve("@vision", env={})

    def test_provider_default_used_when_env_absent(self, tmp_path):
        payload = _minimal()
        payload["providers"][0]["base_url_default"] = "https://openrouter.ai/api/v1"
        catalog = load_catalog(_write(tmp_path, payload))
        selection = catalog.resolve("@vision", env={})
        assert selection.base_url == "https://openrouter.ai/api/v1"


class TestCommittedCatalog:
    def test_default_catalog_loads_with_pr81b_models(self):
        catalog = load_catalog()
        for model_id in (
            "oc/mimo-v2.5-free",
            "kr/claude-sonnet-4.5",
            "kr/claude-haiku-4.5",
            "cx/gpt-5.6-luna",
            "free/bbl/gemini-3.0-flash",
        ):
            assert model_id in catalog.models, model_id
            assert catalog.models[model_id].vision is True

    def test_default_catalog_carries_no_secrets_or_private_endpoints(self):
        raw = DEFAULT_CATALOG_PATH.read_text(encoding="utf-8")
        assert "sk-" not in raw
        assert "localhost" not in raw
        assert "20128" not in raw
        # only env var NAMES may appear for credentials
        assert "api_key_env" in raw

    def test_vision_selector_is_single_provider(self):
        # gemma (openrouter) is also vision-capable, so a bare @vision
        # spans two providers and must refuse rather than guess
        catalog = load_catalog()
        with pytest.raises(ModelCatalogError, match="multiple providers"):
            catalog.resolve("@vision", env={"MARKER_LLM_BASE_URL": "http://gw"})

    def test_each_gateway_vision_model_resolves(self):
        catalog = load_catalog()
        for model_id in (
            "oc/mimo-v2.5-free",
            "kr/claude-sonnet-4.5",
            "kr/claude-haiku-4.5",
            "cx/gpt-5.6-luna",
            "free/bbl/gemini-3.0-flash",
        ):
            selection = catalog.resolve(model_id, env={"MARKER_LLM_BASE_URL": "http://gw"})
            assert selection.models[0].vision is True
            assert selection.provider.id == "local-gateway"

    def test_tier_frontier_selector(self):
        catalog = load_catalog()
        selection = catalog.resolve(
            "@tier:frontier", env={"MARKER_LLM_BASE_URL": "http://gw"}
        )
        assert {m.id for m in selection.models} == {
            "kr/claude-sonnet-4.5",
            "kr/claude-haiku-4.5",
            "cx/gpt-5.6-luna",
        }

    def test_thinking_metadata_preserved(self):
        catalog = load_catalog()
        sonnet = catalog.models["kr/claude-sonnet-4.5"]
        assert sonnet.thinking_format == "claude-budget"
        assert sonnet.thinking_can_disable is True
        assert sonnet.context_window == 200000
        assert sonnet.max_output == 64000
