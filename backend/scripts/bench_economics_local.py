"""Invariant-57 local economics envelope benchmark.

Drives the representative local workload through the real kernel
authorities on SQLite — the deterministic PR81A corpus (16 PDFs / 27
pages) committed via ``seed_workspace``, a real document revision with
republication, the publication-pin/GC retention lifecycle, a
review-required extraction scenario, and repeated cold-start samples —
and emits the machine-readable economics envelope
(``marker.economics_envelope.v1``).

Every dimension named by masterplan invariant 57 is reported for this
profile with an honest state: measured, or explicitly
unavailable/not-applicable (vector storage has no implementation;
physical FTS byte attribution needs the DBSTAT virtual table this
SQLite build does not expose; SQLite runs in rollback-journal mode so
there is no WAL to amplify). The artifact fails its own validation on
zero-as-unknown, unitless numbers, and unmatched-window ratios.

Usage:
  python scripts/bench_economics_local.py            # print summary
  python scripts/bench_economics_local.py --write    # + write artifact
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.context_runtime import execute_query, parse_query_request, QUERY_SCHEMA_VERSION  # noqa: E402
from app.db_migration import upgrade_database  # noqa: E402
from app.eval.economics import collectors  # noqa: E402
from app.eval.economics.contract import (  # noqa: E402
    Envelope,
    derived,
    measured,
    not_applicable,
    unavailable,
)
from app.eval.economics.validate import validate_envelope  # noqa: E402
from app.eval.pr81a.corpus import load_corpus  # noqa: E402
from app.eval.pr81a.kernel_seed import revise_document, seed_workspace  # noqa: E402
from app.extraction.contract import INVOICE_SCHEMA, ExtractionRequest  # noqa: E402
from app.extraction.results import (  # noqa: E402
    FIELD_OUTCOME_REVIEW_REQUIRED,
    RUN_REVIEW_REQUIRED,
)
from app.extraction.review import ReviewDecision  # noqa: E402
from app.extraction.service import ExtractionService  # noqa: E402
from app.kernel import gc as kernel_gc  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.patches import ViewDocumentRecord  # noqa: E402
from app.kernel.payloads import LocalPayloadStore  # noqa: E402
from app.kernel.publications import (  # noqa: E402
    PublicationService,
    acquire_publication_pin,
    release_publication_pin,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph  # noqa: E402
from app.kernel.snapshots import resolve_snapshot  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402

MEASUREMENTS = BACKEND.parent / "docs" / "reference" / "measurements"
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"
ARTIFACT = MEASUREMENTS / "pr87a-local-economics-envelope.json"
REVIEW_WS = "ws-econ-review"
MAIN_WS = "ws-econ-local"


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _snapshot(db_path: Path, payload_root: Path, source_root: Path) -> dict:
    conn = collectors.sqlite_connect_readonly(db_path)
    try:
        counts = collectors.table_row_counts(conn)
        states = collectors.generation_state_counts(conn)
        lexical = collectors.lexical_generation_stats(conn)
    finally:
        conn.close()
    profile = collectors.storage_profile(db_path)
    return {
        "rows_total": sum(counts.values()),
        "rows_by_category": collectors.row_counts_by_category(counts),
        "generation_states": states,
        "lexical_generations": lexical,
        "storage": profile,
        "payload_store": collectors.object_store_profile(payload_root),
        "source_store": collectors.object_store_profile(source_root),
    }


def _doc(record_id: str, texts: dict[str, str]) -> ViewDocumentRecord:
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    return ViewDocumentRecord(
        record_id=f"view.{record_id}",
        content_revision_ref=f"rev.{record_id}",
        graph=graph,
        texts=dict(texts),
        view_id=f"doc-{record_id}",
    )


async def _review_scenario(tmp: Path) -> dict:
    """Conflicting invoice candidates -> review-required burden, exactly counted.

    Mirrors the PR80A extraction seam on its own database so the main
    workload's row envelope stays attributable to the corpus workload.
    """
    url = f"sqlite+aiosqlite:///{(tmp / 'review.db').as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory, payload_store=LocalPayloadStore(tmp / "review-payloads"))
    try:
        def _header(total: str) -> dict[str, str]:
            return {
                "h1": "Invoice Number: INV-2026-042",
                "h2": "Invoice Date: 2026-03-01",
                "h3": "Currency: USD",
                "h4": f"Total Due: {total}",
            }

        items = {
            f"r{i}": row for i, row in enumerate(
                [
                    "LINEITEM | SKU-1 | Widget | 2 | 9.99 | 19.98",
                    "LINEITEM | SKU-2 | Gadget | 3 | 15.00 | 45.00",
                    "LINEITEM | SKU-3 | Gizmo | 1 | 89.99 | 89.99",
                ],
                start=1,
            )
        }
        await service.commit(
            KernelCommitBatch(
                workspace_id=REVIEW_WS,
                records=(
                    _doc("invoice-header", _header("154.97")),
                    _doc("invoice-header-alt", _header("777.77")),
                    _doc("invoice-items", items),
                ),
            )
        )
        generation = await GenerationService(factory).build_and_activate(
            await resolve_snapshot(factory, REVIEW_WS)
        )
        await PublicationService(factory).publish(
            materialized_generation_id=generation.generation_id
        )

        conn = collectors.sqlite_connect_readonly(tmp / "review.db")
        try:
            rows_before = sum(collectors.table_row_counts(conn).values())
        finally:
            conn.close()
        extraction = ExtractionService(factory, service, workspace_id=REVIEW_WS)
        result = await extraction.run(
            ExtractionRequest(
                schema_id=INVOICE_SCHEMA.schema_id,
                schema_version=INVOICE_SCHEMA.version,
                workspace_id=REVIEW_WS,
            )
        )
        review_fields = sorted(
            name for name, field in result.fields.items()
            if field.status == FIELD_OUTCOME_REVIEW_REQUIRED
        )
        review_runs = 1 if result.run_status == RUN_REVIEW_REQUIRED else 0

        decisions_applied = 0
        if review_fields:
            await extraction.apply_review(
                ReviewDecision(
                    result_identity=result.identity,
                    schema_identity=result.schema_identity,
                    publication_set_id=result.context.publication_set_id,
                    field_path=review_fields[0],
                    action="accept",
                    reviewer="econ-bench@example.test",
                    rationale="economics bench review probe",
                )
            )
            decisions_applied = 1

        conn = collectors.sqlite_connect_readonly(tmp / "review.db")
        try:
            counts = collectors.table_row_counts(conn)
            rows_after = sum(counts.values())
            decision_record_rows = conn.execute(
                "SELECT COUNT(*) FROM kernel_records WHERE record_class = 'decision'"
            ).fetchone()[0]
            assessment_rows = conn.execute(
                "SELECT COUNT(*) FROM kernel_records WHERE record_class LIKE 'claim%'"
            ).fetchone()[0]
        finally:
            conn.close()

        return {
            "review_required_runs": review_runs,
            "review_required_fields": len(review_fields),
            "review_field_names": review_fields,
            "review_decisions_applied": decisions_applied,
            "decision_record_rows": int(decision_record_rows),
            "assessment_record_rows": int(assessment_rows),
            "rows_added_by_review_window": int(rows_after - rows_before),
        }
    finally:
        await engine.dispose()


async def _cold_start_sample(tmp: Path, corpus, index: int) -> dict:
    """One fresh-database sample: migrate -> seed -> build -> publish -> first query."""
    root = tmp / f"cold-{index}"
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "kernel.db"
    t_start = time.perf_counter()
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(
        factory, payload_store=LocalPayloadStore(root / "payloads")
    )
    try:
        t_seed0 = time.perf_counter()
        ws = await seed_workspace(
            factory=factory,
            service=service,
            corpus=corpus,
            workspace_id=f"ws-cold-{index}",
            source_root=root / "source-store",
        )
        t_build0 = time.perf_counter()
        seed_s = t_build0 - t_seed0
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
        await engine.dispose()


async def _main_workload(tmp: Path, corpus) -> dict:
    db_path = tmp / "kernel.db"
    payload_root = tmp / "payloads"
    source_root = tmp / "source-store"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payload_store = LocalPayloadStore(payload_root)
    service = KernelCommitService(factory, payload_store=payload_store)
    try:
        baseline = _snapshot(db_path, payload_root, source_root)

        t0 = time.perf_counter()
        ws = await seed_workspace(
            factory=factory,
            service=service,
            corpus=corpus,
            workspace_id=MAIN_WS,
            source_root=source_root,
        )
        ingest_publish_s = time.perf_counter() - t0
        after_ingest = _snapshot(db_path, payload_root, source_root)

        t0 = time.perf_counter()
        await revise_document(ws, "doc-rev-01", "v4")
        revision_s = time.perf_counter() - t0
        after_revision = _snapshot(db_path, payload_root, source_root)
        payload_stats_post_revision = collectors.payload_store_stats(payload_store)

        # retention lifecycle: a pinned old publication must survive GC,
        # releasing the pin must let the superseded generation retire
        pinned_publication = ws.publication_history[0]
        pin = await acquire_publication_pin(
            factory, pinned_publication.publication_set_id, lease_seconds=60.0
        )
        report_pinned = await kernel_gc.collect(factory, payload_store)
        after_gc_pinned = _snapshot(db_path, payload_root, source_root)
        await release_publication_pin(factory, pin.pin_id)
        report_released = await kernel_gc.collect(factory, payload_store)
        after_gc_released = _snapshot(db_path, payload_root, source_root)

        return {
            "baseline": baseline,
            "after_ingest": after_ingest,
            "after_revision": after_revision,
            "after_gc_pinned": after_gc_pinned,
            "after_gc_released": after_gc_released,
            "gc_pinned": report_pinned.summary(),
            "gc_released": report_released.summary(),
            "ingest_publish_s": round(ingest_publish_s, 4),
            "revision_s": round(revision_s, 4),
            "payload_stats_post_revision": payload_stats_post_revision,
            "corpus": corpus,
        }
    finally:
        await engine.dispose()


def _category_delta(before: dict, after: dict) -> dict[str, int]:
    deltas = {}
    for category, count in after["rows_by_category"].items():
        deltas[category] = count - before["rows_by_category"].get(category, 0)
    return {k: v for k, v in sorted(deltas.items()) if v}


def build_envelope(run: dict, review: dict, cold_samples: list[dict],
                   git_sha: str, sample_count: int) -> Envelope:
    corpus = run["corpus"]
    base = run["baseline"]
    ingest = run["after_ingest"]
    revision = run["after_revision"]
    gc_pinned = run["after_gc_pinned"]
    gc_released = run["after_gc_released"]
    storage_final = gc_released["storage"]

    envelope = Envelope(
        profile="local-sqlite-dev",
        dimension_set="invariant_57",
        git_sha=git_sha,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        run_mode="offline",
        model_participation={"mode": "none"},
        workload={
            "identity": (
                "PR81A deterministic corpus (16 PDFs/27 pages) via seed_workspace: "
                "ingest+publish -> doc-rev-01 v3->v4 revision+republish -> "
                "publication-pin GC lifecycle -> PR80A-style review scenario; "
                "cold-start samples on fresh databases"
            ),
            "fingerprint": f"pr81a:{corpus.fingerprint}",
            "documents": len(corpus.docs),
            "queries_available": len(corpus.queries),
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "sqlite": sqlite3.sqlite_version,
            "database": f"sqlite rollback-journal ({storage_final['journal_mode']})",
            "dbstat_available": storage_final["dbstat_available"],
        },
        windows=[
            {"id": "ingest_publish", "label": "seed 16 docs + build generation + publish lexical"},
            {"id": "revision", "label": "revise doc-rev-01 v3->v4 + rebuild + republish"},
            {"id": "gc_lifecycle", "label": "publication-pin GC hold then release"},
            {"id": "review", "label": "conflicting invoice extraction scenario"},
            {"id": "cold_start", "label": "fresh db: migrate->seed->build->publish->first query"},
            {"id": "full", "label": "entire main workload (baseline -> post-GC)"},
        ],
        non_claims=[
            "no human review time is claimed; review burden is item/row counts only",
            "no queue dwell time is claimed; no timed review queue is driven",
            "physical FTS byte attribution is unavailable on SQLite builds without DBSTAT",
            "visual-derived storage is not exercised here; the OFF/ON visual economics "
            "artifact (pr87c-visual-economics.json) measures the same corpus with visual enabled",
        ],
    )

    full_rows_delta = gc_released["rows_total"] - base["rows_total"]
    envelope.set("database_rows", measured(
        full_rows_delta, "count", "full",
        "exact per-table SELECT COUNT(*) via readonly sqlite connection, "
        "summed and categorized by table-name prefix",
        breakdown=_category_delta(base, gc_released),
    ))

    payload_objects = (
        gc_released["payload_store"]["object_count"]
        + gc_released["source_store"]["object_count"]
    )
    envelope.set("payload_objects", measured(
        payload_objects, "count", "full",
        "content-addressed store object walk (objects/ shards)",
        breakdown={
            "payload_objects": gc_released["payload_store"]["object_count"],
            "source_objects": gc_released["source_store"]["object_count"],
        },
    ))
    envelope.counters["payload_object_bytes"] = measured(
        gc_released["payload_store"]["object_bytes"], "bytes", "full",
        "content-addressed payload store walk",
    )
    envelope.counters["source_object_bytes"] = measured(
        gc_released["source_store"]["object_bytes"], "bytes", "full",
        "content-addressed source store walk",
    )
    envelope.counters["logical_source_bytes"] = measured(
        sum(doc.current.byte_length for doc in corpus.docs), "bytes", "full",
        "corpus manifest byte_length of committed PDF artifacts",
    )

    envelope.set("wal_write_amplification", not_applicable(
        "ratio", "full",
        "SQLite runs in rollback-journal mode on this profile (journal_mode="
        f"{storage_final['journal_mode']}); there is no write-ahead log to "
        "amplify. WAL-mode amplification is an industrial-profile dimension "
        "(pr87b-industrial-economics-envelope.json).",
    ))
    envelope.counters["sqlite_wal_bytes"] = measured(
        storage_final["wal_bytes"], "bytes", "full",
        "kernel.db-wal file size (absent under rollback-journal mode)",
    )

    superseded_before_release = (
        revision["generation_states"].get("superseded", 0)
        + revision["generation_states"].get("active", 0)
    )
    final_states = gc_released["generation_states"]
    envelope.set("retained_generations", measured(
        sum(final_states.values()), "count", "gc_lifecycle",
        "kernel_generations grouped by state after pin-held GC and pin-released GC",
        breakdown={
            **{f"state_{k}": v for k, v in final_states.items()},
            "generations_retired_after_pin_release": run["gc_released"]["generations_retired"],
            "generations_retired_while_pinned": run["gc_pinned"]["generations_retired"],
            "generations_before_gc": superseded_before_release,
        },
    ))

    if storage_final["dbstat_available"] and storage_final["dbstat_bytes_by_table"]:
        fts_tables = set()
        for lexical in gc_released["lexical_generations"]:
            fts_tables.add(lexical["fts_table"])
            for suffix in collectors.FTS5_SHADOW_SUFFIXES:
                fts_tables.add(f"{lexical['fts_table']}{suffix}")
        fts_bytes = sum(
            bytes_ for name, bytes_ in storage_final["dbstat_bytes_by_table"].items()
            if name in fts_tables or name.startswith("kernel_lexical")
        )
        envelope.set("fts_storage", measured(
            fts_bytes, "bytes", "full",
            "dbstat per-table page bytes summed over FTS5 shadow tables "
            "and kernel_lexical_* registries",
        ))
    else:
        envelope.set("fts_storage", unavailable(
            "bytes", "full",
            "this SQLite build does not expose the DBSTAT virtual table, so "
            "physical FTS5 shadow-table bytes cannot be attributed; logical "
            "volume is measured in counters (lexical rows + text characters)",
        ))
    envelope.counters["lexical_generations"] = measured(
        len(gc_released["lexical_generations"]), "count", "full",
        "kernel_lexical_generations rows",
    )
    envelope.counters["lexical_rows"] = measured(
        sum(l["row_count"] for l in gc_released["lexical_generations"]),
        "count", "full", "kernel_lexical_generations.row_count summed",
    )
    envelope.counters["lexical_text_chars"] = measured(
        sum(l["text_char_count"] for l in gc_released["lexical_generations"]),
        "count", "full", "kernel_lexical_generations.text_char_count summed",
    )
    envelope.counters["database_bytes"] = measured(
        storage_final["db_bytes"], "bytes", "full",
        "PRAGMA page_count * page_size on the workload database",
    )

    envelope.set("vector_storage", not_applicable(
        "bytes", "full",
        "no vector store implementation exists on any profile; the publication "
        "schema reserves a nullable vector_generation_id slot only",
    ))
    envelope.set("visual_storage", not_applicable(
        "bytes", "full",
        "this workload runs with visual capability absent (no render store, no "
        "visual index); visual storage is measured by the OFF/ON visual "
        "economics artifact on the same corpus",
    ))

    payload_counters = run["payload_stats_post_revision"]
    envelope.set("copy_bytes", measured(
        payload_counters["bytes_written"] + payload_counters["bytes_read_back"]
        + gc_released["source_store"]["object_bytes"],
        "bytes", "full",
        "LocalPayloadStore staging writes + verification read-backs + source "
        "store staged artifact bytes",
        breakdown={
            "payload_staging_writes": payload_counters["bytes_written"],
            "payload_verification_read_backs": payload_counters["bytes_read_back"],
            "source_staging_bytes": gc_released["source_store"]["object_bytes"],
            "payload_dedup_hits": payload_counters["dedup_hits"],
        },
    ))

    cold_values = [sample["total_s"] * 1000.0 for sample in cold_samples]
    build_values = [sample["build_publish_s"] * 1000.0 for sample in cold_samples]
    envelope.set("cold_start", measured(
        round(statistics.median(cold_values), 2), "milliseconds", "cold_start",
        "fresh SQLite database per sample: migrate -> seed 16 docs -> build "
        "generation -> publish lexical -> first lexical_search query answered",
        samples={
            "n": sample_count,
            "min": round(min(cold_values), 2),
            "p50": round(statistics.median(cold_values), 2),
            "max": round(max(cold_values), 2),
            "per_sample_total_ms": [round(v, 2) for v in cold_values],
            "per_sample_build_publish_ms": [round(v, 2) for v in build_values],
        },
    ))

    revision_rows = revision["rows_total"] - ingest["rows_total"]
    envelope.set("reprocessing", measured(
        1, "count", "revision",
        "doc-rev-01 v3->v4 content revision through the real commit/generation/"
        "publication path",
        breakdown={
            "revision_events": 1,
            "documents_changed": 1,
            "documents_unchanged_reused": len(corpus.docs) - 1,
            "rows_added_by_revision": revision_rows,
            **{
                f"revision_rows_{category}": delta
                for category, delta in _category_delta(ingest, revision).items()
            },
            "payload_dedup_hits_during_revision": (
                payload_counters["dedup_hits"]
            ),
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
        "perf_counter around seed_workspace (16 docs + generation + publication)",
        samples={"n": 1},
    )

    envelope.set("review_burden", measured(
        review["review_required_runs"] + review["review_required_fields"],
        "count", "review",
        "deterministic conflicting-candidate invoice extraction through the "
        "real extraction service; review-required runs + fields counted, one "
        "review decision applied and its kernel rows counted",
        breakdown={
            "review_required_runs": review["review_required_runs"],
            "review_required_fields": review["review_required_fields"],
            "review_decisions_applied": review["review_decisions_applied"],
            "decision_record_rows": review["decision_record_rows"],
            "assessment_record_rows": review["assessment_record_rows"],
            "rows_added_by_review_window": review["rows_added_by_review_window"],
        },
    ))
    return envelope


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

    async def run_all() -> tuple[dict, dict, list[dict]]:
        with tempfile.TemporaryDirectory(prefix="econ-local-") as tmp:
            tmp_path = Path(tmp)
            run = await _main_workload(tmp_path, corpus)
            review = await _review_scenario(tmp_path)
            cold = [
                await _cold_start_sample(tmp_path, corpus, i)
                for i in range(args.samples)
            ]
            return run, review, cold

    run, review, cold = asyncio.run(run_all())
    envelope = build_envelope(run, review, cold, git_sha, args.samples)
    artifact = envelope.to_dict()
    artifact["wall_time_s"] = round(time.perf_counter() - started, 3)
    errors = validate_envelope(artifact)

    summary = {
        "profile": artifact["profile"],
        "database_rows_delta": artifact["dimensions"]["database_rows"]["value"],
        "rows_by_category": artifact["dimensions"]["database_rows"]["breakdown"],
        "payload_objects": artifact["dimensions"]["payload_objects"],
        "retained_generations": artifact["dimensions"]["retained_generations"]["breakdown"],
        "cold_start_ms_p50": artifact["dimensions"]["cold_start"]["samples"]["p50"],
        "review": artifact["dimensions"]["review_burden"]["breakdown"],
        "fts_storage_status": artifact["dimensions"]["fts_storage"]["status"],
        "validation_errors": errors,
    }
    print(json.dumps(summary, indent=2))

    if errors:
        print(f"envelope failed validation: {len(errors)} error(s)", file=sys.stderr)
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
