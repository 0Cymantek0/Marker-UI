"""PR79A continuation contract and cursor codec primitives."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.context_runtime.authorization import EffectiveAuthorization
from app.context_runtime.continuation import (
    CONTINUATION_SCHEMA_VERSION,
    CURSOR_REPLAY_FRESH,
    CURSOR_STATUS_ACTIVE,
    ContinuationContractError,
    ContinuationOutcome,
    CursorState,
    canonical_cursor_state_json,
    parse_cursor_state_json,
    validate_cursor_state_expiry,
)
from app.context_runtime.cursor import (
    CURSOR_TOKEN_VERSION,
    CursorCodec,
    CursorExpiredError,
    CursorIntegrityError,
    CursorKeyError,
    CursorKeyring,
    CursorMalformedError,
    CursorVersionError,
    new_cursor_handle,
    new_cursor_nonce,
    validate_cursor_expiry,
)
from app.context_runtime.continuation_paging import ContinuationPager
from app.context_runtime.continuation_state import (
    coerce_request,
    initial_budget,
    initial_keyset,
)
from app.context_runtime.contract import (
    QUERY_SCHEMA_VERSION,
    LexicalSearchOp,
    QueryBudget,
    QueryRequest,
    RecordGetOp,
)
from app.context_runtime.errors import QueryBudgetError
from app.kernel.publications import LexicalHit, LexicalSearchPage, PublishedRecord
from app.utils.canonical import payload_byte_hash


def _codec(*, current: str = "k1", old: bool = False) -> CursorCodec:
    keys = {current: (f"secret-{current}-".encode() + b"x" * 32)}
    if old:
        keys["k0"] = b"old-secret-" + b"x" * 32
    return CursorCodec(CursorKeyring(keys, current_key_id=current))


def _state(**overrides) -> CursorState:
    values = {
        "handle": "handle-opaque",
        "workspace_id": "workspace-a",
        "query": {"operations": [{"op": "lexical_search", "text": "private"}]},
        "snapshot": {"snapshot_id": "snapshot-private"},
        "publication": {"publication_set_id": "publication-private"},
        "authorization": {"policy_digest": "sha256:private", "deny_revision": 7},
        "keyset": {"last_rank": "opaque-rank", "last_record": "record-private"},
        "cumulative_budget": {"pages": 1, "candidates": 10},
        "page_count": 1,
        "expires_at": datetime(2030, 8, 18, 12, 5, tzinfo=timezone.utc),
        "pin_id": "pin-private",
        "status": CURSOR_STATUS_ACTIVE,
        "nonce": "nonce-opaque",
        "replay_state": CURSOR_REPLAY_FRESH,
    }
    values.update(overrides)
    return CursorState.model_validate(values)


def test_outcome_requires_cursor_only_for_partial_pages() -> None:
    complete = ContinuationOutcome(
        status="complete",
        packet={"evidence": []},
    )
    assert complete.complete and not complete.partial
    assert complete.next_cursor is None

    partial = ContinuationOutcome(
        status="partial",
        result={"evidence": [{"record_id": "r1"}]},
        next_cursor="opaque-token",
    )
    assert partial.partial and partial.continuation == "opaque-token"

    with pytest.raises(ValidationError, match="requires next_cursor"):
        ContinuationOutcome(status="partial", packet={})
    with pytest.raises(ValidationError, match="cannot carry next_cursor"):
        ContinuationOutcome(status="complete", packet={}, next_cursor="token")


def test_outcome_accepts_common_transport_aliases_but_stays_structured() -> None:
    outcome = ContinuationOutcome.model_validate(
        {"status": "partial", "value": {"count": 1}, "cursor": "token"}
    )
    assert outcome.result == {"count": 1}
    assert outcome.next_cursor == "token"
    assert outcome.schema_version == CONTINUATION_SCHEMA_VERSION


@pytest.mark.parametrize(
    "status",
    [
        "invalidated",
        "stale",
        "loop_limit",
        "policy_fail_closed",
        "execution_failure",
    ],
)
def test_terminal_outcomes_are_explicit_and_never_continue(status: str) -> None:
    outcome = ContinuationOutcome(
        status=status,
        reason="server-side terminal reason",
        error_code="terminal",
        packet={"evidence": []},
    )
    assert outcome.terminal
    assert outcome.next_cursor is None
    assert not outcome.partial
    with pytest.raises(ValidationError, match="cannot carry next_cursor"):
        ContinuationOutcome(status=status, next_cursor="must-not-continue")


def test_cursor_token_is_opaque_reference_only() -> None:
    codec = _codec()
    handle = new_cursor_handle()
    nonce = new_cursor_nonce()
    token = codec.issue(handle, nonce)
    decoded = codec.decode(token)

    assert decoded.version == CURSOR_TOKEN_VERSION
    assert decoded.handle == handle
    assert decoded.nonce == nonce
    assert "private" not in token
    assert "workspace" not in token
    assert "publication" not in token
    assert "policy_digest" not in token


def test_cursor_token_tampering_fails_closed() -> None:
    codec = _codec()
    token = codec.issue("handle-a", "nonce-a")
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[-1] ^= 1
    tampered = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(CursorIntegrityError):
        codec.decode(tampered)


def test_cursor_key_rotation_verifies_old_and_signs_new() -> None:
    old_key = b"old-secret-" + b"x" * 32
    new_key = b"new-secret-" + b"x" * 32
    old_codec = CursorCodec(CursorKeyring({"k0": old_key}, current_key_id="k0"))
    old_token = old_codec.issue("handle-old", "nonce-old")

    rotated = CursorCodec(
        CursorKeyring(
            {"k0": old_key, "k1": new_key}, current_key_id="k1"
        )
    )
    assert rotated.decode(old_token).key_id == "k0"
    assert rotated.decode(rotated.issue("handle-new", "nonce-new")).key_id == "k1"

    retired = _codec()
    with pytest.raises(CursorKeyError):
        retired.decode(old_token)


def test_cursor_keyring_rejects_weak_signing_material() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        CursorKeyring({"k1": b"too-short"}, current_key_id="k1")


@pytest.mark.parametrize(
    "token",
    ["", "not-base64!", "eA", "A" * 4096],
)
def test_cursor_malformed_tokens_rejected(token: str) -> None:
    with pytest.raises(CursorMalformedError):
        _codec().decode(token)


def test_cursor_version_rejected_before_signature_acceptance() -> None:
    codec = _codec()
    token = codec.issue("handle-version", "nonce-version")
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    payload, signature = raw[:-32], raw[-32:]
    claims = json.loads(payload)
    claims["version"] = CURSOR_TOKEN_VERSION + 1
    changed_payload = json.dumps(
        claims, separators=(",", ":"), sort_keys=True
    ).encode()
    changed = base64.urlsafe_b64encode(changed_payload + signature).decode().rstrip("=")
    with pytest.raises(CursorVersionError):
        codec.decode(changed)


def test_cursor_expiry_is_server_side_and_strict_at_boundary() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    expires = now + timedelta(seconds=1)
    validate_cursor_expiry(expires, now=now)
    with pytest.raises(CursorExpiredError):
        validate_cursor_expiry(expires, now=expires)
    with pytest.raises(CursorExpiredError):
        validate_cursor_expiry(expires, now=expires + timedelta(seconds=1))


def test_cursor_state_canonical_json_is_deterministic_and_round_trips() -> None:
    first = canonical_cursor_state_json(_state())
    second = canonical_cursor_state_json(
        _state(
            query={"operations": [{"text": "private", "op": "lexical_search"}]},
            authorization={"deny_revision": 7, "policy_digest": "sha256:private"},
        )
    )
    assert first == second
    parsed = parse_cursor_state_json(first)
    assert CursorState.model_validate(parsed).handle == "handle-opaque"
    with pytest.raises(ContinuationContractError, match="not canonical"):
        parse_cursor_state_json(json.dumps(parsed, indent=2))


def test_cursor_state_expiry_uses_row_state_not_token_claims() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    state = _state(expires_at=now + timedelta(seconds=1))
    validate_cursor_state_expiry(state, now=now)
    with pytest.raises(CursorExpiredError):
        validate_cursor_state_expiry(state, now=now + timedelta(seconds=1))


def test_coerce_request_revalidates_direct_query_budget() -> None:
    request = QueryRequest(
        schema_version=QUERY_SCHEMA_VERSION,
        workspace_id="ws-budget",
        operations=[
            RecordGetOp(op="record_get", record_id="record-1"),
            RecordGetOp(op="record_get", record_id="record-2"),
        ],
        budget=QueryBudget(max_operations=1),
    )

    with pytest.raises(QueryBudgetError, match="2 operations"):
        coerce_request(request)


class _DedupeReader:
    def __init__(self, hit: LexicalHit, record: PublishedRecord) -> None:
        self._hit = hit
        self._record = record

    def explain(self) -> dict[str, object]:
        return {
            "publication_set_id": "publication-1",
            "workspace_id": "ws-dedupe",
            "profile": "default",
            "kernel_commit_id": 1,
            "snapshot_id": "snapshot-1",
            "materialized_generation_id": "materialized-1",
            "lexical_generation_id": "lexical-1",
            "tokenizer": "unicode-v1",
            "vector_generation_id": None,
            "lexical_row_count": 1,
        }

    async def get_record(self, record_id: str) -> PublishedRecord | None:
        return self._record if record_id == self._record.record_id else None

    async def search_after(
        self,
        query: str,
        *,
        limit: int,
        after: object | None = None,
    ) -> LexicalSearchPage:
        if after is None:
            return LexicalSearchPage(
                hits=(self._hit,),
                next_after=None,
                has_more=False,
            )
        return LexicalSearchPage(hits=(), next_after=None, has_more=False)


@pytest.mark.asyncio
async def test_continuation_dedupes_lexical_and_record_get_across_pages() -> None:
    text_hash = payload_byte_hash(b"needle")
    record = PublishedRecord(
        record_id="view-1",
        workspace_id="ws-dedupe",
        kernel_commit_id=1,
        record_class="view_document",
        record_type="view",
        schema_version="view.v1",
        identity_hash="sha256:record-1",
        payload={"view_id": "view-1", "texts": {"n1": "needle"}},
        payload_byte_hash="sha256:payload-1",
    )
    hit = LexicalHit(
        publication_set_id="publication-1",
        lexical_generation_id="lexical-1",
        row_index=7,
        record_id="view-1",
        view_id="view-1",
        node_id="n1",
        revision_ref=record.identity_hash,
        text_hash=text_hash,
        rank=-1.0,
        text="needle",
    )
    request = QueryRequest(
        schema_version=QUERY_SCHEMA_VERSION,
        workspace_id="ws-dedupe",
        operations=[
            LexicalSearchOp(op="lexical_search", text="needle", limit=1),
            RecordGetOp(op="record_get", record_id="view-1", node_id="n1"),
        ],
        budget=QueryBudget(
            max_operations=2,
            max_candidates=4,
            max_evidence_units=4,
            max_output_chars=10_000,
        ),
    )
    auth = EffectiveAuthorization(
        profile="local_v1",
        assurance="standard",
        workspace_id="ws-dedupe",
        epoch_number=0,
        epoch_fingerprint=None,
        policy_digest="sha256:policy",
    )
    reader = _DedupeReader(hit, record)
    pager = ContinuationPager()

    first = await pager.run_async(
        reader,
        request,
        auth,
        initial_keyset(request),
        initial_budget(),
        page_size=1,
    )
    assert [(unit.locator.record_id, unit.locator.node_id) for unit in first.packet.evidence] == [
        ("view-1", "n1")
    ]
    assert first.packet.evidence[0].locator.row_index == 7
    assert first.more_work

    second = await pager.run_async(
        reader,
        request,
        auth,
        first.keyset,
        first.cumulative_budget,
        page_size=1,
    )
    assert second.packet.evidence == ()
    assert [omission.reason for omission in second.packet.omitted] == ["duplicate"]
    assert second.cumulative_budget["evidence_units"] == 1
    assert not second.more_work
