"""Collect candidate targets from a Marker document.

The collector is deliberately duck-typed so tests can use small fake document
objects and production can adapt as Marker block APIs evolve.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from app.hybrid_ocr.contracts import HybridTarget, TargetKind


_TYPE_TO_KIND = {
    "table": TargetKind.TABLE,
    "equation": TargetKind.FORMULA,
    "formula": TargetKind.FORMULA,
    "math": TargetKind.FORMULA,
    "text": TargetKind.TEXT,
    "line": TargetKind.TEXT,
}


def _children(obj: Any) -> Iterable[Any]:
    for attr in ("children", "blocks", "structure"):
        value = getattr(obj, attr, None)
        if isinstance(value, (list, tuple)):
            yield from value


def _block_type(block: Any) -> str:
    raw = getattr(block, "block_type", None) or getattr(block, "type", None) or type(block).__name__
    if hasattr(raw, "value"):
        raw = raw.value
    return str(raw)


def _text(block: Any) -> str:
    for attr in ("text", "raw_text", "html"):
        value = getattr(block, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    getter = getattr(block, "assemble_html", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, str):
                return value
        except Exception:
            return ""
    return ""


def _bbox(block: Any) -> list[float] | None:
    value = getattr(block, "bbox", None) or getattr(block, "polygon", None)
    if isinstance(value, (list, tuple)) and value:
        flat = list(value)
        if flat and isinstance(flat[0], (int, float)):
            return [float(v) for v in flat[:4]]
    return None


def collect_targets(document: Any, *, filepath: str, job_dir: Path) -> list[HybridTarget]:
    targets: list[HybridTarget] = []
    seen: set[str] = set()
    document_id = hashlib.sha256(str(filepath).encode("utf-8")).hexdigest()[:12]

    def visit(block: Any, page_index: int = 0) -> None:
        block_type = _block_type(block)
        kind = _TYPE_TO_KIND.get(block_type.lower())
        baseline = _text(block)
        confidence = getattr(block, "confidence", None)
        if kind in {None, TargetKind.TEXT} and isinstance(confidence, (int, float)) and confidence < 0.65:
            kind = TargetKind.DEGRADED_TEXT
        if kind in {TargetKind.TABLE, TargetKind.FORMULA, TargetKind.DEGRADED_TEXT}:
            block_id = str(getattr(block, "id", None) or getattr(block, "block_id", None) or "")
            fp_src = f"{page_index}:{block_type}:{_bbox(block)}:{baseline[:200]}"
            fingerprint = hashlib.sha256(fp_src.encode("utf-8", errors="ignore")).hexdigest()
            if fingerprint not in seen:
                seen.add(fingerprint)
                target_id = f"p{page_index + 1}_{kind.value}_{len(targets) + 1:02d}"
                targets.append(
                    HybridTarget(
                        target_id=target_id,
                        document_id=document_id,
                        page_index=page_index,
                        page_number=page_index + 1,
                        block_id=block_id or None,
                        block_type=block_type,
                        target_kind=kind,
                        bbox=_bbox(block),
                        polygon=None,
                        crop_path=str(job_dir / f"{target_id}.png"),
                        crop_width=0,
                        crop_height=0,
                        baseline_text=baseline,
                        baseline_html=getattr(block, "html", "") if isinstance(getattr(block, "html", ""), str) else "",
                        baseline_confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                        baseline_source="marker",
                        route_hints={},
                        fingerprint=fingerprint,
                        block_ref=block,
                    )
                )
        for child in _children(block):
            visit(child, page_index=page_index)

    pages = getattr(document, "pages", None)
    if isinstance(pages, (list, tuple)):
        for index, page in enumerate(pages):
            visit(page, page_index=index)
    else:
        visit(document, page_index=0)
    return targets
