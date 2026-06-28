"""Merge accepted Hybrid OCR replacements back into a Marker document."""

from __future__ import annotations

from app.hybrid_ocr.contracts import HybridResult, HybridTarget, ReplacementPolicy


def merge_results(document: object, targets: list[HybridTarget], results: list[HybridResult]) -> int:
    by_id = {result.target_id: result for result in results}
    replacements = 0
    for target in targets:
        result = by_id.get(target.target_id)
        if result is None:
            continue
        if result.status != "ok" or not result.validation.accepted:
            continue
        if result.replacement_policy == ReplacementPolicy.NO_CHANGE:
            continue
        block = target.block_ref
        if block is None:
            continue
        replacement = result.markdown or result.text or result.html
        if not replacement.strip():
            continue
        if hasattr(block, "text"):
            setattr(block, "text", replacement)
            replacements += 1
        elif hasattr(block, "html"):
            setattr(block, "html", result.html or replacement)
            replacements += 1
    return replacements

