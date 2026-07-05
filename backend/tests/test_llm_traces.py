"""Tests for the per-job LLM call trace buffer (app.core.llm_trace)."""

from __future__ import annotations

import base64
import json

import pytest
import httpx

import app.core.llm_trace as lt


@pytest.fixture(autouse=True)
def clean_buffer():
    lt.clear_all()
    yield
    lt.clear_all()


def _gemini_request(prompt: str = "rewrite this table", image_b64: str = "QUJDRA==") -> httpx.Request:
    body = {
        "contents": [
            {"inline_data": {"data": image_b64, "mime_type": "image/webp"}},
            {"text": prompt},
        ],
        "generationConfig": {"temperature": 0},
    }
    return httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash:generateContent",
        headers={"x-goog-api-key": "key-A", "content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
    )


def _openai_request(prompt: str = "fix this", model: str = "gpt-4") -> httpx.Request:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    return httpx.Request(
        "POST",
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer sk-key-A", "content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
    )


def test_capture_call_records_text_and_image_parts():
    req = _gemini_request(prompt="Rewrite: <table><tr><td>A</td></tr></table>")
    resp = httpx.Response(
        200,
        content=json.dumps({"candidates": [{"content": {"parts": [{"text": '{"corrected_html":"<table>ok</table>}'}]}}]}).encode(),
        request=req,
    )
    lt.capture_call("job-A", req, resp, elapsed_ms=42)
    traces = lt.get_traces("job-A")
    assert len(traces) == 1
    t = traces[0]
    assert t["job_id"] == "job-A"
    assert t["elapsed_ms"] == 42
    assert t["status"] == 200
    assert t["model"] == "gemini-flash"
    assert t["image_count"] == 1
    # Two parts: image + text.
    assert len(t["parts"]) == 2
    text_part = next(p for p in t["parts"] if p["type"] == "text")
    assert "<table>" in text_part["text"]
    img_part = next(p for p in t["parts"] if p["type"] == "image")
    assert img_part["data_url"].startswith("data:image/webp;base64,")
    # Response captured.
    assert "corrected_html" in t["response"]


def test_capture_call_openai_text_only():
    req = _openai_request(prompt="summarize", model="gpt-4")
    resp = httpx.Response(
        200,
        content=json.dumps({"choices": [{"message": {"content": "hello world"}}]}).encode(),
        request=req,
    )
    lt.capture_call("job-B", req, resp)
    traces = lt.get_traces("job-B")
    assert len(traces) == 1
    assert traces[0]["model"] == "gpt-4"
    assert traces[0]["image_count"] == 0
    assert traces[0]["response"] == "hello world"


def test_cache_hit_flag_recorded():
    req = _gemini_request()
    resp = httpx.Response(200, content=b'{"candidates":[{"content":{"parts":[{"text":"x"}]}}]}', request=req)
    lt.capture_call("job-C", req, resp, cache_hit=True)
    assert lt.get_traces("job-C")[0]["cache_hit"] is True


def test_job_isolation():
    """Traces for one job must not leak into another job's buffer."""
    req = _gemini_request()
    resp = httpx.Response(200, content=b'{}', request=req)
    lt.capture_call("job-1", req, resp)
    lt.capture_call("job-2", req, resp)
    assert len(lt.get_traces("job-1")) == 1
    assert len(lt.get_traces("job-2")) == 1
    assert lt.get_traces("job-1")[0]["job_id"] == "job-1"


def test_reset_traces_clears_job():
    req = _gemini_request()
    resp = httpx.Response(200, content=b'{}', request=req)
    lt.capture_call("job-D", req, resp)
    assert len(lt.get_traces("job-D")) == 1
    lt.reset_traces("job-D")
    assert lt.get_traces("job-D") == []


def test_buffer_bounded():
    """The ring buffer caps at _MAX_TRACES_PER_JOB entries (no memory growth)."""
    req = _gemini_request()
    resp = httpx.Response(200, content=b'{}', request=req)
    for _ in range(lt._MAX_TRACES_PER_JOB + 50):
        lt.capture_call("job-E", req, resp)
    traces = lt.get_traces("job-E")
    assert len(traces) == lt._MAX_TRACES_PER_JOB


def test_large_image_skipped():
    """An image exceeding the cap is recorded as truncated, not stored."""
    big_b64 = "A" * (lt._MAX_IMAGE_BYTES + 1000)
    req = _gemini_request(image_b64=big_b64)
    resp = httpx.Response(200, content=b'{}', request=req)
    lt.capture_call("job-F", req, resp)
    img_part = next(p for p in lt.get_traces("job-F")[0]["parts"] if p["type"] == "image")
    assert img_part.get("truncated") is True
    assert "data_url" not in img_part


def test_get_traces_unknown_job_returns_empty():
    assert lt.get_traces("no-such-job") == []


def test_resolve_job_id_returns_none_outside_worker():
    """Outside a conversion thread, resolve_job_id returns None (no crash)."""
    assert lt.resolve_job_id() is None


def test_index_increments_sequentially():
    """Each captured call gets a stable, increasing index for the UI."""
    req = _gemini_request()
    resp = httpx.Response(200, content=b'{}', request=req)
    for _ in range(3):
        lt.capture_call("job-G", req, resp)
    traces = lt.get_traces("job-G")
    assert [t["index"] for t in traces] == [0, 1, 2]
