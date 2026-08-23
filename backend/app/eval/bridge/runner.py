"""Hybrid bridge lane runner and authority metrics (bridge workstream §14).

Mirrors :mod:`app.eval.pr80b.pr80a_lane`: every corpus document gets a
throwaway migrated SQLite kernel and its own workspace, so the hybrid
benchmark runs the REAL production authorities — publication, query,
specialist lane, reconciliation, claim/proof commit — with the only
scripted piece being the recorded provider response, replayed through
the production ReplayProvider. Metrics are read from the production
result tree itself, not from an adapter's opinion.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.eval.pr80b.pr80a_lane import _part_record, _result_to_output
from app.eval.pr80b.scoring import SystemDocOutput
from app.extraction.hybrid import (
    RULE_HYBRID_CORROBORATED,
    RULE_HYBRID_PROPOSAL_ROW,
)
from app.extraction.provider import ReplayProvider
from app.extraction.results import FIELD_OUTCOME_ACCEPTED, ExtractionResult
from app.extraction.service import ExtractionService

SYSTEM_ID = "marker-hybrid-bridge"


def hybrid_system_id(model: str) -> str:
    return f"marker-hybrid-bridge:{model}"


def result_authority_metrics(result: ExtractionResult) -> dict[str, Any]:
    """Bridge-specific authority metrics straight from the result tree."""
    accepted_fields: list[str] = []
    corroborated_fields: list[str] = []
    proposal_review_fields: list[str] = []
    proposal_only_fields: list[str] = []
    proposal_fields: list[str] = []
    false_authority: list[str] = []
    accepted_without_lineage: list[str] = []
    conflicts_preserved: list[str] = []
    review_required: list[str] = []

    def _field(name: str, outcome) -> None:
        if outcome.proposals:
            proposal_fields.append(name)
        if outcome.status == FIELD_OUTCOME_ACCEPTED:
            accepted_fields.append(name)
            if outcome.rule == RULE_HYBRID_CORROBORATED:
                corroborated_fields.append(name)
            if not [c for c in outcome.candidates if c.evidence]:
                false_authority.append(name)
                accepted_without_lineage.append(name)
        elif outcome.status == "review_required":
            review_required.append(name)
            if outcome.proposals:
                proposal_review_fields.append(name)
                if not outcome.candidates:
                    proposal_only_fields.append(name)
        elif outcome.status == "unresolved":
            conflicts_preserved.append(name)

    for name, outcome in result.fields.items():
        _field(name, outcome)
    rows_total = 0
    rows_accepted = 0
    rows_proposal_only = 0
    for item_name, rows in result.line_items.items():
        for row in rows:
            rows_total += 1
            if row.status == FIELD_OUTCOME_ACCEPTED:
                rows_accepted += 1
            if any(
                out.rule == RULE_HYBRID_PROPOSAL_ROW for out in row.fields.values()
            ):
                rows_proposal_only += 1
            for field_name, outcome in row.fields.items():
                _field(f"{item_name}[{row.identity.get('sku')}].{field_name}", outcome)

    lane = result.specialist
    return {
        "run_status": result.run_status,
        "result_identity": result.identity,
        "accepted_fields": accepted_fields,
        "corroborated_fields": corroborated_fields,
        "proposal_review_fields": proposal_review_fields,
        "proposal_only_fields": proposal_only_fields,
        "proposal_fields": proposal_fields,
        "false_authority_events": false_authority,
        "accepted_without_lineage": accepted_without_lineage,
        "conflicts_preserved": conflicts_preserved,
        "review_required_fields": review_required,
        "rows": {
            "total": rows_total,
            "accepted": rows_accepted,
            "proposal_only": rows_proposal_only,
        },
        "lane_status": lane.status if lane is not None else "disabled",
        "lane_proposals": lane.proposal_count if lane is not None else 0,
        "lane_runtime": lane.runtime.to_dict() if lane is not None and lane.runtime else None,
    }


async def run_bridge_lane(
    doc: Any,
    workdir: Path,
    lookup: Callable[[str, str], str | None],
    *,
    model: str,
) -> tuple[SystemDocOutput, dict[str, Any]]:
    """Publish and extract one corpus document through the hybrid lane."""
    from app.db_migration import upgrade_database
    from app.extraction.contract import ExtractionRequest
    from app.kernel.commit import KernelCommitBatch, KernelCommitService
    from app.kernel.generations import GenerationService
    from app.kernel.payloads import LocalPayloadStore
    from app.kernel.publications import PublicationService
    from app.kernel.snapshots import resolve_snapshot
    from app.extraction.specialist import SpecialistLane

    workspace_id = f"ws-bridge-{doc.doc_id}"
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

            provider = ReplayProvider(lookup, model=model)
            service = ExtractionService(
                factory,
                commit_service,
                workspace_id=workspace_id,
                specialist=SpecialistLane(provider),
            )
            request = ExtractionRequest(
                schema_id="demo.invoice",
                schema_version="1.0.0",
                workspace_id=workspace_id,
                expected_publication_set_id=publication.publication_set_id,
            )
            t0 = time.perf_counter()
            result = await service.run(request)
            timings["extract_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        finally:
            await engine.dispose()
    except Exception as exc:  # honest lane failure capture
        output = SystemDocOutput(
            system_id=hybrid_system_id(model),
            doc_id=doc.doc_id,
            fields={},
            rows=(),
            error=f"{type(exc).__name__}: {exc}",
            raw={"timings_ms": timings},
        )
        return output, {"lane_status": "execution_failure", "error": str(exc)}
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    output = _result_to_output(doc.doc_id, result, timings)
    output.raw["specialist"] = (
        result.specialist.to_dict() if result.specialist is not None else None
    )
    metrics = result_authority_metrics(result)
    metrics["timings_ms"] = timings
    return output, metrics
