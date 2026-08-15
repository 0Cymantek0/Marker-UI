"""Synthetic Truth Kernel workload characterization (V3.2 PR63A, plan 11.8).

Runs a repeatable metadata-only workload against a throwaway SQLite
database and prints a JSON report: commit/record/edge counts, database
and WAL bytes, p50/p95 commit latency, replay + verification time,
observed SQLITE_BUSY/head-contention retries, and the SQLite runtime
version/journal mode. No hard threshold is imposed; the report is the
first PR63 baseline that PR64/65 compare against.

Usage (from ``backend/``)::

    python scripts/kernel_characterization.py
    python scripts/kernel_characterization.py --commits 200 --records 25
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.db_migration import upgrade_database  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.records import (  # noqa: E402
    EDGE_KIND_EVIDENCE_FOR,
    ClaimAssertionRecord,
    KernelEdge,
    NativeFactRecord,
    NativeObjectRecord,
    ObservationRecord,
)
from app.kernel.replay import replay, verify_history  # noqa: E402


def build_batch(index: int, records_per_commit: int, payload_bytes: bytes):
    records = []
    for j in range(records_per_commit):
        slot = j % 4
        if slot == 0:
            records.append(
                NativeObjectRecord(
                    source_uri=f"file:///docs/doc-{index}-{j}.pdf",
                    locator=f"pdf:obj:{j}",
                    media_type="application/pdf",
                    extractor_name="marker",
                    extractor_version="1.0.0",
                )
            )
        elif slot == 1:
            records.append(
                ObservationRecord(
                    observer="marker",
                    derivation={"commit": index, "record": j, "stage": "layout"},
                    payload_bytes=payload_bytes,
                )
            )
        elif slot == 2:
            records.append(
                ClaimAssertionRecord(
                    claim_key=f"claim-{index}-{j}",
                    subject=f"doc:doc-{index}-{j}.pdf",
                    predicate="contains_table",
                    value=True,
                )
            )
        else:
            records.append(
                NativeFactRecord(
                    native_object_ref=records[j - 3].record_id
                    if j >= 3
                    else records[0].record_id,
                    property_name="page.count",
                    raw_representation=str(index * 100 + j),
                    typed_interpretation=index * 100 + j,
                    extractor_name="marker",
                    extractor_version="1.0.0",
                )
            )
    edges = (
        KernelEdge(
            edge_kind=EDGE_KIND_EVIDENCE_FOR,
            source_ref=records[1].record_id,
            target_ref=records[2].record_id,
        ),
    )
    return KernelCommitBatch(
        workspace_id="bench", records=tuple(records), edges=edges
    )


async def run(
    db_dir: Path,
    *,
    commits: int,
    records_per_commit: int,
    concurrent_writers: int,
) -> dict:
    db_path = db_dir / "kernel-bench.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    started = time.perf_counter()
    await upgrade_database(url=url)
    migration_seconds = round(time.perf_counter() - started, 3)

    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    payload_bytes = b"x" * 512

    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrent_writers)

    async def one_commit(index: int) -> None:
        batch = build_batch(index, records_per_commit, payload_bytes)
        async with sem:
            t0 = time.perf_counter()
            await service.commit(batch)
            latencies.append(time.perf_counter() - t0)

    started = time.perf_counter()
    await asyncio.gather(*(one_commit(i) for i in range(commits)))
    workload_seconds = round(time.perf_counter() - started, 3)

    sorted_lat = sorted(latencies)
    p50 = round(statistics.median(sorted_lat) * 1000, 2)
    p95 = round(sorted_lat[int(len(sorted_lat) * 0.95) - 1] * 1000, 2) if sorted_lat else 0.0

    t0 = time.perf_counter()
    replayed = await replay(factory, "bench")
    replay_seconds = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    verification = await verify_history(factory, "bench")
    verify_seconds = round(time.perf_counter() - t0, 3)
    await engine.dispose()

    wal_path = db_path.with_name(db_path.name + "-wal")
    conn = sqlite3.connect(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()

    return {
        "workload": {
            "commits": commits,
            "records_per_commit": records_per_commit,
            "edges_per_commit": 1,
            "payload_bytes_per_observation": len(payload_bytes),
            "concurrent_writers": concurrent_writers,
        },
        "totals": {
            "records": verification.checked_records,
            "edges": verification.checked_edges,
            "replay_digest": replayed.replay_digest,
            "verification_ok": verification.ok,
        },
        "storage": {
            "db_bytes": db_path.stat().st_size,
            "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            "journal_mode": journal_mode,
            "page_count": page_count,
        },
        "latency_ms": {"p50": p50, "p95": p95, "mean": round(statistics.mean(sorted_lat) * 1000, 2) if sorted_lat else 0.0},
        "duration_seconds": {
            "migration_to_head": migration_seconds,
            "commit_workload": workload_seconds,
            "full_replay": replay_seconds,
            "full_verification": verify_seconds,
        },
        "contention": {
            "busy_retries": service.busy_retries,
            "head_retries": service.head_retries,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "sqlite_library": sqlite3.sqlite_version,
            "sqlite_runtime": sqlite3.connect(":memory:").execute("select sqlite_version()").fetchone()[0],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commits", type=int, default=100)
    parser.add_argument("--records", dest="records_per_commit", type=int, default=12)
    parser.add_argument("--concurrency", dest="concurrent_writers", type=int, default=4)
    parser.add_argument("--keep", action="store_true", help="keep the scratch database")
    args = parser.parse_args()

    kwargs = {
        "commits": args.commits,
        "records_per_commit": args.records_per_commit,
        "concurrent_writers": args.concurrent_writers,
    }
    if args.keep:
        db_dir = Path(tempfile.mkdtemp(prefix="kernel-bench-"))
        report = asyncio.run(run(db_dir, **kwargs))
        print(json.dumps(report, indent=2))
        print(f"scratch dir kept: {db_dir}", file=sys.stderr)
    else:
        with tempfile.TemporaryDirectory(prefix="kernel-bench-") as tmp:
            report = asyncio.run(run(Path(tmp), **kwargs))
            print(json.dumps(report, indent=2))
    return 0 if report["totals"]["verification_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
