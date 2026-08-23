"""EvidencePacket reuse invalidation across citation, renderer, and
tokenizer semantics (PR86 / readiness invariant 53).

Adversarial matrix over the reuse contract: every semantic dimension
that can change the legally visible evidence or its caller-visible
citation/render interpretation rotates reuse identity; runtime noise and
semantically equivalent spellings do not; a live continuation chain
never silently crosses a representation rotation; and disclosed
historical truth stays immutable across rotations. Tokenizer rotation is
proven at its real source: lexical generation identity binds the
tokenizer, the supported set is backend-pinned, and an unsupported
rotation attempt fails closed instead of silently reindexing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

import app.context_runtime.packets as packets_module
from app.answer_evidence.domain import EvidenceRef
from app.answer_evidence.errors import AnswerEvidenceContractError
from app.agent_answer_evidence import (
    configure_answer_evidence_runtime,
    read_agent_answer_trace,
    record_agent_answer_assessment,
    record_agent_answer_trace,
    reset_answer_evidence_runtime,
)
from app.agent_query import (
    configure_query_runtime,
    reset_query_runtime,
    run_agent_query,
)
from app.context_runtime import (
    CITATION_LOCATOR_FIELDS,
    QUERY_SCHEMA_VERSION,
    ContinuationService,
    CursorCodec,
    CursorKeyring,
    execute_query,
    parse_query_request,
    representation_semantics,
)
from app.context_runtime.continuation_state import publication_matches
from app.context_runtime.errors import QueryContractError
from app.context_runtime.packets import citation_view
from app.kernel.errors import KernelError
from app.kernel.generations import GenerationService
from app.kernel.lexical import supported_tokenizers
from app.kernel.models import KernelContextDisclosure, KernelQueryCursor
from app.kernel.publications import (
    PublicationService,
    compute_lexical_identity,
)
from app.kernel.snapshots import resolve_snapshot
from tests.test_context_runtime_service import _publish
from tests.test_kernel_publication import _db_path, _fresh_factory

pytestmark = pytest.mark.asyncio

_REPR_CHANGED = "representation_changed"


def _codec() -> CursorCodec:
    return CursorCodec(
        CursorKeyring({"k1": b"repr-test-key--" + b"x" * 32}, current_key_id="k1")
    )


def _request(workspace: str = "ws-repr", **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": [{"op": "lexical_search", "text": "needle", "limit": 25}],
    }
    base.update(overrides)
    return base


async def _query(factory, workspace: str = "ws-repr"):
    return await execute_query(factory, parse_query_request(_request(workspace)))


# ---------------------------------------------------------------------------
# RI-15 / RI-19: server ownership and determinism of representation semantics
# ---------------------------------------------------------------------------


def test_representation_semantics_are_server_owned_and_deterministic() -> None:
    first = representation_semantics()
    second = representation_semantics()
    assert first == second
    # Exact content documents the binding: packet schema, citation locator
    # scheme, identity framing, canonicalization. No timestamps, no build
    # noise, nothing request-derived.
    assert first == {
        "packet_schema": "marker.evidence_packet.v1",
        "citation_locator_fields": ["node_id", "record_id", "view_id"],
        "identity_framing": {
            "record_type": "marker.context_runtime.evidence_packet",
            "schema_version": "2.0.0",
        },
        "canonicalization": "marker.record_identity.v1",
    }


def test_normalized_query_carries_no_representation_dimensions() -> None:
    # Legacy/default marker.query.v1 requests keep deterministic v1
    # semantics: representation state is server-derived and never a
    # request field, so no caller spelling can pin or forge it.
    parsed = parse_query_request(_request())
    normalized = parse_query_request(_request()).model_dump()
    assert "representation" not in normalized
    assert set(normalized["context"]) == {
        "security_context_id",
        "verifier_policy_id",
        "redaction_profile_id",
        "serialization_profile",
    }
    assert parsed.context.serialization_profile == "default"


# ---------------------------------------------------------------------------
# RI-01 / RI-07: citation semantics rotate reuse identity
# ---------------------------------------------------------------------------


async def test_identity_stable_then_rotates_with_citation_scheme(
    payload_env: tuple, monkeypatch
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-repr")

    base = await _query(factory)
    repeat = await _query(factory)
    # RI-01 negative control: unchanged semantics reuse identically.
    assert base.identity_id == repeat.identity_id
    assert "representation" in base.identity_dimensions

    # A deployed citation scheme that cites fewer fields is a different
    # citation semantics: reuse identity must rotate.
    monkeypatch.setattr(
        packets_module, "CITATION_LOCATOR_FIELDS", ("record_id", "view_id")
    )
    reduced = await _query(factory)
    assert reduced.identity_id != base.identity_id

    # A scheme that additionally cites revision_ref is a third semantics.
    monkeypatch.setattr(
        packets_module,
        "CITATION_LOCATOR_FIELDS",
        ("record_id", "view_id", "node_id", "revision_ref"),
    )
    extended = await _query(factory)
    assert extended.identity_id != base.identity_id
    assert extended.identity_id != reduced.identity_id


async def test_citation_scheme_field_order_is_not_identity_noise(
    payload_env: tuple, monkeypatch
) -> None:
    # Reordering the field tuple is a semantic no-op (the scheme is a
    # field set): identity must not churn on it.
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-repr")
    base = await _query(factory)
    monkeypatch.setattr(
        packets_module, "CITATION_LOCATOR_FIELDS", tuple(reversed(CITATION_LOCATOR_FIELDS))
    )
    reordered = await _query(factory)
    assert reordered.identity_id == base.identity_id


# ---------------------------------------------------------------------------
# RI-08 / RI-16: renderer semantics rotate reuse identity, across restarts
# ---------------------------------------------------------------------------


async def test_identity_rotates_with_renderer_schema_and_framing(
    payload_env: tuple, monkeypatch
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-repr")
    base = await _query(factory)

    monkeypatch.setattr(
        packets_module, "EVIDENCE_PACKET_SCHEMA_VERSION", "marker.evidence_packet.v2"
    )
    rotated = await _query(factory)
    assert rotated.identity_id != base.identity_id
    assert rotated.schema_version == "marker.evidence_packet.v2"
    # Same evidence truth, different representation identity: content
    # selection is untouched by the rotation.
    assert [u.locator.node_id for u in rotated.evidence] == [
        u.locator.node_id for u in base.evidence
    ]

    monkeypatch.undo()
    monkeypatch.setattr(packets_module, "_PACKET_ID_SCHEMA_VERSION", "3.0.0")
    reframed = await _query(factory)
    assert reframed.identity_id != base.identity_id


async def test_renderer_rotation_across_restart_changes_reusable_variant(
    payload_env: tuple, monkeypatch
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-repr")
    before = await _query(factory)

    # Restart over unchanged committed and deployed state reproduces the
    # identity (RI-15)...
    fresh = _fresh_factory(_db_path(factory))
    after_restart = await _query(fresh)
    assert after_restart.identity_id == before.identity_id

    # ...but a restart into a deployment whose renderer semantics rotated
    # must not treat the old variant as equivalent (RI-16).
    monkeypatch.setattr(
        packets_module, "EVIDENCE_PACKET_SCHEMA_VERSION", "marker.evidence_packet.v2"
    )
    rotated_restart = await _query(_fresh_factory(_db_path(factory)))
    assert rotated_restart.identity_id != before.identity_id
    assert rotated_restart.identity_id != after_restart.identity_id


# ---------------------------------------------------------------------------
# RI-13: callers cannot spoof trusted representation dimensions
# ---------------------------------------------------------------------------


async def test_caller_cannot_spoof_representation_dimensions(
    payload_env: tuple,
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-repr")

    for forged in (
        {**_request(), "representation": {"packet_schema": "marker.evidence_packet.v2"}},
        {**_request(), "context": {"serialization_profile": "default", "citation_profile": "v2"}},
        {**_request(), "context": {"serialization_profile": "default", "renderer": "legacy"}},
    ):
        with pytest.raises(QueryContractError):
            parse_query_request(forged)

    # A caller-chosen serialization_profile still participates in identity
    # through the caller-owned context seam — while the server-owned
    # representation dimension stays byte-identical.
    base = await _query(factory)
    other_profile = await execute_query(
        factory,
        parse_query_request(
            _request("ws-repr", context={"serialization_profile": "cl100k"})
        ),
    )
    assert other_profile.identity_id != base.identity_id
    assert other_profile.identity_dimensions["representation"] == (
        base.identity_dimensions["representation"]
    )


# ---------------------------------------------------------------------------
# RI-09: tokenizer semantics — real identity source, fail-closed rotation
# ---------------------------------------------------------------------------


async def test_tokenizer_identity_binds_generation_and_fails_closed(
    payload_env: tuple,
) -> None:
    factory, _store, service = payload_env
    publication = await _publish(factory, service, "ws-repr")

    shared = dict(
        workspace_id="ws-repr",
        kernel_commit_id=1,
        snapshot_id="snap-1",
        source_generation_id="gen-1",
    )
    # The real identity function a lexical build flows through binds the
    # tokenizer and its config: a tokenizer change is a new generation.
    unicode61 = compute_lexical_identity(tokenizer="unicode61", **shared)
    other = compute_lexical_identity(tokenizer="porter", **shared)
    configured = compute_lexical_identity(
        tokenizer="unicode61", tokenizer_config_json='{"remove_diacritics": 2}', **shared
    )
    assert len({unicode61, other, configured}) == 3

    # The supported set is backend-pinned: SQLite cannot even build a
    # porter corpus, so a rotation attempt fails closed rather than
    # silently reindexing under a borrowed identity.
    assert supported_tokenizers("sqlite") == frozenset({"unicode61"})
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-repr")
    )
    pubs = PublicationService(factory)
    with pytest.raises(KernelError):
        await pubs.build_lexical(generation.generation_id, tokenizer="porter")

    # End to end: the runtime's tokenizer identity source is the pinned
    # publication projection, and it participates in packet identity.
    packet = await _query(factory)
    assert packet.publication["tokenizer"] == "unicode61"
    assert packet.identity_dimensions["publication"]["tokenizer"] == "unicode61"
    assert (
        packet.identity_dimensions["publication"]["lexical_generation_id"]
        == packet.publication["lexical_generation_id"]
    )
    assert packet.publication["publication_set_id"] == publication.publication_set_id

    # Continuation defense-in-depth: a stored binding whose tokenizer
    # differs from the opened set can never match.
    tampered = dict(packet.publication)
    tampered["tokenizer"] = "porter"
    assert publication_matches(packet.publication, packet.publication)
    assert not publication_matches(tampered, packet.publication)


# ---------------------------------------------------------------------------
# RI-14 / RI-20: continuation coherence under representation rotation
# ---------------------------------------------------------------------------


async def _fresh_partial(service: ContinuationService, workspace: str):
    outcome = await service.fresh_query(
        parse_query_request(_request(workspace)), page_size=2
    )
    assert outcome.status == "partial"
    assert outcome.next_cursor
    return outcome


async def test_continuation_proceeds_without_rotation(payload_env: tuple) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-cont-control")
    continuation = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=240)
    first = await _fresh_partial(continuation, "ws-cont-control")
    second = await continuation.continue_query(
        first.next_cursor, workspace_id="ws-cont-control", page_size=2
    )
    assert second.status in {"partial", "complete"}
    assert second.packet is not None


async def test_continuation_rotation_invalidates_before_mixed_output(
    payload_env: tuple, monkeypatch
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-cont-rot")
    continuation = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=240)
    first = await _fresh_partial(continuation, "ws-cont-rot")

    monkeypatch.setattr(
        packets_module, "EVIDENCE_PACKET_SCHEMA_VERSION", "marker.evidence_packet.v2"
    )
    outcome = await continuation.continue_query(
        first.next_cursor, workspace_id="ws-cont-rot", page_size=2
    )
    # The chain ends explicitly: page one was rendered under v1, and the
    # deployed renderer can no longer produce v1, so no page is emitted
    # and no budget/result payload leaks.
    assert outcome.status == "invalidated"
    assert outcome.error_code == _REPR_CHANGED
    assert outcome.reason == _REPR_CHANGED
    assert outcome.packet is None
    assert outcome.result is None

    async with factory() as session:
        row = (
            await session.execute(
                select(KernelQueryCursor).where(
                    KernelQueryCursor.workspace_id == "ws-cont-rot"
                )
            )
        ).scalar_one()
        assert row.status == "revoked"


async def test_legacy_cursor_without_representation_binding_fails_closed(
    payload_env: tuple,
) -> None:
    factory, _store, service = payload_env
    await _publish(factory, service, "ws-cont-legacy")
    continuation = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=240)
    first = await _fresh_partial(continuation, "ws-cont-legacy")

    # A row predating the binding cannot be verified against any deployed
    # semantics: it is invalidated explicitly, never reinterpreted.
    async with factory() as session:
        await session.execute(
            update(KernelQueryCursor)
            .where(KernelQueryCursor.workspace_id == "ws-cont-legacy")
            .values(representation_json=None)
        )
        await session.commit()

    outcome = await continuation.continue_query(
        first.next_cursor, workspace_id="ws-cont-legacy", page_size=2
    )
    assert outcome.status == "invalidated"
    assert outcome.error_code == _REPR_CHANGED
    assert outcome.packet is None


# ---------------------------------------------------------------------------
# Citation scheme single-source: answer-side construction and validation
# ---------------------------------------------------------------------------


def test_citation_views_derive_from_the_authority_tuple(monkeypatch) -> None:
    ref = EvidenceRef(disclosure_id="dsc_1", record_id="view-1", view_id="view-1", node_id="n1")
    assert ref.locator_view() == {"record_id": "view-1", "view_id": "view-1", "node_id": "n1"}

    unit = {"record_id": "view-1", "view_id": "view-1", "node_id": "n1", "text_hash": "sha256:x"}
    assert citation_view(unit) == {"record_id": "view-1", "view_id": "view-1", "node_id": "n1"}
    # A mapping missing a scheme field is not citable — never partial.
    assert citation_view({"record_id": "view-1", "view_id": "view-1"}) is None

    # An extended deployed scheme a reference does not carry fails closed
    # at construction instead of producing a weakened citation.
    monkeypatch.setattr(
        packets_module,
        "CITATION_LOCATOR_FIELDS",
        ("record_id", "view_id", "node_id", "text_hash"),
    )
    with pytest.raises(AnswerEvidenceContractError):
        ref.locator_view()


# ---------------------------------------------------------------------------
# RI-17 / RI-18: disclosure and answer-trace truth across rotation
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_env(payload_env, monkeypatch):
    factory, _store, commit_service = payload_env
    monkeypatch.setenv("MARKER_QUERY_CURSOR_KEY", "pr86-repr-test-key")
    configure_query_runtime(factory)
    configure_answer_evidence_runtime(factory)
    yield factory, commit_service
    reset_query_runtime()
    reset_answer_evidence_runtime()


def _citing_assessment(trace: dict, disclosure_id: str) -> dict:
    packet = trace["disclosures"][0]["packet"]
    unit = packet["evidence"][0]
    return {
        "claim_id": "c1",
        "span": {"start": 0, "end": 16},
        "verdict": "supported",
        "evidence": [
            {
                "disclosure_id": disclosure_id,
                "record_id": unit["record_id"],
                "view_id": unit["view_id"],
                "node_id": unit["node_id"],
            }
        ],
    }


async def test_disclosure_history_immutable_across_representation_rotation(
    agent_env, monkeypatch
) -> None:
    factory, commit_service = agent_env
    await _publish(factory, commit_service, "ws-disclose")

    first = await run_agent_query(query=_request("ws-disclose"), page_size=10, disclose=True)
    assert first["status"] == "complete"
    first_id = first["disclosure_id"]
    first_packet_id = first["result"]["packet"]["identity_id"]
    assert first["result"]["packet"]["schema_version"] == "marker.evidence_packet.v1"

    trace = await record_agent_answer_trace(
        workspace_id="ws-disclose",
        answer_ref="turn-1",
        answer="Supported claim here.",
        disclosure_ids=[first_id],
    )
    judged = await record_agent_answer_assessment(
        workspace_id="ws-disclose",
        trace_id=trace["trace_id"],
        verdict="supported",
        claims=[_citing_assessment(trace, first_id)],
        assessor={
            "kind": "model",
            "assessor_id": "verifier-1",
            "procedure": "claim-align",
            "procedure_version": "0.3",
        },
        assessment_key="verify-1",
    )
    assert judged["assessment_state"] == "supported"

    async with factory() as session:
        historical = await session.get(KernelContextDisclosure, first_id)
        historical_json = historical.packet_json
        historical_packet_id = historical.packet_id

    # Deployed renderer semantics rotate. The same logical request now
    # delivers a different representation with a distinct reuse identity.
    monkeypatch.setattr(
        packets_module, "EVIDENCE_PACKET_SCHEMA_VERSION", "marker.evidence_packet.v2"
    )
    second = await run_agent_query(query=_request("ws-disclose"), page_size=10, disclose=True)
    assert second["disclosure_id"] != first_id
    second_packet_id = second["result"]["packet"]["identity_id"]
    assert second_packet_id != first_packet_id
    assert second["result"]["packet"]["schema_version"] == "marker.evidence_packet.v2"

    # RI-18: historical truth stays historical — no rewrite, no backfill.
    async with factory() as session:
        preserved = await session.get(KernelContextDisclosure, first_id)
        assert preserved.packet_json == historical_json
        assert preserved.packet_id == historical_packet_id
        assert "marker.evidence_packet.v1" in preserved.packet_json
        rotated = await session.get(KernelContextDisclosure, second["disclosure_id"])
        assert rotated.packet_id == second_packet_id

    reread = await read_agent_answer_trace(
        workspace_id="ws-disclose", trace_id=trace["trace_id"]
    )
    assert reread["context_fingerprint"] == trace["context_fingerprint"]
    assert reread["disclosures"][0]["packet_id"] == first_packet_id
    assert reread["current_assessment"]["verdict"] == "supported"
