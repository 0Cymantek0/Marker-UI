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

from app.context_runtime import QUERY_SCHEMA_VERSION, execute_query, parse_query_request
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
SENTINEL_TEXT = f"secret token {SENTINEL} inside otherwise ordinary text"


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
