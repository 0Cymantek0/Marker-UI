"""Declarative model catalog for evaluation VLM/LLM clients.

Replaces hardcoded model chains with a committed, validated catalog:
providers declare *env var names* for base URL / API key (values never
live in the repo), models declare identity plus capability metadata
(context window, max output, vision, thinking format and toggles,
tier). Clients resolve a chain either by explicit model ids or by a
capability selector (``@vision``, ``@tier:frontier``), and environment
variables always override catalog-supplied defaults.

The catalog is configuration, not truth: it names candidates an
experiment may call, and every scored result still comes from the
committed evaluation machinery. Product runtime adoption is a separate,
promoted decision — nothing here extends ``marker.query.v1``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

CATALOG_SCHEMA_VERSION = "marker.model_catalog.v1"
DEFAULT_CATALOG_PATH = Path(__file__).with_name("model_catalog.default.json")

#: the one transport the eval clients speak today; new endpoint shapes
#: must be added here deliberately, never inferred from a URL
KNOWN_TRANSPORTS = frozenset({"openai_chat"})

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*$")


class ModelCatalogError(ValueError):
    """Raised for any inconsistency in a model catalog."""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    transport: str
    base_url_env: str
    api_key_env: str
    base_url_default: str | None = None  # public default only (e.g. openrouter); None = env required


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    context_window: int
    max_output: int
    tier: str = "undeclared"
    vision: bool = False
    reasoning: bool = False
    tools: bool = False
    search: bool = False
    thinking_format: str | None = None
    thinking_can_disable: bool | None = None
    thinking_levels: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def matches(self, selector: Mapping[str, object]) -> bool:
        for key, expected in selector.items():
            if key == "tier":
                if self.tier != expected:
                    return False
            elif key == "tag":
                if expected not in self.tags:
                    return False
            else:
                if getattr(self, key, None) != expected:
                    return False
        return True


@dataclass(frozen=True)
class ModelSelection:
    models: tuple[ModelSpec, ...]
    provider: ProviderSpec
    base_url: str | None
    api_key_env: str
    selector: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ModelCatalogError(message)


@dataclass(frozen=True)
class ModelCatalog:
    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)

    def pick(self, selection: str) -> tuple[ModelSpec, ...]:
        """Resolve model ids / capability selector WITHOUT endpoint env.

        For offline work (aggregating committed artifacts) no base URL
        or key is needed — only which models a selection names.
        """
        selection = selection.strip()
        _require(bool(selection), "empty model selection")
        if selection.startswith("@"):
            selector = _parse_selector(selection[1:])
            picked = [m for m in self.models.values() if m.matches(selector)]
            picked.sort(key=lambda m: m.id)
            _require(bool(picked), f"no catalog model matches {selection!r}")
        else:
            ids = [part.strip() for part in selection.split(",") if part.strip()]
            _require(bool(ids), "no model ids in selection")
            picked = []
            for model_id in ids:
                model = self.models.get(model_id)
                _require(model is not None, f"unknown model id: {model_id!r}")
                picked.append(model)
        return tuple(picked)

    def resolve(
        self,
        selection: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ModelSelection:
        """Resolve one selection string against this catalog.

        ``selection`` is either a comma-separated list of model ids or a
        ``@``-prefixed capability selector (``@vision``, ``@tier:frontier``
        and ``@tier:frontier&vision`` style conjunctions). Base URL and
        API key are read from the provider's env names through ``env``
        (defaults to ``os.environ`` at call time — values are never
        stored in the catalog).
        """
        import os

        env = os.environ if env is None else env
        picked = self.pick(selection)
        providers = {m.provider for m in picked}
        _require(
            len(providers) == 1,
            f"selection spans multiple providers ({sorted(providers)}); "
            "resolve one provider at a time",
        )
        provider = self.providers[next(iter(providers))]
        base_url = env.get(provider.base_url_env) or provider.base_url_default
        _require(
            base_url is not None,
            f"provider {provider.id!r} requires ${provider.base_url_env}",
        )
        selection = selection.strip()
        return ModelSelection(
            models=picked,
            provider=provider,
            base_url=base_url,
            api_key_env=provider.api_key_env,
            selector=selection[1:] if selection.startswith("@") else ",".join(m.id for m in picked),
        )


def _parse_selector(text: str) -> dict[str, object]:
    selector: dict[str, object] = {}
    for clause in text.split("&"):
        clause = clause.strip()
        _require(bool(clause), f"empty selector clause in {text!r}")
        key, sep, value = clause.partition(":")
        key = key.strip()
        _require(bool(sep) or key in ("vision", "reasoning", "tools", "search"), 
                 f"selector {clause!r} needs a value (or must be a known boolean capability)")
        if not sep:
            selector[key] = True
            continue
        _require(key in ("tier", "tag"), f"selector key {key!r} does not take a value")
        selector["tier" if key == "tier" else "tag"] = value.strip()
    _require(bool(selector), "empty capability selector")
    return selector


def load_catalog(path: Path | str | None = None) -> ModelCatalog:
    """Load and fully validate a model catalog. Fail closed."""
    path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    _require(path.is_file(), f"missing model catalog: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    _require(
        data.get("schema_version") == CATALOG_SCHEMA_VERSION,
        f"unsupported model catalog schema: {data.get('schema_version')!r}",
    )
    providers: dict[str, ProviderSpec] = {}
    for entry in data.get("providers") or []:
        pid = entry.get("id")
        _require(isinstance(pid, str) and pid.strip(), "provider id missing")
        _require(pid not in providers, f"duplicate provider id: {pid}")
        transport = entry.get("transport")
        _require(transport in KNOWN_TRANSPORTS, f"provider {pid}: unknown transport {transport!r}")
        base_url_env = entry.get("base_url_env")
        _require(
            isinstance(base_url_env, str) and _ENV_NAME.match(base_url_env),
            f"provider {pid}: bad base_url_env",
        )
        api_key_env = entry.get("api_key_env")
        _require(
            isinstance(api_key_env, str) and _ENV_NAME.match(api_key_env),
            f"provider {pid}: bad api_key_env",
        )
        default = entry.get("base_url_default")
        _require(
            default is None or (isinstance(default, str) and default.startswith("https://")),
            f"provider {pid}: base_url_default must be an https URL or null",
        )
        providers[pid] = ProviderSpec(
            id=pid,
            transport=transport,
            base_url_env=base_url_env,
            api_key_env=api_key_env,
            base_url_default=default,
        )
    _require(bool(providers), "catalog has no providers")

    models: dict[str, ModelSpec] = {}
    for entry in data.get("models") or []:
        mid = entry.get("id")
        _require(isinstance(mid, str) and _MODEL_ID.match(mid or ""), f"bad model id: {mid!r}")
        _require(mid not in models, f"duplicate model id: {mid}")
        provider = entry.get("provider")
        _require(provider in providers, f"model {mid}: unknown provider {provider!r}")
        for int_field in ("context_window", "max_output"):
            value = entry.get(int_field)
            _require(
                isinstance(value, int) and not isinstance(value, bool) and value > 0,
                f"model {mid}: {int_field} must be a positive integer",
            )
        thinking = entry.get("thinking") or {}
        _require(isinstance(thinking, Mapping), f"model {mid}: thinking must be an object")
        levels = thinking.get("levels") or []
        _require(
            isinstance(levels, list) and all(isinstance(l, str) and l.strip() for l in levels),
            f"model {mid}: thinking.levels must be strings",
        )
        tags = entry.get("tags") or []
        _require(
            isinstance(tags, list) and all(isinstance(t, str) and t.strip() for t in tags),
            f"model {mid}: tags must be strings",
        )
        models[mid] = ModelSpec(
            id=mid,
            provider=provider,
            context_window=entry["context_window"],
            max_output=entry["max_output"],
            tier=entry.get("tier", "undeclared"),
            vision=bool(entry.get("vision", False)),
            reasoning=bool(entry.get("reasoning", False)),
            tools=bool(entry.get("tools", False)),
            search=bool(entry.get("search", False)),
            thinking_format=thinking.get("format"),
            thinking_can_disable=thinking.get("can_disable"),
            thinking_levels=tuple(levels),
            tags=tuple(tags),
        )
    _require(bool(models), "catalog has no models")
    return ModelCatalog(providers=providers, models=models)
