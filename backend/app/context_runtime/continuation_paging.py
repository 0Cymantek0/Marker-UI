"""Deterministic keyset page execution for PR79A."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from app.context_runtime.authorization import EffectiveAuthorization
from app.context_runtime.contract import LexicalSearchOp, QueryRequest, compile_lexical_match, normalized_query
from app.context_runtime.executor import (
    build_record_candidate,
    resolve_source_ref,
    resolve_source_ref_for_id,
)
from app.context_runtime.packets import (
    CandidateUnit,
    EvidenceLocator,
    EvidencePacket,
    OmittedEvidence,
    assemble_packet,
    candidate_unit_cost,
)
from app.context_runtime.continuation_state import after_from_storage, after_storage, locator_key
from app.kernel.publications import LexicalSearchAfter, PublicationReader
from app.utils.canonical import payload_byte_hash


# Preserve PR78's authorized-recall behavior while bounding forbidden-heavy
# scans. Candidate budget counts authorized candidates; raw index work has a
# separate cumulative hard cap across the continuation chain.
LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION = 2000


def max_work_units(request: QueryRequest) -> int:
    lexical = sum(isinstance(operation, LexicalSearchOp) for operation in request.operations)
    exact = len(request.operations) - lexical
    return lexical * LEXICAL_TRAVERSAL_MAX_ROWS_PER_OPERATION + exact


@dataclass(frozen=True)
class PageRun:
    packet: EvidencePacket
    keyset: dict[str, Any]
    cumulative_budget: dict[str, Any]
    more_work: bool


class ContinuationPager:
    """Run one page against one already-authorized pinned reader."""

    async def run_async(
        self,
        reader: PublicationReader,
        request: QueryRequest,
        auth: EffectiveAuthorization,
        keyset: Mapping[str, Any],
        cumulative: Mapping[str, Any],
        page_size: int,
    ) -> PageRun:
        state = copy.deepcopy(dict(keyset))
        totals = copy.deepcopy(dict(cumulative))
        emitted = set(totals["emitted_keys"])
        candidates: list[CandidateUnit] = []
        omissions: list[OmittedEvidence] = []
        lineage_cache: dict[str, str | None] = {}
        page_considered = 0
        selected_output_chars = 0
        selected_units = 0
        stopped_for_page = False
        attribution = reader.explain()
        query = normalized_query(request)

        for index, operation in enumerate(request.operations):
            if stopped_for_page:
                break
            op_state = state["operations"][str(index)]
            if op_state["exhausted"]:
                continue
            if not op_state["started"]:
                op_state["started"] = True
                totals["operations_executed"] += 1
            if isinstance(operation, LexicalSearchOp):
                (
                    selected_output_chars,
                    selected_units,
                    page_considered,
                    stopped_for_page,
                ) = await self._run_lexical_async(
                    reader=reader,
                    request=request,
                    auth=auth,
                    operation=operation,
                    operation_index=index,
                    op_state=op_state,
                    totals=totals,
                    attribution=attribution,
                    lineage_cache=lineage_cache,
                    candidates=candidates,
                    omissions=omissions,
                    emitted=emitted,
                    page_size=page_size,
                    selected_output_chars=selected_output_chars,
                    selected_units=selected_units,
                    page_considered=page_considered,
                    stopped_for_page=stopped_for_page,
                )
            else:
                if op_state["position"] == 1:
                    op_state["exhausted"] = True
                    continue
                record = await reader.get_record(operation.record_id)
                totals["work_units"] += 1
                op_state["position"] = 1
                op_state["exhausted"] = True
                source_ref = await resolve_source_ref(reader, record, lineage_cache)
                if record is None or not auth.allows(
                    operation.record_id,
                    source_ref=source_ref,
                    domain_key=auth.domain_of(source_ref),
                ):
                    omissions.append(
                        OmittedEvidence(
                            operation_index=index,
                            op=operation.op,
                            reason="not_found",
                            detail="record is unavailable",
                        )
                    )
                    continue
                if totals["candidates_considered"] >= request.budget.max_candidates:
                    omissions.append(
                        OmittedEvidence(
                            operation_index=index,
                            op=operation.op,
                            reason="candidate_budget",
                            detail="cumulative candidate budget exhausted",
                        )
                    )
                    continue
                page_considered += 1
                totals["candidates_considered"] += 1
                candidate = build_record_candidate(
                    index, attribution, record, operation.node_id
                )
                if candidate is None:
                    omissions.append(
                        OmittedEvidence(
                            operation_index=index,
                            op=operation.op,
                            reason="node_not_found",
                            detail="requested node is unavailable",
                        )
                    )
                    continue
                added, selected_output_chars, selected_units = self._accept_candidate(
                    candidate,
                    request,
                    totals,
                    emitted,
                    selected_output_chars,
                    selected_units,
                    omissions,
                )
                if added:
                    candidates.append(candidate)
                    stopped_for_page = len(candidates) >= page_size

        packet = assemble_packet(
            query=query,
            publication=attribution,
            publication_status="published",
            candidates=candidates,
            pre_omissions=omissions,
            budget_view=query["budget"],
            context=query["context"],
            include_text=request.output.include_text,
            operations_executed=totals["operations_executed"],
            candidates_considered=page_considered,
            authorization=auth.identity_view(),
        )
        totals["evidence_units"] += len(packet.evidence)
        totals["output_chars"] += packet.budget.output_chars
        totals["emitted_keys"] = sorted(emitted)
        more_work = any(
            not operation_state["exhausted"]
            for operation_state in state["operations"].values()
        )
        return PageRun(
            packet=packet,
            keyset=state,
            cumulative_budget=totals,
            more_work=more_work,
        )

    async def _run_lexical_async(
        self,
        *,
        reader: PublicationReader,
        request: QueryRequest,
        auth: EffectiveAuthorization,
        operation: LexicalSearchOp,
        operation_index: int,
        op_state: dict[str, Any],
        totals: dict[str, Any],
        attribution: Mapping[str, Any],
        lineage_cache: dict[str, str | None],
        candidates: list[CandidateUnit],
        omissions: list[OmittedEvidence],
        emitted: set[str],
        page_size: int,
        selected_output_chars: int,
        selected_units: int,
        page_considered: int,
        stopped_for_page: bool,
    ) -> tuple[int, int, int, bool]:
        match = compile_lexical_match(operation.text, operation.mode)
        query_hash = payload_byte_hash(match.encode("utf-8"))
        while not op_state["exhausted"] and not stopped_for_page:
            remaining_work = max_work_units(request) - totals["work_units"]
            if remaining_work <= 0:
                omissions.append(
                    OmittedEvidence(
                        operation_index=operation_index,
                        op=operation.op,
                        reason="work_budget",
                        detail="cumulative lexical traversal bound exhausted",
                    )
                )
                break
            if totals["evidence_units"] >= request.budget.max_evidence_units:
                break
            after = None
            if op_state["after"] is not None:
                after = after_from_storage(op_state["after"])
            page = await reader.search_after(
                match,
                limit=min(64, remaining_work),
                after=after,
            )
            processed = 0
            for hit in page.hits:
                if stopped_for_page:
                    break
                processed += 1
                totals["work_units"] += 1
                op_state["after"] = after_storage(
                    LexicalSearchAfter(
                        publication_set_id=hit.publication_set_id,
                        lexical_generation_id=hit.lexical_generation_id,
                        rank=hit.rank,
                        row_index=hit.row_index,
                        query_hash=query_hash,
                    )
                )
                source_ref = await resolve_source_ref_for_id(
                    reader, hit.record_id, lineage_cache
                )
                if not auth.allows(
                    hit.record_id,
                    source_ref=source_ref,
                    domain_key=auth.domain_of(source_ref),
                ):
                    continue
                if totals["candidates_considered"] >= request.budget.max_candidates:
                    omissions.append(
                        OmittedEvidence(
                            operation_index=operation_index,
                            op=operation.op,
                            reason="candidate_budget",
                            detail="cumulative candidate budget exhausted",
                        )
                    )
                    break
                page_considered += 1
                totals["candidates_considered"] += 1
                if op_state["authorized_count"] >= operation.limit:
                    op_state["exhausted"] = True
                    omissions.append(
                        OmittedEvidence(
                            operation_index=operation_index,
                            op=operation.op,
                            reason="candidate_budget",
                            detail="operation result limit reached",
                        )
                    )
                    break
                op_state["authorized_count"] += 1
                candidate = CandidateUnit(
                    operation_index=operation_index,
                    op=operation.op,
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
                added, selected_output_chars, selected_units = self._accept_candidate(
                    candidate,
                    request,
                    totals,
                    emitted,
                    selected_output_chars,
                    selected_units,
                    omissions,
                )
                if added:
                    candidates.append(candidate)
                    stopped_for_page = len(candidates) >= page_size
                if op_state["authorized_count"] >= operation.limit:
                    op_state["exhausted"] = True
                if totals["candidates_considered"] >= request.budget.max_candidates:
                    break
            if totals["candidates_considered"] >= request.budget.max_candidates:
                break
            if op_state["exhausted"] or stopped_for_page:
                break
            if processed < len(page.hits):
                break
            if not page.has_more or not page.hits:
                op_state["exhausted"] = True
                break
        return selected_output_chars, selected_units, page_considered, stopped_for_page

    @staticmethod
    def _accept_candidate(
        candidate: CandidateUnit,
        request: QueryRequest,
        totals: Mapping[str, Any],
        emitted: set[str],
        selected_output_chars: int,
        selected_units: int,
        omissions: list[OmittedEvidence],
    ) -> tuple[bool, int, int]:
        key = locator_key(candidate)
        if key in emitted:
            omissions.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="duplicate",
                    detail="unit already delivered in this continuation chain",
                )
            )
            return False, selected_output_chars, selected_units
        if (
            totals["evidence_units"] + selected_units
            >= request.budget.max_evidence_units
        ):
            omissions.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="unit_budget",
                    detail="cumulative evidence-unit budget exhausted",
                )
            )
            return False, selected_output_chars, selected_units
        cost = candidate_unit_cost(candidate, request.output.include_text)
        remaining_output = request.budget.max_output_chars - totals["output_chars"]
        if cost > remaining_output:
            reason = "unit_too_large" if cost > request.budget.max_output_chars else "output_budget"
            omissions.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason=reason,
                    detail="cumulative output budget cannot fit this unit",
                )
            )
            return False, selected_output_chars, selected_units
        if selected_output_chars + cost > remaining_output:
            omissions.append(
                OmittedEvidence(
                    operation_index=candidate.operation_index,
                    op=candidate.op,
                    reason="output_budget",
                    detail="cumulative output budget exhausted",
                )
            )
            return False, selected_output_chars, selected_units
        emitted.add(key)
        return True, selected_output_chars + cost, selected_units + 1

    @staticmethod
    def budget_allows_continuation(
        request: QueryRequest, cumulative: Mapping[str, Any]
    ) -> bool:
        return ContinuationPager.budget_stop_reason(request, cumulative) is None

    @staticmethod
    def budget_stop_reason(
        request: QueryRequest, cumulative: Mapping[str, Any]
    ) -> str | None:
        if cumulative["work_units"] >= max_work_units(request):
            return "work_budget"
        if cumulative["candidates_considered"] >= request.budget.max_candidates:
            return "candidate_budget"
        if cumulative["evidence_units"] >= request.budget.max_evidence_units:
            return "unit_budget"
        if cumulative["output_chars"] >= request.budget.max_output_chars:
            return "output_budget"
        return None
