"""End-to-end redaction closure across derived serving paths (PR89 /
readiness invariant 18 ``redaction-all-paths``).

Adversarial sentinel suite: a unique high-entropy sentinel is published
as ordinary workspace content, proven retrievable through the supported
serving surface, and then restricted by a newer effective redaction
policy. Every currently supported release path — lexical search, exact
record reads, EvidencePacket identity/reuse, and cursor continuation —
must either return a safe redacted representation or refuse/stale the
request. Unsupported retrieval operators must stay explicitly
unsupported, and retained derived state (the still-published lexical
generation that physically contains the sentinel) must never be
mistaken for releasable content.
"""

from __future__ import annotations

import pytest

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    ContinuationService,
    CursorCodec,
    CursorKeyring,
    execute_query,
    parse_query_request,
)
from app.context_runtime.errors import (
    QueryAuthorizationError,
    UnsupportedOperatorError,
)
from app.context_runtime.packets import to_json
from app.kernel.generations import GenerationService
from app.kernel.publications import PublicationService
from app.kernel.snapshots import resolve_snapshot
from app.services.redaction_policy import RedactionPolicyService
from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio

#: Unique high-entropy sentinel: easy to grep for in any output, log, or
#: serialized packet, and impossible to produce by accident.
SENTINEL = "MU_RED_7f3a9c2e4b"

#: Neighboring public content in the same record and a sibling record:
#: closure must be selective (public material keeps flowing), not a
#: document-wide denial.
PUBLIC_TEXT = "public needle content for retrieval"
SENTINEL_TEXT = f"secret token {SENTINEL} inside otherwise ordinary needle text"


async def _publish_sentinel_workspace(factory, commit_service, workspace: str):
    """Publish one workspace whose corpus contains the sentinel plus
    public material, and return the publication set id."""
    await _commit_view(
        commit_service,
        workspace,
        _view(
            "view-red-1",
            {"n-sentinel": SENTINEL_TEXT, "n-public": PUBLIC_TEXT},
            "rev-red-1",
        ),
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    publication = await PublicationService(factory).publish(
        materialized_generation_id=generation.generation_id
    )
    return publication.publication_set_id


def _request(workspace: str, text: str, **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": [{"op": "lexical_search", "text": text, "limit": 25}],
    }
    base.update(overrides)
    return base


def _packet_text(packet) -> str:
    return "\n".join(unit.text or "" for unit in packet.evidence)


def _assert_no_sentinel(*parts: str) -> None:
    for part in parts:
        assert SENTINEL not in part, "redacted sentinel escaped a serving path"


def _assert_packet_clean(packet) -> None:
    """Assert no serving path discloses the sentinel.

    The caller's own ``packet.query`` echo is excluded: the caller
    supplied that text. Everything the *service* contributed —
    evidence text, omission details, context, publication attribution,
    authorization view, budget — must be sentinel-free.
    """
    payload = to_json(packet)
    payload.pop("query", None)
    _assert_no_sentinel(_packet_text(packet), repr(payload))


# ---------------------------------------------------------------------------
# Baseline: before redaction, the authorized caller can retrieve the
# sentinel through the intended paths (scenario 1).
# ---------------------------------------------------------------------------


async def test_sentinel_baseline_retrievable_before_redaction(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")

    packet = await execute_query(
        factory, parse_query_request(_request("ws-red", SENTINEL))
    )
    assert SENTINEL in _packet_text(packet), "fixture must publish the sentinel"

    exact = await execute_query(
        factory,
        parse_query_request(
            {
                "schema_version": QUERY_SCHEMA_VERSION,
                "workspace_id": "ws-red",
                "operations": [
                    {"op": "record_get", "record_id": "view-red-1", "node_id": "n-sentinel"}
                ],
            }
        ),
    )
    assert SENTINEL in _packet_text(exact)


# ---------------------------------------------------------------------------
# Text closure: after a redaction policy revision becomes effective,
# lexical and exact reads never disclose the sentinel (scenario 2), the
# hit that matched only redacted material is dropped instead of leaked
# through existence, and public material keeps flowing (selectivity).
# ---------------------------------------------------------------------------


async def test_redaction_closes_lexical_and_exact_reads(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")

    policy = RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    )
    await policy.define_profile(
        "default", [{"kind": "literal", "value": SENTINEL}]
    )

    # A direct search for the sentinel itself: the stale-but-published
    # lexical generation still matches the bytes, so the release gate
    # must drop the hit rather than confirm its existence.
    sentinel_query = await execute_query(
        factory, parse_query_request(_request("ws-red", SENTINEL))
    )
    _assert_packet_clean(sentinel_query)
    assert not sentinel_query.evidence, (
        "a hit that matched only redacted material must be dropped, not "
        "returned as a placeholder row that confirms the sentinel exists"
    )

    # A broad query that matches both records: public rows flow with the
    # sentinel masked inside the affected row.
    broad = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    _assert_packet_clean(broad)
    assert _packet_text(broad), "public material must keep flowing after redaction"

    # Exact read of the affected node resolves to redacted content.
    exact = await execute_query(
        factory,
        parse_query_request(
            {
                "schema_version": QUERY_SCHEMA_VERSION,
                "workspace_id": "ws-red",
                "operations": [
                    {
                        "op": "record_get",
                        "record_id": "view-red-1",
                        "node_id": "n-sentinel",
                    }
                ],
            }
        ),
    )
    _assert_packet_clean(exact)
    assert exact.evidence, "the redacted node must resolve, not vanish"


# ---------------------------------------------------------------------------
# Fail-closed: caller-named redaction identities are server-resolved; an
# unknown profile can never degrade to an unrestricted ruleset
# (scenarios 7/8).
# ---------------------------------------------------------------------------


async def test_unknown_redaction_profile_fails_closed(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    policy = RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    )
    await policy.define_profile(
        "default", [{"kind": "literal", "value": SENTINEL}]
    )

    with pytest.raises(QueryAuthorizationError):
        await execute_query(
            factory,
            parse_query_request(
                _request(
                    "ws-red",
                    "needle",
                    context={"redaction_profile_id": "attacker-invented"},
                )
            ),
        )


async def test_unsupported_visual_and_vector_paths_stay_unsupported(
    payload_env,
) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")

    for op in ("visual_search", "vector_search"):
        with pytest.raises(UnsupportedOperatorError):
            parse_query_request(
                {
                    "schema_version": QUERY_SCHEMA_VERSION,
                    "workspace_id": "ws-red",
                    "operations": [{"op": op, "text": SENTINEL}],
                }
            )


# ---------------------------------------------------------------------------
# Scenario 4 - packet/reuse closure: a packet built under the old policy
# cannot be reused under the new one, and the fresh packet carries no
# sentinel at the content level (identity rotation alone is not proof).
# ---------------------------------------------------------------------------


async def test_packet_identity_rotates_and_content_never_discloses(
    payload_env,
) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    policy = RedactionPolicyService(factory, commit_service, workspace_id="ws-red")

    before = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    assert any(SENTINEL in (u.text or "") for u in before.evidence), (
        "pre-redaction packet must contain the sentinel for the rotation proof"
    )

    await policy.define_profile("default", [{"kind": "literal", "value": SENTINEL}])
    after = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    assert after.identity_id != before.identity_id
    assert after.authorization["redaction"]["revision"] > 0
    _assert_packet_clean(after)
    assert _packet_text(after), "public evidence must keep flowing"


# ---------------------------------------------------------------------------
# Scenario 5 - cursor closure: a cursor issued before the transition
# cannot continue into old privileged content; a chain issued after the
# transition pages only projected content.
# ---------------------------------------------------------------------------


def _codec() -> CursorCodec:
    return CursorCodec(
        CursorKeyring({"k1": b"pr89-red-key----" + b"x" * 32}, current_key_id="k1")
    )


async def test_cursor_issued_before_redaction_is_invalidated(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)

    first = await service.fresh_query(_request("ws-red", "needle"), page_size=1)
    assert first.status == "partial"
    assert first.next_cursor

    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    resumed = await service.continue_query(
        first.next_cursor, workspace_id="ws-red", page_size=1
    )
    assert resumed.status == "invalidated"
    assert resumed.reason == "authorization_changed"
    assert resumed.packet is None


async def test_post_redaction_chain_pages_only_projected_content(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])
    service = ContinuationService(factory, cursor_codec=_codec(), pin_lease_seconds=60)

    delivered: list[str] = []
    cursor = None
    pages = 0
    fresh = await service.fresh_query(_request("ws-red", "needle"), page_size=1)
    pages += 1
    if fresh.packet is not None:
        delivered.extend(unit.text or "" for unit in fresh.packet.evidence)
    cursor = fresh.next_cursor
    while cursor is not None and pages < 12:
        page = await service.continue_query(cursor, workspace_id="ws-red", page_size=1)
        pages += 1
        if page.packet is not None:
            payload = to_json(page.packet)
            payload.pop("query", None)
            _assert_no_sentinel(repr(payload))
            delivered.extend(unit.text or "" for unit in page.packet.evidence)
        cursor = page.next_cursor
        if page.status == "complete":
            break

    _assert_no_sentinel(*delivered)
    assert delivered, "the chain must deliver public material"


# ---------------------------------------------------------------------------
# Scenario 6b - direct-release analog: disclosures minted after the
# transition store already-projected bytes; the durable row a future
# answer binds to carries no sentinel.
# ---------------------------------------------------------------------------


async def test_post_redaction_disclosure_rows_are_sentinel_free(payload_env) -> None:
    from sqlalchemy import select

    from app.agent_answer_evidence import (
        configure_answer_evidence_runtime,
        reset_answer_evidence_runtime,
    )
    from app.agent_query import (
        configure_query_runtime,
        reset_query_runtime,
        run_agent_query,
    )
    from app.kernel.models import KernelContextDisclosure

    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    configure_query_runtime(session_factory=factory)
    configure_answer_evidence_runtime(session_factory=factory)
    try:
        envelope = await run_agent_query(
            query=_request("ws-red", "needle"), disclose=True
        )
    finally:
        reset_query_runtime()
        reset_answer_evidence_runtime()

    assert envelope.get("status") in ("partial", "complete"), envelope

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(KernelContextDisclosure.packet_json)
                    .order_by(KernelContextDisclosure.created_at.desc())
                    .limit(5)
                )
            )
            .all()
        )
    assert rows, "durable disclosure rows must exist"
    for (payload_json,) in rows:
        _assert_no_sentinel(payload_json)


# ---------------------------------------------------------------------------
# Scenario 7 - cross-profile isolation: profiles are distinct committed
# rulesets; material restricted under one profile flows under another
# only because that profile's rules say so, and identities never mix.
# ---------------------------------------------------------------------------


async def test_cross_profile_isolation(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    policy = RedactionPolicyService(factory, commit_service, workspace_id="ws-red")
    await policy.define_profile("strict", [{"kind": "literal", "value": SENTINEL}])
    await policy.define_profile("open", [])

    strict = await execute_query(
        factory,
        parse_query_request(
            _request("ws-red", "needle", context={"redaction_profile_id": "strict"})
        ),
    )
    open_packet = await execute_query(
        factory,
        parse_query_request(
            _request("ws-red", "needle", context={"redaction_profile_id": "open"})
        ),
    )

    _assert_packet_clean(strict)
    assert "[redacted]" in _packet_text(strict)
    assert strict.identity_id != open_packet.identity_id
    # "open" carries no rules: the sentinel is releasable there by the
    # operator's explicit choice, which is exactly why identities must
    # differ - an "open" packet can never be replayed as a "strict" one.
    assert any(SENTINEL in (u.text or "") for u in open_packet.evidence)


# ---------------------------------------------------------------------------
# Scenario 9 - concurrent transition: a redaction commit that lands
# mid-execution linearizes before the next operation, and concurrent
# readers never observe a mixed packet after the boundary.
# ---------------------------------------------------------------------------


async def test_mid_query_redaction_linearizes_before_next_operation(
    payload_env,
) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    policy = RedactionPolicyService(factory, commit_service, workspace_id="ws-red")

    async def redact_after_first(index: int) -> None:
        if index == 0:
            await policy.define_profile(
                "default", [{"kind": "literal", "value": SENTINEL}]
            )

    request = parse_query_request(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": "ws-red",
            "operations": [
                {"op": "lexical_search", "text": "public", "limit": 5},
                {"op": "lexical_search", "text": SENTINEL, "limit": 5},
            ],
        }
    )
    packet = await execute_query(factory, request, _after_operation=redact_after_first)
    _assert_packet_clean(packet)
    assert packet.authorization["redaction"]["revision"] > 0


async def test_concurrent_readers_settle_projected(payload_env) -> None:
    import asyncio

    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")

    stop = asyncio.Event()
    errors: list[Exception] = []

    async def reader() -> None:
        while not stop.is_set():
            await execute_query(factory, parse_query_request(_request("ws-red", "needle")))

    async def writer() -> None:
        await asyncio.sleep(0.05)
        await RedactionPolicyService(
            factory, commit_service, workspace_id="ws-red"
        ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])
        for _ in range(4):
            await asyncio.sleep(0.05)

    readers = [asyncio.create_task(reader()) for _ in range(4)]
    try:
        await writer()
    finally:
        stop.set()
        for task in readers:
            try:
                await task
            except Exception as exc:
                errors.append(exc)

    assert not errors, f"reader exceptions: {errors}"
    # Post-boundary sweep: once the commit is durable and settled, no
    # read may disclose the sentinel.
    for _ in range(4):
        packet = await execute_query(
            factory, parse_query_request(_request("ws-red", "needle"))
        )
        _assert_packet_clean(packet)


# ---------------------------------------------------------------------------
# Scenario 10 - rebuild failure: a failed re-materialization/reindex
# never reopens stale unrestricted content; release safety never waits
# for derived-state rebuilds.
# ---------------------------------------------------------------------------


async def test_failed_rematerialization_never_reopens_stale_content(
    payload_env, monkeypatch
) -> None:
    import app.kernel.publications as publications_module

    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    # New content revision exists to publish, but validation of the
    # staged set fails (injected fault): the publication head must not
    # move, and serving must stay projected under the old generation.
    await _commit_view(
        commit_service,
        "ws-red",
        _view(
            "view-red-1b",
            {"n-sentinel": SENTINEL_TEXT, "n-public": PUBLIC_TEXT},
            "rev-red-1b",
        ),
        advance=False,
    )
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-red")
    )

    async def broken_validate(self, publication_set_id, *args, **kwargs):
        raise publications_module.PublicationIntegrityError(
            "injected validation failure"
        )

    monkeypatch.setattr(
        publications_module.PublicationService,
        "validate_publication_set",
        broken_validate,
    )
    with pytest.raises(Exception):
        await PublicationService(factory).publish(
            materialized_generation_id=generation.generation_id
        )
    monkeypatch.undo()

    packet = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    _assert_packet_clean(packet)
    assert _packet_text(packet), "serving must continue on the old generation"


# ---------------------------------------------------------------------------
# Scenario 11 - restart recovery: redaction state is durable; a process
# restart cannot resurrect an unrestricted serving route.
# ---------------------------------------------------------------------------


async def test_restart_preserves_redaction_state(payload_env) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    db_path = factory.kw["bind"].url.database
    await factory.kw["bind"].dispose()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    reborn = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        packet = await execute_query(
            reborn, parse_query_request(_request("ws-red", "needle"))
        )
        _assert_packet_clean(packet)
        assert _packet_text(packet)

        service = ContinuationService(
            reborn, cursor_codec=_codec(), pin_lease_seconds=60
        )
        fresh = await service.fresh_query(_request("ws-red", "needle"), page_size=1)
        assert fresh.status in ("partial", "complete")
        if fresh.packet is not None:
            _assert_packet_clean(fresh.packet)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Scenario 13 - secondary leakage: no service-contributed surface (exact
# masked text, omission details, authorization view) echoes the sentinel.
# ---------------------------------------------------------------------------


async def test_no_secondary_leakage_in_details_and_placeholders(payload_env) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    sentinel_query = await execute_query(
        factory, parse_query_request(_request("ws-red", SENTINEL))
    )
    assert not sentinel_query.evidence
    for omission in sentinel_query.omitted:
        _assert_no_sentinel(omission.detail or "", omission.reason)

    broad = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    _assert_packet_clean(broad)
    assert "secret token [redacted] inside otherwise ordinary needle text" in _packet_text(
        broad
    )


# ---------------------------------------------------------------------------
# Scenario 14 - policy relaxation: a newer revision that lifts the rule
# re-evaluates fresh; no stale pre-redaction packet is resurrected.
# ---------------------------------------------------------------------------


async def test_relaxation_reevaluates_fresh_without_stale_resurrection(
    payload_env,
) -> None:
    factory, _store, commit_service = payload_env
    await _publish_sentinel_workspace(factory, commit_service, "ws-red")
    policy = RedactionPolicyService(factory, commit_service, workspace_id="ws-red")

    pre = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    await policy.define_profile("default", [{"kind": "literal", "value": SENTINEL}])
    redacted = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    await policy.define_profile("default", [])
    relaxed = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )

    assert SENTINEL in _packet_text(pre)
    _assert_packet_clean(redacted)
    assert SENTINEL in _packet_text(relaxed), "a relaxed policy lawfully releases"
    assert len({pre.identity_id, redacted.identity_id, relaxed.identity_id}) == 3


# ---------------------------------------------------------------------------
# Scenario 6 (REST convert routes) - the publication-serving corpus is
# disjoint from conversion job outputs: no serving path can resolve to
# job-store bytes, so a publication redaction policy has no binding
# point there (operator pre-publication surface).
# ---------------------------------------------------------------------------


async def test_serving_corpus_is_disjoint_from_job_outputs(payload_env) -> None:
    factory, _store, commit_service = payload_env
    publication_set_id = await _publish_sentinel_workspace(
        factory, commit_service, "ws-red"
    )
    await RedactionPolicyService(
        factory, commit_service, workspace_id="ws-red"
    ).define_profile("default", [{"kind": "literal", "value": SENTINEL}])

    packet = await execute_query(
        factory, parse_query_request(_request("ws-red", "needle"))
    )
    _assert_packet_clean(packet)
    # Every served locator is bound to the published generation, never
    # to a job-store path: the serving surface resolves exclusively
    # through the pinned publication set.
    for unit in packet.evidence:
        assert unit.locator.publication_set_id == publication_set_id
        assert unit.locator.record_id.startswith("view-")
