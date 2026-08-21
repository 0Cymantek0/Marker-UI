"""Authorization-first, publication-pinned query execution (PR77/PR78).

One ``execute_query`` call resolves trusted effective authorization,
resolves and pins exactly one PublicationSet, and produces one
:class:`EvidencePacket`. Every retrieval — lexical or exact — is served
through that pinned reader, so a concurrent publication-head switch can
never mix generations inside one packet; and every candidate is checked
against the *current* authorization before it can compete for the
caller's attention, so revoked or forbidden material cannot surface
merely because the pinned generation still contains it.

Authorization is re-resolved before each operation: content stays
pinned, policy does not. A deny committed mid-query linearizes before
the next operation, and the packet identity reflects the freshest
authorization state that shaped delivery.

Kernel integrity errors (tampered index/locator, malformed query
state) propagate unchanged: this layer never falls back or retries
against different state. The publication pin is released in ``finally``
on success, budget termination, validation failure, and cancellation.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.context_runtime.authorization import (
    ASSURANCE_HIGH,
    EffectiveAuthorization,
    resolve_effective_authorization,
)
from app.context_runtime.contract import (
    QueryRequest,
    normalized_query,
)
from app.context_runtime.errors import QueryAuthorizationError, QueryBudgetError
from app.context_runtime.packets import (
    CandidateUnit,
    EvidenceLocator,
    EvidencePacket,
    OmittedEvidence,
    assemble_packet,
)
from app.kernel.publications import (
    LexicalHit,
    PublicationReader,
    PublishedRecord,
    open_published_reader,
)
from app.kernel.retention import DEFAULT_PIN_LEASE_SECONDS
from app.utils.canonical import payload_byte_hash

__all__ = [
    "build_record_candidate",
    "execute_query",
    "resolve_source_ref",
    "resolve_source_ref_for_id",
    "unpublished_packet",
]

#: Authorized-universe traversal bounds for lexical candidate
#: selection. The window pages through the deterministic rank order;
#: the cap bounds worst-case work on a shared corpus dominated by
#: unauthorized matches. Reaching the cap reports an explicit
#: candidate-budget omission ("more matches may exist") — never a
#: silently truncated page presented as exhaustive.
_LEXICAL_TRAVERSAL_WINDOW = 128
_LEXICAL_TRAVERSAL_MAX_ROWS = 2000


async def execute_query(
    session_factory: async_sessionmaker,
    request: QueryRequest,
    *,
    _after_operation: Callable[[int], Any] | None = None,
) -> EvidencePacket:
    """Execute one validated typed request against one pinned
    PublicationSet under trusted effective authorization, and return a
    bounded :class:`EvidencePacket`.

    ``_after_operation`` is a deterministic test hook (mirroring the
    PR76 fault-phase convention): called with the operation index after
    each operation completes, it lets tests switch the publication head
    or commit a live deny mid-execution and prove the in-flight packet
    stays attributable to exactly the original pinned set while no
    newly forbidden evidence is delivered.
    """
    if len(request.operations) > request.budget.max_operations:
        raise QueryBudgetError(
            f"request has {len(request.operations)} operations; budget "
            f"allows {request.budget.max_operations}"
        )

    auth = await resolve_effective_authorization(
        session_factory, request.workspace_id, assurance=request.assurance
    )
    if request.assurance == ASSURANCE_HIGH:
        # High assurance reads only the security-domain partition
        # derived from trusted state. A partition that was never
        # published fails closed — there is deliberately no fallback to
        # the shared index.
        reader = await open_published_reader(
            session_factory,
            request.workspace_id,
            profile=auth.partition_profile(),
            pin_lease_seconds=DEFAULT_PIN_LEASE_SECONDS,
        )
        if reader is None:
            raise QueryAuthorizationError(
                "the high-assurance partition for this workspace is not "
                "published; refusing to fall back to a shared index"
            )
    else:
        reader = await open_published_reader(
            session_factory,
            request.workspace_id,
            profile=request.profile,
            pin_lease_seconds=DEFAULT_PIN_LEASE_SECONDS,
        )
        if reader is None:
            return await _unpublished_packet(request, auth)

    try:
        return await _execute_pinned(
            session_factory, reader, request, auth, _after_operation
        )
    finally:
        # V19: pin released on success, error, cancellation, and budget
        # termination alike — never leaked past the call.
        await reader.close()


async def _unpublished_packet(
    request: QueryRequest, auth: EffectiveAuthorization
) -> EvidencePacket:
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
        authorization=auth.identity_view(),
    )


async def unpublished_packet(
    request: QueryRequest, auth: EffectiveAuthorization
) -> EvidencePacket:
    """Public continuation-service helper for honest unpublished results."""

    return await _unpublished_packet(request, auth)


def _op_name(op: Any) -> str:
    return op.op


async def _execute_pinned(
    session_factory: async_sessionmaker,
    reader: PublicationReader,
    request: QueryRequest,
    initial_auth: EffectiveAuthorization,
    _after_operation: Callable[[int], Any] | None,
) -> EvidencePacket:
    normalized = normalized_query(request)
    budget = request.budget
    attribution = reader.explain()
    include_text = request.output.include_text

    # record_id -> resolved source_ref (content lineage is immutable
    # inside the pinned generation, so it caches across operations; the
    # *policy* over that lineage is re-derived per operation).
    lineage_cache: dict[str, str | None] = {}

    candidates: list[CandidateUnit] = []
    omissions: list[OmittedEvidence] = []
    candidates_considered = 0
    operations_executed = 0
    auth = initial_auth

    for index, op in enumerate(request.operations):
        name = _op_name(op)
        # Live authorization: a deny/epoch/policy change committed after
        # the query started linearizes before this operation.
        if index > 0:
            auth = await resolve_effective_authorization(
                session_factory, request.workspace_id, assurance=request.assurance
            )
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
                if _after_operation is not None:
                    await _after_operation(index)
                continue
            # Probe one authorized row beyond the requested limit
            # (within the candidate budget) so "more matches exist" is
            # reported over the authorized universe instead of silently
            # presenting a truncated page as exhaustive.
            probe_limit = min(op.limit + 1, remaining)
            authorized, traversal_capped = await _authorized_lexical_hits(
                reader,
                auth,
                lineage_cache,
                op.text,
                op.mode,
                probe_limit,
            )
            operations_executed += 1
            more_exist = len(authorized) > op.limit
            budget_capped = (
                not more_exist
                and probe_limit < op.limit + 1
                and len(authorized) == probe_limit
            )
            hits = authorized[: min(op.limit, len(authorized))]
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
            elif more_exist or budget_capped or traversal_capped:
                if more_exist:
                    detail = (
                        f"more matches exist beyond the requested limit="
                        f"{op.limit}; at least one additional match was "
                        "withheld"
                    )
                elif budget_capped:
                    detail = (
                        f"candidate budget max_candidates={budget.max_candidates} "
                        f"capped retrieval at {probe_limit} of requested "
                        f"{op.limit}; more matches may exist"
                    )
                else:
                    detail = (
                        f"authorized-candidate traversal reached its bound of "
                        f"{_LEXICAL_TRAVERSAL_MAX_ROWS} ranked rows before the "
                        f"requested limit={op.limit} was filled; more matches "
                        "may exist"
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
            source_ref = await _resolve_source_ref(reader, record, lineage_cache)
            if record is None or not auth.allows(
                record.record_id,
                source_ref=source_ref,
                domain_key=auth.domain_of(source_ref),
            ):
                # Unauthorized and nonexistent are deliberately the
                # same caller-visible outcome: an exact response must
                # not disclose that hidden material exists.
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
        authorization=auth.identity_view(),
    )


async def _authorized_lexical_hits(
    reader: PublicationReader,
    auth: EffectiveAuthorization,
    lineage_cache: dict[str, str | None],
    text: str,
    mode: str,
    target: int,
) -> tuple[list[LexicalHit], bool]:
    """Walk the pinned generation's deterministic rank order and keep
    only candidates the current authorization allows, until ``target``
    authorized hits are found or the corpus is exhausted.

    Returns the authorized hits plus whether the traversal bound was
    reached first (an honest "more matches may exist" signal — the
    alternative, a fixed over-fetch, silently loses authorized recall
    when unauthorized matches crowd the top of a shared index).
    """
    authorized: list[LexicalHit] = []
    offset = 0
    while len(authorized) < target and offset < _LEXICAL_TRAVERSAL_MAX_ROWS:
        fetch = min(_LEXICAL_TRAVERSAL_WINDOW, _LEXICAL_TRAVERSAL_MAX_ROWS - offset)
        hits = await reader.search(text, mode, limit=fetch, offset=offset)
        if not hits:
            return authorized, False
        for hit in hits:
            source_ref = await _resolve_source_ref_for_id(
                reader, hit.record_id, lineage_cache
            )
            if auth.allows(
                hit.record_id,
                source_ref=source_ref,
                domain_key=auth.domain_of(source_ref),
            ):
                authorized.append(hit)
                if len(authorized) >= target:
                    break
        if len(hits) < fetch:
            return authorized, False
        offset += len(hits)
    return authorized, offset >= _LEXICAL_TRAVERSAL_MAX_ROWS and len(authorized) < target


async def _resolve_source_ref(
    reader: PublicationReader,
    record: PublishedRecord | None,
    lineage_cache: dict[str, str | None],
) -> str | None:
    """Trusted source lineage of one published record, through verified
    reads of the pinned generation only (tampered payloads fail closed
    inside :meth:`PublicationReader.get_record` before they can steer
    an authorization decision)."""
    if record is None:
        return None
    return await _resolve_source_ref_for_id(reader, record.record_id, lineage_cache)


async def resolve_source_ref(
    reader: PublicationReader,
    record: PublishedRecord | None,
    lineage_cache: dict[str, str | None],
) -> str | None:
    """Resolve trusted lineage for continuation paging."""

    return await _resolve_source_ref(reader, record, lineage_cache)


async def resolve_source_ref_for_id(
    reader: PublicationReader,
    record_id: str,
    lineage_cache: dict[str, str | None],
) -> str | None:
    """Resolve trusted lineage by record id for lexical candidates."""

    return await _resolve_source_ref_for_id(reader, record_id, lineage_cache)


async def _resolve_source_ref_for_id(
    reader: PublicationReader, record_id: str, lineage_cache: dict[str, str | None]
) -> str | None:
    if record_id in lineage_cache:
        return lineage_cache[record_id]
    record = await reader.get_record(record_id)
    if record is None:
        lineage_cache[record_id] = None
        return None
    payload = record.payload if isinstance(record.payload, Mapping) else {}
    source_ref: str | None = None
    if record.record_class == "view_document":
        revision_ref = payload.get("content_revision_ref")
        if isinstance(revision_ref, str) and revision_ref:
            revision = await reader.get_record(revision_ref)
            if revision is not None and isinstance(revision.payload, Mapping):
                candidate = revision.payload.get("source_ref")
                if isinstance(candidate, str) and candidate:
                    source_ref = candidate
    elif record.record_class == "content_revision":
        candidate = payload.get("source_ref")
        if isinstance(candidate, str) and candidate:
            source_ref = candidate
    lineage_cache[record_id] = source_ref
    return source_ref


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


def build_record_candidate(
    index: int,
    attribution: Mapping[str, Any],
    record: Any,
    node_id: str | None,
) -> CandidateUnit | None:
    """Build one exact-read candidate for continuation paging."""

    return _record_candidate(index, attribution, record, node_id)
