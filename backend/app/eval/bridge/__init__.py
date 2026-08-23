"""Specialist-bridge benchmark harness (PR80B corpus x production lane).

Deterministic offline rerun: committed synthetic corpus + committed
recorded provider responses, replayed through the production
ReplayProvider into the real extraction authorities. See
``docs/reference/specialist-bridge.md`` for the evidence contract.
"""

from __future__ import annotations

from app.eval.bridge.runner import (
    hybrid_system_id,
    result_authority_metrics,
    run_bridge_lane,
)
from app.eval.bridge.translate import (
    build_corpus_lookup,
    extract_prompt_document,
    translate_recorded_content,
)

__all__ = [
    "build_corpus_lookup",
    "extract_prompt_document",
    "hybrid_system_id",
    "result_authority_metrics",
    "run_bridge_lane",
    "translate_recorded_content",
]
