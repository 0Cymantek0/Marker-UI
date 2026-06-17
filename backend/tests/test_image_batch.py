"""Tests for batched classify+extract (plan §3): prompt build, tolerant parse,
reconciliation, selective retry, and processor batch wiring.

All against fake clients — no torch, no network, no provider keys.
"""

from __future__ import annotations

import json

from app.models.image_understanding import ImageType
from app.models.schemas import LLMProvider
from app.prompts.image_batch import (
    BatchItem,
    build_batch_system_prompt,
    build_batch_user_content,
    parse_batch_response,
)
from app.services.vlm_service import VLMService


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_batch_system_prompt_lists_all_types_and_envelope():
    sys = build_batch_system_prompt()
    for t in ImageType:
        assert t.value in sys
    assert '"results"' in sys
    assert '"index"' in sys


def test_batch_user_content_interleaves_labels_and_images():
    items = [
        BatchItem(b"a", "image/png", "H1: A", "ctx a"),
        BatchItem(b"b", "image/png", "H1: B", "ctx b"),
    ]
    content = build_batch_user_content(items, lambda b, m: f"data:{m};x")
    # header + (label, image) * 2
    assert len(content) == 1 + 2 * 2
    assert "=== IMAGE 0 ===" in content[1]["text"]
    assert "ctx a" in content[1]["text"]
    assert content[2]["type"] == "image_url"
    assert "=== IMAGE 1 ===" in content[3]["text"]


# ---------------------------------------------------------------------------
# Tolerant response parser
# ---------------------------------------------------------------------------


def test_parse_results_envelope():
    raw = json.dumps(
        {
            "results": [
                {"index": 0, "image_type": "chart_bar", "confidence": 0.9, "payload": {"title": "x"}},
                {"index": 1, "image_type": "equation", "confidence": 0.8, "payload": {"latex": "a"}},
            ]
        }
    )
    out = parse_batch_response(raw, 2)
    assert set(out.keys()) == {0, 1}
    assert out[0]["image_type"] == "chart_bar"


def test_parse_accepts_bare_array():
    raw = json.dumps([{"index": 0, "image_type": "photo", "confidence": 1.0, "payload": {}}])
    out = parse_batch_response(raw, 1)
    assert out[0]["image_type"] == "photo"


def test_parse_skips_out_of_range_and_dupes():
    raw = json.dumps(
        {
            "results": [
                {"index": 0, "image_type": "photo", "payload": {}},
                {"index": 5, "image_type": "photo", "payload": {}},  # out of range
                {"index": 0, "image_type": "chart_bar", "payload": {}},  # dupe, ignored
            ]
        }
    )
    out = parse_batch_response(raw, 2)
    assert set(out.keys()) == {0}
    assert out[0]["image_type"] == "photo"  # first wins


def test_parse_garbage_returns_empty():
    assert parse_batch_response("not json", 3) == {}
    assert parse_batch_response("", 3) == {}
    assert parse_batch_response(json.dumps({"nope": 1}), 3) == {}


def test_parse_accepts_fenced_and_prose_wrapped_json():
    fenced = '```json\n{"results": [{"index": 0, "route": "ocr_sufficient", "image_type": "other", "payload": {}}]}\n```'
    prose = 'Here is the result: {"results": [{"index": 0, "route": "decorative", "image_type": "decorative", "payload": {}}]} Thanks.'

    assert parse_batch_response(fenced, 1)[0]["route"] == "ocr_sufficient"
    assert parse_batch_response(prose, 1)[0]["route"] == "decorative"


# ---------------------------------------------------------------------------
# VLMService.classify_and_extract_batch — reconciliation + selective retry
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """Returns a queued response per chat.completions.create call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

        class _Comp:
            def __init__(self, parent):
                self._p = parent

            def create(self, **kwargs):
                self._p.calls += 1
                content = self._p._responses.pop(0)
                return {"choices": [{"message": {"content": content}}]}

        class _Chat:
            def __init__(self, parent):
                self.completions = _Comp(parent)

        self.chat = _Chat(self)


def _provider():
    return LLMProvider(
        id="openai",
        type="openai",
        label="OpenAI",
        api_key="k",
        models=[{"model_id": "gpt-4o", "vision_capable": True}],
    )


def _svc(client):
    return VLMService(provider=_provider(), model_id="gpt-4o", http_client=client)


def test_batch_reconciles_all_in_one_call():
    resp = json.dumps(
        {
            "results": [
                {"index": 0, "image_type": "chart_bar", "confidence": 0.9, "payload": {"title": "t"}},
                {"index": 1, "image_type": "table_image", "confidence": 0.8, "payload": {"headers": ["a"], "rows": [["1"]]}},
            ]
        }
    )
    client = _ScriptedClient([resp])
    svc = _svc(client)
    items = [BatchItem(b"x"), BatchItem(b"y")]
    results = svc.classify_and_extract_batch(items, max_retries=2)

    assert client.calls == 1  # single round-trip
    assert len(results) == 2
    assert results[0].image_type == ImageType.chart_bar
    assert results[1].image_type == ImageType.table_image
    assert all(r.error is None for r in results)


def test_batch_selective_retry_recovers_missing_index():
    # First call returns only index 0; retry returns the missing index (now 0
    # of the smaller follow-up batch).
    first = json.dumps({"results": [{"index": 0, "image_type": "photo", "confidence": 1.0, "payload": {}}]})
    retry = json.dumps({"results": [{"index": 0, "image_type": "equation", "confidence": 0.9, "payload": {"latex": "a"}}]})
    client = _ScriptedClient([first, retry])
    svc = _svc(client)
    items = [BatchItem(b"x"), BatchItem(b"y")]
    results = svc.classify_and_extract_batch(items, max_retries=2)

    assert client.calls == 2  # one retry for the missing index
    assert results[0].image_type == ImageType.photo
    assert results[1].image_type == ImageType.equation
    assert results[1].error is None


def test_batch_unrecovered_index_marked_error():
    # Attempt 1 returns only index 0 (recovers item 0). The retry batch (just
    # item 1, re-indexed to 0) returns an empty result set, so item 1 never
    # recovers and must be marked with an explicit error.
    first = json.dumps({"results": [{"index": 0, "image_type": "photo", "confidence": 1.0, "payload": {}}]})
    empty = json.dumps({"results": []})
    client = _ScriptedClient([first, empty])
    svc = _svc(client)
    items = [BatchItem(b"x"), BatchItem(b"y")]
    results = svc.classify_and_extract_batch(items, max_retries=1)

    assert results[0].error is None
    assert results[0].image_type == ImageType.photo
    assert results[1].error is not None
    assert "no usable result" in results[1].error


def test_batch_invalid_mermaid_demotes_to_description():
    resp = json.dumps(
        {
            "results": [
                {"index": 0, "image_type": "diagram_flow", "confidence": 0.9, "payload": {"mermaid": "garbage no arrows"}},
            ]
        }
    )
    client = _ScriptedClient([resp, resp])  # retry also bad
    svc = _svc(client)
    results = svc.classify_and_extract_batch([BatchItem(b"x")], max_retries=1)
    # Demoted to a description payload, not discarded.
    assert results[0].image_type == ImageType.diagram_flow
    assert "alt_text" in results[0].payload
    assert results[0].error is None


def test_batch_returns_ocr_sufficient_route_for_processor_spillover():
    resp = json.dumps(
        {
            "results": [
                {"index": 0, "route": "ocr_sufficient", "image_type": "other", "confidence": 0.8, "payload": {}},
            ]
        }
    )
    client = _ScriptedClient([resp])
    svc = _svc(client)
    results = svc.classify_and_extract_batch([BatchItem(b"x")], max_retries=0)

    assert results[0].route == "ocr_sufficient"
    assert results[0].error is None


def test_empty_batch_returns_empty():
    svc = _svc(_ScriptedClient([]))
    assert svc.classify_and_extract_batch([]) == []
