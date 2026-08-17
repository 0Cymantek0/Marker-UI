"""Benchmark PR76 publication sets and lexical FTS generations.

Run from repository root::

    python backend/scripts/bench_pr76_publications.py --write

Characterizes the operational tax of the PR76 serving layer on the
local SQLite topology: lexical build throughput, index size relative to
indexed text, steady-state query latency distribution, activation
transaction latency (excluding build time), and reader behavior across
a reindex.  Semantic identities stay separate from wall-clock
measurements: a runtime change must never change any
``semantic_identity`` value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.db_migration import upgrade_database  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord  # noqa: E402
from app.kernel.publications import (  # noqa: E402
    PublicationService,
    open_pinned_publication,
    resolve_published_set,
)
from app.kernel.reading_order import OrderNode, ReadingOrderGraph  # noqa: E402
from app.kernel.snapshots import resolve_snapshot  # noqa: E402

MEASUREMENTS_PATH = (
    BACKEND.parent / "docs" / "reference" / "measurements" / "pr76-publication-sets.json"
)

SMALL = ("small", 20, 10)   # docs, nodes per doc
LARGE = ("large", 100, 20)
NODE_TEXT_CHARS = 400
QUERY_SAMPLES = 200

_WORDS = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
    "nu xi omicron pi rho sigma tau upsilon phi chi psi omega"
).split()


def _node_text(doc: int, node: int) -> str:
    words = [
        _WORDS[(doc * 7 + node * 3 + offset) % len(_WORDS)]
        for offset in range(NODE_TEXT_CHARS // 6)
    ]
    return " ".join(words) + f" doc{doc}node{node}"


def _corpus_view(docs: int, nodes: int) -> ViewDocumentRecord:
    """One view document whose content nodes are the whole corpus.

    v1 manages a single view per workspace, so a multi-document corpus
    is one view_document with many nodes (doc-major, node-minor ids).
    """
    graph = ReadingOrderGraph.build(
        tuple(
            OrderNode(node_id=f"d{doc}n{node}")
            for doc in range(docs)
            for node in range(nodes)
        ),
        (),
    )
    return ViewDocumentRecord(
        record_id="view-corpus",
        content_revision_ref="rev-corpus",
        graph=graph,
        texts={
            f"d{doc}n{node}": _node_text(doc, node)
            for doc in range(docs)
            for node in range(nodes)
        },
    )


def _db_size(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(page_count) * int(page_size)


async def _build_corpus(
    tmp: Path, slice_id: str, docs: int, nodes: int
) -> tuple[async_sessionmaker, Any, Path]:
    db_path = tmp / f"{slice_id}.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    view = _corpus_view(docs, nodes)
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-bench",
            records=(view,),
            view_advancement=ViewAdvancement(
                new_revision_id=view.view_revision_id()
            ),
        )
    )
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-bench")
    )
    return factory, gen, db_path


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)

    def pct(fraction: float) -> float:
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 3)

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


async def _measure_lexical_build(
    tmp: Path, slice_id: str, docs: int, nodes: int, repeat: int
) -> dict[str, Any]:
    best_ms = float("inf")
    ref = None
    text_chars = docs * nodes * NODE_TEXT_CHARS
    index_bytes = 0
    for attempt in range(repeat):
        factory, gen, db_path = await _build_corpus(
            tmp, f"{slice_id}-build-{attempt}", docs, nodes
        )
        try:
            before = _db_size(db_path)
            pubs = PublicationService(factory)
            started = time.perf_counter()
            ref = await pubs.build_lexical(gen.generation_id)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            best_ms = min(best_ms, elapsed_ms)
            index_bytes = _db_size(db_path) - before
        finally:
            await factory.kw["bind"].dispose()
    assert ref is not None
    return {
        "slice": slice_id,
        "docs": docs,
        "nodes_per_doc": nodes,
        "rows": ref.row_count,
        "text_chars": text_chars,
        "semantic_identity": {
            "source_generation_id": ref.source_generation_id,
            "lexical_generation_id": ref.lexical_generation_id,
            "content_digest": ref.content_digest,
            "tokenizer": ref.tokenizer,
        },
        "runtime_ms": round(best_ms, 3),
        "rows_per_second": round(ref.row_count / (best_ms / 1000.0), 1),
        "index_bytes": index_bytes,
        "index_bytes_per_text_char": round(index_bytes / max(1, text_chars), 3),
    }


async def _measure_query_and_activation(
    tmp: Path, repeat: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    factory, gen, db_path = await _build_corpus(tmp, "serve", *LARGE[1:])
    pubs = PublicationService(factory)
    lexical = await pubs.build_lexical(gen.generation_id)
    p1 = await pubs.publish(materialized_generation_id=gen.generation_id)
    resolved = await resolve_published_set(factory, "ws-bench")
    assert resolved is not None and resolved.publication_set_id == p1.publication_set_id

    reader = await open_pinned_publication(factory, p1.publication_set_id)
    samples_ms: list[float] = []
    hits_total = 0
    for sample in range(QUERY_SAMPLES):
        term = _WORDS[sample % len(_WORDS)]
        started = time.perf_counter()
        hits = await reader.search(term)
        samples_ms.append((time.perf_counter() - started) * 1000.0)
        hits_total += len(hits)

    query_slice = {
        "slice": "query-steady-state",
        "query_samples": QUERY_SAMPLES,
        "hits_total": hits_total,
        "semantic_identity": {
            "publication_set_id": p1.publication_set_id,
            "lexical_generation_id": lexical.lexical_generation_id,
        },
        "latency_ms": _percentiles(samples_ms),
        "mean_ms": round(statistics.fmean(samples_ms), 3),
    }

    # activation latency: advance the kernel cut repeatedly and publish;
    # each activation transaction is timed excluding build/stage/validate
    service = KernelCommitService(factory)
    gen_service = GenerationService(factory)
    activation_ms: list[float] = []
    set_ids: list[str] = []
    for step in range(5):
        view = ViewDocumentRecord(
            record_id=f"view-act-{step}",
            content_revision_ref=f"rev-act-{step}",
            graph=ReadingOrderGraph.build(
                (OrderNode(node_id="act"),), ()
            ),
            texts={"act": f"activation step {step} alpha"},
        )
        await service.commit(
            KernelCommitBatch(
                workspace_id="ws-bench",
                records=(view,),
                view_advancement=None,
            )
        )
        gen_next = await gen_service.build_and_activate(
            await resolve_snapshot(factory, "ws-bench")
        )
        staged = await pubs.stage_publication_set(
            materialized_generation_id=gen_next.generation_id
        )
        validated = await pubs.validate_publication_set(staged.publication_set_id)
        started = time.perf_counter()
        activated = await pubs.activate_publication_set(validated.publication_set_id)
        activation_ms.append((time.perf_counter() - started) * 1000.0)
        set_ids.append(activated.publication_set_id)

    activation_slice = {
        "slice": "activation-transaction",
        "activations": len(activation_ms),
        "semantic_identity": {"publication_set_ids": set_ids},
        "runtime_ms": {
            "best": round(min(activation_ms), 3),
            "median": round(statistics.median(activation_ms), 3),
            "max": round(max(activation_ms), 3),
        },
    }

    # reader across reindex: pinned to P1 while later sets publish
    before_hits = await reader.search("alpha")
    await reader.renew(lease_seconds=300)
    reindex_started = time.perf_counter()
    view = ViewDocumentRecord(
        record_id="view-reindex",
        content_revision_ref="rev-reindex",
        graph=ReadingOrderGraph.build((OrderNode(node_id="ri"),), ()),
        texts={"ri": "alpha reindexed content"},
    )
    await service.commit(
        KernelCommitBatch(workspace_id="ws-bench", records=(view,))
    )
    gen_final = await gen_service.build_and_activate(
        await resolve_snapshot(factory, "ws-bench")
    )
    await pubs.publish(materialized_generation_id=gen_final.generation_id)
    reindex_ms = (time.perf_counter() - reindex_started) * 1000.0
    during_hits = await reader.search("alpha")
    final = await resolve_published_set(factory, "ws-bench")
    await reader.close()

    reindex_slice = {
        "slice": "reader-across-reindex",
        "semantic_identity": {
            "pinned_publication_set_id": p1.publication_set_id,
            "pinned_lexical_generation_id": lexical.lexical_generation_id,
            "published_after_reindex": final.publication_set_id
        if final is not None
        else None,
            "pinned_reader_stayed_on_pinned_generation": all(
                hit.lexical_generation_id == lexical.lexical_generation_id
                for hit in before_hits + during_hits
            ),
        },
        "hits_before": len(before_hits),
        "hits_after": len(during_hits),
        "reindex_plus_publication_ms": round(reindex_ms, 3),
    }
    return query_slice, activation_slice, reindex_slice


async def run(repeat: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="marker-pr76-bench-") as tmp_dir:
        tmp = Path(tmp_dir)
        migration = await upgrade_database(
            url=f"sqlite+aiosqlite:///{(tmp / 'migration.db').as_posix()}"
        )
        small = await _measure_lexical_build(tmp, *SMALL, repeat=repeat)
        large = await _measure_lexical_build(tmp, *LARGE, repeat=repeat)
        query_slice, activation_slice, reindex_slice = await _measure_query_and_activation(
            tmp, repeat
        )
        return {
            "benchmark": "pr76-publication-sets",
            "schema_version": "marker.publication_sets_measurements.v1",
            "corpus": {
                "kind": "synthetic view documents (unicode61 tokenizer)",
                "node_text_chars": NODE_TEXT_CHARS,
                "slices": {"small": SMALL[1:], "large": LARGE[1:]},
            },
            "method": (
                f"best-of-{repeat} wall milliseconds via time.perf_counter "
                "for build/activation slices; full distribution for query "
                "latency; index size from PRAGMA page_count before/after "
                "the lexical build; runtime metadata is non-semantic"
            ),
            "migration": {
                "action": migration.action,
                "to_revision": migration.to_revision,
            },
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "slices": [small, large, query_slice, activation_slice, reindex_slice],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the JSON artifact")
    parser.add_argument("--repeat", type=int, default=3, help="best-of repeats")
    parser.add_argument("--output", type=Path, default=MEASUREMENTS_PATH)
    args = parser.parse_args(argv)

    report = asyncio.run(run(args.repeat))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
