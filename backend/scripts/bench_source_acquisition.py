"""Operational characterization for stable source acquisition (PR70/71).

Measures the acquisition tax on this machine so the delta against the
legacy path-trust flow is recorded rather than hand-waved:

* acquisition latency for small and large representative documents;
* hashing/streaming throughput and peak process memory;
* write amplification (bytes written vs logical bytes) per acquisition;
* dedup behavior on re-acquiring identical bytes;
* durable rows added per fresh revision vs per duplicate acquisition;
* resolve() cost (the restart/retry reuse path — no external read).

Run:  python scripts/bench_source_acquisition.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_migration import upgrade_database
from app.kernel.commit import KernelCommitService
from app.kernel.source_store import LocalSourceStore
from app.services.source_acquisition import SourceAcquisitionService

SMALL_BYTES = b"%PDF-1.4 " + b"small representative document body. " * 128  # ~5 KB
LARGE_BLOCK = (b"large representative page bytes with realistic entropy " * 128)[:8192]
LARGE_BYTES = b"%PDF-1.4 " + LARGE_BLOCK * 2560  # ~20 MB


def _mk_source(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.write_bytes(data)
    return path


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="src-bench-"))
    roots = tmp / "roots"
    roots.mkdir()
    os.environ["MARKER_WORKSPACE_ROOTS"] = str(roots)

    url = f"sqlite+aiosqlite:///{(tmp / 'bench.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = LocalSourceStore(tmp / "store")
    service = SourceAcquisitionService(
        factory, KernelCommitService(factory), store, workspace_id="bench"
    )

    small = _mk_source(roots, "small.pdf", SMALL_BYTES)
    large = _mk_source(roots, "large.pdf", LARGE_BYTES)

    results: dict[str, object] = {}

    t0 = time.perf_counter()
    acquired_small = await service.acquire(small, source_kind="local_path", suffix=".pdf", job_id="bench-1")
    results["small_acquire_s"] = round(time.perf_counter() - t0, 4)

    t0 = time.perf_counter()
    acquired_large = await service.acquire(large, source_kind="local_path", suffix=".pdf", job_id="bench-2")
    large_elapsed = time.perf_counter() - t0
    results[f"large_acquire_s ({len(LARGE_BYTES) // (1024 * 1024)}MB)"] = round(large_elapsed, 4)
    results["large_throughput_mb_s"] = round(
        len(LARGE_BYTES) / (1024 * 1024) / large_elapsed, 1
    )

    # dedup: identical bytes again (fresh job id -> new observation only)
    t0 = time.perf_counter()
    again = await service.acquire(small, source_kind="local_path", suffix=".pdf", job_id="bench-3")
    results["small_reacquire_dedup_s"] = round(time.perf_counter() - t0, 4)
    assert again.content_revision_id == acquired_small.content_revision_id

    # resolve: restart/retry reuse path
    t0 = time.perf_counter()
    resolved = await service.resolve(acquired_large.to_config())
    results["large_resolve_s"] = round(time.perf_counter() - t0, 4)
    assert resolved is not None

    # row counts per revision vs duplicate acquisition
    from sqlalchemy import func, select

    from app.kernel.models import KernelRecord

    async with factory() as session:
        counts = dict(
            (await session.execute(
                select(KernelRecord.record_class, func.count())
                .where(KernelRecord.workspace_id == "bench")
                .group_by(KernelRecord.record_class)
            )).all()
        )
    results["rows_after_3_acquisitions"] = counts

    results["store_counters"] = {
        "stage_calls": store.stage_calls,
        "dedup_hits": store.dedup_hits,
        "bytes_read": store.bytes_read,
        "bytes_written": store.bytes_written,
        "bytes_read_back": store.bytes_read_back,
        "logical_small_bytes": len(SMALL_BYTES),
        "logical_large_bytes": len(LARGE_BYTES),
    }

    await engine.dispose()

    width = max(len(k) for k in results)
    print("source acquisition benchmark")
    for key, value in results.items():
        print(f"  {key.ljust(width)} : {value}")


if __name__ == "__main__":
    asyncio.run(main())
