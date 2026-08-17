"""Authorization-first retrieval over real publications (PR78).

Adversarial integration matrix over the two implemented operators:
exact nondisclosure, authorized-universe lexical competition, live
revocation (including mid-query), packet identity invalidation, and
count honesty. Fixtures commit full source lineage (source → content
revision → domain assignment → view) so authorization decisions run
through real committed policy state, not caller assertions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime import (
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
    to_json,
)
from app.kernel.commit import KernelCommitBatch
from app.kernel.generations import GenerationService
from app.kernel.publications import PublicationService, fts_table_name
from app.kernel.records import (
    SOURCE_CONSISTENCY_NATIVE_ATOMIC,
    AccessPolicyRevisionRecord,
    AuthorizationEpochRecord,
    ContentRevisionRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph
from app.kernel.patches import ViewDocumentRecord
from app.kernel.snapshots import resolve_snapshot
from app.services.query_policy import QueryPolicyService

from tests.test_kernel_publication import _commit_view, _view

pytestmark = pytest.mark.asyncio


def _blob(value: int) -> str:
    return f"sha256:{value:064x}"


async def seed_domain_doc(
    service,
    workspace: str,
    *,
    tag: str,
    domain: str,
    texts: dict[str, str],
) -> str:
    source = SourceIdentityRecord(
        record_id=f"src.{tag}",
        source_kind="local_path",
        source_key=f"C:/docs/{tag}.md",
    )
    revision = ContentRevisionRecord(
        record_id=f"rev.{tag}",
        source_ref=source.record_id,
        blob_key=_blob(abs(int.from_bytes(tag.encode(), "big")) % (1 << 256)),
        byte_length=sum(len(v) for v in texts.values()),
        media_type="text/markdown",
        consistency_class=SOURCE_CONSISTENCY_NATIVE_ATOMIC,
        suffix=".md",
    )
    assignment = SecurityDomainRecord(
        record_id=f"assign.{tag}",
        source_ref=source.record_id,
        domain_key=domain,
    )
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    view = ViewDocumentRecord(
        record_id=f"view.{tag}",
        content_revision_ref=revision.record_id,
        graph=graph,
        texts=dict(texts),
        view_id=f"doc-{tag}",
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(source, revision, assignment, view),
        )
    )
    return view.record_id


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-a",
        "operations": operations,
    }
    base.update(overrides)
    return base


def _lexical(text: str, **overrides) -> dict:
    op = {"op": "lexical_search", "text": text}
    op.update(overrides)
    return op


def _get(record_id: str, node_id: str | None = None) -> dict:
    op: dict[str, Any] = {"op": "record_get", "record_id": record_id}
    if node_id is not None:
        op["node_id"] = node_id
    return op


async def _publish(factory, service, workspace: str = "ws-a"):
    pubs = PublicationService(factory)
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return pubs, gen, ref


async def _seed_two_domains(factory, service):
    """dom-alpha (authorized fixture) + dom-beta + one unattributed view."""
    alpha = await seed_domain_doc(
        service,
        "ws-a",
        tag="alpha",
        domain="dom-alpha",
        texts={
            "n1": "needle alpha common words",
            "n2": "alpha filler unrelated prose",
            "n3": "needle alpha appears twice here needle",
        },
    )
    beta = await seed_domain_doc(
        service,
        "ws-a",
        tag="beta",
        domain="dom-beta",
        texts={
            "n1": "needle beta secret",
            "n2": "needle beta confidential",
        },
    )
    await _commit_view(
        service, "ws-a", _view("view-bare", {"n1": "needle unattributed"}, "rev-x")
    )
    return alpha, beta


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


# ---------------------------------------------------------------------------
# E: exact retrieval under authorization
# ---------------------------------------------------------------------------


async def test_authorized_exact_read_retains_attribution(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    packet = await execute_query(
        factory, parse_query_request(_request([_get(alpha, "n1")]))
    )
    assert packet.status == "complete"
    unit = packet.evidence[0]
    assert unit.locator.record_id == alpha
    assert unit.locator.node_id == "n1"
    assert unit.text == "needle alpha common words"
    assert packet.budget.operations_executed == 1


async def test_unauthorized_exact_read_discloses_nothing(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta", basis={"reason": "restricted"})

    whole = await execute_query(
        factory, parse_query_request(_request([_get(beta)]))
    )
    node = await execute_query(
        factory, parse_query_request(_request([_get(beta, "n1")]))
    )
    for packet in (whole, node):
        assert packet.evidence == ()
        assert packet.omitted[0].reason == "not_found"
    # No protected text anywhere in the serialized packet.
    serialized = json.dumps(to_json(whole))
    assert "secret" not in serialized
    assert "confidential" not in serialized


async def test_unauthorized_and_nonexistent_exact_are_identical(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta")

    hidden = await execute_query(
        factory, parse_query_request(_request([_get(beta)]))
    )
    missing = await execute_query(
        factory, parse_query_request(_request([_get("no-such-record")]))
    )
    # Caller-visible shape: same reason, same outcome template, same
    # status. The only textual difference is the caller's own echoed
    # record id — nothing in the response distinguishes hidden-exists
    # from never-existed.
    assert hidden.omitted[0].reason == missing.omitted[0].reason == "not_found"
    template = "record {!r} is not present in the pinned materialized generation"
    assert hidden.omitted[0].detail == template.format(beta)
    assert missing.omitted[0].detail == template.format("no-such-record")
    assert hidden.status == missing.status == "complete"
    assert hidden.budget.operations_executed == missing.budget.operations_executed
    lowered = (
        hidden.omitted[0].reason + hidden.omitted[0].detail
    ).lower()
    assert "forbidden" not in lowered and "denied" not in lowered


async def test_tombstone_wins_over_pinned_publication(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    pubs, gen, ref = await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")

    visible = await execute_query(
        factory, parse_query_request(_request([_get(beta, "n1")]))
    )
    assert visible.evidence[0].text == "needle beta secret"

    await policy.deny_record(beta, basis={"reason": "revoked"})
    # The pinned generation still physically contains the record and
    # its lexical rows — the live deny, not a rebuild, refuses it.
    denied = await execute_query(
        factory, parse_query_request(_request([_get(beta, "n1")]))
    )
    assert denied.publication["publication_set_id"] == ref.publication_set_id
    assert denied.evidence == ()
    assert denied.omitted[0].reason == "not_found"


# ---------------------------------------------------------------------------
# L: lexical competition over the authorized universe
# ---------------------------------------------------------------------------


async def test_forbidden_hits_are_not_caller_candidates(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta")

    packet = await execute_query(
        factory, parse_query_request(_request([_lexical("needle")]))
    )
    records = {u.locator.record_id for u in packet.evidence}
    nodes = {u.locator.node_id for u in packet.evidence}
    assert beta not in records
    assert "needle beta secret" not in json.dumps(to_json(packet))
    # Authorized hits only: alpha's two needle nodes + the bare view.
    assert records == {"view.alpha", "view-bare"}
    assert nodes == {"n1", "n3"}
    # Counts describe the authorized universe, not the raw index.
    assert packet.budget.candidates_considered == 3


async def test_hidden_extra_hits_do_not_signal_more_matches(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")

    # Before the deny: 5 raw matches (alpha 2, beta 2, bare 1) — a
    # limit of 3 must report withheld matches.
    open_packet = await execute_query(
        factory, parse_query_request(_request([_lexical("needle", limit=3)]))
    )
    assert open_packet.status == "partial"
    assert open_packet.omitted[0].reason == "candidate_budget"
    assert "beyond the requested limit" in open_packet.omitted[0].detail

    # After denying dom-beta: exactly 3 authorized matches remain. The
    # probe-one-beyond operates over the authorized universe, so the
    # two hidden hits must NOT surface as "more matches exist".
    await policy.deny_domain("dom-beta")
    denied_packet = await execute_query(
        factory, parse_query_request(_request([_lexical("needle", limit=3)]))
    )
    assert denied_packet.status == "complete"
    assert len(denied_packet.evidence) == 3
    assert not any(
        o.reason == "candidate_budget" for o in denied_packet.omitted
    )


async def test_hidden_only_query_looks_like_no_hit(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta")

    packet = await execute_query(
        factory, parse_query_request(_request([_lexical("confidential")]))
    )
    # Only dom-beta matched; the caller sees the same honest no_hit it
    # would see for a term matching nothing at all. (The query text is
    # the caller's own echo; the hidden corpus text never appears.)
    assert packet.evidence == ()
    assert packet.omitted[0].reason == "no_hit"
    assert packet.status == "complete"
    assert not any(
        "confidential" in (unit.text or "") for unit in packet.evidence
    )
    assert "needle beta confidential" not in json.dumps(to_json(packet))


async def test_mixed_query_preserves_authorized_recall(payload_env: tuple) -> None:
    """A forbidden crowd at the top of the shared ranking cannot evict
    lower-ranked authorized hits: candidate selection keeps walking the
    deterministic order instead of over-fetching a fixed page."""
    factory, store, service = payload_env
    # dom-beta: 12 short high-frequency docs (bm25 darlings).
    crowd = {f"n{i}": f"needle beta {i}" for i in range(12)}
    await seed_domain_doc(
        service, "ws-a", tag="crowd", domain="dom-beta", texts=crowd
    )
    # dom-alpha: one long, low-frequency authorized hit + padding.
    alpha = await seed_domain_doc(
        service,
        "ws-a",
        tag="alpha",
        domain="dom-alpha",
        texts={
            "n1": "needle buried in a much longer authorized document with "
            "plenty of unrelated filler words diluting the term",
        },
    )
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta")

    packet = await execute_query(
        factory, parse_query_request(_request([_lexical("needle", limit=5)]))
    )
    assert packet.status == "complete"
    assert len(packet.evidence) == 1
    assert packet.evidence[0].locator.record_id == alpha
    assert packet.budget.candidates_considered == 1


# ---------------------------------------------------------------------------
# R: immediate revocation
# ---------------------------------------------------------------------------


async def test_revoke_without_reindex_blocks_exact_and_lexical(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    pubs, gen, ref = await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")

    before = await execute_query(
        factory, parse_query_request(_request([_lexical("secret")]))
    )
    assert len(before.evidence) == 1

    await policy.deny_source("src.beta", basis={"reason": "revoked"})
    # No rebuild: the stale FTS rows are still physically present.
    table = fts_table_name(ref.lexical_generation_id)
    with sqlite3.connect(_db_path(factory)) as conn:
        rows = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE text LIKE ?', ("%secret%",)
        ).fetchone()[0]
    assert rows == 1  # R3: physical cleanup is not the linearization point

    after = await execute_query(
        factory, parse_query_request(_request([_lexical("secret")]))
    )
    assert after.evidence == ()
    assert after.omitted[0].reason == "no_hit"
    exact = await execute_query(
        factory, parse_query_request(_request([_get(beta)]))
    )
    assert exact.evidence == ()
    assert exact.omitted[0].reason == "not_found"


async def test_revoke_mid_query_blocks_later_operations(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    pubs, gen, ref = await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")

    async def deny_after_first(index: int) -> None:
        if index == 0:
            await policy.deny_domain("dom-alpha")

    request = parse_query_request(
        _request(
            [
                _lexical("common"),
                _get(alpha, "n1"),
            ]
        )
    )
    packet = await execute_query(factory, request, _after_operation=deny_after_first)
    # Operation 0 delivered allowed evidence from the pinned set; the
    # denial committed between operations linearized before operation 1.
    assert packet.publication["publication_set_id"] == ref.publication_set_id
    assert [u.locator.record_id for u in packet.evidence] == ["view.alpha"]
    denied_get = [o for o in packet.omitted if o.op == "record_get"]
    assert denied_get and denied_get[0].reason == "not_found"
    # Identity reflects the post-revocation authorization state.
    assert packet.authorization is not None
    assert packet.authorization["deny_revision"] > 0


async def test_restore_is_explicit_and_invalidates_identity(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    request = parse_query_request(_request([_get(beta, "n1")]))

    allowed = await execute_query(factory, request)
    assert allowed.evidence[0].text == "needle beta secret"
    id_allowed = allowed.identity_id

    await policy.deny_domain("dom-beta")
    denied = await execute_query(factory, request)
    assert denied.evidence == ()
    id_denied = denied.identity_id

    await policy.allow_domain("dom-beta", basis={"reason": "restored"})
    restored = await execute_query(factory, request)
    assert restored.evidence[0].text == "needle beta secret"

    assert len({id_allowed, id_denied, restored.identity_id}) == 3


# ---------------------------------------------------------------------------
# C: packet identity invalidation
# ---------------------------------------------------------------------------


async def test_identity_stable_under_unchanged_state(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    request = parse_query_request(_request([_lexical("needle")]))
    first = await execute_query(factory, request)
    second = await execute_query(factory, request)
    assert first.identity_id == second.identity_id
    assert first.authorization == second.authorization


async def test_unrelated_commits_do_not_churn_identity(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    request = parse_query_request(_request([_lexical("needle")]))
    before = await execute_query(factory, request)
    await _commit_view(
        service,
        "ws-a",
        _view("view-late", {"n1": "late words"}, "rev-late"),
        advance=False,
    )
    after = await execute_query(factory, request)
    # A later content commit does not change the pinned publication or
    # the effective authorization: identity must not churn.
    assert after.identity_id == before.identity_id


async def test_policy_epoch_and_deny_changes_invalidate_identity(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    request = parse_query_request(_request([_lexical("needle")]))
    base = await execute_query(factory, request)

    # PR70 access-policy revision (policy-only; content untouched).
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                AccessPolicyRevisionRecord(
                    record_id="policy.beta.1",
                    source_ref="src.beta",
                    policy_profile="local_v1",
                    policy_facts={"basis": "workspace_roots"},
                ),
            ),
        )
    )
    after_policy = await execute_query(factory, request)
    assert after_policy.identity_id != base.identity_id

    # Workspace authorization epoch advance.
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                AuthorizationEpochRecord(
                    record_id="epoch.test.1",
                    epoch_number=1,
                    fingerprint=_blob(7),
                    domain_facts={"profile": "local_v1"},
                ),
            ),
        )
    )
    after_epoch = await execute_query(factory, request)
    assert after_epoch.identity_id != after_policy.identity_id

    # Live deny revision.
    await policy.deny_record("view-bare")
    after_deny = await execute_query(factory, request)
    assert after_deny.identity_id != after_epoch.identity_id


# ---------------------------------------------------------------------------
# N: nondisclosure
# ---------------------------------------------------------------------------


async def test_counts_and_truncation_describe_authorized_universe_only(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta")

    packet = await execute_query(
        factory,
        parse_query_request(
            _request([_lexical("needle", limit=2)], budget={"max_candidates": 2})
        ),
    )
    # Two authorized candidates were withheld by the caller's own limit
    # — the signal counts authorized matches only (no forbidden total).
    assert packet.status == "partial"
    assert packet.budget.candidates_considered == 2
    assert "beta" not in packet.omitted[0].detail


async def test_packet_carries_no_hidden_topology(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta", basis={"reason": "secret-reason"})

    packet = await execute_query(
        factory,
        parse_query_request(
            _request([_lexical("needle"), _get(beta), _get("missing")])
        ),
    )
    view = to_json(packet)
    # The caller's own query echo is expected; every response-owned
    # section must stay free of hidden topology. No domain names,
    # denied-record evidence, or denial reasons escape: the
    # authorization view is digests and counters only.
    response_owned = json.dumps(
        {
            "evidence": view["evidence"],
            "omitted": [
                {**o, "detail": ""} for o in view["omitted"]
            ],  # detail echoes the caller's own ids by template
            "budget": view["budget"],
            "authorization": view["authorization"],
        }
    )
    assert "dom-beta" not in response_owned
    assert "view.beta" not in response_owned
    assert "secret-reason" not in response_owned
    assert "beta secret" not in response_owned
    assert packet.authorization is not None
    assert set(packet.authorization) == {
        "profile",
        "assurance",
        "epoch_number",
        "epoch_fingerprint",
        "deny_revision",
        "policy_digest",
    }


async def test_caller_identity_hints_grant_nothing(payload_env: tuple) -> None:
    factory, store, service = payload_env
    alpha, beta = await _seed_two_domains(factory, service)
    await _publish(factory, service)
    policy = QueryPolicyService(factory, service, workspace_id="ws-a")
    await policy.deny_domain("dom-beta")

    forged = parse_query_request(
        _request(
            [_get(beta, "n1")],
            context={
                "security_context_id": "admin",
                "verifier_policy_id": "override",
                "redaction_profile_id": "none",
            },
        )
    )
    packet = await execute_query(factory, forged)
    assert packet.evidence == ()
    assert packet.omitted[0].reason == "not_found"
