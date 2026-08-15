"""Kernel replay and integrity verification tests (V3.2 PR63A, workstream D).

Deterministic metadata-only replay in commit order, bounded ranges that
ignore timestamps, and a verifier that detects deliberate corruption of
committed history (missing members, tampered payloads, forged manifests,
broken parent chain, forged head).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import InvalidWorkspaceIdError
from app.kernel.records import (
    EDGE_KIND_EVIDENCE_FOR,
    ClaimAssertionRecord,
    KernelEdge,
    ObservationRecord,
)
from app.kernel.replay import (
    list_manifests,
    read_head,
    replay,
    verify_history,
)

pytestmark = pytest.mark.asyncio


def _assertion(key: str) -> ClaimAssertionRecord:
    return ClaimAssertionRecord(
        claim_key=key, subject="doc:report.pdf", predicate="p", value=key
    )


def _observation(tag: str) -> ObservationRecord:
    return ObservationRecord(observer="obs", derivation={"tag": tag})


async def seed_history(service: KernelCommitService) -> None:
    a1 = _assertion("a1")
    o1 = _observation("o1")
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-a",
            records=(a1, o1),
            edges=(
                KernelEdge(
                    edge_kind=EDGE_KIND_EVIDENCE_FOR,
                    source_ref=o1.record_id,
                    target_ref=a1.record_id,
                ),
            ),
        )
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_assertion("a2"),))
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-a", records=(_observation("o2"),))
    )
    # independent workspace chain sharing nothing with ws-a
    await service.commit(
        KernelCommitBatch(workspace_id="ws-b", records=(_assertion("b1"),))
    )


def _db_path(factory: async_sessionmaker) -> Path:
    return Path(factory.kw["bind"].url.database)


def _sql(db_path: Path, statement: str, params: tuple = ()) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(statement, params)
        conn.commit()


# ---------------------------------------------------------------------------
# replay determinism and ordering
# ---------------------------------------------------------------------------


async def test_replay_is_deterministic(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)

    first = await replay(kernel_env, "ws-a")
    second = await replay(kernel_env, "ws-a")

    assert first.replay_digest == second.replay_digest
    assert [c.manifest.kernel_commit_id for c in first.commits] == [1, 2, 3]
    assert first.from_commit == 1 and first.to_commit == 3
    # commit 1: two records and the edge; later commits carry their own
    assert len(first.commits[0].records) == 2
    assert len(first.commits[0].edges) == 1
    assert len(first.commits[1].records) == 1
    assert first.commits[1].edges == ()


async def test_replay_range_membership_ignores_timestamps(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)

    bounded = await replay(kernel_env, "ws-a", to_commit=2)
    assert [c.manifest.kernel_commit_id for c in bounded.commits] == [1, 2]
    # membership is by kernel_commit_id only; to_commit=0 yields the empty cut
    empty = await replay(kernel_env, "ws-a", to_commit=0)
    assert empty.commits == () and empty.from_commit == 0


async def test_replay_isolated_per_workspace(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    ws_b = await replay(kernel_env, "ws-b")
    assert [c.manifest.kernel_commit_id for c in ws_b.commits] == [1]
    assert all(r.record_class == "claim_assertion" for r in ws_b.commits[0].records)


async def test_list_manifests_causal_order_and_head(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    manifests = await list_manifests(kernel_env, "ws-a")
    assert [m.kernel_commit_id for m in manifests] == [1, 2, 3]
    assert [m.parent_kernel_commit_id for m in manifests] == [0, 1, 2]
    assert await read_head(kernel_env, "ws-a") == 3
    assert await read_head(kernel_env, "ws-b") == 1
    assert await read_head(kernel_env, "missing") == 0  # initial empty state
    with pytest.raises(InvalidWorkspaceIdError):
        await read_head(kernel_env, "Not A Workspace")


# ---------------------------------------------------------------------------
# verification on clean history
# ---------------------------------------------------------------------------


async def test_verify_clean_history(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    result = await verify_history(kernel_env, "ws-a")
    assert result.ok, result.problems
    assert result.checked_commits == 3
    assert result.checked_records == 4
    assert result.checked_edges == 1
    assert result.head_kernel_commit_id == 3

    empty_ws = await verify_history(kernel_env, "never-committed")
    assert empty_ws.ok and empty_ws.checked_commits == 0


# ---------------------------------------------------------------------------
# tamper detection (disposable databases)
# ---------------------------------------------------------------------------


async def test_verify_detects_missing_record(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    victim = None
    with sqlite3.connect(db_path) as conn:
        victim = conn.execute(
            "SELECT id FROM kernel_records WHERE workspace_id = 'ws-a' "
            "AND kernel_commit_id = 1 LIMIT 1"
        ).fetchone()[0]
    _sql(db_path, "DELETE FROM kernel_records WHERE id = ?", (victim,))

    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("record count mismatch" in p for p in result.problems)
    assert any("root mismatch" in p for p in result.problems)
    assert all("workspace='ws-a'" in p and "commit=1" in p for p in result.problems)


async def test_verify_detects_tampered_payload(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, payload_json FROM kernel_records "
            "WHERE workspace_id = 'ws-a' LIMIT 1"
        ).fetchone()
    tampered = json.loads(row[1])
    tampered["value"] = "forged"
    _sql(
        db_path,
        "UPDATE kernel_records SET payload_json = ? WHERE id = ?",
        (json.dumps(tampered, separators=(",", ":")), row[0]),
    )

    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("identity hash mismatch" in p for p in result.problems)


async def test_verify_detects_forged_manifest_counts(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    _sql(
        db_path,
        "UPDATE kernel_commit_manifests SET record_count = 99 "
        "WHERE workspace_id = 'ws-a' AND kernel_commit_id = 2",
    )

    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("record count mismatch" in p for p in result.problems)
    assert any("manifest identity hash mismatch" in p for p in result.problems)


async def test_verify_detects_forged_manifest_root(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    _sql(
        db_path,
        "UPDATE kernel_commit_manifests SET record_identity_root = 'sha256:deadbeef' "
        "WHERE workspace_id = 'ws-a' AND kernel_commit_id = 1",
    )
    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("record identity root mismatch" in p for p in result.problems)


async def test_verify_detects_broken_parent_chain(
    kernel_env: async_sessionmaker,
) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    _sql(
        db_path,
        "UPDATE kernel_commit_manifests SET parent_kernel_commit_id = 0 "
        "WHERE workspace_id = 'ws-a' AND kernel_commit_id = 3",
    )
    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("does not name the immediately preceding" in p for p in result.problems)


async def test_verify_detects_forged_head(kernel_env: async_sessionmaker) -> None:
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    _sql(
        db_path,
        "UPDATE kernel_commit_heads SET head_kernel_commit_id = 9 "
        "WHERE workspace_id = 'ws-a'",
    )
    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("does not match last committed manifest" in p for p in result.problems)


async def test_verify_detects_deleted_manifest_gap(
    kernel_env: async_sessionmaker,
) -> None:
    """A missing manifest must not replay as a contiguous complete history."""
    service = KernelCommitService(kernel_env)
    await seed_history(service)
    db_path = _db_path(kernel_env)
    # remove the middle manifest; verifier must name the gap, not crash
    _sql(
        db_path,
        "DELETE FROM kernel_commit_manifests "
        "WHERE workspace_id = 'ws-a' AND kernel_commit_id = 2",
    )
    result = await verify_history(kernel_env, "ws-a")
    assert not result.ok
    assert any("breaks contiguity" in p for p in result.problems)
    # commit 2's records still exist but their manifest is gone: orphaned
    assert any("commit without a manifest" in p for p in result.problems)
