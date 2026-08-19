"""OpenRouter adapter boundary tests (matrix T) - no network, no credentials.

All transport interactions use an injected fake; the default urllib
transport is never exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.pr80b.llm import (
    CacheMissError,
    OpenRouterClient,
    envelope_to_output,
    parse_content,
)


def _ok_body(content: str, usage: dict | None = None) -> str:
    return json.dumps(
        {
            "model": "fake/model:free",
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )


VALID_CONTENT = json.dumps(
    {
        "invoice_number": "INV-1",
        "invoice_date": "2026-01-01",
        "currency": "USD",
        "po_number": None,
        "total_due": "30.00",
        "items": [
            {
                "sku": "SKU-A",
                "description": "Thing",
                "quantity": "2",
                "unit_price": "10.00",
                "amount": "20.00",
            }
        ],
        "flags": [],
    }
)


class FakeTransport:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, payload: dict) -> tuple[int, str]:
        self.calls.append(payload)
        action = self.responses.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class TestLivePath:
    def test_valid_response_parsed_with_usage(self, tmp_path):
        transport = FakeTransport([(200, _ok_body(VALID_CONTENT))])
        client = OpenRouterClient(
            ("m1",), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
        )
        envelope = client.extract("doc text")
        assert envelope["error"] is None
        assert envelope["model_served"] == "fake/model:free"
        assert envelope["usage"]["prompt_tokens"] == 10
        assert envelope["attempts"] == 1
        assert client.model_served == "m1"

    def test_fenced_json_content_parsed(self):
        fenced = "```json\n" + VALID_CONTENT + "\n```"
        envelope = {"content_raw": fenced}
        assert parse_content(envelope)["invoice_number"] == "INV-1"

    def test_429_then_200_retries(self, tmp_path):
        transport = FakeTransport(
            [(429, "rate limited"), (200, _ok_body(VALID_CONTENT))]
        )
        client = OpenRouterClient(
            ("m1",), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
        )
        envelope = client.extract("doc text")
        assert envelope["error"] is None
        assert envelope["attempts"] == 2
        assert len(transport.calls) == 2

    def test_401_fails_fast(self, tmp_path):
        transport = FakeTransport([(401, "unauthorized")] * 5)
        client = OpenRouterClient(
            ("m1",), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
        )
        envelope = client.extract("doc text")
        assert "HTTP 401" in envelope["error"]
        assert envelope["attempts"] == 1

    def test_transport_exception_retries_then_succeeds(self, tmp_path):
        transport = FakeTransport(
            [TimeoutError("boom"), (200, _ok_body(VALID_CONTENT))]
        )
        client = OpenRouterClient(
            ("m1",), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
        )
        envelope = client.extract("doc text")
        assert envelope["error"] is None

    def test_all_retries_exhausted_records_error(self, tmp_path):
        transport = FakeTransport([TimeoutError("boom")] * 6)
        client = OpenRouterClient(
            ("m1",), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
            max_retries=2,
        )
        envelope = client.extract("doc text")
        assert "transport error" in envelope["error"]
        assert envelope["attempts"] == 2

    def test_empty_content_retries_and_errors(self, tmp_path):
        transport = FakeTransport([(200, _ok_body("")), (200, _ok_body(""))])
        client = OpenRouterClient(
            ("m1",), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
            max_retries=2,
        )
        envelope = client.extract("doc text")
        assert envelope["error"] == "HTTP 200 with empty content"

    def test_model_chain_falls_back_to_second_model(self, tmp_path):
        transport = FakeTransport(
            [(429, "limited"), (429, "limited"), (429, "limited"),
             (429, "limited"), (429, "limited"), (200, _ok_body(VALID_CONTENT))]
        )
        client = OpenRouterClient(
            ("m1", "m2"), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
        )
        envelope = client.extract("doc text")
        assert envelope["error"] is None
        assert envelope["model_requested"] == "m2"
        assert client.model_served == "m2"

    def test_pinned_model_reused_for_subsequent_docs(self, tmp_path):
        transport = FakeTransport([(200, _ok_body(VALID_CONTENT))] * 3)
        client = OpenRouterClient(
            ("m1", "m2"), api_key="k", cache_path=tmp_path / "c.json",
            mode="live", transport=transport, sleep=lambda _: None,
        )
        client.extract("first")
        second = client.extract("second")
        assert second["model_requested"] == "m1"


class TestCache:
    def test_live_writes_replayable_cache(self, tmp_path):
        cache = tmp_path / "cache.json"
        transport = FakeTransport([(200, _ok_body(VALID_CONTENT))])
        live = OpenRouterClient(
            ("m1",), api_key="k", cache_path=cache, mode="live",
            transport=transport, sleep=lambda _: None,
        )
        envelope = live.extract("doc text")
        assert envelope["from_cache"] is False
        assert cache.is_file()
        replay = OpenRouterClient(
            ("m1",), api_key=None, cache_path=cache, mode="replay"
        )
        replayed = replay.extract("doc text")
        assert replayed["from_cache"] is True
        assert replayed["content_raw"] == envelope["content_raw"]

    def test_replay_miss_raises(self, tmp_path):
        client = OpenRouterClient(
            ("m1",), cache_path=tmp_path / "cache.json", mode="replay"
        )
        with pytest.raises(CacheMissError):
            client.extract("unrecorded")

    def test_auto_mode_uses_cache_before_network(self, tmp_path):
        cache = tmp_path / "cache.json"
        transport = FakeTransport([(200, _ok_body(VALID_CONTENT))])
        first = OpenRouterClient(
            ("m1",), api_key="k", cache_path=cache, mode="auto",
            transport=transport, sleep=lambda _: None,
        )
        first.extract("doc text")
        second = OpenRouterClient(
            ("m1",), api_key="k", cache_path=cache, mode="auto",
            transport=transport, sleep=lambda _: None,
        )
        assert second.extract("doc text")["from_cache"] is True
        assert len(transport.calls) == 1

    def test_corrupt_cache_version_rejected(self, tmp_path):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({"cache_schema_version": "bogus", "responses": {}}))
        with pytest.raises(ValueError, match="unsupported llm cache"):
            OpenRouterClient(("m1",), cache_path=cache, mode="replay")

    def test_cache_contains_no_api_key_material(self, tmp_path):
        cache = tmp_path / "cache.json"
        transport = FakeTransport([(200, _ok_body(VALID_CONTENT))])
        client = OpenRouterClient(
            ("m1",), api_key="sk-secret-value", cache_path=cache,
            mode="live", transport=transport, sleep=lambda _: None,
        )
        client.extract("doc text")
        assert "sk-secret-value" not in cache.read_text(encoding="utf-8")


class TestOutputMapping:
    def _envelope(self, content: str) -> dict:
        return {
            "model_requested": "m1",
            "model_served": "m1:free",
            "content_raw": content,
            "usage": None,
            "error": None,
        }

    def test_full_mapping(self):
        out = envelope_to_output(self._envelope(VALID_CONTENT), "doc-1")
        assert out.error is None
        assert out.system_id == "llm-openrouter:m1:free"
        assert out.fields["invoice_number"].value == "INV-1"
        assert out.fields["po_number"].status == "absent"
        assert out.fields["total_due"].value == "30.00"
        assert out.rows[0].sku == "SKU-A"
        assert out.rows[0].fields["quantity"].value == "2"
        assert out.invariant_findings is None

    def test_flags_map_to_flagged_conflict(self):
        payload = json.loads(VALID_CONTENT)
        payload["total_due"] = None
        payload["flags"] = ["total_due_conflict"]
        out = envelope_to_output(self._envelope(json.dumps(payload)), "doc-1")
        assert out.fields["total_due"].status == "flagged_conflict"
        assert out.fields["total_due"].self_flagged is True

    def test_row_conflict_flag_maps(self):
        payload = json.loads(VALID_CONTENT)
        payload["flags"] = ["items_SKU-A_conflict"]
        payload["items"][0]["amount"] = None
        out = envelope_to_output(self._envelope(json.dumps(payload)), "doc-1")
        row = out.rows[0]
        assert row.status == "flagged_conflict"
        assert row.fields["amount"].status == "flagged_conflict"

    def test_unparseable_content_becomes_error_output(self):
        out = envelope_to_output(self._envelope("not json at all"), "doc-1")
        assert out.error == "unparseable model content"
        assert out.fields == {} and out.rows == ()

    def test_null_items_member_maps_to_absent(self):
        payload = json.loads(VALID_CONTENT)
        payload["items"][0]["unit_price"] = None
        out = envelope_to_output(self._envelope(json.dumps(payload)), "doc-1")
        assert out.rows[0].fields["unit_price"].status == "absent"

    def test_extra_fields_in_model_output_ignored(self):
        payload = json.loads(VALID_CONTENT)
        payload["mystery"] = "x"
        out = envelope_to_output(self._envelope(json.dumps(payload)), "doc-1")
        assert set(out.fields) == {
            "invoice_number", "invoice_date", "currency", "po_number", "total_due"
        }

    def test_error_envelope_maps_to_error_output(self):
        envelope = {
            "model_requested": "m1",
            "model_served": None,
            "content_raw": None,
            "usage": None,
            "error": "HTTP 402: payment required",
        }
        out = envelope_to_output(envelope, "doc-1")
        assert out.error == "HTTP 402: payment required"

    def test_raw_envelope_preserved_without_content(self):
        out = envelope_to_output(self._envelope(VALID_CONTENT), "doc-1")
        assert "content_raw" not in out.raw["envelope"]
        assert out.raw["envelope"]["model_served"] == "m1:free"
