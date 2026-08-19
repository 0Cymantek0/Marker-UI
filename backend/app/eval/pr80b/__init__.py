"""PR80B direct-specialist displacement harness (evaluation-only).

Compares the PR80A evidence-backed extraction route against external
direct specialists on the declared benchmark corpus. Nothing here is
a truth or proof authority: lanes read production contracts, they
never mint them. Production kernel/publication/evidence semantics
are exercised in throwaway per-document workspaces.
"""

from __future__ import annotations

from typing import Any

_LAZY: dict[str, str] = {
    "CorpusError": "app.eval.pr80b.corpus",
    "Corpus": "app.eval.pr80b.corpus",
    "CorpusDoc": "app.eval.pr80b.corpus",
    "load_corpus": "app.eval.pr80b.corpus",
    "NormResult": "app.eval.pr80b.normalize",
    "normalize_by_type": "app.eval.pr80b.normalize",
    "DocScore": "app.eval.pr80b.scoring",
    "SystemDocOutput": "app.eval.pr80b.scoring",
    "EmittedField": "app.eval.pr80b.scoring",
    "EmittedRow": "app.eval.pr80b.scoring",
    "score_document": "app.eval.pr80b.scoring",
    "aggregate_metrics": "app.eval.pr80b.scoring",
    "OpenRouterClient": "app.eval.pr80b.llm",
    "CacheMissError": "app.eval.pr80b.llm",
    "Invoice2DataAdapter": "app.eval.pr80b.invoice2data_adapter",
    "run_pr80a_lane": "app.eval.pr80b.pr80a_lane",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_name)
    attribute = getattr(module, name)
    globals()[name] = attribute
    return attribute
