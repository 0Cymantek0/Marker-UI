"""PR81A selective visual retrieval promotion experiment (evaluation-only).

This package answers the PR81 experiment gate question: does one credible
selective visual retrieval route beat the strongest non-visual alternative
on a declared visual-hard downstream task enough to pay its measured
generation, storage, update, and authorization cost?

Everything here is evaluation tooling. Lanes may *read* production
contracts (kernel records, publications, authorization, queries); they
never mint truth, never extend ``marker.query.v1``, and never present a
visual result as an authoritative ``EvidencePacket`` citation.
"""

from __future__ import annotations

import importlib

_LAZY: dict[str, str] = {
    "corpus_gen": "app.eval.pr81a.corpus_gen",
    "normalize": "app.eval.pr81a.normalize",
    "corpus": "app.eval.pr81a.corpus",
    "visual_store": "app.eval.pr81a.visual_store",
    "embeddings": "app.eval.pr81a.embeddings",
    "visual_index": "app.eval.pr81a.visual_index",
}

__all__ = list(_LAZY)


def __getattr__(name: str):
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    globals()[name] = module
    return module
