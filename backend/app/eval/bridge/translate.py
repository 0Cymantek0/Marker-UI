"""Translate recorded PR80B specialist responses for the bridge benchmark.

The committed PR80B replay cache stores answers in the benchmark's flat
invoice shape under ``(model, full document text)`` keys. The
production specialist lane speaks the versioned
``marker.specialist.output.v1`` contract over bounded packet prompts.
This module is the deterministic adapter between the two: it rekeys
recorded responses onto production prompts (via whitespace-normalized
document matching) and lifts recorded content into the production
output envelope. No content is invented — a lookup miss is ``None``
and the lane reports an honest replay miss.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from app.eval.pr80b.llm import cache_key
from app.extraction.contract import INVOICE_SCHEMA
from app.extraction.specialist import OUTPUT_CONTRACT_VERSION

_SCALAR_NAMES = tuple(spec.name for spec in INVOICE_SCHEMA.fields)
_ITEM_NAME = INVOICE_SCHEMA.line_items[0].name
_IDENTITY_KEYS = tuple(INVOICE_SCHEMA.line_items[0].identity_keys)
_ITEM_FIELD_NAMES = tuple(
    spec.name for spec in INVOICE_SCHEMA.line_items[0].fields
)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def translate_recorded_content(content_raw: str) -> str | None:
    """Lift one recorded flat invoice answer into the output contract.

    Returns ``None`` when the recorded content is not a JSON object —
    the caller treats that as a replay miss rather than improvising.
    """
    try:
        parsed = json.loads(_strip_fences(content_raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    fields: dict[str, str | None] = {}
    for name in _SCALAR_NAMES:
        value = parsed.get(name)
        fields[name] = value if isinstance(value, str) else None

    items: list[dict[str, Any]] = []
    rows = parsed.get("items")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity: dict[str, str] = {}
            for key in _IDENTITY_KEYS:
                value = row.get(key)
                identity[key] = value if isinstance(value, str) else ""
            row_fields: dict[str, str | None] = {}
            for name in _ITEM_FIELD_NAMES:
                if name in _IDENTITY_KEYS:
                    continue
                value = row.get(name)
                row_fields[name] = value if isinstance(value, str) else None
            items.append({"identity": identity, "fields": row_fields})

    flags = parsed.get("flags")
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        flags = []

    return json.dumps(
        {
            "contract_version": OUTPUT_CONTRACT_VERSION,
            "fields": fields,
            _ITEM_NAME: items,
            "flags": flags,
        }
    )


def _doc_line_set(text: str) -> frozenset[str]:
    """Deduplicated non-empty lines of a document/prompt body."""
    return frozenset(
        line.strip() for line in text.splitlines() if line.strip()
    )


def extract_prompt_document(user_text: str) -> str:
    """Pull the document body out of one lane prompt user message."""
    lines = user_text.splitlines()
    body: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == "<document>":
            inside = True
            continue
        if line.strip() == "</document>":
            break
        if inside:
            body.append(line)
    return "\n".join(body)


def build_corpus_lookup(
    corpus: Any,
    responses: Mapping[str, Mapping[str, Any]],
    *,
    model: str,
) -> Callable[[str, str], str | None]:
    """Build the ReplayProvider lookup over recorded corpus responses.

    A lane prompt contains only the lines the authorized query served
    (a subset of the document), so matching is by line-set containment:
    every prompt line must belong to one corpus document, and the
    document with the fewest unserved lines wins. Same recorded
    content, zero invention; an ambiguous or unmatched prompt is a
    miss, never a guess.
    """
    doc_lines = {doc.doc_id: _doc_line_set(doc.full_text) for doc in corpus.docs}
    docs_by_id = {doc.doc_id: doc for doc in corpus.docs}

    def lookup(provider_model: str, user_text: str) -> str | None:
        prompt_lines = _doc_line_set(extract_prompt_document(user_text))
        if not prompt_lines:
            return None
        best_id: str | None = None
        best_extra: int | None = None
        for doc_id, lines in doc_lines.items():
            if not prompt_lines <= lines:
                continue
            extra = len(lines - prompt_lines)
            if best_extra is None or extra < best_extra:
                best_extra = extra
                best_id = doc_id
        if best_id is None:
            return None
        envelope = responses.get(cache_key(model, docs_by_id[best_id].full_text))
        if envelope is None:
            return None
        if envelope.get("error") is not None:
            return None
        content_raw = envelope.get("content_raw")
        if not isinstance(content_raw, str):
            return None
        return translate_recorded_content(content_raw)

    return lookup
