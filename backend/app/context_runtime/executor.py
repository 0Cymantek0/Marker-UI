"""Publication-pinned bounded query execution (PR77).

One ``execute_query`` call resolves and pins exactly one PublicationSet
and produces one :class:`EvidencePacket`. Every retrieval — lexical or
exact — is served through that pinned reader, so a concurrent
publication-head switch can never mix generations inside one packet.
Kernel integrity errors (tampered index/locator, malformed query
state) propagate unchanged: this layer never falls back or retries
against different state.

The publication pin is released in ``finally`` on success, budget
termination, validation failure, and cancellation.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.contract import (
    QueryRequest,
    compile_lexical_match,
    normalized_query,
)
from app.context_runtime.errors import QueryBudgetError
from app.context_runtime.packets import (
    CandidateUnit,
    EvidenceLocator,
    EvidencePacket,
    OmittedEvidence,
    assemble_packet,
)
from app.kernel.publications import PublicationReader, open_published_reader
from app.kernel.retention import DEFAULT_PIN_LEASE_SECONDS
from app.utils.canonical import payload_byte_hash

__all__ = ["execute_query"]


async def execute_query(
    session_factory: async_sessionmaker,
    request: QueryRequest,
    *,
    _after_operation: Callable[[int], Any] | None = None,
) -> EvidencePacket:
    """Execute one validated typed request against one pinned
    PublicationSet and return a bounded :class:`EvidencePacket`.

    ``_after_operation`` is a deterministic test hook (mirroring the
    PR76 fault-phase convention): called with the operation index after
    each operation completes, it lets tests switch the publication head
    mid-execution and prove the in-flight packet stays attributable to
    the original pinned set.
    """
    if len(request.operations) > request.budget.max_operations:
        raise QueryBudgetError(
            f"request has {len(request.operations)} operations; budget "
            f"allows {request.budget.max_operations}"
        )

    reader = await open_published_reader(
        session_factory,
        request.workspace_id,
        profile=request.profile,
        pin_lease_seconds=DEFAULT_PIN_LEASE_SECONDS,
    )
    if reader is None:
        return await _unpublished_packet(request)

    try:
        return await _execute_pinned(reader, request, _after_operation)
    finally:
        # V19: pin released on success, error, cancellation, and budget
        # termination alike — never leaked past the call.
        await reader.close()


async def _unpublished_packet(request: QueryRequest) -> EvidencePacket:
    """Honest empty response for a workspace that never published."""
    normalized = normalized_query(request)
    omissions = tuple(
        OmittedEvidence(
            operation_index=index,
            op=_op_name(op),
            reason="unpublished",
            detail="workspace/profile has no published set",
        )
        for index, op in enumerate(request.operations)
    )
    return assemble_packet(
        query=normalized,
        publication=None,
        publication_status="unpublished",
        candidates=(),
        pre_omissions=omissions,
        budget_view=normalized["budget"],
        context=normalized["context"],
        include_text=request.output.include_text,
        operations_executed=0,
        candidates_considered=0,
    )


def _op_name(op: Any) -> str:
    return op.op


async def _execute_pinned(
    reader: PublicationReader,
    request: QueryRequest,
    _after_operation: Callable[[int], Any] | None,
) -> EvidencePacket:
    normalized = normalized_query(request)
    budget = request.budget
    attribution = reader.explain()
    include_text = request.output.include_text

    candidates: list[CandidateUnit] = []
    omissions: list[OmittedEvidence] = []
    candidates_considered = 0
    operations_executed = 0

    for index, op in enumerate(request.operations):
        name = _op_name(op)
        if name == "lexical_search":
            remaining = budget.max_candidates - candidates_considered
            if remaining <= 0:
                omissions.append(
                    OmittedEvidence(
                        operation_index=index,
                        op=name,
                        reason="candidate_budget",
                        detail=(
                            f"candidate budget max_candidates="
                            f"{budget.max_candidates} exhausted by earlier "
                            "operations; retrieval withheld"
                        ),
                    )
                )
                continue
            # Probe one row beyond the requested limit (within the
            # candidate budget) so "more matches exist" is reported
            # instead of silently presenting a truncated page as
            # exhaustive.
            probe_limit = min(op.limit + 1, remaining)
            hits = await reader.search(
                compile_lexical_match(op.text, op.mode),
                limit=probe_limit,
            )
            operations_executed += 1
            more_exist = len(hits) > op.limit
            budget_capped = (
                not more_exist
                and probe_limit < op.limit + 1
                and len(hits) == probe_limit
            )
            hits = hits[: min(op.limit, len(hits))]
            candidates_considered += len(hits)
            if not hits:
                omissions.append(
                    OmittedEvidence(
                        operation_index=index,
                        op=name,
                        reason="no_hit",
                        detail="lexical search matched no published content",
                    )
                )
            elif more_exist or budget_capped:
                if more_exist:
                    detail = (
                        f"more matches exist beyond the requested limit="
                        f"{op.limit}; at least one additional match was "
                        "withheld"
                    )
                else:
                    detail = (
                        f"candidate budget max_candidates={budget.max_candidates} "
                        f"capped retrieval at {probe_limit} of requested "
                        f"{op.limit}; more matches may exist"
                    )
                omissions.append(
                    OmittedEvidence(
                        operation_index=index,
                        op=name,
                        reason="candidate_budget",
                        detail=detail,
                    )
                )
            for hit in hits:
                candidates.append(
                    CandidateUnit(
                        operation_index=index,
                        op=name,
                        locator=EvidenceLocator(
                            publication_set_id=hit.publication_set_id,
                            materialized_generation_id=attribution[
                                "materialized_generation_id"
                            ],
                            lexical_generation_id=hit.lexical_generation_id,
                            record_id=hit.record_id,
                            view_id=hit.view_id,
                            node_id=hit.node_id,
                            revision_ref=hit.revision_ref,
                            text_hash=hit.text_hash,
                            row_index=hit.row_index,
                        ),
                        text=hit.text,
                        rank=hit.rank,
                    )
                )
        elif name == "record_get":
            record = await reader.get_record(op.record_id)
            operations_executed += 1
            if record is None:
                omissions.append(
                    OmittedEvidence(
                        operation_index=index,
                        op=name,
                        reason="not_found",
                        detail=(
                            f"record {op.record_id!r} is not present in the "
                            "pinned materialized generation"
                        ),
                    )
                )
            else:
                unit = _record_candidate(
                    index, attribution, record, op.node_id
                )
                if unit is None:
                    omissions.append(
                        OmittedEvidence(
                            operation_index=index,
                            op=name,
                            reason="node_not_found",
                            detail=(
                                f"record {op.record_id!r} has no text node "
                                f"{op.node_id!r}"
                            ),
                        )
                    )
                else:
                    candidates.append(unit)
        else:  # pragma: no cover - contract guarantees finite ops
            raise QueryBudgetError(f"unhandled operator {name!r}")
        if _after_operation is not None:
            await _after_operation(index)

    return assemble_packet(
        query=normalized,
        publication=attribution,
        publication_status="published",
        candidates=candidates,
        pre_omissions=omissions,
        budget_view=normalized["budget"],
        context=normalized["context"],
        include_text=include_text,
        operations_executed=operations_executed,
        candidates_considered=candidates_considered,
    )


def _record_candidate(
    index: int,
    attribution: Mapping[str, Any],
    record: Any,
    node_id: str | None,
) -> CandidateUnit | None:
    """Build an exact-selection candidate from a pinned record.

    With ``node_id``: the unit is that one content node (text plus the
    record's revision identity); without it, the unit is the whole
    record (no text body claimed — its content hash is the record's
    payload byte hash). ``None`` means the requested node does not
    exist in the record.
    """
    payload = record.payload if isinstance(record.payload, Mapping) else {}
    view_id = payload.get("view_id") or "document"
    if node_id is not None:
        texts = payload.get("texts")
        if not isinstance(texts, Mapping) or node_id not in texts:
            return None
        text = texts[node_id]
        if not isinstance(text, str):
            return None
        return CandidateUnit(
            operation_index=index,
            op="record_get",
            locator=EvidenceLocator(
                publication_set_id=attribution["publication_set_id"],
                materialized_generation_id=attribution[
                    "materialized_generation_id"
                ],
                lexical_generation_id=attribution["lexical_generation_id"],
                record_id=record.record_id,
                view_id=str(view_id),
                node_id=node_id,
                revision_ref=record.identity_hash,
                text_hash=payload_byte_hash(text.encode("utf-8")),
                row_index=None,
            ),
            text=text,
            rank=None,
        )
    return CandidateUnit(
        operation_index=index,
        op="record_get",
        locator=EvidenceLocator(
            publication_set_id=attribution["publication_set_id"],
            materialized_generation_id=attribution["materialized_generation_id"],
            lexical_generation_id=attribution["lexical_generation_id"],
            record_id=record.record_id,
            view_id=str(view_id),
            node_id=None,
            revision_ref=record.identity_hash,
            text_hash=record.payload_byte_hash or record.identity_hash,
            row_index=None,
        ),
        text=None,
        rank=None,
    )
