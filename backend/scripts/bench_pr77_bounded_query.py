"""Benchmark PR77 bounded typed queries and EvidencePackets.

Run from repository root::

    python backend/scripts/bench_pr77_bounded_query.py --write

Proves the architectural point on a synthetic corpus large enough that
unbounded/manual traversal would be obvious (2,000 content nodes):
server-side execution performs a bounded number of operations against
one pinned PublicationSet regardless of corpus size, evidence units and
output stay within declared budgets, packets keep single-set
attribution across a mid-query publication switch, and identical runs
over unchanged state reproduce the same packet identity.  Wall-clock
numbers are recorded as evidence only; acceptance is structural and
count-based.  Semantic identities stay separate from runtime numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.context_runtime import (  # noqa: E402
    QUERY_SCHEMA_VERSION,
    execute_query,
    parse_query_request,
    to_json,
)
from app.db_migration import upgrade_database  # noqa: E402
from app.kernel.commit import KernelCommitBatch, KernelCommitService  # noqa: E402
from app.kernel.generations import GenerationService  # noqa: E402
from app.kernel.patches import ViewAdvancement, ViewDocumentRecord  # noqa: E402
from app.kernel.publications import PublicationService  # noqa: E402
from app.kernel.reading_order import OrderNode, ReadingOrderGraph  # noqa: E402
from app.kernel.snapshots import resolve_snapshot  # noqa: E402

MEASUREMENTS_PATH = (
    BACKEND.parent / "docs" / "reference" / "measurements" / "pr77-bounded-query.json"
)

DOCS = 100
NODES_PER_DOC = 20
NODE_TEXT_CHARS = 400
REPEATS = 30

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


def _request(operations: list[dict], **overrides) -> dict:
    base = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "workspace_id": "ws-bench",
        "operations": operations,
    }
    base.update(overrides)
    return base


async def _build_published_corpus(tmp: Path):
    db_path = tmp / "pr77.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    await upgrade_database(url=url)
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = KernelCommitService(factory)
    view = _corpus_view(DOCS, NODES_PER_DOC)
    await service.commit(
        KernelCommitBatch(
            workspace_id="ws-bench",
            records=(view,),
            view_advancement=ViewAdvancement(new_revision_id=view.view_revision_id()),
        )
    )
    gen = await GenerationService(factory).build_and_activate(
        await resolve_snapshot(factory, "ws-bench")
    )
    pubs = PublicationService(factory)
    ref = await pubs.publish(materialized_generation_id=gen.generation_id)
    return factory, service, pubs, gen, ref


def _packet_summary(packet) -> dict[str, Any]:
    return {
        "status": packet.status,
        "publication_set_id": packet.publication["publication_set_id"]
        if packet.publication
        else None,
        "materialized_generation_id": packet.publication[
            "materialized_generation_id"
        ]
        if packet.publication
        else None,
        "lexical_generation_id": packet.publication["lexical_generation_id"]
        if packet.publication
        else None,
        "operations_executed": packet.budget.operations_executed,
        "candidates_considered": packet.budget.candidates_considered,
        "units_included": packet.budget.units_included,
        "units_omitted": packet.budget.units_omitted,
        "output_chars": packet.budget.output_chars,
        "omission_reasons": sorted({o.reason for o in packet.omitted}),
        "identity_id": packet.identity_id,
        "single_set_attribution": (
            len(
                {
                    u.locator.publication_set_id for u in packet.evidence
                }
            )
            <= 1
        ),
        "within_budget": (
            packet.budget.units_included <= packet.budget.max_evidence_units
            and packet.budget.output_chars <= packet.budget.max_output_chars
        ),
    }


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)

    def pct(fraction: float) -> float:
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 3)

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


async def _measure(factory, request_data: dict, repeats: int) -> dict[str, Any]:
    request = parse_query_request(request_data)
    samples_ms: list[float] = []
    packet = None
    for _ in range(repeats):
        started = time.perf_counter()
        packet = await execute_query(factory, request)
        samples_ms.append((time.perf_counter() - started) * 1000.0)
    assert packet is not None
    summary = _packet_summary(packet)
    summary["query_latency_ms"] = _percentiles(samples_ms)
    summary["mean_ms"] = round(statistics.fmean(samples_ms), 3)
    summary["budget"] = to_json(packet)["budget"]
    return summary


async def run(repeats: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="marker-pr77-bench-") as tmp_dir:
        tmp = Path(tmp_dir)
        migration = await upgrade_database(
            url=f"sqlite+aiosqlite:///{(tmp / 'migration.db').as_posix()}"
        )
        factory, service, pubs, gen, ref = await _build_published_corpus(tmp)
        corpus_nodes = DOCS * NODES_PER_DOC

        # 1. high-hit lexical term: a large fraction of the corpus
        high_hit = await _measure(
            factory,
            _request(
                [{"op": "lexical_search", "text": "alpha", "limit": 200}],
            ),
            repeats,
        )

        # 2. selective lexical term: exactly one node's marker token
        selective = await _measure(
            factory,
            _request([{"op": "lexical_search", "text": "doc7node3"}]),
            repeats,
        )

        # 3. exact selector
        exact = await _measure(
            factory,
            _request(
                [{"op": "record_get", "record_id": "view-corpus", "node_id": "d7n3"}]
            ),
            repeats,
        )

        # 4. deliberately over-budget evidence request
        squeezed = await _measure(
            factory,
            _request(
                [{"op": "lexical_search", "text": "alpha", "limit": 200}],
                budget={"max_evidence_units": 5, "max_output_chars": 20_000},
            ),
            repeats,
        )

        # 5. query across a publication switch: attribution must stay
        # on the pinned set for the in-flight packet.
        switch_view = ViewDocumentRecord(
            record_id="view-switch",
            content_revision_ref="rev-switch",
            graph=ReadingOrderGraph.build((OrderNode(node_id="sw"),), ()),
            texts={"sw": "alpha switched publication"},
        )

        async def switch_head_after_first_operation(index: int) -> None:
            if index == 0:
                await service.commit(
                    KernelCommitBatch(
                        workspace_id="ws-bench",
                        records=(switch_view,),
                        view_advancement=None,
                    )
                )
                gen2 = await GenerationService(factory).build_and_activate(
                    await resolve_snapshot(factory, "ws-bench")
                )
                await pubs.publish(materialized_generation_id=gen2.generation_id)

        switch_request = parse_query_request(
            _request(
                [
                    {"op": "lexical_search", "text": "alpha"},
                    {"op": "lexical_search", "text": "alpha"},
                ]
            )
        )
        switch_started = time.perf_counter()
        switch_packet = await execute_query(
            factory, switch_request, _after_operation=switch_head_after_first_operation
        )
        switch_ms = (time.perf_counter() - switch_started) * 1000.0
        across_switch = _packet_summary(switch_packet)
        across_switch["query_runtime_ms"] = round(switch_ms, 3)
        across_switch["stayed_on_pinned_set"] = (
            switch_packet.publication["publication_set_id"]
            == ref.publication_set_id
        )
        follow_up = await execute_query(
            factory,
            parse_query_request(
                _request([{"op": "lexical_search", "text": "switched"}])
            ),
        )
        across_switch["follow_up_saw_new_set"] = (
            follow_up.publication["publication_set_id"] != ref.publication_set_id
        )

        # Identity stability: identical request, unchanged state.
        identity_a = await execute_query(
            factory, parse_query_request(_request([{"op": "lexical_search", "text": "alpha"}]))
        )
        identity_b = await execute_query(
            factory, parse_query_request(_request([{"op": "lexical_search", "text": "alpha"}]))
        )

        acceptance = {
            "server_operations_bounded_by_request": all(
                s["operations_executed"] <= 2 for s in (high_hit, selective, exact)
            ),
            "evidence_units_within_caps": all(
                s["within_budget"]
                for s in (high_hit, selective, exact, squeezed, across_switch)
            ),
            "single_set_attribution": all(
                s["single_set_attribution"]
                for s in (high_hit, selective, exact, squeezed, across_switch)
            ),
            "over_budget_partial_explicit": (
                squeezed["status"] == "partial"
                and "unit_budget" in squeezed["omission_reasons"]
            ),
            "pinned_across_publication_switch": (
                across_switch["stayed_on_pinned_set"]
                and across_switch["follow_up_saw_new_set"]
            ),
            "packet_identity_stable": identity_a.identity_id == identity_b.identity_id,
            "corpus_nodes": corpus_nodes,
            "wedge_note": (
                "one bounded server-side call answers over the whole "
                f"{corpus_nodes}-node corpus; no page-by-page client traversal"
            ),
        }

        await factory.kw["bind"].dispose()

        return {
            "benchmark": "pr77-bounded-query",
            "schema_version": "marker.bounded_query_measurements.v1",
            "corpus": {
                "kind": "synthetic view document (unicode61 tokenizer)",
                "docs": DOCS,
                "nodes_per_doc": NODES_PER_DOC,
                "nodes": corpus_nodes,
                "node_text_chars": NODE_TEXT_CHARS,
                "publication_set_id": ref.publication_set_id,
                "materialized_generation_id": gen.generation_id,
                "lexical_generation_id": ref.lexical_generation_id,
            },
            "method": (
                f"each typed query repeated {repeats}x via time.perf_counter; "
                "acceptance is structural/count-based, wall-clock is evidence "
                "only; runtime metadata is non-semantic"
            ),
            "migration": {
                "action": migration.action,
                "to_revision": migration.to_revision,
            },
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "slices": {
                "high_hit_lexical": high_hit,
                "selective_lexical": selective,
                "exact_selector": exact,
                "over_budget_request": squeezed,
                "across_publication_switch": across_switch,
            },
            "acceptance": acceptance,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the JSON artifact")
    parser.add_argument("--repeat", type=int, default=REPEATS, help="repeats per query")
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
    ok = all(
        value is True
        for key, value in report["acceptance"].items()
        if isinstance(value, bool)
    )
    if not ok:
        print("ACCEPTANCE FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
