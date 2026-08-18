"""PR79A snapshot-safe query continuation conformance evidence.

Run from repository root::

    python backend/scripts/bench_pr79_continuation.py --write

This benchmark drives the real commit spine, materialized-generation builder,
publication service, authorization resolver, and ``ContinuationService`` over
temporary SQLite state.  Structural checks are acceptance evidence.  Timing
values are characterization only; they are not a constant-time claim.

The corpus is fixed so ordering, counts, digests, and invalidation observations
are reproducible.  Retention clocks start at invocation time because the real
publication-pin API validates leases against process wall clock, then advance
by fixed deltas. Cursor handles, nonces, pin ids, and publication ids remain
runtime values and are reported only as observed bindings where useful.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.context_runtime import (  # noqa: E402
    CONTINUATION_SCHEMA_VERSION,
    CURSOR_TOKEN_VERSION,
    EVIDENCE_PACKET_SCHEMA_VERSION,
    QUERY_SCHEMA_VERSION,
    ContinuationService,
    CursorCodec,
    CursorKeyring,
    parse_query_request,
)
from app.context_runtime.continuation_paging import (  # noqa: E402
    LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION,
)
import app.context_runtime.continuation_paging as continuation_paging  # noqa: E402
from app.context_runtime.continuation_state import (  # noqa: E402
    BUDGET_SCHEMA_VERSION,
    KEYSET_SCHEMA_VERSION,
)
from app.db_migration import upgrade_database  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402
from app.kernel.publications import (  # noqa: E402
    PublicationService,
    active_publication_pins,
)
from app.kernel.records import (  # noqa: E402
    AccessPolicyRevisionRecord,
    AuthorizationEpochRecord,
)
from app.kernel.snapshots import resolve_snapshot  # noqa: E402
from app.services.query_policy import QueryPolicyService  # noqa: E402
from app.utils.canonical import canonical_json_bytes  # noqa: E402
from tests.test_context_runtime_authz_retrieval import seed_domain_doc  # noqa: E402
from tests.test_kernel_publication import _commit_view, _view  # noqa: E402

MEASUREMENTS_PATH = (
    ROOT / "docs" / "reference" / "measurements" / "pr79-snapshot-safe-query-continuation.json"
)

BENCHMARK_SCHEMA_VERSION = "marker.pr79a_continuation_measurements.v1"
WORKSPACE_PREFIX = "ws79"
DEFAULT_REPEATS = 3


class ScenarioFailure(RuntimeError):
    """A structural conformance check failed."""


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _runtime_clock() -> MutableClock:
    """Start retention scenarios near wall clock used by pin persistence."""

    return MutableClock(datetime.now(timezone.utc))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailure(message)


def _blob(value: int) -> str:
    return f"sha256:{value:064x}"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
    except OSError:
        return None
    value = completed.stdout.strip()
    return value or None


def _key_material(label: str) -> bytes:
    """Derive deterministic non-secret benchmark material from a label."""

    return hashlib.sha256(f"marker-pr79a-benchmark:{label}".encode("utf-8")).digest()


def _codec(key_id: str = "bench-k1", key: bytes | None = None) -> CursorCodec:
    material = key or _key_material(key_id)
    return CursorCodec(CursorKeyring({key_id: material}, current_key_id=key_id))


def _request(
    workspace: str,
    operations: list[dict[str, Any]],
    **overrides: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": workspace,
        "operations": operations,
    }
    value.update(overrides)
    return value


def _lexical(text: str, **overrides: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {"op": "lexical_search", "text": text}
    operation.update(overrides)
    return operation


def _get(record_id: str, node_id: str | None = None) -> dict[str, Any]:
    operation: dict[str, Any] = {"op": "record_get", "record_id": record_id}
    if node_id is not None:
        operation["node_id"] = node_id
    return operation


async def _seed_doc(
    service: KernelCommitService,
    workspace: str,
    *,
    tag: str,
    domain: str,
    texts: Mapping[str, str],
) -> str:
    """Reuse PR78 real-lineage fixture used by conformance tests."""

    return await seed_domain_doc(
        service,
        workspace,
        tag=tag,
        domain=domain,
        texts=dict(texts),
    )


async def _seed_bare_view(
    service: KernelCommitService,
    workspace: str,
    *,
    record_id: str,
    text: str,
) -> str:
    """Commit content without policy lineage, preserving auth identity."""
    await _commit_view(
        service,
        workspace,
        _view(
            record_id,
            {"n1": text},
            f"rev.{record_id.removeprefix('view.')}",
        ),
    )
    return record_id


async def _publish(
    factory: async_sessionmaker,
    workspace: str,
    *,
    partition_domains: frozenset[str] | None = None,
) -> tuple[Any, Any, Any | None]:
    generation = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, workspace)
    )
    publications = PublicationService(factory)
    shared = await publications.publish(materialized_generation_id=generation.generation_id)
    partition = None
    if partition_domains is not None:
        partition = await publications.publish_high_assurance(
            materialized_generation_id=generation.generation_id,
            partition_domains=partition_domains,
        )
    return generation, shared, partition


def _unit_key(unit: Any) -> tuple[str, str | None]:
    return unit.locator.record_id, unit.locator.node_id


def _page_summary(outcome: Any, page_number: int) -> dict[str, Any]:
    packet = outcome.packet
    publication = packet.publication if packet is not None else None
    units = tuple(packet.evidence) if packet is not None else ()
    budget = outcome.result.get("cumulative_budget", {}) if outcome.result else {}
    return {
        "page": page_number,
        "status": outcome.status,
        "error_code": outcome.error_code,
        "publication_set_id": publication.get("publication_set_id") if publication else None,
        "snapshot_id": publication.get("snapshot_id") if publication else None,
        "materialized_generation_id": (
            publication.get("materialized_generation_id") if publication else None
        ),
        "lexical_generation_id": (
            publication.get("lexical_generation_id") if publication else None
        ),
        "locators": [
            {
                "record_id": unit.locator.record_id,
                "node_id": unit.locator.node_id,
                "row_index": unit.locator.row_index,
                "rank": unit.rank,
            }
            for unit in units
        ],
        "cumulative_budget": dict(budget),
    }


async def _collect_pages(
    service: ContinuationService,
    first: Any,
    *,
    workspace: str,
    page_size: int,
) -> list[Any]:
    pages = [first]
    cursor = first.next_cursor
    while cursor is not None:
        _require(len(pages) < 128, "continuation did not terminate within safety bound")
        page = await service.continue_query(
            cursor,
            workspace_id=workspace,
            page_size=page_size,
        )
        pages.append(page)
        cursor = page.next_cursor
    return pages


def _outcome_serialized(outcome: Any) -> str:
    return outcome.model_dump_json()


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 3)

    return {
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }


async def _scenario_pages(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-pages"
    tag = "pages"
    node_ids = [f"n{index}" for index in range(1, 9)]
    record_id = await _seed_doc(
        commits,
        workspace,
        tag=tag,
        domain="pages",
        texts={node_id: "needle" for node_id in node_ids},
    )
    generation, publication, _ = await _publish(factory, workspace)
    service = ContinuationService(factory, cursor_codec=_codec())
    request = parse_query_request(
        _request(workspace, [_lexical("needle", limit=len(node_ids))])
    )
    first = await service.fresh_query(request, page_size=2)
    pages = await _collect_pages(service, first, workspace=workspace, page_size=2)
    units = [unit for page in pages for unit in (page.packet.evidence if page.packet else ())]
    observed = [_unit_key(unit) for unit in units]
    expected = [(record_id, node_id) for node_id in node_ids]
    digest_payload = [
        {
            "record_id": unit.locator.record_id,
            "node_id": unit.locator.node_id,
            "text_hash": unit.locator.text_hash,
        }
        for unit in units
    ]
    observed_digest = _digest(digest_payload)
    expected_digest = _digest(
        [
            {
                "record_id": record_id,
                "node_id": node_id,
                "text_hash": units[0].locator.text_hash,
            }
            for node_id in node_ids
        ]
    )
    ranks = [unit.rank for unit in units]
    duplicates = len(observed) - len(set(observed))
    missing = len(set(expected) - set(observed))
    _require(first.status == "partial", "first lexical page was not partial")
    _require(pages[-1].status == "complete", "lexical continuation did not complete")
    _require(observed == expected, "lexical keyset order has duplicate, skip, or drift")
    _require(observed_digest == expected_digest, "ordered locator digest mismatch")
    return {
        "workspace": workspace,
        "record_id": record_id,
        "publication_set_id": publication.publication_set_id,
        "materialized_generation_id": generation.generation_id,
        "page_count": len(pages),
        "page_size": 2,
        "expected_unit_count": len(expected),
        "observed_unit_count": len(observed),
        "duplicate_count": duplicates,
        "missing_count": missing,
        "distinct_rank_count": len(set(ranks)),
        "expected_order": expected,
        "observed_order": observed,
        "ordered_locator_digest": observed_digest,
        "expected_ordered_locator_digest": expected_digest,
        "pages": [_page_summary(page, index + 1) for index, page in enumerate(pages)],
        "acceptance": {
            "multi_page": len(pages) > 1,
            "exact_ordered_digest": observed_digest == expected_digest,
            "no_duplicates": duplicates == 0,
            "no_skips": missing == 0,
            "all_pages_same_publication": len(
                {summary["publication_set_id"] for summary in (_page_summary(page, 0) for page in pages)}
            )
            == 1,
        },
    }


async def _scenario_head_switch(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-head"
    old_record = await _seed_doc(
        commits,
        workspace,
        tag="headold",
        domain="head",
        texts={f"n{index}": "needle old" for index in range(1, 6)},
    )
    _old_generation, old_publication, _ = await _publish(factory, workspace)
    service = ContinuationService(factory, cursor_codec=_codec())
    request = parse_query_request(
        _request(workspace, [_lexical("needle", limit=5)])
    )
    first = await service.fresh_query(request, page_size=2)
    _require(first.next_cursor is not None, "head-switch setup did not issue cursor")

    new_record = await _seed_bare_view(
        commits,
        workspace,
        record_id="view.headnew",
        text="needle new head",
    )
    _new_generation, new_publication, _ = await _publish(factory, workspace)
    continued = await service.continue_query(
        first.next_cursor,
        workspace_id=workspace,
        page_size=2,
    )
    _require(continued.packet is not None, "head-switch continuation had no packet")
    continued_publication = continued.packet.publication["publication_set_id"]
    _require(
        continued_publication == old_publication.publication_set_id,
        "continuation followed new publication head",
    )
    _require(
        continued_publication != new_publication.publication_set_id,
        "head switch did not create distinct publication",
    )
    pages = await _collect_pages(
        service,
        continued,
        workspace=workspace,
        page_size=2,
    )
    all_pages = [first, *pages]
    records = [
        unit.locator.record_id
        for page in all_pages
        for unit in (page.packet.evidence if page.packet else ())
    ]
    _require(new_record not in records, "continued snapshot included new-head record")
    _require(old_record in records, "continued snapshot lost old record")
    old_bindings = {
        page.packet.publication["publication_set_id"]
        for page in all_pages
        if page.packet is not None
    }
    return {
        "workspace": workspace,
        "first_publication_set_id": old_publication.publication_set_id,
        "new_head_publication_set_id": new_publication.publication_set_id,
        "continued_publication_set_ids": sorted(old_bindings),
        "first_snapshot_id": first.packet.publication["snapshot_id"],
        "continued_snapshot_id": continued.packet.publication["snapshot_id"],
        "new_record_id": new_record,
        "continued_record_count": len(records),
        "acceptance": {
            "head_switch_observed": old_publication.publication_set_id != new_publication.publication_set_id,
            "continuation_stayed_on_original_publication": old_bindings == {old_publication.publication_set_id},
            "continuation_stayed_on_original_snapshot": (
                first.packet.publication["snapshot_id"]
                == continued.packet.publication["snapshot_id"]
            ),
            "new_head_record_not_visible": new_record not in records,
        },
    }


async def _scenario_authorization_invalidation(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-auth"
    hidden = await _seed_doc(
        commits,
        workspace,
        tag="authhidden",
        domain="hidden",
        texts={f"n{index}": "secret hidden evidence" for index in range(1, 4)},
    )
    _generation, publication, _ = await _publish(factory, workspace)
    policy = QueryPolicyService(factory, commits, workspace_id=workspace)
    service = ContinuationService(factory, cursor_codec=_codec())
    request = parse_query_request(
        _request(workspace, [_lexical("secret", limit=3)])
    )

    domain_first = await service.fresh_query(request, page_size=1)
    _require(domain_first.next_cursor is not None, "domain deny setup did not issue cursor")
    await policy.deny_domain("hidden", basis={"reason": "benchmark-private"})
    domain_denied = await service.continue_query(
        domain_first.next_cursor,
        workspace_id=workspace,
        page_size=1,
    )
    domain_wire = _outcome_serialized(domain_denied)

    await policy.allow_domain("hidden", basis={"reason": "benchmark-reset"})
    record_first = await service.fresh_query(request, page_size=1)
    _require(record_first.next_cursor is not None, "record deny setup did not issue cursor")
    await policy.deny_record(hidden, basis={"reason": "benchmark-private-record"})
    record_denied = await service.continue_query(
        record_first.next_cursor,
        workspace_id=workspace,
        page_size=1,
    )
    record_wire = _outcome_serialized(record_denied)
    for outcome in (domain_denied, record_denied):
        _require(outcome.status == "invalidated", "deny did not invalidate cursor")
        _require(outcome.packet is None, "deny invalidation returned protected packet")
    forbidden_strings = (
        "hidden",
        hidden,
        "secret hidden evidence",
        "benchmark-private",
        "benchmark-private-record",
    )
    _require(
        not any(value in domain_wire + record_wire for value in forbidden_strings),
        "authorization invalidation serialized hidden topology or basis",
    )
    pins = await active_publication_pins(
        factory,
        publication_set_id=publication.publication_set_id,
    )
    _require(not pins, "authorization invalidation retained publication pin")
    return {
        "workspace": workspace,
        "hidden_record_id": hidden,
        "publication_set_id": publication.publication_set_id,
        "domain_outcome": {"status": domain_denied.status, "error_code": domain_denied.error_code},
        "record_outcome": {"status": record_denied.status, "error_code": record_denied.error_code},
        "serialized_nondisclosure_checked": True,
        "acceptance": {
            "domain_deny_invalidates": domain_denied.status == "invalidated",
            "record_deny_invalidates": record_denied.status == "invalidated",
            "no_packet_after_deny": domain_denied.packet is None and record_denied.packet is None,
            "nondisclosure": not any(value in domain_wire + record_wire for value in forbidden_strings),
            "pin_released_after_invalidation": not pins,
        },
    }


async def _scenario_policy_epoch_invalidation(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-identity"
    await _seed_doc(
        commits,
        workspace,
        tag="identity",
        domain="identity",
        texts={f"n{index}": "identity needle" for index in range(1, 4)},
    )
    await _publish(factory, workspace)
    service = ContinuationService(factory, cursor_codec=_codec())
    request = parse_query_request(_request(workspace, [_lexical("needle", limit=3)]))

    policy_first = await service.fresh_query(request, page_size=1)
    _require(policy_first.next_cursor is not None, "policy setup did not issue cursor")
    await commits.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(
                AccessPolicyRevisionRecord(
                    record_id="policy.identity.1",
                    source_ref="src.identity",
                    policy_profile="local_v1",
                    policy_facts={"basis": "benchmark"},
                ),
            ),
        )
    )
    policy_outcome = await service.continue_query(
        policy_first.next_cursor,
        workspace_id=workspace,
        page_size=1,
    )

    epoch_first = await service.fresh_query(request, page_size=1)
    _require(epoch_first.next_cursor is not None, "epoch setup did not issue cursor")
    await commits.commit(
        KernelCommitBatch(
            workspace_id=workspace,
            records=(
                AuthorizationEpochRecord(
                    record_id="epoch.identity.1",
                    epoch_number=1,
                    fingerprint=_blob(791),
                    domain_facts={"profile": "local_v1"},
                ),
            ),
        )
    )
    epoch_outcome = await service.continue_query(
        epoch_first.next_cursor,
        workspace_id=workspace,
        page_size=1,
    )
    _require(policy_outcome.status == "invalidated", "policy revision did not invalidate cursor")
    _require(epoch_outcome.status == "invalidated", "authorization epoch did not invalidate cursor")
    _require(policy_outcome.error_code == epoch_outcome.error_code == "authorization_changed", "identity invalidation code drifted")
    return {
        "workspace": workspace,
        "policy_outcome": {"status": policy_outcome.status, "error_code": policy_outcome.error_code},
        "epoch_outcome": {"status": epoch_outcome.status, "error_code": epoch_outcome.error_code},
        "acceptance": {
            "access_policy_revision_invalidates": policy_outcome.status == "invalidated",
            "authorization_epoch_invalidates": epoch_outcome.status == "invalidated",
            "same_safe_invalidation_category": (
                policy_outcome.error_code == epoch_outcome.error_code == "authorization_changed"
            ),
        },
    }


async def _scenario_high_assurance(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    missing_workspace = f"{WORKSPACE_PREFIX}-hamiss"
    await _seed_doc(
        commits,
        missing_workspace,
        tag="hamiss",
        domain="alpha",
        texts={"n1": "high assurance needle"},
    )
    await _publish(factory, missing_workspace)
    missing_service = ContinuationService(factory, cursor_codec=_codec())
    missing_request = parse_query_request(
        _request(missing_workspace, [_lexical("needle")], assurance="high")
    )
    missing = await missing_service.fresh_query(missing_request, page_size=1)

    workspace = f"{WORKSPACE_PREFIX}-ha"
    await _seed_doc(
        commits,
        workspace,
        tag="haalpha",
        domain="alpha",
        texts={f"n{index}": "high needle allowed" for index in range(1, 4)},
    )
    await _seed_doc(
        commits,
        workspace,
        tag="habeta",
        domain="beta",
        texts={"n1": "high needle forbidden"},
    )
    policy = QueryPolicyService(factory, commits, workspace_id=workspace)
    await policy.deny_domain("beta")
    _generation, shared, partition = await _publish(
        factory,
        workspace,
        partition_domains=frozenset({"alpha"}),
    )
    _require(partition is not None, "high-assurance setup did not publish partition")
    service = ContinuationService(factory, cursor_codec=_codec())
    request = parse_query_request(
        _request(workspace, [_lexical("needle", limit=3)], assurance="high")
    )
    first = await service.fresh_query(request, page_size=1)
    _require(first.next_cursor is not None, "high-assurance setup did not issue cursor")
    await policy.deny_domain("alpha", basis={"reason": "benchmark-high-revoke"})
    invalidated = await service.continue_query(
        first.next_cursor,
        workspace_id=workspace,
        page_size=1,
    )
    _require(invalidated.status == "invalidated", "high-assurance auth change did not invalidate")
    _require(invalidated.packet is None, "high-assurance invalidation returned packet")
    return {
        "missing_partition_workspace": missing_workspace,
        "missing_partition_status": missing.status,
        "workspace": workspace,
        "shared_publication_set_id": shared.publication_set_id,
        "partition_publication_set_id": partition.publication_set_id,
        "invalidated_status": invalidated.status,
        "acceptance": {
            "missing_partition_fails_closed": missing.status == "policy_fail_closed" and missing.packet is None,
            "high_assurance_change_invalidates": invalidated.status == "invalidated",
            "no_shared_fallback": missing.packet is None and invalidated.packet is None,
            "partition_differs_from_shared": partition.publication_set_id != shared.publication_set_id,
        },
    }


async def _scenario_cursor_abuse(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-abuse"
    await _seed_doc(
        commits,
        workspace,
        tag="abuse",
        domain="abuse",
        texts={f"n{index}": "abuse needle" for index in range(1, 5)},
    )
    await _publish(factory, workspace)
    clock = _runtime_clock()
    codec = _codec()
    service = ContinuationService(
        factory,
        cursor_codec=codec,
        ttl_seconds=5,
        pin_lease_seconds=30,
        clock=clock,
    )
    request = parse_query_request(_request(workspace, [_lexical("needle", limit=4)]))
    first = await service.fresh_query(request, page_size=1)
    _require(first.next_cursor is not None, "cursor abuse setup did not issue cursor")
    token = first.next_cursor

    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[-1] ^= 1
    tampered = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    tampered_outcome = await service.continue_query(tampered, workspace_id=workspace)

    wrong_workspace = await service.continue_query(token, workspace_id=f"{workspace}-other")
    valid_after_wrong_binding = await service.continue_query(
        token,
        workspace_id=workspace,
        page_size=1,
    )
    _require(valid_after_wrong_binding.status in {"partial", "complete"}, "wrong binding consumed valid cursor")

    replay = await service.continue_query(token, workspace_id=workspace, page_size=1)

    other_codec = _codec("other-key", _key_material("other-key"))
    wrong_key = await service.continue_query(
        other_codec.issue("opaque-handle", "opaque-nonce"),
        workspace_id=workspace,
    )

    expiry_first = await service.fresh_query(request, page_size=1)
    _require(expiry_first.next_cursor is not None, "expiry setup did not issue cursor")
    clock.value += timedelta(seconds=6)
    expired = await service.continue_query(
        expiry_first.next_cursor,
        workspace_id=workspace,
        page_size=1,
    )

    old_key = _key_material("old-key")
    rotated = CursorCodec(
        CursorKeyring(
            {"old": old_key, "new": _key_material("new-key")},
            current_key_id="new",
        )
    )
    old_token = CursorCodec(CursorKeyring({"old": old_key}, current_key_id="old")).issue(
        "rotation-handle", "rotation-nonce"
    )
    old_key_verifies = rotated.decode(old_token).key_id == "old"
    retired_rejected = False
    try:
        _codec().decode(old_token)
    except Exception:
        retired_rejected = True

    for outcome, expected in (
        (tampered_outcome, "invalidated"),
        (wrong_workspace, "invalidated"),
        (wrong_key, "invalidated"),
        (expired, "stale"),
        (replay, "invalidated"),
    ):
        _require(outcome.status == expected, f"cursor abuse outcome expected {expected}, got {outcome.status}")
        _require(outcome.packet is None, "cursor abuse returned packet on terminal outcome")
    _require("abuse" not in _outcome_serialized(tampered_outcome), "tamper outcome leaked workspace")
    _require(old_key_verifies and retired_rejected, "key rotation verification behavior drifted")
    return {
        "workspace": workspace,
        "tampered_status": tampered_outcome.status,
        "wrong_workspace_status": wrong_workspace.status,
        "wrong_key_status": wrong_key.status,
        "expired_status": expired.status,
        "replay_status": replay.status,
        "cursor_token_opaque": "abuse" not in token and "needle" not in token,
        "key_rotation": {
            "retained_old_key_verifies": old_key_verifies,
            "retired_key_rejected": retired_rejected,
        },
        "acceptance": {
            "tamper_fails_closed": tampered_outcome.status == "invalidated",
            "expiry_is_stale": expired.status == "stale",
            "wrong_security_binding_fails_closed": wrong_workspace.status == "invalidated",
            "wrong_binding_does_not_consume_cursor": valid_after_wrong_binding.status in {"partial", "complete"},
            "replay_fails_closed": replay.status == "invalidated",
            "token_is_opaque": "abuse" not in token and "needle" not in token,
            "key_rotation_retains_old_window": old_key_verifies,
            "retired_key_fails_closed": retired_rejected,
        },
    }


async def _scenario_budget_and_loop(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    budget_workspace = f"{WORKSPACE_PREFIX}-budget"
    await _seed_doc(
        commits,
        budget_workspace,
        tag="budget",
        domain="budget",
        texts={f"n{index}": "budget needle" for index in range(1, 7)},
    )
    await _publish(factory, budget_workspace)
    budget_service = ContinuationService(factory, cursor_codec=_codec())
    budget_request = parse_query_request(
        _request(
            budget_workspace,
            [_lexical("needle", limit=6)],
            budget={
                "max_operations": 8,
                "max_candidates": 100,
                "max_evidence_units": 3,
                "max_output_chars": 100000,
            },
        )
    )
    budget_first = await budget_service.fresh_query(budget_request, page_size=1)
    budget_pages = await _collect_pages(
        budget_service,
        budget_first,
        workspace=budget_workspace,
        page_size=1,
    )
    budgets = [
        page.result["cumulative_budget"]
        for page in budget_pages
        if page.result is not None and "cumulative_budget" in page.result
    ]
    _require(budget_pages[-1].status == "complete", "cumulative budget did not terminate")
    _require(budget_pages[-1].error_code == "unit_budget", "unit budget termination code missing")
    _require(budgets and max(item["evidence_units"] for item in budgets) <= 3, "evidence budget exceeded")
    _require(all(budgets[index]["evidence_units"] <= budgets[index + 1]["evidence_units"] for index in range(len(budgets) - 1)), "evidence budget regressed")

    work_workspace = f"{WORKSPACE_PREFIX}-work"
    await _seed_doc(
        commits,
        work_workspace,
        tag="work",
        domain="workhidden",
        texts={f"n{index}": "work needle hidden" for index in range(1, 7)},
    )
    await _publish(factory, work_workspace)
    work_policy = QueryPolicyService(factory, commits, workspace_id=work_workspace)
    await work_policy.deny_domain("workhidden")
    work_service = ContinuationService(factory, cursor_codec=_codec())
    work_request = parse_query_request(
        _request(work_workspace, [_lexical("needle", limit=6)])
    )
    original_cap = continuation_paging.LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION
    continuation_paging.LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION = 2
    try:
        work_outcome = await work_service.fresh_query(work_request, page_size=1)
    finally:
        continuation_paging.LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION = original_cap
    work_budget = work_outcome.result["cumulative_budget"] if work_outcome.result else {}
    _require(work_outcome.status == "complete", "work cap did not terminate query")
    _require(work_outcome.error_code == "work_budget", "work cap termination code missing")
    _require(work_budget.get("work_units") == 2, "work cap counter did not match configured bound")

    loop_workspace = f"{WORKSPACE_PREFIX}-loop"
    await _seed_doc(
        commits,
        loop_workspace,
        tag="loop",
        domain="loop",
        texts={f"n{index}": "loop needle" for index in range(1, 6)},
    )
    await _publish(factory, loop_workspace)
    loop_service = ContinuationService(factory, cursor_codec=_codec(), max_chain_pages=2)
    loop_request = parse_query_request(_request(loop_workspace, [_lexical("needle", limit=5)]))
    loop_first = await loop_service.fresh_query(loop_request, page_size=1)
    _require(loop_first.next_cursor is not None, "loop setup did not issue cursor")
    loop_second = await loop_service.continue_query(
        loop_first.next_cursor,
        workspace_id=loop_workspace,
        page_size=1,
    )
    _require(loop_second.status == "loop_limit", "chain limit did not stop pagination")
    _require(loop_second.next_cursor is None, "loop-limit outcome carried cursor")
    return {
        "budget_workspace": budget_workspace,
        "budget_page_count": len(budget_pages),
        "budget_pages": [_page_summary(page, index + 1) for index, page in enumerate(budget_pages)],
        "final_budget": budgets[-1] if budgets else {},
        "work_workspace": work_workspace,
        "work_budget": work_budget,
        "work_cap_rows_per_operation": 2,
        "default_work_cap_rows_per_operation": LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION,
        "loop_workspace": loop_workspace,
        "loop_second_status": loop_second.status,
        "acceptance": {
            "cumulative_evidence_budget_enforced": bool(budgets) and max(item["evidence_units"] for item in budgets) <= 3,
            "cumulative_budget_monotonic": all(
                budgets[index]["evidence_units"] <= budgets[index + 1]["evidence_units"]
                for index in range(len(budgets) - 1)
            ),
            "work_budget_enforced": work_outcome.error_code == "work_budget" and work_budget.get("work_units") == 2,
            "loop_limit_enforced": loop_second.status == "loop_limit" and loop_second.next_cursor is None,
        },
    }


async def _scenario_pin_lifecycle(
    factory: async_sessionmaker,
    commits: KernelCommitService,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-pins"
    await _seed_doc(
        commits,
        workspace,
        tag="pins",
        domain="pins",
        texts={f"n{index}": "pins needle" for index in range(1, 5)},
    )
    _generation, publication, _ = await _publish(factory, workspace)
    clock = _runtime_clock()
    service = ContinuationService(
        factory,
        cursor_codec=_codec(),
        ttl_seconds=5,
        pin_lease_seconds=30,
        clock=clock,
    )
    request = parse_query_request(_request(workspace, [_lexical("needle", limit=4)]))
    first = await service.fresh_query(request, page_size=2)
    _require(first.next_cursor is not None, "pin lifecycle setup did not issue cursor")
    envelope = service.cursor_codec.decode(first.next_cursor)
    row = await service.store.load(envelope.handle)
    _require(row is not None and row.pin_id is not None, "cursor row missing pin")
    active_during_cursor = await active_publication_pins(
        factory,
        publication_set_id=publication.publication_set_id,
    )
    _require(any(pin.pin_id == row.pin_id for pin in active_during_cursor), "valid cursor did not retain pin")
    completed = await _collect_pages(service, first, workspace=workspace, page_size=2)
    active_after_terminal = await active_publication_pins(
        factory,
        publication_set_id=publication.publication_set_id,
    )
    _require(not active_after_terminal, "terminal cursor retained publication pin")

    abandoned = await service.fresh_query(request, page_size=1)
    _require(abandoned.next_cursor is not None, "reclaim setup did not issue cursor")
    abandoned_envelope = service.cursor_codec.decode(abandoned.next_cursor)
    abandoned_row = await service.store.load(abandoned_envelope.handle)
    _require(abandoned_row is not None and abandoned_row.pin_id is not None, "abandoned cursor missing pin")
    clock.value += timedelta(seconds=6)
    reclaimed = await service.reclaim_expired_cursors()
    reclaimed_row = await service.store.load(abandoned_envelope.handle)
    active_after_reclaim = await active_publication_pins(
        factory,
        publication_set_id=publication.publication_set_id,
    )
    _require(reclaimed >= 1, f"expected expired cursor reclaim, got {reclaimed}")
    _require(reclaimed_row is None, "reclaimed cursor row still present")
    _require(not active_after_reclaim, "reclaim retained publication pin")
    return {
        "workspace": workspace,
        "publication_set_id": publication.publication_set_id,
        "cursor_pin_id": row.pin_id,
        "page_count_to_terminal": len(completed),
        "active_pin_count_during_valid_cursor": len(active_during_cursor),
        "active_pin_count_after_terminal": len(active_after_terminal),
        "reclaimed_cursor_count": reclaimed,
        "active_pin_count_after_reclaim": len(active_after_reclaim),
        "acceptance": {
            "valid_cursor_retains_pin": any(pin.pin_id == row.pin_id for pin in active_during_cursor),
            "terminal_releases_pin": not active_after_terminal,
            "expiry_reclaim_removes_row": reclaimed_row is None,
            "reclaim_releases_pin": not active_after_reclaim,
        },
    }


async def _timing_characterization(
    factory: async_sessionmaker,
    repeats: int,
) -> dict[str, Any]:
    workspace = f"{WORKSPACE_PREFIX}-pages"
    request = parse_query_request(_request(workspace, [_lexical("needle", limit=8)]))
    service = ContinuationService(factory, cursor_codec=_codec())
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        outcome = await service.fresh_query(request, page_size=100)
        samples.append((time.perf_counter() - started) * 1000.0)
        if outcome.status != "complete":
            raise ScenarioFailure("timing query unexpectedly issued continuation")
    return {
        "fresh_complete_query_ms": {
            "mean": round(statistics.fmean(samples), 3),
            **_percentiles(samples),
        },
        "repeats": repeats,
        "note": "wall-clock characterization only; no constant-time or isolation claim",
    }


async def _run_scenario(
    name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    blockers: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        result = await operation()
    except Exception as exc:  # noqa: BLE001 - benchmark must report blockers
        blocker = f"{type(exc).__name__}: {exc}"
        blockers.append({"scenario": name, "blocker": blocker})
        return {"implemented": False, "blocker": blocker, "acceptance": {}}
    result["implemented"] = True
    return result


async def run(repeats: int = DEFAULT_REPEATS) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    results: dict[str, Any] = {
        "benchmark": "pr79a-snapshot-safe-query-continuation",
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "git_sha": _git_sha(),
        "schema_versions": {
            "query": QUERY_SCHEMA_VERSION,
            "continuation_outcome": CONTINUATION_SCHEMA_VERSION,
            "evidence_packet": EVIDENCE_PACKET_SCHEMA_VERSION,
            "cursor_token": CURSOR_TOKEN_VERSION,
            "keyset": KEYSET_SCHEMA_VERSION,
            "budget": BUDGET_SCHEMA_VERSION,
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "method": (
            "fixed-corpus real-kernel conformance over temporary migrated SQLite; "
            "structural booleans/counts are acceptance evidence; timing is "
            "characterization only"
        ),
        "repeats_for_timing": repeats,
    }
    db_path: Path | None = None
    engine = None
    try:
        with tempfile.TemporaryDirectory(prefix="marker-pr79a-bench-") as tmp_dir:
            db_path = Path(tmp_dir) / "pr79a.db"
            url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
            await upgrade_database(url=url)
            engine = create_async_engine(url, connect_args={"check_same_thread": False})
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            commits = KernelCommitService(factory)

            results["scenarios"] = {
                "multi_page_order": await _run_scenario(
                    "multi_page_order", lambda: _scenario_pages(factory, commits), blockers
                ),
                "head_switch": await _run_scenario(
                    "head_switch", lambda: _scenario_head_switch(factory, commits), blockers
                ),
                "authorization_invalidation": await _run_scenario(
                    "authorization_invalidation",
                    lambda: _scenario_authorization_invalidation(factory, commits),
                    blockers,
                ),
                "policy_epoch_invalidation": await _run_scenario(
                    "policy_epoch_invalidation",
                    lambda: _scenario_policy_epoch_invalidation(factory, commits),
                    blockers,
                ),
                "high_assurance": await _run_scenario(
                    "high_assurance", lambda: _scenario_high_assurance(factory, commits), blockers
                ),
                "cursor_abuse": await _run_scenario(
                    "cursor_abuse", lambda: _scenario_cursor_abuse(factory, commits), blockers
                ),
                "budget_and_loop": await _run_scenario(
                    "budget_and_loop", lambda: _scenario_budget_and_loop(factory, commits), blockers
                ),
                "pin_lifecycle": await _run_scenario(
                    "pin_lifecycle", lambda: _scenario_pin_lifecycle(factory, commits), blockers
                ),
            }
            results["timing_characterization"] = await _run_scenario(
                "timing_characterization",
                lambda: _timing_characterization(factory, repeats),
                blockers,
            )
            results["database"] = {
                "engine": "sqlite+aiosqlite",
                "temporary": True,
                "path_reported": False,
            }
            await engine.dispose()
            engine = None
    except Exception as exc:  # noqa: BLE001 - preserve artifact on setup failure
        blockers.append({"scenario": "benchmark_setup", "blocker": f"{type(exc).__name__}: {exc}"})
    finally:
        if engine is not None:
            await engine.dispose()

    boolean_checks: dict[str, bool] = {}
    for scenario_name, scenario in results.get("scenarios", {}).items():
        boolean_checks[f"{scenario_name}.implemented"] = scenario.get("implemented") is True
        for check_name, value in scenario.get("acceptance", {}).items():
            if isinstance(value, bool):
                boolean_checks[f"{scenario_name}.{check_name}"] = value
    timing = results.get("timing_characterization", {})
    boolean_checks["timing_characterization.implemented"] = timing.get("implemented") is True
    results["acceptance"] = boolean_checks
    results["blockers"] = blockers
    results["counts"] = {
        "scenario_count": len(results.get("scenarios", {})),
        "implemented_scenario_count": sum(
            scenario.get("implemented") is True for scenario in results.get("scenarios", {}).values()
        ),
        "acceptance_check_count": len(boolean_checks),
        "acceptance_pass_count": sum(value is True for value in boolean_checks.values()),
        "blocker_count": len(blockers),
    }
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write checked-in JSON artifact")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEATS, help="timing repetitions")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be positive")

    report = asyncio.run(run(args.repeat))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)
    ok = bool(report["acceptance"]) and all(report["acceptance"].values()) and not report["blockers"]
    if not ok:
        print("ACCEPTANCE FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
