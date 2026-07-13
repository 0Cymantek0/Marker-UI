"""In-memory LLM call trace buffer for the per-job trace viewer UI.

Each LLM generation call is captured as a list of structured "parts" so the
frontend can render them richly:
  - text parts -> shown as monospace (or rendered HTML for table blocks)
  - image parts -> shown as an <img> preview (data URL)

The buffer is per-job and bounded. Image data is captured as a base64 data URL
so the browser can render it directly without a round-trip. Image capture is
capped per-image to avoid memory bloat on large runs.

Polled via ``GET /api/convert/{job_id}/llm-traces``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any

import httpx

_MAX_TRACES_PER_JOB = 200
_PROMPT_LIMIT = 8000          # generous: the table HTML lives in the prompt
_RESPONSE_LIMIT = 8000
_MAX_IMAGE_BYTES = 250_000    # skip images larger than this (rare for table crops)

_traces: dict[str, deque[dict[str, Any]]] = {}
_lock = threading.Lock()


def reset_traces(job_id: str) -> None:
    """Drop all captured traces for a job (called when a job starts)."""
    with _lock:
        _traces.pop(job_id, None)


def clear_all() -> None:
    """Wipe every job's trace buffer (test helper)."""
    with _lock:
        _traces.clear()


def get_traces(job_id: str) -> list[dict[str, Any]]:
    """Return a list copy of the traces captured for a job, oldest first."""
    with _lock:
        buf = _traces.get(job_id)
        return list(buf) if buf else []


def _append_trace(job_id: str, entry: dict[str, Any]) -> None:
    with _lock:
        buf = _traces.setdefault(job_id, deque(maxlen=_MAX_TRACES_PER_JOB))
        entry["index"] = len(buf)
        buf.append(entry)


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f" ... [+{len(text) - limit} chars]"


def _make_image_part(data_b64: str, mime: str) -> dict[str, Any]:
    """Build an image part. Skips oversized images to bound memory."""
    if len(data_b64) > _MAX_IMAGE_BYTES:
        return {
            "type": "image",
            "truncated": True,
            "size_bytes": len(data_b64),
            "note": f"image skipped ({len(data_b64)} bytes > {_MAX_IMAGE_BYTES} cap)",
        }
    return {
        "type": "image",
        "data_url": f"data:{mime or 'image/webp'};base64,{data_b64}",
        "mime": mime,
        "size_bytes": len(data_b64),
    }


def _extract_gemini_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Build structured parts from a Gemini request body.

    Gemini sends ``contents: [{inline_data: {data, mime_type}}, {text: prompt}]``.
    """
    contents = body.get("contents") or []
    if not isinstance(contents, list):
        return []
    parts: list[dict[str, Any]] = []
    for part in contents:
        if not isinstance(part, dict):
            continue
        if "text" in part and isinstance(part["text"], str):
            parts.append({"type": "text", "text": _truncate(part["text"], _PROMPT_LIMIT)})
        elif "inline_data" in part or "inlineData" in part:
            d = part.get("inline_data") or part.get("inlineData") or {}
            data_b64 = d.get("data") or ""
            mime = d.get("mime_type") or d.get("mimeType") or "image/webp"
            parts.append(_make_image_part(data_b64, mime))
    return parts


def _extract_chat_parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Build structured parts from an OpenAI/Claude request body."""
    messages = body.get("messages") or body.get("contents") or []
    if not isinstance(messages, list):
        return []
    parts: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append({"type": "text", "text": _truncate(content, _PROMPT_LIMIT)})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    parts.append({"type": "text", "text": _truncate(part.get("text", ""), _PROMPT_LIMIT)})
                elif ptype in ("image_url", "image", "input_image"):
                    url = part.get("image_url", {}).get("url", "") if isinstance(part.get("image_url"), dict) else part.get("image_url", "")
                    if isinstance(url, str) and url.startswith("data:"):
                        # data:image/webp;base64,XXXX
                        m = re.match(r"data:([^;]+);base64,(.+)", url, re.DOTALL)
                        if m:
                            parts.append(_make_image_part(m.group(2), m.group(1)))
                        else:
                            parts.append({"type": "text", "text": "[unparseable image data URI]"})
                    else:
                        parts.append({"type": "text", "text": f"[image url: {url}]"})
    return parts


def _parse_request(request: httpx.Request) -> dict[str, Any]:
    """Extract model + structured parts from an LLM generation request."""
    host = request.url.host or "?"
    model = ""
    m = re.search(r"/models/([^/:]+)", request.url.path)
    if m:
        model = m.group(1)
    parts: list[dict[str, Any]] = []
    body_text = ""
    if hasattr(request, "_content") and request._content:
        body_text = request._content.decode("utf-8", errors="ignore")
    if body_text:
        try:
            body = json.loads(body_text)
            if "contents" in body and "googleapis.com" in host:
                parts = _extract_gemini_parts(body)
            else:
                parts = _extract_chat_parts(body)
                if not model:
                    model = body.get("model", "")
        except Exception:  # noqa: BLE001
            parts = [{"type": "text", "text": f"[unparseable body, {len(body_text)} bytes]"}]
    image_count = sum(1 for p in parts if p.get("type") == "image")
    prompt_chars = sum(len(p.get("text", "")) for p in parts if p.get("type") == "text")
    return {
        "host": host,
        "model": model,
        "parts": parts,
        "image_count": image_count,
        "prompt_chars": prompt_chars,
    }


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    """Extract the text body from an LLM response (Gemini/OpenAI/Claude)."""
    body = response.content or b""
    text = ""
    if body:
        try:
            parsed = json.loads(body.decode("utf-8", errors="ignore"))
            if "candidates" in parsed:
                cparts = parsed.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in cparts if isinstance(p, dict))
            elif "choices" in parsed:
                text = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
            elif "content" in parsed and isinstance(parsed.get("content"), list):
                text = "".join(
                    b.get("text", "") for b in parsed["content"] if isinstance(b, dict)
                )
            else:
                text = body.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            text = f"[unparseable body, {len(body)} bytes]"
    return {
        "status": response.status_code,
        "response": _truncate(text, _RESPONSE_LIMIT),
        "response_chars": len(text),
    }


def capture_call(
    job_id: str | None,
    request: httpx.Request,
    response: httpx.Response,
    *,
    cache_hit: bool = False,
    elapsed_ms: int = 0,
) -> None:
    """Record one LLM call (request parts + response) into the job's buffer."""
    if not job_id:
        return
    req = _parse_request(request)
    res = _parse_response(response)
    entry = {
        "ts": time.time(),
        "job_id": job_id,
        "cache_hit": cache_hit,
        "elapsed_ms": elapsed_ms,
        **req,
        **res,
    }
    _append_trace(job_id, entry)


def resolve_job_id() -> str | None:
    """Best-effort resolve the current thread's active job id.

    Marker's converter runs in a ThreadPoolExecutor worker thread, and the httpx
    patched send runs in that same thread. ``task_manager`` records the thread
    ident -> job_id mapping when a conversion starts, so we can attribute each
    LLM call to its job without threading a contextvar through marker.
    """
    try:
        from app.services.task_manager import active_conversion_threads
        return active_conversion_threads.get(threading.get_ident())
    except Exception:  # noqa: BLE001
        return None
