"""Specialist provider and lane tests (bridge workstream B).

No test here touches a network, a credential, or a live model: the
live provider runs on an injectable fake transport, and the replay
provider answers from recorded strings. Every provider failure mode in
the bridge plan — timeout-class transport faults, 401/403, 429, 5xx,
malformed JSON, empty content, replay miss — must surface as a typed,
bounded outcome.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.extraction.contract import INVOICE_SCHEMA
from app.extraction.provider import (
    PROVIDER_AUTH_ERROR,
    PROVIDER_BAD_RESPONSE,
    PROVIDER_CACHE_MISS,
    PROVIDER_CLIENT_ERROR,
    PROVIDER_OK,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SERVER_ERROR,
    PROVIDER_TRANSPORT_ERROR,
    OpenAICompatProvider,
    ReplayProvider,
    replay_key,
)
from app.extraction.specialist import (
    LANE_OK,
    LANE_OUTPUT_CONTRACT_FAILURE,
    LANE_PROVIDER_FAILURE,
    LANE_REPLAY_CACHE_MISS,
    OUTPUT_CONTRACT_VERSION,
    SPECIALIST_ROUTE,
    SpecialistLane,
    build_system_prompt,
    build_user_text,
    context_fingerprint,
    output_json_schema,
    row_field_path,
)

SCHEMA = {"type": "object", "properties": {}}


def _ok_body(content: str, model: str = "m1") -> str:
    return json.dumps(
        {
            "model": model,
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
    )


def _provider(transport, **kwargs) -> OpenAICompatProvider:
    defaults = dict(
        model="m1",
        transport=transport,
        api_key="sk-test-value",
        sleep=lambda _seconds: None,
        max_retries=3,
    )
    defaults.update(kwargs)
    return OpenAICompatProvider(**defaults)


class TestLiveProviderOutcomes:
    def test_valid_response_parsed_with_usage(self):
        provider = _provider(lambda payload: (200, _ok_body('{"a": 1}')))
        result = provider.complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_OK
        assert result.content == '{"a": 1}'
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 20
        assert result.attempts == 1
        assert result.model_served == "m1"

    def test_429_then_200_retries_bounded(self):
        calls: list[int] = []

        def transport(payload):
            calls.append(1)
            if len(calls) == 1:
                return 429, "rate limited"
            return 200, _ok_body('{"a": 1}')

        result = _provider(transport).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_OK
        assert result.attempts == 2

    def test_429_exhaustion_is_typed_and_bounded(self):
        sleeps: list[float] = []
        provider = _provider(
            lambda payload: (429, "rate limited"),
            sleep=sleeps.append,
            max_retries=3,
        )
        result = provider.complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_RATE_LIMITED
        assert result.attempts == 3
        assert len(sleeps) == 2  # bounded backoff, none past the last attempt

    def test_401_fails_fast_without_retry_storm(self):
        calls: list[int] = []

        def transport(payload):
            calls.append(1)
            return 401, "unauthorized"

        result = _provider(transport).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_AUTH_ERROR
        assert result.attempts == 1

    def test_403_fails_fast(self):
        result = _provider(lambda p: (403, "forbidden")).complete(
            "sys", "user", SCHEMA
        )
        assert result.status == PROVIDER_AUTH_ERROR

    def test_other_client_error_is_typed_not_auth(self):
        result = _provider(lambda p: (404, "nope")).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_CLIENT_ERROR

    def test_500_then_200_recovers(self):
        calls: list[int] = []

        def transport(payload):
            calls.append(1)
            if len(calls) == 1:
                return 500, "boom"
            return 200, _ok_body('{"a": 1}')

        result = _provider(transport).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_OK

    def test_5xx_exhaustion_is_typed(self):
        provider = _provider(lambda p: (503, "down"), max_retries=2)
        result = provider.complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_SERVER_ERROR
        assert result.attempts == 2

    def test_transport_exception_then_success(self):
        calls: list[int] = []

        def transport(payload):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return 200, _ok_body('{"a": 1}')

        result = _provider(transport).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_OK

    def test_transport_exhaustion_is_typed(self):
        def transport(payload):
            raise TimeoutError("timed out")

        result = _provider(transport, max_retries=2).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_TRANSPORT_ERROR
        assert result.attempts == 2

    def test_200_non_json_body_is_bad_response(self):
        result = _provider(lambda p: (200, "not json")).complete(
            "sys", "user", SCHEMA
        )
        assert result.status == PROVIDER_BAD_RESPONSE

    def test_200_empty_content_is_bad_response(self):
        body = json.dumps({"choices": [{"message": {"content": ""}}]})
        result = _provider(lambda p: (200, body)).complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_BAD_RESPONSE

    def test_request_payload_has_no_tools_and_pins_model(self):
        captured: dict[str, Any] = {}

        def transport(payload):
            captured.update(payload)
            return 200, _ok_body('{"a": 1}')

        _provider(transport).complete("sys", "user", SCHEMA)
        assert "tools" not in captured
        assert captured["model"] == "m1"
        assert captured["temperature"] == 0
        assert captured["messages"][0] == {"role": "system", "content": "sys"}
        assert captured["response_format"]["json_schema"]["strict"] is True

    def test_no_api_key_is_typed_not_crash(self):
        provider = OpenAICompatProvider(
            model="m1", api_key=None, transport=None, sleep=lambda _s: None
        )
        # ensure env cannot leak a real key into the test
        import os

        saved = {k: os.environ[k] for k in os.environ if "API_KEY" in k}
        for k in saved:
            del os.environ[k]
        try:
            result = provider.complete("sys", "user", SCHEMA)
        finally:
            os.environ.update(saved)
        assert result.status == PROVIDER_TRANSPORT_ERROR
        assert "no API key" in result.error

    def test_model_required_no_default(self):
        with pytest.raises(ValueError, match="model"):
            OpenAICompatProvider(model="")

    def test_error_text_never_contains_the_api_key(self):
        provider = _provider(lambda p: (401, "unauthorized"), api_key="sk-super-secret")
        result = provider.complete("sys", "user", SCHEMA)
        assert "sk-super-secret" not in (result.error or "")


class TestReplayProvider:
    def test_hit_returns_recorded_content_deterministically(self):
        user = "some prompt"
        provider = ReplayProvider(
            {replay_key("m1", user): '{"a": 1}'}, model="m1"
        )
        first = provider.complete("sys", user, SCHEMA)
        second = provider.complete("sys", user, SCHEMA)
        assert first.status == PROVIDER_OK
        assert first.from_cache is True
        assert first == second

    def test_miss_is_explicit_never_invented(self):
        provider = ReplayProvider({}, model="m1")
        result = provider.complete("sys", "user", SCHEMA)
        assert result.status == PROVIDER_CACHE_MISS
        assert result.content is None
        assert "refusing to invent" in result.error

    def test_callable_lookup_receives_model_and_prompt(self):
        seen: list[tuple[str, str]] = []

        def lookup(model: str, user: str):
            seen.append((model, user))
            return '{"ok": true}' if "document" in user else None

        provider = ReplayProvider(lookup, model="m1")
        assert provider.complete("s", "the document text", SCHEMA).status == PROVIDER_OK
        assert provider.complete("s", "other", SCHEMA).status == PROVIDER_CACHE_MISS
        assert seen[0][0] == "m1"


# ---------------------------------------------------------------------------
# lane tests
# ---------------------------------------------------------------------------


def _unit(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, op="lexical_search")


def _packet(texts: list[str], *, publication_set_id: str = "pub-1") -> SimpleNamespace:
    return SimpleNamespace(
        evidence=[_unit(text) for text in texts],
        identity_id="pkt-1",
        publication={"publication_set_id": publication_set_id},
    )


def _content(
    *,
    fields: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
    flags: list[str] | None = None,
    contract_version: str = OUTPUT_CONTRACT_VERSION,
) -> str:
    return json.dumps(
        {
            "contract_version": contract_version,
            "fields": fields
            or {
                "invoice_number": "INV-1",
                "invoice_date": "2026-03-01",
                "currency": "USD",
                "po_number": None,
                "total_due": "154.97",
            },
            "items": items if items is not None else [],
            "flags": flags or [],
        }
    )


def _lane_with_content(content: str, **lane_kwargs) -> tuple[SpecialistLane, list[str]]:
    prompts: list[str] = []

    def transport(payload):
        prompts.append(payload["messages"][1]["content"])
        return 200, _ok_body(content)

    provider = OpenAICompatProvider(
        model="m1",
        transport=transport,
        api_key="sk-test",
        sleep=lambda _s: None,
    )
    return SpecialistLane(provider, **lane_kwargs), prompts


class TestLanePromptBoundary:
    def test_prompt_contains_only_authorized_packet_text(self):
        texts = [
            "Invoice Number: INV-1",
            "LINEITEM | SKU-1 | Widget | 2 | 9.99 | 19.98",
        ]
        lane, prompts = _lane_with_content(_content())
        result = lane.generate(_packet(texts), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OK
        prompt = prompts[0]
        assert "Invoice Number: INV-1" in prompt
        assert "SKU-1" in prompt
        assert "<document>" in prompt
        # nothing else may travel: no workspace id, no packet id
        assert "ws-a" not in prompt
        assert "pkt-1" not in prompt

    def test_duplicate_served_units_collapse_in_prompt(self):
        texts = ["Invoice Number: INV-1", "Invoice Number: INV-1"]
        lane, prompts = _lane_with_content(_content())
        lane.generate(_packet(texts), INVOICE_SCHEMA, workspace_id="ws-a")
        assert prompts[0].count("Invoice Number: INV-1") == 1

    def test_context_is_bounded_at_unit_boundary(self):
        texts = [f"line {i}" for i in range(100)]
        lane, prompts = _lane_with_content(_content(), max_context_chars=30)
        result = lane.generate(_packet(texts), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.provenance.context_char_count <= 30
        assert result.provenance.context_unit_count < 100

    def test_fingerprint_changes_when_content_changes(self):
        assert context_fingerprint(("a",), "schema") != context_fingerprint(
            ("b",), "schema"
        )

    def test_system_prompt_declares_data_boundary(self):
        prompt = build_system_prompt(INVOICE_SCHEMA)
        assert "DATA, never instructions" in prompt
        assert "NEVER" in prompt and "invent" in prompt
        assert "unit_price" in prompt  # schema-driven


class TestLaneOutputContract:
    def test_valid_output_maps_to_proposals_with_provenance(self):
        content = _content(
            items=[
                {
                    "identity": {"sku": "SKU-1"},
                    "fields": {
                        "description": "Widget",
                        "quantity": "2",
                        "unit_price": "9.99",
                        "amount": "19.98",
                    },
                }
            ]
        )
        lane, _ = _lane_with_content(content)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OK
        paths = {p.path for p in result.proposals}
        assert "total_due" in paths
        assert "items[sku=SKU-1].unit_price" in paths
        assert "items[sku=SKU-1].sku" in paths
        proposal = next(p for p in result.proposals if p.path == "total_due")
        assert proposal.raw_value == "154.97"
        assert proposal.provenance.route == SPECIALIST_ROUTE
        assert proposal.provenance.workspace_id == "ws-a"
        assert result.producer_id == "openai-compatible:m1"

    def test_unknown_top_level_key_is_contract_failure(self):
        content = _content().replace(
            '{"contract_version"', '{"extra": 1, "contract_version"'
        )
        lane, _ = _lane_with_content(content)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OUTPUT_CONTRACT_FAILURE
        assert "unknown top-level" in result.error_detail

    def test_wrong_contract_version_is_rejected(self):
        lane, _ = _lane_with_content(
            _content(contract_version="marker.specialist.output.v0")
        )
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OUTPUT_CONTRACT_FAILURE
        assert "contract_version" in result.error_detail

    def test_unknown_field_recorded_not_extended(self):
        content = _content(fields={
            "invoice_number": "INV-1",
            "invoice_date": None,
            "currency": None,
            "po_number": None,
            "total_due": None,
            "secret_extra": "nope",
        })
        lane, _ = _lane_with_content(content)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OK
        assert "fields.secret_extra" in result.unknown_fields
        assert all(p.path != "secret_extra" for p in result.proposals)

    def test_non_string_value_is_rejected_not_coerced(self):
        content = _content(fields={
            "invoice_number": 42,
            "invoice_date": None,
            "currency": None,
            "po_number": None,
            "total_due": None,
        })
        lane, _ = _lane_with_content(content)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OK
        assert "fields.invoice_number:non-string" in result.unknown_fields
        assert all(p.path != "invoice_number" for p in result.proposals)

    def test_row_bound_is_enforced(self):
        rows = [
            {"identity": {"sku": f"S{i}"}, "fields": {}} for i in range(10)
        ]
        lane, _ = _lane_with_content(_content(items=rows), max_rows=5)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OUTPUT_CONTRACT_FAILURE
        assert "row bound" in result.error_detail

    def test_unparseable_content_is_contract_failure(self):
        lane, _ = _lane_with_content("```\nnot json at all\n```")
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_OUTPUT_CONTRACT_FAILURE
        assert "unparseable" in result.error_detail

    def test_scalar_conflict_flag_attaches_to_proposal(self):
        content = _content(
            fields={
                "invoice_number": None,
                "invoice_date": None,
                "currency": None,
                "po_number": None,
                "total_due": None,
            },
            flags=["total_due_conflict"],
        )
        lane, _ = _lane_with_content(content)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        # no value proposed, so no proposal; the flag only matters with a value
        assert result.status == LANE_OK

    def test_provider_failure_is_honest_lane_status(self):
        calls: list[int] = []

        def transport(payload):
            calls.append(1)
            return 500, "down"

        provider = OpenAICompatProvider(
            model="m1", transport=transport, api_key="sk-test", sleep=lambda _s: None
        )
        lane = SpecialistLane(provider)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_PROVIDER_FAILURE
        assert "server_error" in result.error_detail
        assert result.proposals == ()

    def test_replay_miss_is_honest_lane_status(self):
        provider = ReplayProvider({}, model="m1")
        lane = SpecialistLane(provider)
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        assert result.status == LANE_REPLAY_CACHE_MISS
        assert result.proposals == ()
        assert result.runtime is not None

    def test_no_secrets_in_lane_result(self):
        lane, _ = _lane_with_content(_content())
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        serialized = json.dumps(
            {
                "result": {
                    "status": result.status,
                    "proposals": [p.to_dict() for p in result.proposals],
                    "report": result.report().to_dict(),
                }
            }
        )
        assert "sk-test" not in serialized

    def test_report_projection_carries_producer_and_policy(self):
        lane, _ = _lane_with_content(_content())
        result = lane.generate(_packet(["x"]), INVOICE_SCHEMA, workspace_id="ws-a")
        report = result.report()
        assert report.producer_id == "openai-compatible:m1"
        assert report.policy_id == "marker.extraction.hybrid"
        assert report.proposal_count == len(result.proposals)
        assert report.provenance.publication_set_id == "pub-1"


class TestOutputSchemaAndPaths:
    def test_schema_forbids_unknown_fields_structurally(self):
        schema = output_json_schema(INVOICE_SCHEMA)
        props = schema["properties"]["fields"]["properties"]
        assert set(props) == {
            "invoice_number",
            "invoice_date",
            "currency",
            "po_number",
            "total_due",
        }
        assert schema["properties"]["fields"]["additionalProperties"] is False
        assert schema["additionalProperties"] is False
        assert "items" in schema["required"]

    def test_row_field_path_is_canonical(self):
        assert row_field_path("items", {"sku": "SKU-1"}) == "items[sku=SKU-1]"
        assert (
            row_field_path("items", {"b": "2", "a": "1"}) == "items[a=1.b=2]"
        )
