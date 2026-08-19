"""PR80A lane: run the evidence-backed extraction route over one corpus doc.

Each document gets a throwaway migrated SQLite kernel and its own
workspace: publication, generation, query, and extraction all run
through the REAL production authorities. Nothing is mocked, and no
state survives the lane outside the benchmark work directory, so
benchmark runs cannot mutate production truth.
"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.eval.pr80b.scoring import (
    ABSENT,
    EMITTED,
    FLAGGED_CONFLICT,
    EmittedField,
    EmittedRow,
    SystemDocOutput,
)
from app.extraction.results import USABLE_FIELD_OUTCOMES

SYSTEM_ID = "marker-pr80a"

_USABLE = set(USABLE_FIELD_OUTCOMES)


def _part_record(doc_id: str, part_index: int, part_text: str):
    """One view document: one node per non-empty line, in reading order."""
    from app.kernel.patches import ViewDocumentRecord
    from app.kernel.reading_order import OrderNode, ReadingOrderGraph

    lines = [line.strip() for line in part_text.splitlines() if line.strip()]
    texts = {f"n{index}": line for index, line in enumerate(lines, start=1)}
    graph = ReadingOrderGraph.build(
        tuple(OrderNode(node_id=node_id) for node_id in texts), ()
    )
    return ViewDocumentRecord(
        record_id=f"{doc_id}-p{part_index}",
        content_revision_ref="rev-1",
        graph=graph,
        texts=texts,
        view_id=f"view-{doc_id}-p{part_index}",
    )


def _field_outcome_to_emitted(outcome) -> EmittedField:
    if outcome.status in _USABLE and outcome.value is not None:
        has_evidence = bool(outcome.candidates) and bool(outcome.candidates[0].evidence)
        return EmittedField(
            status=EMITTED,
            value=str(outcome.value),
            has_evidence=has_evidence,
        )
    # Non-usable outcomes (missing/invalid/unresolved/review_required/
    # rejected) never deliver a production value: the route reports them
    # for review instead of inventing one.
    return EmittedField(status=ABSENT, self_flagged=True)


def _result_to_output(doc_id: str, result, timings: dict) -> SystemDocOutput:
    fields = {
        name: _field_outcome_to_emitted(outcome)
        for name, outcome in result.fields.items()
    }
    rows = []
    for item_name, outcomes in result.line_items.items():
        for row in outcomes:
            row_fields = {
                name: _field_outcome_to_emitted(outcome)
                for name, outcome in row.fields.items()
            }
            sku = row.identity.get("sku")
            rows.append(
                EmittedRow(
                    sku=str(sku) if sku is not None else None,
                    fields=row_fields,
                    status=FLAGGED_CONFLICT if row.status not in _USABLE else EMITTED,
                    self_flagged=row.status not in _USABLE,
                )
            )
    invariant_findings = {
        finding.target: finding.finding for finding in result.invariants
    }
    record_ids: set[str] = set()
    for outcome in result.fields.values():
        for candidate in outcome.candidates:
            for citation in candidate.evidence:
                record_ids.add(citation.record_id)
    for item_outcomes in result.line_items.values():
        for row in item_outcomes:
            for outcome in row.fields.values():
                for candidate in outcome.candidates:
                    for citation in candidate.evidence:
                        record_ids.add(citation.record_id)
    return SystemDocOutput(
        system_id=SYSTEM_ID,
        doc_id=doc_id,
        fields=fields,
        rows=tuple(rows),
        run_status=result.run_status,
        invariant_findings=invariant_findings,
        raw={
            "timings_ms": timings,
            "result_identity": result.identity,
            "publication_set_id": result.context.publication_set_id,
            "policy": f"{result.context.policy_id}/{result.context.policy_version}",
            "cited_record_ids": sorted(record_ids),
        },
    )


async def run_pr80a_lane(doc, workdir: Path) -> SystemDocOutput:
    """Publish and extract one corpus document through real authorities."""
    from app.db_migration import upgrade_database
    from app.extraction.contract import INVOICE_SCHEMA, ExtractionRequest
    from app.extraction.service import ExtractionService
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.generations import GenerationService
    from app.kernel.payloads import LocalPayloadStore
    from app.kernel.publications import PublicationService
    from app.kernel.snapshots import resolve_snapshot

    workspace_id = f"ws-pr80b-{doc.doc_id}"
    doc_dir = Path(workdir) / doc.doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{(doc_dir / 'kernel.db').as_posix()}"
    timings: dict[str, float] = {}
    started = time.perf_counter()
    try:
        await upgrade_database(url=url)
        engine = create_async_engine(url, connect_args={"check_same_thread": False})
        try:
            factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            store = LocalPayloadStore(doc_dir / "payloads")
            commit_service = KernelCommitService(factory, payload_store=store)

            t0 = time.perf_counter()
            await commit_service.commit(
                KernelCommitBatch(
                    workspace_id=workspace_id,
                    records=tuple(
                        _part_record(doc.doc_id, index, text)
                        for index, text in enumerate(doc.part_texts, start=1)
                    ),
                )
            )
            timings["commit_ms"] = round((time.perf_counter() - t0) * 1000, 2)

            t0 = time.perf_counter()
            generation = await GenerationService(factory).build_and_activate(
                await resolve_snapshot(factory, workspace_id)
            )
            publication = await PublicationService(factory).publish(
                materialized_generation_id=generation.generation_id
            )
            timings["publish_ms"] = round((time.perf_counter() - t0) * 1000, 2)

            service = ExtractionService(
                factory, commit_service, workspace_id=workspace_id
            )
            request = ExtractionRequest(
                schema_id=INVOICE_SCHEMA.schema_id,
                schema_version=INVOICE_SCHEMA.version,
                workspace_id=workspace_id,
                expected_publication_set_id=publication.publication_set_id,
            )
            t0 = time.perf_counter()
            result = await service.run(request)
            timings["extract_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        finally:
            await engine.dispose()
    except Exception as exc:  # honest lane failure capture
        return SystemDocOutput(
            system_id=SYSTEM_ID,
            doc_id=doc.doc_id,
            fields={},
            rows=(),
            error=f"{type(exc).__name__}: {exc}",
            raw={"timings_ms": timings},
        )
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return _result_to_output(doc.doc_id, result, timings)
