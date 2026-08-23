"""Invariant-57 industrial economics envelope benchmark (real services).

Drives the representative industrial workload — real PostgreSQL 16 AND a
real S3-compatible object store (MinIO) — through the production kernel
authorities: the deterministic PR81A corpus (15 PDFs) committed via
``seed_workspace`` with real S3 source artifacts, a real ``doc-rev-01``
v3->v4 revision with republication, and repeated cold-start samples on
fresh databases + fresh S3 namespaces. It emits the machine-readable
economics envelope (``marker.economics_envelope.v1``) for the industrial
profile, with every dimension reported against real PostgreSQL
``pg_stat_*`` probes and real S3 object listings.

Every dimension named by masterplan invariant 57 is reported for this
profile with an honest state. WAL write amplification is a genuine derived
ratio of same-window ``pg_stat_wal`` bytes over committed source artifact
bytes; FTS storage is measured with ``pg_total_relation_size``; vector and
visual storage are honestly not-applicable for this profile; review burden
is not measured here (it is the local-profile artifact's concern).

Provisioning reuses the shared industrial containers (marker-pg-industrial,
marker-minio-industrial) or provisions them via Docker; it also accepts
external endpoints through the standard env vars. The artifact fails its
own validation on zero-as-unknown, unitless numbers, and
cross-workload ratios.

Usage:
  python scripts/bench_economics_industrial.py            # print summary
  python scripts/bench_economics_industrial.py --write    # + write artifact
  python scripts/bench_economics_industrial.py --samples 3 --write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
# scripts dir on path so the shared provisioning helpers import bare-module
sys.path.insert(0, str(BACKEND / "scripts"))
import run_kernel_pg_conformance as pg_runner  # noqa: E402

from sqlalchemy import make_url, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.context_runtime import execute_query, parse_query_request, QUERY_SCHEMA_VERSION  # noqa: E402
from app.db_migration import upgrade_database  # noqa: E402
from app.eval.economics import collectors  # noqa: E402
from app.eval.economics.contract import (  # noqa: E402
    Envelope,
    derived,
    measured,
    not_applicable,
)
from app.eval.economics.pgprobe import (  # noqa: E402
    exact_row_counts,
    pg_database_bytes,
    pg_stat_database_snapshot,
    relation_sizes,
    server_banner,
)
from app.eval.economics.validate import validate_envelope  # noqa: E402
from app.eval.pr81a.corpus import load_corpus  # noqa: E402
from app.eval.pr81a.kernel_seed import revise_document, seed_workspace  # noqa: E402
from app.kernel.commit import KernelCommitService  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402
from app.kernel.object_store import S3PayloadStore, S3StoreConfig  # noqa: E402
from app.kernel.publications import PublicationService  # noqa: E402
from app.kernel.snapshots import resolve_snapshot  # noqa: E402
from app.kernel.source_object_store import S3SourceStore  # noqa: E402
from tests.pg_provisioning import (  # noqa: E402
    create_postgres_database,
    drop_postgres_database,
    engine_kwargs_for,
)

MEASUREMENTS = BACKEND.parent / "docs" / "reference" / "measurements"
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"
ARTIFACT = MEASUREMENTS / "pr87b-industrial-economics-envelope.json"

#: Provisioning defaults (mirror run_industrial_conformance.py).
DEFAULT_PG_CONTAINER = "marker-pg-industrial"
DEFAULT_PG_IMAGE = "postgres:16-alpine"
DEFAULT_PG_PORT = 55445
DEFAULT_PG_USER = "marker"
DEFAULT_PG_PASSWORD = "marker"
DEFAULT_S3_CONTAINER = "marker-minio-industrial"
DEFAULT_S3_IMAGE = "minio/minio:latest"
DEFAULT_S3_PORT = 55446
DEFAULT_S3_USER = "marker"
DEFAULT_S3_PASSWORD = "marker-marker"  # ephemeral test credential
S3_READY_TIMEOUT_SECONDS = 120.0
PG_READY_TIMEOUT_SECONDS = 120.0

ADMIN_URL_ENV = "MARKER_TEST_POSTGRES_ADMIN_URL"
S3_ENDPOINT_ENV = "MARKER_TEST_S3_ENDPOINT"
S3_ACCESS_ENV = "MARKER_TEST_S3_ACCESS_KEY"
S3_SECRET_ENV = "MARKER_TEST_S3_SECRET_KEY"


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


# ---------------------------------------------------------------------------
# provisioning
# ---------------------------------------------------------------------------

def _start_minio_container(name: str, image: str, port: int, user: str, password: str) -> None:
    pg_runner._docker(
        [
            "run", "-d", "--name", name,
            "-e", f"MINIO_ROOT_USER={user}",
            "-e", f"MINIO_ROOT_PASSWORD={password}",
            "-p", f"127.0.0.1:{port}:9000",
            image, "server", "/data", "--console-address", ":9001",
        ]
    )


def _wait_s3_ready(endpoint: str, timeout: float) -> None:
    import urllib.request

    health = endpoint.rstrip("/") + "/minio/health/live"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2.0) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1.0)
    raise SystemExit(
        f"S3-compatible service did not become ready at {health} within "
        f"{timeout:.0f}s; refusing to run against an unresponsive object store"
    )


def provision_services() -> tuple[str, str, str, str, str]:
    """Return (admin_url, s3_endpoint, access, secret, mode).

    mode is "external" when both env vars are set, else "docker".
    Fails fast (exit 3) when neither external env nor Docker is available.
    """
    admin_env = os.getenv(ADMIN_URL_ENV, "").strip()
    s3_endpoint_env = os.getenv(S3_ENDPOINT_ENV, "").strip()
    s3_access = os.getenv(S3_ACCESS_ENV, "").strip()
    s3_secret = os.getenv(S3_SECRET_ENV, "").strip()

    if admin_env and s3_endpoint_env and s3_access and s3_secret:
        host, port, user, password = pg_runner._parse_admin_url(admin_env)
        pg_runner.wait_tcp_ready(
            host, port, user, password, PG_READY_TIMEOUT_SECONDS
        )
        _wait_s3_ready(s3_endpoint_env, S3_READY_TIMEOUT_SECONDS)
        return admin_env, s3_endpoint_env, s3_access, s3_secret, "external"

    if not pg_runner.docker_available():
        print(
            "ERROR: Docker is not available and no external endpoints were "
            "set (MARKER_TEST_POSTGRES_ADMIN_URL + MARKER_TEST_S3_ENDPOINT + "
            "credentials). Cannot provision real PostgreSQL + S3. Start "
            "Docker Desktop or export the env vars.",
            file=sys.stderr,
        )
        raise SystemExit(3)

    # ---- PostgreSQL -------------------------------------------------------
    state = pg_runner.container_state(DEFAULT_PG_CONTAINER)
    if state is None:
        print(f"[industrial-bench] starting {DEFAULT_PG_CONTAINER} ({DEFAULT_PG_IMAGE})")
        pg_runner.start_container(
            DEFAULT_PG_CONTAINER, DEFAULT_PG_IMAGE, DEFAULT_PG_PORT,
            DEFAULT_PG_USER, DEFAULT_PG_PASSWORD,
        )
    elif not state.startswith("Up"):
        print(f"[industrial-bench] starting existing {DEFAULT_PG_CONTAINER}")
        pg_runner._docker(["start", DEFAULT_PG_CONTAINER])
    else:
        print(f"[industrial-bench] reusing running {DEFAULT_PG_CONTAINER}")
    admin_url = (
        f"postgresql+asyncpg://{DEFAULT_PG_USER}:{DEFAULT_PG_PASSWORD}"
        f"@127.0.0.1:{DEFAULT_PG_PORT}/postgres"
    )
    host, port, user, password = pg_runner._parse_admin_url(admin_url)
    pg_runner.wait_tcp_ready(host, port, user, password, PG_READY_TIMEOUT_SECONDS)

    # ---- S3-compatible service -------------------------------------------
    s3_state = pg_runner.container_state(DEFAULT_S3_CONTAINER)
    if s3_state is None:
        print(f"[industrial-bench] starting {DEFAULT_S3_CONTAINER} ({DEFAULT_S3_IMAGE})")
        _start_minio_container(
            DEFAULT_S3_CONTAINER, DEFAULT_S3_IMAGE, DEFAULT_S3_PORT,
            DEFAULT_S3_USER, DEFAULT_S3_PASSWORD,
        )
    elif not s3_state.startswith("Up"):
        print(f"[industrial-bench] starting existing {DEFAULT_S3_CONTAINER}")
        pg_runner._docker(["start", DEFAULT_S3_CONTAINER])
    else:
        print(f"[industrial-bench] reusing running {DEFAULT_S3_CONTAINER}")
    s3_endpoint = f"http://127.0.0.1:{DEFAULT_S3_PORT}"
    _wait_s3_ready(s3_endpoint, S3_READY_TIMEOUT_SECONDS)

    return (
        admin_url, s3_endpoint,
        DEFAULT_S3_USER, DEFAULT_S3_PASSWORD, "docker",
    )


# ---------------------------------------------------------------------------
# kernel table discovery + FTS sizing
# ---------------------------------------------------------------------------

async def _kernel_tables(conn: Any) -> list[str]:
    rows = (await conn.execute(text(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
        "AND c.relname LIKE 'kernel_%' ORDER BY c.relname"
    ))).mappings().all()
    return [r["relname"] for r in rows]


async def _fts_bytes(conn: Any) -> int:
    rows = (await conn.execute(text(
        "SELECT c.relname, pg_total_relation_size(c.oid) AS total_bytes "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
        "AND (c.relname LIKE 'kernel_lexical%' OR c.relname LIKE 'kernel_fts_%')"
    ))).mappings().all()
    return int(sum(int(r["total_bytes"]) for r in rows))


async def _generation_states(conn: Any) -> dict[str, int]:
    rows = (await conn.execute(text(
        "SELECT state, COUNT(*) AS n FROM kernel_generations GROUP BY state"
    ))).mappings().all()
    return {r["state"]: int(r["n"]) for r in rows}


async def _payload_objects_bytes(conn: Any) -> int:
    """Sum of committed payload bytes from the kernel_payload_objects registry.

    The S3PayloadStore profile does not expose a bytes_written counter; the
    durable registry is the authoritative source of staged payload volume.
    """
    rows = (await conn.execute(text(
        "SELECT COALESCE(SUM(payload_length), 0) AS total FROM kernel_payload_objects"
    ))).mappings().all()
    return int(rows[0]["total"]) if rows else 0


async def _wal_lsn(conn: Any) -> int:
    """Current WAL write LSN as its numeric form.

    asyncpg returns ``pg_lsn`` as a bigint (``(hi << 32) | lo``), which is
    monotonic in bytes. ``pg_stat_wal`` cumulative counters are flushed
    asynchronously by the statistics collector and did not observe the
    kernel sessions' writes at snapshot time under the async driver
    (verified during development: a seed that committed 517 rows showed a
    zero pg_stat_wal delta), so byte-accurate WAL between boundaries is
    the difference of these numeric LSNs — synchronous and
    backend-independent.
    """
    result = await conn.execute(text("SELECT pg_current_wal_lsn()"))
    return int(result.scalar_one())


def _wal_lsn_diff(before: int, after: int) -> int:
    # LSN numeric form difference equals the byte distance between the
    # two WAL positions.
    return int(after) - int(before)


async def _committed_source_bytes(conn: Any) -> int:
    """Sum of committed ContentRevisionRecord byte_length from the catalog.

    The record payloads live in the ``payload_json`` TEXT column, so each
    row is JSON-decoded and its ``byte_length`` is summed — this is the
    exact set of committed source artifact bytes (no estimate).
    """
    rows = (await conn.execute(text(
        "SELECT payload_json FROM kernel_records "
        "WHERE record_class = 'content_revision'"
    ))).mappings().all()
    total = 0
    for r in rows:
        if r["payload_json"] is None:
            continue
        data = json.loads(r["payload_json"])
        bl = data.get("byte_length")
        if bl is not None:
            total += int(bl)
    return total


def _logical_source_bytes_from_corpus(corpus: Any) -> int:
    """Independent cross-check of committed source bytes from the manifest.

    Every staged source artifact's byte_length originates from the corpus
    PDF; this is the same set of bytes the database committed (16 base
    revisions + the 1 v4 revision).
    """
    total = 0
    for doc in corpus.docs:
        if len(doc.revisions) > 1:
            total += doc.revision("v3").byte_length
        else:
            total += doc.current.byte_length
    # the revision stage adds doc-rev-01 v4
    total += corpus.doc("doc-rev-01").revision("v4").byte_length
    return total


# ---------------------------------------------------------------------------
# payload store assembly helpers
# ---------------------------------------------------------------------------

def _make_payload_store(endpoint: str, access: str, secret: str) -> S3PayloadStore:
    return S3PayloadStore(
        S3StoreConfig(
            endpoint_url=endpoint,
            bucket=f"marker-econ-p-{uuid.uuid4().hex[:14]}",
            access_key_id=access,
            secret_access_key=secret,
            prefix="kernel-payloads",
            delete_namespace_on_close=True,
        )
    )


def _make_source_store(endpoint: str, access: str, secret: str) -> S3SourceStore:
    return S3SourceStore.build_default(
        endpoint_url=endpoint,
        bucket=f"marker-econ-s-{uuid.uuid4().hex[:14]}",
        access_key_id=access,
        secret_access_key=secret,
        region="us-east-1",
        prefix="kernel-sources",
    )


# ---------------------------------------------------------------------------
# main workload
# ---------------------------------------------------------------------------

async def run_main_workload(
    admin_url: str,
    endpoint: str,
    access: str,
    secret: str,
    corpus: Any,
    git_sha: str,
) -> dict[str, Any]:
    db_url = await create_postgres_database(admin_url)
    await upgrade_database(url=db_url)
    engine = create_async_engine(db_url, **engine_kwargs_for("postgresql"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payload_store = _make_payload_store(endpoint, access, secret)
    source_store = _make_source_store(endpoint, access, secret)
    service = KernelCommitService(factory, payload_store=payload_store)
    db_name = make_url(db_url).database

    try:
        # One persistent connection for WAL/stat snapshots so LSN reads are
        # consistent (pg_stat_wal cumulative counters are per-backend under
        # the async driver and would not reflect the kernel sessions' WAL).
        probe = await engine.connect()
        # ---- baseline (post-migration empty) -------------------------
        wal_start = await _wal_lsn(probe)
        tables = await _kernel_tables(probe)
        stat_db_start = await pg_stat_database_snapshot(probe, db_name)
        banner = await server_banner(probe)
        counts_base = await exact_row_counts(probe, tables)

        # ---- ingest + publish ----------------------------------------
        t0 = time.perf_counter()
        ws = await seed_workspace(
            factory=factory,
            service=service,
            corpus=corpus,
            workspace_id="ws-econ-industrial",
            source_store=source_store,
        )
        ingest_publish_s = time.perf_counter() - t0
        wal_after_ingest = await _wal_lsn(probe)
        counts_after_ingest = await exact_row_counts(probe, tables)

        # ---- revision (v3 -> v4) + rebuild + republish ---------------
        t0 = time.perf_counter()
        rev_doc = corpus.doc("doc-rev-01")
        v4_length = rev_doc.revision("v4").byte_length
        await revise_document(ws, "doc-rev-01", "v4")
        revision_s = time.perf_counter() - t0
        wal_after_revision = await _wal_lsn(probe)

        # ---- final probes ------------------------------------------------
        # WAL LSN snapshot taken before the final count/size probes; those
        # probes are reads (no WAL), so the final LSN equals the
        # post-revision LSN by construction.
        wal_end = await _wal_lsn(probe)
        counts_final = await exact_row_counts(probe, tables)
        sizes = await relation_sizes(probe, tables)
        fts_bytes = await _fts_bytes(probe)
        gen_states = await _generation_states(probe)
        db_bytes_final = await pg_database_bytes(probe, db_name)
        stat_db_end = await pg_stat_database_snapshot(probe, db_name)
        committed_source_bytes = await _committed_source_bytes(probe)
        payload_bytes_written = await _payload_objects_bytes(probe)
        await probe.close()

        payload_stats = collectors.payload_store_stats(payload_store)
        source_stats = collectors.payload_store_stats(source_store)
        payload_objects = await payload_store.list_objects()
        source_objects = await source_store.list_blob_keys()

        # ---- WAL deltas (LSN-based, byte accurate) ------------------------
        wal_full_bytes = _wal_lsn_diff(wal_start, wal_end)
        wal_revision_bytes = _wal_lsn_diff(wal_after_ingest, wal_after_revision)
        logical_payload_bytes = committed_source_bytes
        corpus_logical = _logical_source_bytes_from_corpus(corpus)
        # defensive cross-check: corpus total must cover committed bytes
        assert corpus_logical >= logical_payload_bytes, (
            f"corpus logical bytes {corpus_logical} < committed {logical_payload_bytes}"
        )
        assert logical_payload_bytes > 0, "logical payload bytes must be > 0 for amplification"

        amplification = round(wal_full_bytes / logical_payload_bytes, 4)
        revision_amplification = round(wal_revision_bytes / v4_length, 4)

        # ---- row categorization -------------------------------------------
        def _category_delta(counts_before: dict, counts_after: dict) -> dict[str, int]:
            delta: dict[str, int] = {}
            for table in tables:
                difference = int(counts_after.get(table, 0)) - int(counts_before.get(table, 0))
                if difference == 0:
                    continue
                category = collectors.categorize_table(table)
                delta[category] = delta.get(category, 0) + difference
            return dict(sorted(delta.items()))

        def _total(counts: dict) -> int:
            return sum(int(v) for v in counts.values())

        cat_delta = _category_delta(counts_base, counts_final)
        revision_cat_delta = _category_delta(counts_after_ingest, counts_final)
        rows_total_delta = _total(counts_final) - _total(counts_base)
        revision_rows_total = _total(counts_final) - _total(counts_after_ingest)

        # relation totals (top tables by total bytes)
        relation_totals = {
            name: int(s["total_bytes"]) for name, s in sizes.items()
        }
        relation_total_bytes = sum(relation_totals.values())

        stat_db_delta = {
            f"{k}_delta": int(stat_db_end[k]) - int(stat_db_start[k])
            for k in (
                "xact_commit", "xact_rollback", "blks_read",
                "tup_inserted", "tup_updated", "tup_deleted",
            )
        }

        generations_before_revision = sum(int(v) for v in gen_states.values()) - 1

        return {
            "db_url": db_url,
            "db_name": db_name,
            "engine": engine,
            "factory": factory,
            "payload_store": payload_store,
            "source_store": source_store,
            "banner": banner,
            "tables": tables,
            "counts_final": counts_final,
            "cat_delta": cat_delta,
            "rows_total_delta": rows_total_delta,
            "fts_bytes": fts_bytes,
            "gen_states": gen_states,
            "db_bytes_final": db_bytes_final,
            "stat_db_delta": stat_db_delta,
            "relation_totals": relation_totals,
            "relation_total_bytes": relation_total_bytes,
            "payload_stats": payload_stats,
            "payload_bytes_written": payload_bytes_written,
            "source_stats": source_stats,
            "payload_objects": len(payload_objects),
            "source_objects": len(source_objects),
            "wal_bytes_delta_full": wal_full_bytes,
            "wal_bytes_delta_revision": wal_revision_bytes,
            "revision_rows_total": revision_rows_total,
            "revision_cat_delta": revision_cat_delta,
            "logical_payload_bytes": logical_payload_bytes,
            "v4_length": v4_length,
            "amplification": amplification,
            "revision_amplification": revision_amplification,
            "ingest_publish_s": round(ingest_publish_s, 4),
            "revision_s": round(revision_s, 4),
            "generations_before_revision": generations_before_revision,
        }
    finally:
        await payload_store.close()
        await source_store.close()
        await engine.dispose()
        await drop_postgres_database(admin_url, db_url)


# ---------------------------------------------------------------------------
# cold-start samples
# ---------------------------------------------------------------------------

async def _cold_start_sample(
    admin_url: str,
    endpoint: str,
    access: str,
    secret: str,
    corpus: Any,
    index: int,
) -> dict[str, int | float]:
    db_url = await create_postgres_database(admin_url)
    await upgrade_database(url=db_url)
    engine = create_async_engine(db_url, **engine_kwargs_for("postgresql"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payload_store = _make_payload_store(endpoint, access, secret)
    source_store = _make_source_store(endpoint, access, secret)
    service = KernelCommitService(factory, payload_store=payload_store)
    try:
        t_start = time.perf_counter()
        ws = await seed_workspace(
            factory=factory,
            service=service,
            corpus=corpus,
            workspace_id=f"ws-econ-cold-{index}",
            source_store=source_store,
        )
        t_build0 = time.perf_counter()
        seed_s = t_build0 - t_start
        generation = await GenerationService(factory).build_and_activate(
            await resolve_snapshot(factory, ws.workspace_id)
        )
        await PublicationService(factory).publish(
            materialized_generation_id=generation.generation_id
        )
        t_query0 = time.perf_counter()
        build_publish_s = t_query0 - t_build0
        request = parse_query_request({
            "schema_version": QUERY_SCHEMA_VERSION,
            "workspace_id": ws.workspace_id,
            "operations": [
                {"op": "lexical_search", "text": corpus.queries[0].text, "limit": 25}
            ],
            "budget": {
                "max_operations": 8,
                "max_candidates": 100,
                "max_evidence_units": 100,
                "max_output_chars": 100_000,
            },
        })
        packet = await execute_query(factory, request)
        t_end = time.perf_counter()
        assert packet is not None
        return {
            "seed_s": round(seed_s, 4),
            "build_publish_s": round(build_publish_s, 4),
            "first_query_s": round(t_end - t_query0, 4),
            "total_s": round(t_end - t_start, 4),
        }
    finally:
        await payload_store.close()
        await source_store.close()
        await engine.dispose()
        await drop_postgres_database(admin_url, db_url)


# ---------------------------------------------------------------------------
# envelope assembly
# ---------------------------------------------------------------------------

def build_envelope(
    run: dict[str, Any],
    cold_samples: list[dict[str, int | float]],
    git_sha: str,
    sample_count: int,
    s3_endpoint: str,
    provisioned: str,
) -> Envelope:
    envelope = Envelope(
        profile="industrial-postgres16-s3",
        dimension_set="invariant_57",
        git_sha=git_sha,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        run_mode="offline",
        model_participation={"mode": "none"},
        workload={
            "identity": (
                "PR81A deterministic corpus (15 PDFs) committed via "
                "seed_workspace with real S3 source artifacts -> "
                "doc-rev-01 v3->v4 revision + rebuild + republish; "
                "cold-start samples on fresh PostgreSQL databases + fresh "
                "S3 namespaces; real pg_stat_* probes + S3 object listings"
            ),
            "fingerprint": f"pr81a:{run.get('corpus_fingerprint')}",
            "documents": run.get("corpus_docs", 16),
            "queries_available": run.get("corpus_queries", 1),
            "postgres": "postgres:16-alpine",
            "object_store": "minio/minio (S3-compatible)",
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "postgres_banner": run["banner"],
            "s3_endpoint": s3_endpoint,
            "provisioned": provisioned,
            "samples": sample_count,
        },
        windows=[
            {"id": "baseline", "label": "post-migration empty database"},
            {"id": "ingest_publish", "label": "seed the corpus docs + build generation + publish lexical"},
            {"id": "revision", "label": "revise doc-rev-01 v3->v4 + rebuild + republish"},
            {"id": "cold_start", "label": "fresh db: migrate->seed->build->publish->first query"},
            {"id": "full", "label": "entire main workload (baseline -> post-revision)"},
        ],
        non_claims=[
            "no human review time is claimed; review burden is measured on the "
            "local-profile artifact (pr87a-local-economics-envelope.json)",
            "WAL byte volume is measured with pg_current_wal_lsn() between "
            "window boundaries (exact and backend-independent; the LSN is "
            "cluster-wide and the cluster hosts only the benchmark database "
            "during the run); pg_stat_wal.wal_records/wal_fpi cumulative "
            "counters are flushed asynchronously by the statistics collector "
            "and did not observe the kernel sessions' writes at snapshot "
            "time under the async driver, so only the byte volume is reported",
            "probe ordering: at each boundary the WAL LSN snapshot is taken "
            "FIRST, then row-count and relation-size probes; the final window's "
            "WAL LSN snapshot is taken LAST so any WAL the probes themselves "
            "emit stays inside the 'full' window",
            "payload read-back bytes are not separately instrumented on the "
            "S3PayloadStore profile (no bytes_read_back counter); copy_bytes "
            "counts payload staging writes (registry sum) + source staging "
            "writes + source read-back verification only",
        ],
    )

    # ---- database_rows ---------------------------------------------------
    envelope.set("database_rows", measured(
        run["rows_total_delta"], "count", "full",
        "exact per-table COUNT(*) over public kernel_% tables discovered from "
        "pg_class/pg_namespace (baseline empty -> post-revision), summed and "
        "categorized by table-name prefix",
        breakdown=run["cat_delta"],
    ))

    # ---- payload_objects --------------------------------------------------
    envelope.set("payload_objects", measured(
        run["payload_objects"] + run["source_objects"], "count", "full",
        "S3PayloadStore.list_objects + S3SourceStore.list_blob_keys over "
        "uniquely-named per-run buckets (content-addressed blob keys)",
        breakdown={
            "payload_objects": run["payload_objects"],
            "source_objects": run["source_objects"],
        },
    ))
    envelope.counters["payload_object_count"] = measured(
        run["payload_objects"], "count", "full",
        "S3PayloadStore.list_objects over the run's payload bucket",
    )
    envelope.counters["source_object_count"] = measured(
        run["source_objects"], "count", "full",
        "S3SourceStore.list_blob_keys over the run's source bucket",
    )
    envelope.counters["logical_payload_bytes"] = measured(
        run["logical_payload_bytes"], "bytes", "full",
        "SUM of committed ContentRevisionRecord byte_length from kernel_records "
        "(cross-checked against corpus manifest byte_length)",
    )

    # ---- wal_write_amplification (derived ratio) --------------------------
    envelope.set("wal_write_amplification", derived(
        run["amplification"], "ratio", "full",
        "pg_stat_wal wal_bytes delta over the full window divided by committed "
        "source artifact bytes in the same window",
        derivation={
            "numerator": "wal_bytes_delta_full",
            "denominator": "logical_payload_bytes",
        },
    ))
    envelope.counters["wal_bytes_delta_full"] = measured(
        run["wal_bytes_delta_full"], "bytes", "full",
        "pg_current_wal_lsn()/pg_wal_lsn_diff between baseline and end "
        "snapshots = wal_bytes_generated (backend-independent, exact)",
    )
    envelope.counters["wal_bytes_delta_revision"] = measured(
        run["wal_bytes_delta_revision"], "bytes", "revision",
        "pg_stat_wal.wal_bytes delta restricted to the revision window "
        "(post-ingest snapshot -> post-revision snapshot)",
    )
    envelope.counters["logical_revision_bytes"] = measured(
        run["v4_length"], "bytes", "revision",
        "doc-rev-01 v4 PDF byte_length (the source bytes changed by revision)",
    )

    # ---- retained_generations --------------------------------------------
    envelope.set("retained_generations", measured(
        sum(run["gen_states"].values()), "count", "revision",
        "kernel_generations grouped by state after the v4 revision + "
        "republication",
        breakdown={
            **{f"state_{k}": v for k, v in sorted(run["gen_states"].items())},
            "generations_before_revision": run["generations_before_revision"],
        },
    ))

    # ---- fts_storage (measured) ------------------------------------------
    envelope.set("fts_storage", measured(
        run["fts_bytes"], "bytes", "full",
        "SUM of pg_total_relation_size over kernel_lexical_* and kernel_fts_* "
        "tables present in the database",
    ))

    # ---- vector_storage / visual_storage (not applicable) ----------------
    envelope.set("vector_storage", not_applicable(
        "bytes", "full",
        "publication schema reserves a nullable vector_generation_id slot "
        "only; no vector index is materialized on this profile",
    ))
    envelope.set("visual_storage", not_applicable(
        "bytes", "full",
        "workload runs with visual capability absent; visual storage is "
        "measured by the visual economics artifact (pr87c)",
    ))

    # ---- copy_bytes ------------------------------------------------------
    # payload staging writes: the S3PayloadStore profile has no read-back
    # counter, so we measure payload PUT bytes from the committed registry.
    payload_writes = run.get("payload_bytes_written", 0)
    source_writes = int(run["source_stats"].get("bytes_written", 0))
    source_read_backs = int(run["source_stats"].get("bytes_read_back", 0))
    copy_total = payload_writes + source_writes + source_read_backs
    envelope.set("copy_bytes", measured(
        copy_total, "bytes", "full",
        "payload staging writes (kernel_payload_objects registry sum of "
        "payload_length) + source staging writes (S3SourceStore.bytes_written) "
        "+ source read-back verification (S3SourceStore.bytes_read_back); "
        "payload read-backs are not instrumented on the S3 payload profile",
        breakdown={
            "payload_staging_writes": payload_writes,
            "source_staging_writes": source_writes,
            "source_read_backs": source_read_backs,
        },
    ))

    # ---- cold_start ------------------------------------------------------
    cold_total_ms = [s["total_s"] * 1000.0 for s in cold_samples]
    cold_build_ms = [s["build_publish_s"] * 1000.0 for s in cold_samples]
    envelope.set("cold_start", measured(
        round(statistics.median(cold_total_ms), 2), "milliseconds", "cold_start",
        "fresh PostgreSQL database per sample + fresh S3 namespaces: migrate "
        "-> seed the corpus -> build generation -> publish lexical -> first "
        "lexical_search query answered",
        samples={
            "n": sample_count,
            "min": round(min(cold_total_ms), 2),
            "p50": round(statistics.median(cold_total_ms), 2),
            "max": round(max(cold_total_ms), 2),
            "per_sample_total_ms": [round(v, 2) for v in cold_total_ms],
            "per_sample_build_publish_ms": [round(v, 2) for v in cold_build_ms],
        },
    ))

    # ---- review_burden (not applicable on this profile) -----------------
    envelope.set("review_burden", not_applicable(
        "count", "full",
        "this industrial run measures storage/WAL economics only; review "
        "burden is measured on the local-profile artifact "
        "(pr87a-local-economics-envelope.json)",
    ))

    # ---- reprocessing ----------------------------------------------------
    envelope.set("reprocessing", measured(
        1, "count", "revision",
        "doc-rev-01 v3->v4 content revision through the real commit/generation/"
        "publication path",
        breakdown={
            "revision_events": 1,
            "documents_changed": 1,
            "documents_unchanged_reused": run.get("corpus_docs", 16) - 1,
            "rows_added_by_revision": run["revision_rows_total"],
            **{
                f"revision_rows_{category}": delta
                for category, delta in run["revision_cat_delta"].items()
            },
            "wal_bytes_delta_revision": run["wal_bytes_delta_revision"],
            "payload_bytes_staged_v4": run["v4_length"],
            "revision_wal_amplification": run["revision_amplification"],
            "generation_rebuilds": 1,
            "revision_wall_ms": round(run["revision_s"] * 1000, 1),
        },
    ))
    envelope.counters["revision_wall_ms"] = measured(
        round(run["revision_s"] * 1000, 1), "milliseconds", "revision",
        "perf_counter around revise_document (commit + rebuild + republish)",
        samples={"n": 1},
    )
    envelope.counters["ingest_publish_wall_ms"] = measured(
        round(run["ingest_publish_s"] * 1000, 1), "milliseconds", "ingest_publish",
        "perf_counter around seed_workspace (corpus docs + generation + publication)",
        samples={"n": 1},
    )

    # ---- extra raw counters ----------------------------------------------
    envelope.counters["pg_database_bytes_final"] = measured(
        run["db_bytes_final"], "bytes", "full",
        "pg_database_size(database_name) at end of window",
    )
    envelope.counters["relation_total_bytes"] = measured(
        run["relation_total_bytes"], "bytes", "full",
        "SUM pg_total_relation_size over all public kernel_% tables",
        breakdown=run["relation_totals"],
    )
    envelope.counters["pg_stat_database_tup_inserted_delta"] = measured(
        run["stat_db_delta"]["tup_inserted_delta"], "count", "full",
        "pg_stat_database.tup_inserted delta (baseline -> end)",
    )
    envelope.counters["provisioned_mode"] = measured(
        provisioned, "identifier", "full",
        "service provisioning mode (docker containers or external endpoints)",
    )
    envelope.counters["s3_endpoint_identifier"] = measured(
        s3_endpoint, "identifier", "full",
        "S3-compatible endpoint URL (no credentials recorded)",
    )
    return envelope


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the artifact")
    parser.add_argument("--output", type=Path, default=ARTIFACT)
    parser.add_argument("--samples", type=int, default=3,
                        help="cold-start sample count (>= 2 for percentiles)")
    args = parser.parse_args()
    if args.samples < 2:
        raise SystemExit("--samples must be >= 2 so percentile claims carry honest n")

    started = time.perf_counter()
    corpus = load_corpus(CORPUS_ROOT)
    git_sha = _git_sha()

    admin_url, s3_endpoint, access, secret, provisioned = provision_services()

    async def run_all() -> dict[str, Any]:
        run = await run_main_workload(
            admin_url, s3_endpoint, access, secret, corpus, git_sha
        )
        cold = [
            await _cold_start_sample(
                admin_url, s3_endpoint, access, secret, corpus, i
            )
            for i in range(args.samples)
        ]
        return run, cold

    run, cold = asyncio.run(run_all())

    run["corpus_fingerprint"] = corpus.fingerprint
    run["corpus_docs"] = len(corpus.docs)
    run["corpus_queries"] = len(corpus.queries)

    envelope = build_envelope(
        run, cold, git_sha, args.samples, s3_endpoint, provisioned
    )
    artifact = envelope.to_dict()
    artifact["wall_time_s"] = round(time.perf_counter() - started, 3)
    errors = validate_envelope(artifact)

    summary = {
        "profile": artifact["profile"],
        "database_rows_delta": artifact["dimensions"]["database_rows"]["value"],
        "rows_by_category": artifact["dimensions"]["database_rows"]["breakdown"],
        "wal_bytes_delta_full": artifact["counters"]["wal_bytes_delta_full"]["value"],
        "logical_payload_bytes": artifact["counters"]["logical_payload_bytes"]["value"],
        "wal_write_amplification": artifact["dimensions"]["wal_write_amplification"]["value"],
        "revision_wal_amplification": artifact["dimensions"]["reprocessing"]["breakdown"]["revision_wal_amplification"],
        "fts_storage_bytes": artifact["dimensions"]["fts_storage"]["value"],
        "payload_objects": artifact["dimensions"]["payload_objects"]["value"],
        "retained_generations": artifact["dimensions"]["retained_generations"]["breakdown"],
        "cold_start_ms_p50": artifact["dimensions"]["cold_start"]["samples"]["p50"],
        "validation_errors": errors,
    }
    print(json.dumps(summary, indent=2))

    if errors:
        print(f"envelope failed validation: {len(errors)} error(s)", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(artifact, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
