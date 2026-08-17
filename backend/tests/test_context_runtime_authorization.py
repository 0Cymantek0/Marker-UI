"""Trusted effective-authorization resolution (PR78).

The resolver is the only source of query-time authorization truth:
epoch, domain assignments, live deny state, and policy digests are
derived from committed records, latest-per-target by causal commit
order. Malformed policy lineage fails closed, never falls back to
unrestricted reads. These tests cover the resolver and contract
assurance surface; enforcement over real publications lives in
test_context_runtime_authz_retrieval.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.authorization import (
    AUTHORIZATION_PROFILE_LOCAL,
    resolve_effective_authorization,
)
from app.context_runtime.contract import parse_query_request
from app.context_runtime.errors import (
    QueryAuthorizationError,
    QueryContractError,
)
from app.kernel.commit import KernelCommitBatch
from app.kernel.records import (
    ACCESS_DENIAL_TARGET_DOMAIN,
    ACCESS_DENIAL_TARGET_RECORD,
    ACCESS_DENIAL_TARGET_SOURCE,
    AccessDenialRecord,
    AccessPolicyRevisionRecord,
    AuthorizationEpochRecord,
    SecurityDomainRecord,
    SourceIdentityRecord,
)

pytestmark = pytest.mark.asyncio


def _epoch(number: int, fingerprint: str) -> AuthorizationEpochRecord:
    return AuthorizationEpochRecord(
        record_id=f"epoch.{number}",
        epoch_number=number,
        fingerprint=f"sha256:{fingerprint:<064x}"[:71],
        domain_facts={"profile": "local_v1"},
    )


def _deny(kind: str, ref: str, denied: bool = True, supersedes: str | None = None):
    return AccessDenialRecord(
        record_id=f"deny.{kind}.{ref}.{'1' if denied else '0'}.{supersedes or 'root'}",
        target_kind=kind,
        target_ref=ref,
        denied=denied,
        supersedes=supersedes,
    )


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


async def test_empty_workspace_resolves_to_local_base_grant(payload_env: tuple) -> None:
    factory, store, service = payload_env
    auth = await resolve_effective_authorization(factory, "ws-a")
    assert auth.profile == AUTHORIZATION_PROFILE_LOCAL
    assert auth.epoch_number == 0
    assert auth.epoch_fingerprint is None
    assert dict(auth.domain_assignments) == {}
    assert auth.denied_records == frozenset()
    assert auth.denied_sources == frozenset()
    assert auth.denied_domains == frozenset()
    assert auth.deny_revision == 0
    # Local base grant: the workspace boundary; everything allowed.
    assert auth.allows("anything") is True
    assert auth.policy_digest.startswith("sha256:")


async def test_epoch_is_server_derived_from_committed_truth(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_epoch(1, 1),))
    )
    auth = await resolve_effective_authorization(factory, "ws-a")
    assert auth.epoch_number == 1
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_epoch(2, 2),))
    )
    advanced = await resolve_effective_authorization(factory, "ws-a")
    assert advanced.epoch_number == 2
    assert advanced.epoch_fingerprint != auth.epoch_fingerprint


async def test_denial_latest_event_wins_and_lift_is_explicit(payload_env: tuple) -> None:
    factory, store, service = payload_env
    deny = _deny(ACCESS_DENIAL_TARGET_RECORD, "view-1")
    await service.commit(KernelCommitBatch(workspace_id="ws-a", records=(deny,)))
    denied = await resolve_effective_authorization(factory, "ws-a")
    assert denied.allows("view-1") is False
    assert denied.denied_records == frozenset({"view-1"})
    assert denied.deny_revision > 0

    lift = _deny(ACCESS_DENIAL_TARGET_RECORD, "view-1", denied=False, supersedes=deny.record_id)
    await service.commit(KernelCommitBatch(workspace_id="ws-a", records=(lift,)))
    restored = await resolve_effective_authorization(factory, "ws-a")
    assert restored.allows("view-1") is True
    assert restored.denied_records == frozenset()
    # The overlay revision moved even though the set state returned to
    # its earlier shape — stale packet identity must not survive a lift.
    assert restored.deny_revision > denied.deny_revision


async def test_deny_granularities_record_source_domain(payload_env: tuple) -> None:
    factory, store, service = payload_env
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                _deny(ACCESS_DENIAL_TARGET_RECORD, "view-x"),
                _deny(ACCESS_DENIAL_TARGET_SOURCE, "src-1"),
                _deny(ACCESS_DENIAL_TARGET_DOMAIN, "dom-secret"),
            ),
        )
    )
    auth = await resolve_effective_authorization(factory, "ws-a")
    assert auth.allows("view-x") is False
    assert auth.allows("other", source_ref="src-1") is False
    assert auth.allows("other", domain_key="dom-secret") is False
    assert auth.allows("other") is True
    # Deny at any granularity is independent of the others.
    assert auth.allows("view-x", source_ref="unrelated") is False


async def test_domain_assignments_and_partition_derivation(payload_env: tuple) -> None:
    factory, store, service = payload_env
    source = SourceIdentityRecord(
        record_id="src-1", source_kind="local_path", source_key="C:/docs/a.pdf"
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                source,
                SecurityDomainRecord(
                    record_id="assign.1",
                    source_ref="src-1",
                    domain_key="dom-alpha",
                ),
                SecurityDomainRecord(
                    record_id="assign.2",
                    source_ref="src-1",
                    domain_key="dom-beta",
                ),
            ),
        )
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(_deny(ACCESS_DENIAL_TARGET_DOMAIN, "dom-gamma"),),
        )
    )
    auth = await resolve_effective_authorization(factory, "ws-a")
    # Latest assignment per source wins.
    assert auth.domain_of("src-1") == "dom-beta"
    # Partition domains: assigned minus denied.
    assert auth.partition_domains() == ("dom-beta",)
    profile = auth.partition_profile()
    assert profile.startswith("ha.")
    # Deterministic derivation, independent of resolution order.
    again = await resolve_effective_authorization(factory, "ws-a")
    assert again.partition_profile() == profile


async def test_policy_digest_tracks_every_authorization_dimension(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    source = SourceIdentityRecord(
        record_id="src-1", source_kind="local_path", source_key="C:/docs/a.pdf"
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                source,
                SecurityDomainRecord(
                    record_id="assign.1",
                    source_ref="src-1",
                    domain_key="dom-alpha",
                ),
            ),
        )
    )
    base = await resolve_effective_authorization(factory, "ws-a")

    # Unrelated record commits do not churn the digest.
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                SourceIdentityRecord(
                    record_id="src-2", source_kind="upload", source_key="upload:j2"
                ),
            ),
        )
    )
    unchanged = await resolve_effective_authorization(factory, "ws-a")
    assert unchanged.policy_digest == base.policy_digest

    # A PR70 access-policy revision changes it (policy-only change).
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                AccessPolicyRevisionRecord(
                    record_id="policy.1",
                    source_ref="src-1",
                    policy_profile="local_v1",
                    policy_facts={"basis": "workspace_roots"},
                ),
            ),
        )
    )
    after_policy = await resolve_effective_authorization(factory, "ws-a")
    assert after_policy.policy_digest != base.policy_digest

    # An epoch advance changes it.
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_epoch(1, 9),))
    )
    after_epoch = await resolve_effective_authorization(factory, "ws-a")
    assert after_epoch.policy_digest != after_policy.policy_digest

    # A deny event changes it.
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(_deny(ACCESS_DENIAL_TARGET_RECORD, "view-1"),),
        )
    )
    after_deny = await resolve_effective_authorization(factory, "ws-a")
    assert after_deny.policy_digest != after_epoch.policy_digest


async def test_assurance_is_part_of_identity_not_of_policy_digest(
    payload_env: tuple,
) -> None:
    factory, store, service = payload_env
    standard = await resolve_effective_authorization(factory, "ws-a")
    high = await resolve_effective_authorization(
        factory, "ws-a", assurance="high"
    )
    # Same trusted policy state; the mode differs only in identity view.
    assert standard.policy_digest == high.policy_digest
    assert standard.identity_view() != high.identity_view()
    assert high.identity_view()["assurance"] == "high"


async def test_malformed_policy_lineage_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    source = SourceIdentityRecord(
        record_id="src-1", source_kind="local_path", source_key="C:/docs/a.pdf"
    )
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(
                source,
                SecurityDomainRecord(
                    record_id="assign.1",
                    source_ref="src-1",
                    domain_key="dom-alpha",
                ),
                _deny(ACCESS_DENIAL_TARGET_RECORD, "view-1"),
                _epoch(1, 3),
            ),
        )
    )
    db = _db_path(factory)
    for record_class in ("security_domain", "access_denial", "authorization_epoch"):
        with sqlite3.connect(db) as conn:
            original = conn.execute(
                "SELECT payload_json FROM kernel_records "
                "WHERE workspace_id = 'ws-a' AND record_class = ?",
                (record_class,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE kernel_records SET payload_json = '{broken' "
                "WHERE workspace_id = 'ws-a' AND record_class = ?",
                (record_class,),
            )
            conn.commit()
        with pytest.raises(QueryAuthorizationError, match="refusing to resolve"):
            await resolve_effective_authorization(factory, "ws-a")
        # Restore the exact bytes so later iterations isolate one class.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE kernel_records SET payload_json = ? "
                "WHERE workspace_id = 'ws-a' AND record_class = ?",
                (original, record_class),
            )
            conn.commit()


async def test_unknown_assurance_fails_closed(payload_env: tuple) -> None:
    factory, store, service = payload_env
    with pytest.raises(QueryAuthorizationError, match="assurance"):
        await resolve_effective_authorization(factory, "ws-a", assurance="maximum")


# ---------------------------------------------------------------------------
# Contract: assurance + reserved partition profiles
# ---------------------------------------------------------------------------


def _request(**overrides) -> dict:
    base = {
        "schema_version": "marker.query.v1",
        "workspace_id": "ws-a",
        "operations": [{"op": "lexical_search", "text": "needle"}],
    }
    base.update(overrides)
    return base


def test_assurance_defaults_to_standard_and_parses_high() -> None:
    standard = parse_query_request(_request())
    assert standard.assurance == "standard"
    high = parse_query_request(_request(assurance="high"))
    assert high.assurance == "high"


def test_invalid_assurance_rejected() -> None:
    with pytest.raises(QueryContractError, match="assurance"):
        parse_query_request(_request(assurance="extreme"))


def test_caller_cannot_name_high_assurance_partition_profile() -> None:
    with pytest.raises(QueryContractError, match="reserved"):
        parse_query_request(_request(profile="ha.abc123def456"))


def test_normalized_query_carries_assurance() -> None:
    from app.context_runtime.contract import normalized_query

    standard = normalized_query(parse_query_request(_request()))
    high = normalized_query(parse_query_request(_request(assurance="high")))
    assert standard["assurance"] == "standard"
    assert high["assurance"] == "high"
    assert standard != high
