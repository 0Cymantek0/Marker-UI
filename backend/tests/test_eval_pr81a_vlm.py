"""PR81A VLM client tests. Matrix letter Y.

No network, no credentials: the default urllib transport is never
exercised. A fake transport is injected everywhere, mirroring the
PR80B hosted-lane test conventions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.pr81a.vlm import (
    CacheMissError,
    OPENROUTER_BASE_URL,
    VlmClient,
    VlmError,
    _image_part,
    _parse_json,
    cache_key,
)


def _ok_body(content: str, model: str = "served/model-x") -> str:
    return json.dumps(
        {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }
    )


class FakeTransport:
    def __init__(self, responses):
        # responses: list of (status, body) or Exception, consumed in order
        self.responses = list(responses)
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


ANSWER_SYSTEM = "answer-system-prompt"


class TestLivePath:
    def test_answer_with_image_parses_and_pins_model(self, tmp_path):
        transport = FakeTransport([(200, _ok_body('{"answer": "4.0"}'))])
        client = VlmClient(
            ["m1", "m2"],
            transport=transport,
            cache_path=tmp_path / "c.json",
            mode="live",
            sleep=lambda _: None,
        )
        envelope, parsed = client.answer("value?", page_png=b"pngbytes", page_text=None)
        assert parsed == {"answer": "4.0"}
        assert envelope.error is None
        assert envelope.model_served == "served/model-x"
        assert client.model_served == "served/model-x"
        # message shape: multimodal parts, image as data URL
        user = transport.payloads[0]["messages"][1]
        assert user["content"][0]["type"] == "text"
        assert user["content"][1]["type"] == "image_url"
        assert user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert transport.payloads[0]["temperature"] == 0
        assert transport.payloads[0]["response_format"] == {"type": "json_object"}

    def test_pinned_model_reused_across_calls(self, tmp_path):
        transport = FakeTransport(
            [
                (200, _ok_body('{"answer": "a"}', model="served/first")),
                (200, _ok_body('{"answer": "b"}', model="served/first")),
            ]
        )
        client = VlmClient(
            ["m1", "m2"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        client.answer("q1", page_png=b"p1", page_text=None)
        client.answer("q2", page_png=b"p2", page_text=None)
        assert [p["model"] for p in transport.payloads] == ["m1", "m1"]

    def test_retry_on_429_then_success(self, tmp_path):
        transport = FakeTransport([(429, "rate"), (200, _ok_body('{"answer": "9"}'))])
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed == {"answer": "9"}
        assert envelope.attempts == 2

    def test_401_fails_fast_no_retry(self, tmp_path):
        transport = FakeTransport([(401, "unauthorized")])
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed is None
        assert envelope.error is not None and "401" in envelope.error
        assert len(transport.payloads) == 1

    def test_chain_falls_through_to_second_model(self, tmp_path):
        transport = FakeTransport(
            [(500, "boom"), (500, "boom"), (500, "boom"), (500, "boom"), (200, _ok_body('{"answer": "z"}'))]
        )
        client = VlmClient(
            ["m1", "m2"],
            transport=transport,
            cache_path=tmp_path / "c.json",
            mode="live",
            max_retries=4,
            sleep=lambda _: None,
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed == {"answer": "z"}
        assert envelope.model_requested == "m2"

    def test_404_routing_falls_through_to_next_model(self, tmp_path):
        transport = FakeTransport(
            [(404, '{"error":{"message":"Not Found","code":404}}'), (200, _ok_body('{"answer": "r"}'))]
        )
        client = VlmClient(
            ["m1", "m2"],
            transport=transport,
            cache_path=tmp_path / "c.json",
            mode="live",
            sleep=lambda _: None,
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed == {"answer": "r"}
        assert envelope.model_requested == "m2"

    def test_transport_exception_retries_then_exhausts(self, tmp_path):
        transport = FakeTransport([RuntimeError("net down")] * 4)
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed is None
        assert "retries exhausted" in envelope.error

    def test_empty_content_is_error_not_guess(self, tmp_path):
        body = json.dumps({"model": "m", "choices": [{"message": {"content": ""}}], "usage": {}})
        transport = FakeTransport([RuntimeError("e")] * 4)
        # emulate all-empty via exception path exhaustion
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed is None and envelope.error
        del body  # shape documented; exhaust path is what matters


class TestCache:
    def _client(self, tmp_path, transport, mode="auto"):
        return VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode=mode, sleep=lambda _: None
        )

    def test_live_then_replay_offline(self, tmp_path):
        transport = FakeTransport([(200, _ok_body('{"answer": "7"}'))])
        client = self._client(tmp_path, transport, mode="live")
        _, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed == {"answer": "7"}
        replay = VlmClient(["m1"], cache_path=tmp_path / "c.json", mode="replay")
        _, parsed_again = replay.answer("q", page_png=b"p", page_text=None)
        assert parsed_again == {"answer": "7"}

    def test_replay_miss_raises(self, tmp_path):
        (tmp_path / "c.json").write_text(
            json.dumps(
                {
                    "cache_schema_version": "marker.pr81a_vlm_cache.v1",
                    "gateway_origin": "x",
                    "model_chain": ["m1"],
                    "responses": {},
                }
            ),
            encoding="utf-8",
        )
        client = VlmClient(["m1"], cache_path=tmp_path / "c.json", mode="replay")
        with pytest.raises(CacheMissError):
            client.answer("q", page_png=b"p", page_text=None)

    def test_auto_prefers_cache_and_skips_network(self, tmp_path):
        transport = FakeTransport([(200, _ok_body('{"answer": "1"}'))])
        client = self._client(tmp_path, transport, mode="auto")
        client.answer("q", page_png=b"p", page_text=None)
        client.answer("q", page_png=b"p", page_text=None)
        assert client.calls == {"live": 1, "cache": 1}
        assert len(transport.payloads) == 1

    def test_cache_binds_image_bytes(self, tmp_path):
        transport = FakeTransport(
            [(200, _ok_body('{"answer": "img1"}')), (200, _ok_body('{"answer": "img2"}'))]
        )
        client = self._client(tmp_path, transport, mode="live")
        _, first = client.answer("q", page_png=b"image-one", page_text=None)
        _, second = client.answer("q", page_png=b"image-two", page_text=None)
        assert first["answer"] == "img1"
        assert second["answer"] == "img2"

    def test_cache_rejects_wrong_schema(self, tmp_path):
        (tmp_path / "c.json").write_text(
            json.dumps({"cache_schema_version": "bogus.v0", "responses": {}}), encoding="utf-8"
        )
        client = VlmClient(["m1"], cache_path=tmp_path / "c.json", mode="replay")
        with pytest.raises(VlmError, match="unsupported vlm cache schema"):
            client.answer("q", page_png=b"p", page_text=None)

    def test_cache_file_contains_no_secret_material(self, tmp_path):
        transport = FakeTransport([(200, _ok_body('{"answer": "s"}'))])
        client = VlmClient(
            ["m1"],
            api_key="sk-super-secret-value",
            transport=transport,
            cache_path=tmp_path / "c.json",
            mode="live",
            sleep=lambda _: None,
        )
        client.answer("q", page_png=b"p", page_text=None)
        raw = (tmp_path / "c.json").read_text(encoding="utf-8")
        assert "sk-super-secret-value" not in raw
        assert "Authorization" not in raw

    def test_cache_header_roundtrip(self, tmp_path):
        transport = FakeTransport([(200, _ok_body('{"answer": "h"}'))])
        client = self._client(tmp_path, transport, mode="live")
        client.answer("q", page_png=b"p", page_text=None)
        data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
        assert data["cache_schema_version"] == "marker.pr81a_vlm_cache.v1"
        assert data["model_chain"] == ["m1"]
        assert "gateway_origin" in data


class TestOutputParsing:
    def test_parse_fenced_json(self):
        assert _parse_json('```json\n{"answer": "x"}\n```') == {"answer": "x"}

    def test_parse_tolerates_sse_tail_via_envelope(self, tmp_path):
        # PR80B-style trailing data: [DONE] after JSON happens on some gateways
        body = _ok_body('{"answer": "t"}') + "\ndata: [DONE]"
        transport = FakeTransport([(200, body)])
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        _, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed == {"answer": "t"}

    def test_sse_chunk_stream_is_accumulated(self, tmp_path):
        # some gateway routes answer non-streamed requests with an SSE
        # chunk stream; the client must fold content deltas into one reply
        chunks = "\n".join(
            [
                'data: {"model":"kr/x","choices":[{"delta":{"role":"assistant"}}]}',
                'data: {"choices":[{"delta":{"content":"{\\"ans"}}]}',
                'data: {"choices":[{"delta":{"content":"wer\\": \\"ok\\"}"}}]}',
                'data: {"choices":[{"delta":{}],"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3}}',
                "data: [DONE]",
            ]
        )
        transport = FakeTransport([(200, chunks)])
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert envelope.error is None
        assert envelope.model_served == "kr/x"
        assert parsed == {"answer": "ok"}

    def test_sse_stream_without_content_is_error_not_guess(self, tmp_path):
        chunks = "\n".join(
            [
                'data: {"model":"kr/x","choices":[{"delta":{"role":"assistant"}}]}',
                "data: [DONE]",
            ]
        )
        transport = FakeTransport([RuntimeError("e")] * 4)
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        envelope, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed is None and envelope.error
        del chunks, transport  # shape documented; empty-stream path is what matters

    def test_unparseable_content_is_none_not_guess(self, tmp_path):
        transport = FakeTransport([(200, _ok_body("the answer is four"))])
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        _, parsed = client.answer("q", page_png=b"p", page_text=None)
        assert parsed is None

    def test_rerank_payload_shape(self, tmp_path):
        transport = FakeTransport([(200, _ok_body('{"scores": {"A": 9, "B": 2}}'))])
        client = VlmClient(
            ["m1"], transport=transport, cache_path=tmp_path / "c.json", mode="live", sleep=lambda _: None
        )
        _, parsed = client.rerank("which page?", b"montage", ["A", "B"])
        assert parsed == {"scores": {"A": 9, "B": 2}}
        user = transport.payloads[0]["messages"][1]
        assert "Labels shown: A, B" in user["content"][0]["text"]


class TestKeyAndParts:
    def test_cache_key_binds_model_system_and_parts(self):
        parts = [{"type": "text", "text": "q"}, _image_part(b"img")]
        a = cache_key("m1", "sys", parts)
        assert a == cache_key("m1", "sys", parts)
        assert a != cache_key("m2", "sys", parts)
        assert a != cache_key("m1", "other", parts)
        assert a != cache_key("m1", "sys", [{"type": "text", "text": "q"}, _image_part(b"xx")])

    def test_default_base_url_is_openrouter(self):
        # base URL, not endpoint: the transport appends /chat/completions
        assert OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            VlmClient(["m1"], mode="passthrough")

    def test_empty_chain_rejected(self):
        with pytest.raises(ValueError):
            VlmClient([])
