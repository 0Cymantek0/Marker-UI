"""Evidence-backed extraction service (PR80A).

One vertical seam over the existing authorities:

* evidence comes from :func:`app.context_runtime.executor.execute_query`
  — the same pinned-publication, authorization-first path every other
  consumer uses. There is no second retrieval route here;
* accepted values are committed as kernel claim/assessment/proof-support
  records, so extraction inherits the proof DAG, cycle, grounding, and
  input-integrity rules instead of minting a parallel truth;
* the full result (candidates, conflicts, missing fields, invariant
  findings) is committed as one non-authoritative native-object view
  record, giving review/revalidation a durable, identity-addressed
  artifact that can never masquerade as source evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.context_runtime import QUERY_SCHEMA_VERSION, execute_query, parse_query_request
from app.extraction.contract import ExtractionRequest, resolve_schema
from app.extraction.extractor import extract_candidates
from app.extraction.hybrid import (
    RULE_HYBRID_CORROBORATED,
    reconcile_hybrid,
)
from app.extraction.reconciliation import (
    HYBRID_POLICY_VERSION,
    RECONCILE_POLICY_ID,
    RECONCILE_POLICY_VERSION,
)
from app.extraction.results import (
    FIELD_OUTCOME_ACCEPTED,
    RUN_ACCEPTED,
    RUN_PARTIAL,
    RUN_REVIEW_REQUIRED,
    RUN_STALE_CONTEXT,
    ExtractionContext,
    ExtractionResult,
    result_from_dict,
)
from app.extraction.review import (
    ReviewDecision,
    ReviewError,
    StaleReviewError,
    apply_review,
)
from app.extraction.review_ops import (
    REVIEW_TRANSITION_ACCEPTED,
    REVIEW_TRANSITION_BYPASS_REFUSED,
    REVIEW_TRANSITION_CORRECTED,
    REVIEW_TRANSITION_REJECTED,
    REVIEW_TRANSITION_REQUIRED,
    REVIEW_TRANSITION_STALE,
    ReviewTransition,
    iter_review_required_fields,
    review_transition_record,
    utc_now_iso,
)
from app.extraction.specialist import (
    LANE_CONTEXT_REFUSED,
    SpecialistLane,
    SpecialistLaneResult,
)
from app.extraction.validation import evaluate_invariants
from app.kernel.commit import KernelCommitBatch, KernelCommitService
from app.kernel.errors import DuplicateRecordIdentityError, KernelError
from app.kernel.proofs import ProofSupportRecord
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
    DecisionRecord,
    NativeObjectRecord,
)
from app.kernel.replay import read_head
from app.kernel.models import KernelRecord as KernelRecordRow

logger = logging.getLogger(__name__)

#: Identity labels for committed extraction records.
EXTRACTOR_NAME = "marker-extraction"
EXTRACTOR_VERSION = "pr80a.1"
#: Workflow class stamped on extraction assessments.
EXTRACTION_WORKFLOW_CLASS = "marker.extraction.pr80a.v1"
#: Workflow class stamped on hybrid-corroborated assessments.
HYBRID_WORKFLOW_CLASS = "marker.extraction.hybrid.v1"
#: Authority rule for anchor-witness proof supports.
WITNESS_AUTHORITY_RULE = f"{RECONCILE_POLICY_ID}/v1:anchor-witness"
#: Authority rule for deterministically corroborated proof supports:
#: the proof is the normalizer over cited source text, not the model.
CORROBORATION_AUTHORITY_RULE = (
    f"{RECONCILE_POLICY_ID}/{HYBRID_POLICY_VERSION}:deterministic-normalization"
)


def _short_identity(identity: str) -> str:
    digest = identity.split(":", 1)[1] if ":" in identity else identity
    return digest[:16]


def result_record_id(result_identity: str) -> str:
    """Deterministic record id of a result's view record."""
    return f"extraction.result.{_short_identity(result_identity)}"


def _assertion_record_id(claim_key: str, value: Any) -> str:
    digest = hashlib.sha256(
        json.dumps([claim_key, str(value)], separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"extraction.assertion.{digest}"


def _assessment_record_id(assertion_ref: str, policy_revision: str) -> str:
    digest = hashlib.sha256(
        json.dumps([assertion_ref, policy_revision], separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"extraction.assessment.{digest}"


def _support_record_id(holder_ref: str, evidence_ref: str) -> str:
    digest = hashlib.sha256(
        json.dumps([holder_ref, evidence_ref], separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"extraction.support.{digest}"


class ExtractionService:
    """Run, persist, revalidate, and adjudicate extractions for one workspace."""

    def __init__(
        self,
        session_factory: Any,
        commit_service: KernelCommitService | None = None,
        *,
        workspace_id: str = "local",
        specialist: SpecialistLane | None = None,
        review_clock: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._commit_service = commit_service or KernelCommitService(session_factory)
        self.workspace_id = workspace_id
        self._specialist = specialist
        # Operational review-accounting clock. Injectable for
        # deterministic dwell measurement; defaults to real UTC.
        self._review_clock = review_clock or utc_now_iso

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def _query_for_schema(self, schema: Any) -> Any:
        """Build the marker.query.v1 request covering every schema anchor."""
        anchors: list[str] = []
        for spec in schema.fields:
            if spec.anchor not in anchors:
                anchors.append(spec.anchor)
        for item in schema.line_items:
            if item.anchor not in anchors:
                anchors.append(item.anchor)
        operations = [
            {"op": "lexical_search", "text": anchor, "limit": 25} for anchor in anchors
        ]
        return parse_query_request(
            {
                "schema_version": QUERY_SCHEMA_VERSION,
                "workspace_id": self.workspace_id,
                "operations": operations,
                "budget": {
                    "max_operations": max(8, len(operations)),
                    "max_candidates": 200,
                    "max_evidence_units": 100,
                    "max_output_chars": 1_000_000,
                },
            }
        )

    async def run(self, request: ExtractionRequest) -> ExtractionResult:
        """Execute one extraction run against the current published truth."""
        schema = resolve_schema(request.schema_id, request.schema_version)
        query = self._query_for_schema(schema)
        try:
            packet = await execute_query(self._session_factory, query)
        except KernelError as exc:
            logger.exception("extraction query failed for %s", request.schema_id)
            raise

        publication = packet.publication or {}
        context = ExtractionContext(
            workspace_id=self.workspace_id,
            publication_set_id=str(publication.get("publication_set_id") or ""),
            materialized_generation_id=str(
                publication.get("materialized_generation_id") or ""
            ),
            kernel_snapshot_commit_id=await read_head(
                self._session_factory, self.workspace_id
            ),
            packet_identity_ids=(packet.identity_id,),
            policy_id=RECONCILE_POLICY_ID,
            policy_version=RECONCILE_POLICY_VERSION,
        )

        if (
            request.expected_publication_set_id
            and context.publication_set_id != request.expected_publication_set_id
        ):
            result = ExtractionResult(
                schema_id=schema.schema_id,
                schema_version=schema.version,
                schema_identity=schema.identity,
                context=context,
                run_status=RUN_STALE_CONTEXT,
                fields={},
                line_items={},
                invariants=(),
                error=(
                    f"expected publication {request.expected_publication_set_id!r} "
                    f"but the active publication is {context.publication_set_id!r}"
                ),
            )
            return result

        candidates = extract_candidates(packet, schema)
        lane_result = self._run_specialist_lane(packet, schema, context)
        reconciled = reconcile_hybrid(
            schema,
            candidates,
            lane_result,
            workspace_id=self.workspace_id,
            publication_set_id=context.publication_set_id,
        )
        result = ExtractionResult(
            schema_id=schema.schema_id,
            schema_version=schema.version,
            schema_identity=schema.identity,
            context=context,
            run_status=_run_status(reconciled),
            fields=reconciled.fields,
            line_items=reconciled.line_items,
            invariants=reconciled.invariants,
            specialist=lane_result.report() if lane_result is not None else None,
        )

        await self._persist_result(result)
        return result

    def _run_specialist_lane(
        self, packet: Any, schema: Any, context: ExtractionContext
    ) -> SpecialistLaneResult | None:
        """Invoke the specialist lane if one is configured for this service.

        Opt-in by construction: services built without a lane keep pure
        PR80A semantics. The lane sees only this run's authorized
        packet; if its recorded context binding does not match the
        run's authoritative context (stale replay defense), its
        proposals are refused wholesale and the refusal is reported.
        """
        if self._specialist is None:
            return None
        lane_result = self._specialist.generate(
            packet, schema, workspace_id=self.workspace_id
        )
        provenance = lane_result.provenance
        if provenance is not None and (
            provenance.publication_set_id != context.publication_set_id
            or provenance.workspace_id != self.workspace_id
        ):
            return SpecialistLaneResult(
                status=LANE_CONTEXT_REFUSED,
                producer_id=lane_result.producer_id,
                producer_family=lane_result.producer_family,
                config_identity=lane_result.config_identity,
                provenance=provenance,
                runtime=lane_result.runtime,
                error_detail=(
                    "lane context binding does not match this run's "
                    "publication/workspace; proposals refused"
                ),
            )
        return lane_result

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    async def _persist_result(self, result: ExtractionResult) -> None:
        """Commit accepted claims + the result view record (idempotent)."""
        records: list[Any] = [self._result_view_record(result)]
        head = result.context.kernel_snapshot_commit_id

        def _accept(outcome: FieldOutcome, claim_key: str, predicate: str) -> None:
            if outcome.status != FIELD_OUTCOME_ACCEPTED or outcome.value is None:
                return
            corroborated = outcome.rule == RULE_HYBRID_CORROBORATED
            policy_revision = (
                f"{RECONCILE_POLICY_VERSION}+{HYBRID_POLICY_VERSION}"
                if corroborated
                else RECONCILE_POLICY_VERSION
            )
            assertion = ClaimAssertionRecord(
                record_id=_assertion_record_id(claim_key, outcome.value),
                claim_key=claim_key,
                subject=f"extraction:{result.schema_id}:{self.workspace_id}",
                predicate=predicate,
                value=outcome.value,
                qualifiers={"schema_version": result.schema_version},
            )
            evidence_refs = sorted(
                {
                    cite.record_id
                    for candidate in outcome.candidates
                    if candidate.value == outcome.value
                    for cite in candidate.evidence
                }
            )
            if not evidence_refs:
                return
            declared_context: dict[str, Any] = {
                "publication_set_id": result.context.publication_set_id,
                "schema_identity": result.schema_identity,
                "result_identity": result.identity,
            }
            if corroborated:
                # The committed assessment must say HOW the value was
                # proved: deterministic normalization over cited source
                # text, with the hybrid workflow class and rule named.
                declared_context["hybrid_rule"] = RULE_HYBRID_CORROBORATED
            assessment = ClaimAssessmentRecord(
                record_id=_assessment_record_id(assertion.record_id, policy_revision),
                assertion_ref=assertion.record_id,
                outcome="source_exact",
                policy_id=RECONCILE_POLICY_ID,
                policy_revision=policy_revision,
                evidence_refs=tuple(evidence_refs),
                snapshot_commit_id=head,
                workflow_class=(
                    HYBRID_WORKFLOW_CLASS if corroborated else EXTRACTION_WORKFLOW_CLASS
                ),
                declared_context=declared_context,
            )
            records.append(assertion)
            records.append(assessment)
            authority_rule = (
                CORROBORATION_AUTHORITY_RULE if corroborated else WITNESS_AUTHORITY_RULE
            )
            for ref in evidence_refs:
                records.append(
                    ProofSupportRecord(
                        record_id=_support_record_id(assessment.record_id, ref),
                        holder_ref=assessment.record_id,
                        evidence_ref=ref,
                        role="witness",
                        authority_rule=authority_rule,
                    )
                )

        for name, outcome in result.fields.items():
            _accept(outcome, f"{result.schema_id}@{name}", name)
        for item_name, rows in result.line_items.items():
            for row in rows:
                if row.status != FIELD_OUTCOME_ACCEPTED:
                    continue
                identity_label = ".".join(
                    f"{k}={row.identity[k]}" for k in sorted(row.identity)
                )
                for field_name, outcome in row.fields.items():
                    _accept(
                        outcome,
                        f"{result.schema_id}@{item_name}[{identity_label}].{field_name}",
                        f"{item_name}.{field_name}",
                    )

        # Operational accounting: fields entering the review-required
        # state get a durable transition at the authoritative moment.
        # Non-authoritative view records; they change no claim state.
        required_at = self._review_clock()
        for field_path, _outcome in iter_review_required_fields(result):
            records.append(
                review_transition_record(
                    ReviewTransition(
                        kind=REVIEW_TRANSITION_REQUIRED,
                        result_identity=result.identity,
                        field_path=field_path,
                        publication_set_id=result.context.publication_set_id,
                        occurred_at=required_at,
                    )
                )
            )

        await self._commit_records(
            records, producer={"operation": "extraction.run", "schema": result.schema_id}
        )

    def _result_view_record(self, result: ExtractionResult) -> NativeObjectRecord:
        return NativeObjectRecord(
            record_id=result_record_id(result.identity),
            source_uri=f"marker://extraction/{result.schema_id}",
            locator=result.identity,
            media_type="application/json",
            extractor_name=EXTRACTOR_NAME,
            extractor_version=EXTRACTOR_VERSION,
            properties={"result": result.to_dict()},
        )

    async def _commit_records(self, records: list[Any], *, producer: dict[str, Any]) -> None:
        batch = KernelCommitBatch(
            workspace_id=self.workspace_id,
            records=tuple(records),
            producer=producer,
        )
        try:
            await self._commit_service.commit(batch)
        except DuplicateRecordIdentityError:
            # Deterministic rerun over the same authoritative inputs: the
            # identical records already exist; the kernel's semantic
            # identity uniqueness IS the idempotency mechanism.
            logger.info("extraction commit replayed onto existing identities")

    # ------------------------------------------------------------------
    # stored-result access, revalidation, review
    # ------------------------------------------------------------------

    async def load_result(self, result_identity: str) -> ExtractionResult:
        """Load a stored result view record by result identity."""
        async with self._session_factory() as session:
            row = await session.get(KernelRecordRow, result_record_id(result_identity))
        if row is None:
            raise KeyError(f"no stored extraction result {result_identity!r}")
        payload = json.loads(row.payload_json)
        return result_from_dict(payload["properties"]["result"])

    async def current_publication_set_id(self) -> str | None:
        """Resolve the workspace's active publication via the query path."""
        probe = parse_query_request(
            {
                "schema_version": QUERY_SCHEMA_VERSION,
                "workspace_id": self.workspace_id,
                "operations": [{"op": "lexical_search", "text": "probe", "limit": 1}],
                "budget": {
                    "max_operations": 8,
                    "max_candidates": 5,
                    "max_evidence_units": 5,
                    "max_output_chars": 10000,
                },
            }
        )
        packet = await execute_query(self._session_factory, probe)
        publication = packet.publication or {}
        return str(publication.get("publication_set_id")) or None

    async def revalidate(self, result_identity: str) -> dict[str, Any]:
        """Report whether a stored result still matches live truth."""
        result = await self.load_result(result_identity)
        current = await self.current_publication_set_id()
        status = (
            "current"
            if current == result.context.publication_set_id
            else RUN_STALE_CONTEXT
        )
        return {
            "result_identity": result_identity,
            "recorded_publication_set_id": result.context.publication_set_id,
            "current_publication_set_id": current,
            "status": status,
        }

    async def _record_review_transition(self, transition: ReviewTransition) -> None:
        """Commit one operational transition (its own small batch)."""
        await self._commit_records(
            [review_transition_record(transition)],
            producer={
                "operation": "extraction.review_ops",
                "transition": transition.kind,
            },
        )

    async def apply_review(self, decision: ReviewDecision) -> ExtractionResult:
        """Apply one review decision to its bound stored result.

        Fails with :class:`StaleReviewError` when the decision's bound
        context no longer matches the stored result, or when the stored
        result's publication is no longer the active one (the reviewed
        evidence generation may have been superseded). Refused stale and
        bypass attempts leave a durable operational transition; they
        never change claim state.
        """
        stored = await self.load_result(decision.result_identity)
        current = await self.current_publication_set_id()
        if current != stored.context.publication_set_id:
            await self._record_review_transition(
                ReviewTransition(
                    kind=REVIEW_TRANSITION_STALE,
                    result_identity=decision.result_identity,
                    field_path=decision.field_path,
                    publication_set_id=stored.context.publication_set_id,
                    occurred_at=self._review_clock(),
                    reviewer=decision.reviewer,
                    detail="the reviewed publication is no longer active",
                )
            )
            raise StaleReviewError(
                "the reviewed publication is no longer active: reviewed "
                f"{stored.context.publication_set_id!r}, now {current!r}"
            )

        if decision.field_path not in stored.fields:
            raise KeyError(f"result has no scalar field {decision.field_path!r}")
        schema = resolve_schema(stored.schema_id, stored.schema_version)
        updated_fields = dict(stored.fields)
        try:
            updated_fields[decision.field_path] = apply_review(
                stored.fields[decision.field_path],
                decision,
                result_identity=stored.identity,
                schema_identity=stored.schema_identity,
                publication_set_id=stored.context.publication_set_id,
            )
        except StaleReviewError:
            await self._record_review_transition(
                ReviewTransition(
                    kind=REVIEW_TRANSITION_STALE,
                    result_identity=decision.result_identity,
                    field_path=decision.field_path,
                    publication_set_id=stored.context.publication_set_id,
                    occurred_at=self._review_clock(),
                    reviewer=decision.reviewer,
                    detail="decision context binding does not match the result",
                )
            )
            raise
        except ReviewError:
            # Grounding/already-accepted refusals are bypass attempts:
            # acceptance without the evidence the policy requires.
            await self._record_review_transition(
                ReviewTransition(
                    kind=REVIEW_TRANSITION_BYPASS_REFUSED,
                    result_identity=decision.result_identity,
                    field_path=decision.field_path,
                    publication_set_id=stored.context.publication_set_id,
                    occurred_at=self._review_clock(),
                    reviewer=decision.reviewer,
                    detail="review refused: no grounded evidence or already accepted",
                )
            )
            raise
        invariants = evaluate_invariants(schema, updated_fields, stored.line_items)
        updated = ExtractionResult(
            schema_id=stored.schema_id,
            schema_version=stored.schema_version,
            schema_identity=stored.schema_identity,
            context=stored.context,
            run_status=_derive_run_status(updated_fields, stored.line_items, invariants),
            fields=updated_fields,
            line_items=stored.line_items,
            invariants=invariants,
        )

        decision_record = DecisionRecord(
            record_id=f"extraction.review.{_short_identity(hashlib.sha256(decision.result_identity.encode('utf-8') + decision.field_path.encode('utf-8') + decision.action.encode('utf-8')).hexdigest())}",
            decision_key=(
                f"extraction-review:{_short_identity(decision.result_identity)}"
                f":{decision.field_path}"
            ),
            outcome=decision.action,
            rationale=decision.rationale,
            input_refs=(result_record_id(decision.result_identity),),
        )
        records: list[Any] = [
            decision_record,
            self._result_view_record(updated),
        ]
        # The decision transition commits atomically with the decision
        # itself: a replayed decision is rejected as a duplicate batch,
        # so replay can never double-count review burden.
        records.append(
            review_transition_record(
                ReviewTransition(
                    kind={
                        "accept": REVIEW_TRANSITION_ACCEPTED,
                        "correct": REVIEW_TRANSITION_CORRECTED,
                        "reject": REVIEW_TRANSITION_REJECTED,
                    }[decision.action],
                    result_identity=decision.result_identity,
                    field_path=decision.field_path,
                    publication_set_id=stored.context.publication_set_id,
                    occurred_at=self._review_clock(),
                    reviewer=decision.reviewer,
                    decision_record_id=decision_record.record_id,
                )
            )
        )
        if decision.action == "correct":
            corrected = updated_fields[decision.field_path]
            assertion = ClaimAssertionRecord(
                record_id=_assertion_record_id(
                    f"{stored.schema_id}@{decision.field_path}#reviewed",
                    corrected.value,
                ),
                claim_key=f"{stored.schema_id}@{decision.field_path}#reviewed",
                subject=f"extraction:{stored.schema_id}:{self.workspace_id}",
                predicate=decision.field_path,
                value=corrected.value,
                qualifiers={
                    "schema_version": stored.schema_version,
                    "source": "human_review",
                },
            )
            assessment = ClaimAssessmentRecord(
                record_id=_assessment_record_id(
                    assertion.record_id, f"{RECONCILE_POLICY_VERSION}#review"
                ),
                assertion_ref=assertion.record_id,
                outcome="accepted_with_warning",
                policy_id=RECONCILE_POLICY_ID,
                policy_revision=RECONCILE_POLICY_VERSION,
                evidence_refs=(),
                snapshot_commit_id=stored.context.kernel_snapshot_commit_id,
                workflow_class=EXTRACTION_WORKFLOW_CLASS,
                declared_context={
                    "reviewer": decision.reviewer,
                    "result_identity": decision.result_identity,
                },
            )
            records.extend((assertion, assessment))

        await self._commit_records(
            records,
            producer={
                "operation": "extraction.review",
                "field": decision.field_path,
                "action": decision.action,
            },
        )
        return updated


# ---------------------------------------------------------------------------
# run-status derivation
# ---------------------------------------------------------------------------


def _run_status(reconciled: Any) -> str:
    return _derive_run_status(reconciled.fields, reconciled.line_items, reconciled.invariants)


def _derive_run_status(fields: Any, line_items: Any, invariants: Any = ()) -> str:
    """Derive the honest run status from field/item/invariant states."""
    from app.extraction.results import USABLE_FIELD_OUTCOMES

    statuses = {outcome.status for outcome in fields.values()}
    for rows in line_items.values() if line_items else ():
        for row in rows:
            statuses.add(row.status)
            statuses.update(out.status for out in row.fields.values())

    if any(
        getattr(finding, "finding", None) == "violated" for finding in invariants
    ):
        # Accepted values that jointly break a business invariant are not
        # an accepted extraction; the inconsistency escalates to review.
        return RUN_REVIEW_REQUIRED
    if any(status not in USABLE_FIELD_OUTCOMES for status in statuses):
        if {"review_required", "unresolved", "invalid", "rejected"} & statuses:
            return RUN_REVIEW_REQUIRED
        return RUN_PARTIAL
    return RUN_ACCEPTED
