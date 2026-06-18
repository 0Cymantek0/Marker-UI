"""Batched classify+extract prompt + response parsing (plan §3 / §13.3).

The per-image pipeline makes two serial cloud calls (classify, then extract)
for every figure. This module powers the **batched** path: a single
structured-output call that routes *and* extracts up to B images at once and
returns an array reconciled back to each image by index.

Design choices grounded in the plan + reconciliation notes:

* **One stable, cacheable system prefix.** The taxonomy + per-type payload
  shapes + the array envelope are byte-identical across every batch in a
  document, so provider prompt-caching hits (plan §8). Per-image context lives
  in the user content, never the system prompt.
* **JSON-object mode + defensive parse, not heterogeneous strict-schema.**
  OpenAI strict schemas require a fixed, fully-specified object; our payload is
  a *union* that varies by image type (chart vs mermaid vs latex vs table), so
  a single strict schema cannot express it. We instead pin the envelope shape
  in the prompt and rely on a tolerant parser + per-index selective retry for
  the reconciliation guarantee. (The per-type single-image strict path remains
  available via ``batch_enabled=False``.)
* **Index discipline.** Each image is labelled ``=== IMAGE k ===`` and the model
  must echo that ``index`` in every result object, so a short / reordered /
  partially-malformed response still reconciles by index rather than position.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.prompts.image_classify import TYPE_DEFINITIONS


@dataclass
class BatchItem:
    """One image plus its local document context for a batch call."""

    image_bytes: bytes
    mime_type: str = "image/png"
    heading_chain: str = ""
    surrounding: str = ""


BATCH_DISCIPLINE_LINE = "Return JSON only, no prose, no code fences."

# Compact per-type payload guide. Stable across the whole document so the system
# prompt stays cacheable (plan §8).
_PAYLOAD_GUIDE = """\
Payload shape depends on image_type:
- chart_bar / chart_line / chart_pie / chart_scatter / chart_other:
  {"title": str, "x_label": str, "y_label": str,
   "series": [{"name": str, "points": [{"x": any, "y": any}]}], "notes": str}
- table_image: {"caption": str, "headers": [str], "rows": [[str]]}
- diagram_flow / diagram_sequence / diagram_state / diagram_class /
  diagram_architecture: {"mermaid": str, "caption": str}  (valid Mermaid source)
- equation: {"latex": str, "caption": str}
- screenshot_ui: {"application": str, "area": str,
   "regions": [{"name": str, "description": str, "ocr_text": str}], "summary": str}
- figure_technical / photo / other: {"alt_text": str, "details": [str]}
- decorative: {}  (no informational content)\
"""


def build_batch_system_prompt() -> str:
    """Return the stable, cacheable system prompt for a batch call."""
    type_defs = "\n".join(f"- {line}" for line in TYPE_DEFINITIONS.values())
    return (
        "You are a document image understanding system. You will receive several "
        "images, each introduced by a line '=== IMAGE k ===' followed by its "
        "local document context. For EACH image, classify it into exactly one "
        "type and extract its structured content in a single pass.\n\n"
        f"TYPES:\n{type_defs}\n\n"
        f"{_PAYLOAD_GUIDE}\n\n"
        "Output strict JSON in EXACTLY this shape and nothing else:\n"
        '{"results": [{"index": <int matching the IMAGE number>, '
        '"route": "vlm_required|ocr_sufficient|decorative", '
        '"image_type": "<one type value>", "confidence": <float 0.0-1.0>, '
        '"payload": <the type-specific object above>}]}\n\n'
        "Use route=ocr_sufficient for plain text/table text that deterministic "
        "OCR can transcribe better than you can describe; leave payload empty. "
        "Use route=decorative for non-informational marks; leave payload empty. "
        "Use route=vlm_required when the image needs visual understanding and "
        "fill payload.\n\n"
        "DIAGRAM FIDELITY (when image_type is any diagram_*): reproduce ONLY "
        "nodes, edges, and labels literally drawn in the image — never invent an "
        "edge, arrow, or relationship label that is not visibly present; if two "
        "elements are not connected by a drawn line, do not connect them. If the "
        "image is a set of parallel/independent comparison panels rather than one "
        "connected graph, render each panel as its own subgraph with only its "
        "drawn elements and put the shared comparison axis in the caption — do "
        "NOT fabricate a flow between panels. When unsure about an edge, omit it.\n\n"
        "Include one result object per image. Always echo the correct index. "
        f"{BATCH_DISCIPLINE_LINE}"
    )


def build_batch_user_content(
    items: list[BatchItem],
    encode_data_url: Any,
) -> list[dict[str, Any]]:
    """Build the interleaved (text, image) user content for a batch call.

    Args:
        items: The batch images + context.
        encode_data_url: Callable ``(bytes, mime) -> data-url str`` (injected so
            this module stays free of base64 / encoding concerns).

    Returns:
        A list of OpenAI-style content parts: a header text part, then for each
        image a labelled text part followed by its image part.
    """
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"There are {len(items)} images. Classify and extract each one. "
                "Return one result object per image, echoing its index."
            ),
        }
    ]
    for idx, item in enumerate(items):
        label = (
            f"=== IMAGE {idx} ===\n"
            f"Heading chain: {item.heading_chain}\n"
            f"Surrounding paragraphs: {item.surrounding}"
        )
        content.append({"type": "text", "text": label})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": encode_data_url(item.image_bytes, item.mime_type)},
            }
        )
    return content


def build_batch_response_format(provider_type: str) -> dict[str, Any]:
    """Return the ``response_format`` for a batch call.

    JSON-object mode for every provider: the heterogeneous per-type payload
    union cannot be expressed as a single OpenAI strict schema (see module
    docstring), so the envelope is pinned in the prompt and enforced by the
    tolerant parser + selective retry instead. ``provider_type`` is accepted for
    forward-compatibility (a future per-type strict batch could branch here).
    """
    return {"type": "json_object"}


def parse_batch_response(raw: str, batch_size: int) -> dict[int, dict[str, Any]]:
    """Parse a batch response into ``{index: {image_type, confidence, payload}}``.

    Tolerant by design: accepts ``{"results": [...]}``, a bare ``[...]``,
    fenced JSON, or prose-wrapped JSON; skips entries with an out-of-range /
    missing / duplicate index; ignores
    malformed entries. Missing indices are simply absent from the returned dict
    so the caller can retry exactly those.
    """
    if not raw:
        return {}
    raw = _extract_json_candidate(raw)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}

    if isinstance(parsed, dict):
        results = parsed.get("results")
    elif isinstance(parsed, list):
        results = parsed
    else:
        results = None
    if not isinstance(results, list):
        return {}

    out: dict[int, dict[str, Any]] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= batch_size:
            continue
        if idx in out:
            continue  # first wins; ignore duplicate index
        image_type = entry.get("image_type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        out[idx] = {
            "route": entry.get("route") or "vlm_required",
            "image_type": image_type,
            "confidence": entry.get("confidence", 0.0),
            "payload": payload,
        }
    return out


def _extract_json_candidate(raw: str) -> str:
    """Return likely JSON from raw/fenced/prose-wrapped model output."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{") or text.startswith("["):
        return text

    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return text
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    end = text.rfind(closer)
    if end > start:
        return text[start : end + 1]
    return text
