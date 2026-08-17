"""Commit-boundary verification-risk gate (PR75)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.kernel.errors import KernelError, VerificationRiskGateError
from app.kernel.records import ClaimAssertionRecord, NativeFactRecord
from app.utils.canonical import canonical_json_str, to_json_ready
from .policy import (
    AUTHORITY_SOURCE_NATIVE,
    EVIDENCE_SOURCE_NATIVE,
    HIGH_RISK_SOURCE_NATIVE_MIN_SAMPLES,
    HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
    HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
    HIGH_RISK_SOURCE_NATIVE_RISK_BOUND,
    HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
    OUTCOME_VERIFIED,
    VerificationRiskPolicy,
    evaluate_verification_risk_policy,
)
from .records import VerificationRiskEvidenceRecord

def _risk_gate_payload_json(
    payload_json: Any, *, record_id: str
) -> Mapping[str, Any]:
    """Decode one prepared/stored record payload for commit-time gating."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise VerificationRiskGateError(
            f"record {record_id!r} has invalid JSON payload: {exc}"
        ) from None
    if not isinstance(payload, Mapping):
        raise VerificationRiskGateError(
            f"record {record_id!r} payload must be an object"
        )
    return payload


def _risk_gate_payload(record: Any, *, record_id: str) -> Mapping[str, Any]:
    return _risk_gate_payload_json(record.payload_json, record_id=record_id)


def _risk_gate_mapping(
    value: Any, *, record_id: str, field_name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationRiskGateError(
            f"assessment {record_id!r} {field_name} must be an object"
        )
    return value


def _risk_gate_assertion(
    payload: Mapping[str, Any], *, record_id: str
) -> ClaimAssertionRecord:
    """Rematerialize assessed assertion before consuming native authority."""
    try:
        return ClaimAssertionRecord.from_payload(payload, record_id=record_id)
    except (KernelError, TypeError, ValueError) as exc:
        raise VerificationRiskGateError(
            f"assertion {record_id!r} is invalid for native authority binding: "
            f"{exc}"
        ) from None


def _risk_gate_native_fact(
    payload: Mapping[str, Any], *, record_id: str
) -> NativeFactRecord:
    """Rematerialize native fact identity fields before claim comparison.

    NativeFactRecord predates a public ``from_payload`` helper.  Rebuild it
    from its immutable stored identity shape so malformed historical payloads
    cannot silently become authority for a claim.
    """
    allowed = {
        "native_object_ref",
        "property_name",
        "raw_representation",
        "typed_interpretation",
        "extractor_lineage",
        "anchor",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise VerificationRiskGateError(
            f"native_fact {record_id!r} has unknown payload fields "
            f"{sorted(unknown)}"
        )
    try:
        lineage = payload["extractor_lineage"]
        if not isinstance(lineage, Mapping):
            raise TypeError("extractor_lineage must be an object")
        if set(lineage) != {"name", "version"}:
            raise ValueError(
                "extractor_lineage must name exactly ['name', 'version']"
            )
        return NativeFactRecord(
            record_id=record_id,
            native_object_ref=payload["native_object_ref"],
            property_name=payload["property_name"],
            raw_representation=payload["raw_representation"],
            typed_interpretation=payload["typed_interpretation"],
            extractor_name=lineage["name"],
            extractor_version=lineage["version"],
            anchor=payload.get("anchor"),
        )
    except (KeyError, KernelError, TypeError, ValueError) as exc:
        raise VerificationRiskGateError(
            f"native_fact {record_id!r} is invalid for native authority binding: "
            f"{exc}"
        ) from None


def _risk_gate_values_equal(left: Any, right: Any) -> bool:
    """Compare claim values in canonical form, preserving JSON types."""
    try:
        return canonical_json_str(to_json_ready(left)) == canonical_json_str(
            to_json_ready(right)
        )
    except (TypeError, ValueError):
        return False


async def check_batch_verification_risk(
    session: Any,
    *,
    workspace_id: str,
    batch_records: Mapping[str, Any],
    current_head: int,
) -> None:
    """Apply PR75's narrow authoritative risk gate to one commit batch.

    Gate activates only for a newly submitted ``verified`` assessment in
    ``high_risk.source_native.v1``.  It runs after PR74 structural proof
    validation, while the commit transaction still holds the writer lock.
    Every other workflow/outcome remains governed by PR74 alone.
    """
    del current_head  # Reserved for future slice-cut checks; no wall-clock use.

    active: list[tuple[str, Mapping[str, Any]]] = []
    for record_id, record in batch_records.items():
        if getattr(record, "record_class", None) != "claim_assessment":
            continue
        payload = _risk_gate_payload(record, record_id=record_id)
        if (
            payload.get("outcome") == OUTCOME_VERIFIED
            and payload.get("workflow_class") == HIGH_RISK_SOURCE_NATIVE_WORKFLOW
        ):
            active.append((record_id, payload))
    if not active:
        return

    # Pull only referenced committed rows.  Batch records overlay committed
    # state, matching PR74's visibility semantics.
    from sqlalchemy import select

    from app.kernel.models import KernelRecord as KernelRecordRow

    refs: set[str] = set()
    for record_id, payload in active:
        assertion_ref = payload.get("assertion_ref")
        if isinstance(assertion_ref, str):
            refs.add(assertion_ref)
        evidence_refs = payload.get("evidence_refs") or ()
        if isinstance(evidence_refs, str) or not isinstance(evidence_refs, Sequence):
            raise VerificationRiskGateError(
                f"assessment {record_id!r} evidence_refs must be a sequence"
            )
        refs.update(ref for ref in evidence_refs if isinstance(ref, str))

    committed_records: dict[str, tuple[str, str]] = {}
    if refs:
        rows = (
            await session.execute(
                select(
                    KernelRecordRow.id,
                    KernelRecordRow.record_class,
                    KernelRecordRow.payload_json,
                ).where(
                    KernelRecordRow.workspace_id == workspace_id,
                    KernelRecordRow.id.in_(sorted(refs)),
                )
            )
        ).all()
        committed_records = {
            row.id: (row.record_class, row.payload_json) for row in rows
        }

    # A new assessment's proof support records are normally all in its batch,
    # but load committed support rows too so the check remains explicit about
    # the authority relation it consumes.
    support_rows = (
        await session.execute(
            select(KernelRecordRow.id, KernelRecordRow.payload_json).where(
                KernelRecordRow.workspace_id == workspace_id,
                KernelRecordRow.record_class == "proof_support",
            )
        )
    ).all()
    committed_supports: dict[str, Mapping[str, Any]] = {}
    for support_id, payload_json in support_rows:
        committed_supports[support_id] = _risk_gate_payload_json(
            payload_json,
            record_id=support_id,
        )

    for assessment_id, assessment in active:
        policy = _risk_gate_mapping(
            assessment.get("policy"), record_id=assessment_id, field_name="policy"
        )
        if (
            policy.get("policy_id") != HIGH_RISK_SOURCE_NATIVE_POLICY_ID
            or policy.get("revision") != HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION
        ):
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must use policy "
                f"{HIGH_RISK_SOURCE_NATIVE_POLICY_ID}/"
                f"{HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION}"
            )

        declared_context = _risk_gate_mapping(
            assessment.get("declared_context"),
            record_id=assessment_id,
            field_name="declared_context",
        )
        risk_context = _risk_gate_mapping(
            declared_context.get("verification_risk"),
            record_id=assessment_id,
            field_name="declared_context.verification_risk",
        )
        expected_context_fields = {
            "evidence_ref",
            "evaluation_slice_id",
            "as_of",
        }
        if set(risk_context) != expected_context_fields:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} declared_context.verification_risk "
                f"must name exactly {sorted(expected_context_fields)}"
            )
        evidence_ref = risk_context["evidence_ref"]
        evaluation_slice_id = risk_context["evaluation_slice_id"]
        as_of = risk_context["as_of"]
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} verification-risk evidence_ref "
                "must be a non-empty record id"
            )
        if not isinstance(evaluation_slice_id, str) or not evaluation_slice_id:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} verification-risk "
                "evaluation_slice_id must be a non-empty string"
            )
        if not isinstance(as_of, str) or not as_of:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} verification-risk as_of "
                "must be a non-empty ISO timestamp"
            )

        evidence_refs = assessment.get("evidence_refs") or ()
        if evidence_ref not in evidence_refs:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} does not declare risk evidence "
                f"{evidence_ref!r} in evidence_refs"
            )

        # Compose proof supports from this batch and committed history.
        supports: list[tuple[str, Mapping[str, Any]]] = []
        for support_id, record in batch_records.items():
            if getattr(record, "record_class", None) != "proof_support":
                continue
            payload = _risk_gate_payload(record, record_id=support_id)
            if payload.get("holder_ref") == assessment_id:
                supports.append((support_id, payload))
        for support_id, payload in committed_supports.items():
            if payload.get("holder_ref") == assessment_id:
                supports.append((support_id, payload))
        risk_supports = [
            payload
            for _support_id, payload in supports
            if payload.get("evidence_ref") == evidence_ref
        ]
        if not risk_supports:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} does not carry risk evidence "
                f"{evidence_ref!r} in proof support"
            )
        if any(payload.get("role") != "input" for payload in risk_supports):
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must present risk evidence "
                f"{evidence_ref!r} as role=input; empirical risk calibrates "
                "authority but is not itself an independent witness"
            )

        # At least one independent source-native fact must be supported in
        # addition to the statistical artifact.  Observations/model consensus
        # cannot substitute for a native fact.
        class_by_ref: dict[str, str] = {
            record_id: getattr(record, "record_class", "")
            for record_id, record in batch_records.items()
        }
        class_by_ref.update(
            {record_id: record_class for record_id, (record_class, _payload) in committed_records.items()}
        )
        native_fact_supports = [
            payload
            for _support_id, payload in supports
            if class_by_ref.get(payload.get("evidence_ref")) == "native_fact"
        ]
        if not native_fact_supports:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} requires a supported native_fact "
                "in addition to verification-risk evidence"
            )
        if any(payload.get("role") != "witness" for payload in native_fact_supports):
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must present its native_fact "
                "authority as role=witness"
            )

        assertion_ref = assessment.get("assertion_ref")
        if not isinstance(assertion_ref, str) or not assertion_ref:
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} must name a non-empty assertion_ref "
                "for native authority binding"
            )
        assertion_record = batch_records.get(assertion_ref)
        if assertion_record is not None:
            assertion_class = getattr(assertion_record, "record_class", None)
            assertion_payload = _risk_gate_payload(
                assertion_record, record_id=assertion_ref
            )
        else:
            committed_assertion = committed_records.get(assertion_ref)
            if committed_assertion is None:
                raise VerificationRiskGateError(
                    f"assertion {assertion_ref!r} is not visible in workspace "
                    f"{workspace_id!r}"
                )
            assertion_class, assertion_payload_json = committed_assertion
            assertion_payload = _risk_gate_payload_json(
                assertion_payload_json, record_id=assertion_ref
            )
        if assertion_class != "claim_assertion":
            raise VerificationRiskGateError(
                f"assessment {assessment_id!r} assertion {assertion_ref!r} "
                f"resolves to {assertion_class!r}, not claim_assertion"
            )
        assertion = _risk_gate_assertion(assertion_payload, record_id=assertion_ref)

        for support_payload in native_fact_supports:
            fact_ref = support_payload.get("evidence_ref")
            if not isinstance(fact_ref, str) or not fact_ref:
                raise VerificationRiskGateError(
                    f"assessment {assessment_id!r} native_fact support must "
                    "name a non-empty evidence_ref"
                )
            fact_record = batch_records.get(fact_ref)
            if fact_record is not None:
                fact_class = getattr(fact_record, "record_class", None)
                fact_payload = _risk_gate_payload(fact_record, record_id=fact_ref)
            else:
                committed_fact = committed_records.get(fact_ref)
                if committed_fact is None:
                    raise VerificationRiskGateError(
                        f"native_fact {fact_ref!r} is not visible in workspace "
                        f"{workspace_id!r}"
                    )
                fact_class, fact_payload_json = committed_fact
                fact_payload = _risk_gate_payload_json(
                    fact_payload_json, record_id=fact_ref
                )
            if fact_class != "native_fact":
                raise VerificationRiskGateError(
                    f"assessment {assessment_id!r} witness {fact_ref!r} "
                    f"resolves to {fact_class!r}, not native_fact"
                )
            fact = _risk_gate_native_fact(fact_payload, record_id=fact_ref)
            if not (
                fact.native_object_ref == assertion.subject
                and fact.property_name == assertion.predicate
                and _risk_gate_values_equal(
                    fact.typed_interpretation, assertion.value
                )
            ):
                raise VerificationRiskGateError(
                    f"assessment {assessment_id!r} native_fact {fact_ref!r} "
                    f"is not competent for claim assertion {assertion_ref!r}: "
                    "native_object_ref/property_name/typed_interpretation must "
                    "match subject/predicate/value"
                )

        risk_record = batch_records.get(evidence_ref)
        if risk_record is not None:
            risk_class = getattr(risk_record, "record_class", None)
            risk_payload = _risk_gate_payload(risk_record, record_id=evidence_ref)
        else:
            committed = committed_records.get(evidence_ref)
            if committed is None:
                raise VerificationRiskGateError(
                    f"risk evidence {evidence_ref!r} is not visible in workspace "
                    f"{workspace_id!r}"
                )
            risk_class, risk_payload = committed
        if risk_class != "verification_risk_evidence":
            raise VerificationRiskGateError(
                f"risk evidence reference {evidence_ref!r} resolves to "
                f"{risk_class!r}, not verification_risk_evidence"
            )
        try:
            evidence = VerificationRiskEvidenceRecord.from_payload(
                risk_payload, record_id=evidence_ref
            )
            risk_policy = VerificationRiskPolicy(
                policy_id=HIGH_RISK_SOURCE_NATIVE_POLICY_ID,
                policy_revision=HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION,
                workflow_class=HIGH_RISK_SOURCE_NATIVE_WORKFLOW,
                evaluation_slice_id=evaluation_slice_id,
                claim_authority_class=AUTHORITY_SOURCE_NATIVE,
                risk_bound=HIGH_RISK_SOURCE_NATIVE_RISK_BOUND,
                min_sample_count=HIGH_RISK_SOURCE_NATIVE_MIN_SAMPLES,
                high_risk=True,
                require_independent_witnesses=True,
                allow_shifted=False,
            )
        except (KernelError, TypeError, ValueError) as exc:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} is invalid: {exc}"
            ) from None

        if evidence.evidence_kind != EVIDENCE_SOURCE_NATIVE:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} must be source-native"
            )
        if evidence.model_only or evidence.consensus:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} cannot be model-only consensus"
            )
        if (
            evidence.policy_id != HIGH_RISK_SOURCE_NATIVE_POLICY_ID
            or evidence.policy_revision != HIGH_RISK_SOURCE_NATIVE_POLICY_REVISION
            or evidence.workflow_class != HIGH_RISK_SOURCE_NATIVE_WORKFLOW
            or evidence.evaluation_slice_id != evaluation_slice_id
            or evidence.claim_authority_class != AUTHORITY_SOURCE_NATIVE
        ):
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} policy/workflow/slice/authority "
                "does not match high-risk source-native assessment"
            )
        if evidence.risk_upper_bound is None:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} must declare an upper risk bound"
            )
        try:
            decision = evaluate_verification_risk_policy(
                evidence,
                risk_policy,
                claim_authority_class=AUTHORITY_SOURCE_NATIVE,
                as_of=as_of,
            )
        except (KernelError, TypeError, ValueError) as exc:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} could not be evaluated: {exc}"
            ) from None
        if decision.outcome != OUTCOME_VERIFIED or not decision.authority_granted:
            raise VerificationRiskGateError(
                f"risk evidence {evidence_ref!r} failed policy: "
                f"{decision.reason_code} ({decision.reason})"
            )

__all__ = ["check_batch_verification_risk"]
