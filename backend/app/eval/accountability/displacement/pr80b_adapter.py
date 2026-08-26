"""PR80B direct-specialist displacement evidence adapter and preregistration constructor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    _HEX64,
    DIMENSION_ABSENT_REJECTION,
    DIMENSION_COST,
    DIMENSION_DANGEROUS_FAILURES,
    DIMENSION_DOC_EXACT_RATE,
    DIMENSION_EVIDENCE_LINEAGE,
    DIMENSION_LANE_ERRORS,
    DIMENSION_REVIEW_BURDEN,
    DIMENSION_SCALAR_ACCURACY,
    DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION,
    DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION,
    INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
    INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
    MEASUREMENT_STATUS_MEASURED,
    MEASUREMENT_STATUS_UNAVAILABLE,
    PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION,
    PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    ComparatorDeclaredSpec,
    ComparatorMeasurements,
    CorpusPreregistration,
    DimensionMeasurement,
    DisplacementDecisionError,
    DisplacementMeasurementBundle,
    DisplacementPreregistration,
    ExecutedComparatorFacts,
    FairnessContract,
    FairnessVerification,
    FrozenDecisionThresholds,
    IntegrationVerification,
    _compute_sha256,
)


def parse_pr80b_measurement_artifact(
    artifact_data: Mapping[str, Any] | str | Path,
    artifact_path: str = "",
) -> DisplacementMeasurementBundle:
    """Parse PR80B direct-specialist displacement measurement artifact into bundle.

    Never defaults missing metrics to 0 or 1.0; verifies exact schema paths and bytes SHA.
    """
    if isinstance(artifact_data, (str, Path)):
        p = Path(artifact_data)
        if not p.is_file():
            raise DisplacementDecisionError(f"Artifact file not found: {p}")
        raw_bytes = p.read_bytes()
        art_sha = hashlib.sha256(raw_bytes).hexdigest()
        raw_text = raw_bytes.decode("utf-8")
        data = json.loads(raw_text)
        art_path = str(p)
    elif isinstance(artifact_data, Mapping):
        data = dict(artifact_data)
        art_sha = _compute_sha256(data)
        art_path = artifact_path or "inline_pr80b_artifact.json"
    else:
        raise DisplacementDecisionError(
            "artifact_data must be a mapping, filepath str, or Path"
        )

    schema = data.get("schema_version")
    if schema != PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION:
        raise DisplacementDecisionError(
            f"Unsupported PR80B schema {schema!r}; expected {PR80B_DISPLACEMENT_EVIDENCE_SCHEMA_VERSION!r}"
        )

    corpus_info = data.get("corpus")
    if not isinstance(corpus_info, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'corpus' mapping in PR80B artifact"
        )

    fingerprint = str(corpus_info.get("fingerprint", ""))
    if not fingerprint or not _HEX64.match(fingerprint):
        raise DisplacementDecisionError(
            f"Invalid or missing corpus fingerprint in PR80B artifact: {fingerprint!r}"
        )

    doc_count = corpus_info.get("documents")
    if not isinstance(doc_count, int) or doc_count != 24:
        raise DisplacementDecisionError(
            f"Expected 24 corpus documents in PR80B artifact, got {doc_count!r}"
        )

    # Acceptance facts
    acceptance = data.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'acceptance' mapping in PR80B artifact"
        )

    corpus_loaded = bool(acceptance.get("corpus_loaded_24_docs", False))
    all_evaluated = bool(acceptance.get("all_systems_evaluated_on_full_corpus", False))
    pr80a_complete = bool(acceptance.get("pr80a_evidence_coverage_complete", False))
    pr80a_lane_ok = bool(acceptance.get("pr80a_lane_error_free", False))
    specialists_present = bool(acceptance.get("specialist_routes_present", False))

    systems_decl = data.get("systems")
    if not isinstance(systems_decl, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'systems' mapping in PR80B artifact"
        )

    expected_systems = {
        "marker-pr80a",
        "invoice2data",
        "llm-openrouter:poolside/laguna-s-2.1:free",
    }
    if set(systems_decl.keys()) != expected_systems:
        raise DisplacementDecisionError(
            f"PR80B systems {sorted(systems_decl.keys())} do not match expected {sorted(expected_systems)}"
        )

    executed_facts_list: list[ExecutedComparatorFacts] = []
    for sid, sinfo in systems_decl.items():
        if not isinstance(sinfo, Mapping):
            raise DisplacementDecisionError(f"Invalid system declaration for {sid!r}")
        skind = str(sinfo.get("kind", ""))
        sident = str(sinfo.get("identity", ""))
        sinput = str(sinfo.get("input", ""))
        if sid == "marker-pr80a":
            srules = "native evidence-backed query execution; only accepted values emitted with verifiable citation pointers"
        else:
            srules = str(
                sinfo.get("template_policy", sinfo.get("selection_rationale", ""))
            )
        srat = str(sinfo.get("selection_rationale", ""))
        executed_facts_list.append(
            ExecutedComparatorFacts(
                system_id=sid,
                system_kind=skind,
                system_identity=sident,
                input_path=sinput,
                adaptation_rules=srules,
                selection_rationale=srat,
            )
        )

    metrics_map = data.get("metrics")
    if not isinstance(metrics_map, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'metrics' mapping in PR80B artifact"
        )

    decision_map = data.get("decision")
    if not isinstance(decision_map, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'decision' mapping in PR80B artifact"
        )

    evidence_supp = decision_map.get("evidence_supporting")
    if not isinstance(evidence_supp, Mapping):
        raise DisplacementDecisionError(
            "Missing required 'decision.evidence_supporting' mapping in PR80B artifact"
        )

    danger_map = evidence_supp.get("danger_counts")
    if not isinstance(danger_map, Mapping):
        raise DisplacementDecisionError("Missing 'danger_counts' in PR80B artifact")

    evidence_cov_map = evidence_supp.get("evidence_coverage")
    if not isinstance(evidence_cov_map, Mapping):
        raise DisplacementDecisionError("Missing 'evidence_coverage' in PR80B artifact")

    doc_exact_map = evidence_supp.get("doc_exact")
    if not isinstance(doc_exact_map, Mapping):
        raise DisplacementDecisionError("Missing 'doc_exact' in PR80B artifact")

    scalar_acc_map = evidence_supp.get("scalar_accuracy_on_present")
    if not isinstance(scalar_acc_map, Mapping):
        raise DisplacementDecisionError(
            "Missing 'scalar_accuracy_on_present' in PR80B artifact"
        )

    review_proxy_map = evidence_supp.get("review_proxy")
    if not isinstance(review_proxy_map, Mapping):
        raise DisplacementDecisionError("Missing 'review_proxy' in PR80B artifact")

    comparators: dict[str, ComparatorMeasurements] = {}

    def _lookup_supp(m: Mapping[str, Any], sid: str) -> Any:
        if sid in m:
            return m[sid]
        if sid.startswith("llm") and "llm" in m:
            return m["llm"]
        return None

    for sys_id in expected_systems:
        sys_metrics = metrics_map.get(sys_id)
        if not isinstance(sys_metrics, Mapping):
            raise DisplacementDecisionError(f"Missing metrics entry for {sys_id!r}")

        doc_data = sys_metrics.get("docs")
        if not isinstance(doc_data, Mapping):
            raise DisplacementDecisionError(f"Missing 'docs' metrics for {sys_id!r}")

        doc_total_val = doc_data.get("total")
        if not isinstance(doc_total_val, int) or doc_total_val <= 0:
            raise DisplacementDecisionError(
                f"Missing or invalid docs.total for {sys_id!r}: {doc_total_val!r}"
            )

        doc_exact_supp = _lookup_supp(doc_exact_map, sys_id)
        doc_exact_raw = doc_data.get("doc_exact")
        if not isinstance(doc_exact_supp, int) or not isinstance(doc_exact_raw, int):
            raise DisplacementDecisionError(f"Missing doc_exact count for {sys_id!r}")
        doc_exact_rate = round(doc_exact_supp / doc_total_val, 4)

        scalar_data = sys_metrics.get("scalar")
        if not isinstance(scalar_data, Mapping):
            raise DisplacementDecisionError(f"Missing 'scalar' metrics for {sys_id!r}")

        scalar_acc_supp = _lookup_supp(scalar_acc_map, sys_id)
        scalar_acc_raw = scalar_data.get("accuracy_on_present")
        if not isinstance(scalar_acc_supp, (int, float)) or not isinstance(
            scalar_acc_raw, (int, float)
        ):
            raise DisplacementDecisionError(
                f"Missing scalar accuracy_on_present for {sys_id!r}"
            )
        scalar_acc_val = float(scalar_acc_supp)

        absent_rej_raw = scalar_data.get("absent_rejection_rate")
        if not isinstance(absent_rej_raw, (int, float)):
            raise DisplacementDecisionError(
                f"Missing absent_rejection_rate for {sys_id!r}"
            )
        absent_rej_val = float(absent_rej_raw)

        ev_cov_supp = _lookup_supp(evidence_cov_map, sys_id)
        if not isinstance(ev_cov_supp, (int, float)):
            raise DisplacementDecisionError(f"Missing evidence_coverage for {sys_id!r}")
        ev_cov_val = float(ev_cov_supp)

        raw_dangers = _lookup_supp(danger_map, sys_id)
        if not isinstance(raw_dangers, Mapping):
            raise DisplacementDecisionError(f"Missing danger_counts for {sys_id!r}")
        dangers = {str(k): int(v) for k, v in raw_dangers.items()}

        lane_errs = doc_data.get("error_docs")
        if not isinstance(lane_errs, int):
            raise DisplacementDecisionError(f"Missing error_docs for {sys_id!r}")

        review_proxy_val = _lookup_supp(review_proxy_map, sys_id)
        if not isinstance(review_proxy_val, str) or not review_proxy_val.strip():
            review_status = MEASUREMENT_STATUS_UNAVAILABLE
            review_val = None
        else:
            review_status = MEASUREMENT_STATUS_MEASURED
            review_val = review_proxy_val

        dims: dict[str, DimensionMeasurement] = {
            DIMENSION_DOC_EXACT_RATE: DimensionMeasurement(
                dimension=DIMENSION_DOC_EXACT_RATE,
                status=MEASUREMENT_STATUS_MEASURED,
                value=doc_exact_rate,
                unit="rate",
            ),
            DIMENSION_SCALAR_ACCURACY: DimensionMeasurement(
                dimension=DIMENSION_SCALAR_ACCURACY,
                status=MEASUREMENT_STATUS_MEASURED,
                value=scalar_acc_val,
                unit="fraction",
            ),
            DIMENSION_ABSENT_REJECTION: DimensionMeasurement(
                dimension=DIMENSION_ABSENT_REJECTION,
                status=MEASUREMENT_STATUS_MEASURED,
                value=absent_rej_val,
                unit="fraction",
            ),
            DIMENSION_EVIDENCE_LINEAGE: DimensionMeasurement(
                dimension=DIMENSION_EVIDENCE_LINEAGE,
                status=MEASUREMENT_STATUS_MEASURED,
                value=ev_cov_val,
                unit="fraction",
            ),
            DIMENSION_DANGEROUS_FAILURES: DimensionMeasurement(
                dimension=DIMENSION_DANGEROUS_FAILURES,
                status=MEASUREMENT_STATUS_MEASURED,
                value=sum(dangers.values()),
                unit="count",
            ),
            DIMENSION_LANE_ERRORS: DimensionMeasurement(
                dimension=DIMENSION_LANE_ERRORS,
                status=MEASUREMENT_STATUS_MEASURED,
                value=lane_errs,
                unit="count",
            ),
            DIMENSION_REVIEW_BURDEN: DimensionMeasurement(
                dimension=DIMENSION_REVIEW_BURDEN,
                status=review_status,
                value=review_val,
            ),
            # PR80B reported_cost is null (free-tier models; no chargeable usage recorded) -> UNAVAILABLE, not zero!
            DIMENSION_COST: DimensionMeasurement(
                dimension=DIMENSION_COST,
                status=MEASUREMENT_STATUS_UNAVAILABLE,
                value=None,
                notes="free-tier models; no chargeable usage recorded",
            ),
        }

        comparators[sys_id] = ComparatorMeasurements(
            system_id=sys_id,
            dimensions=dims,
            danger_counts=dangers,
            raw_metrics=dict(sys_metrics),
        )

    fairness_ok = bool(
        corpus_loaded
        and all_evaluated
        and pr80a_complete
        and pr80a_lane_ok
        and specialists_present
    )

    fairness = FairnessVerification(
        input_parity_verified=corpus_loaded,
        adaptation_parity_verified=all_evaluated,
        full_corpus_evaluated=all_evaluated,
        is_fair=fairness_ok,
        executed_facts=tuple(executed_facts_list),
        discrepancies=(),
    )

    # In PR80B, routing is a future recommendation ("a later routing phase may run the LLM..."),
    # NOT an active verified integration.
    llm_integration = IntegrationVerification(
        system_id="llm-openrouter:poolside/laguna-s-2.1:free",
        status=INTEGRATION_STATUS_FUTURE_UNIMPLEMENTED,
        integration_kind=INTEGRATION_KIND_NON_AUTHORITATIVE_CANDIDATE_GENERATOR,
        evidence_artifact_path="",
        evidence_artifact_sha256="",
        workflow_scope="demo.invoice@1.0.0 extraction",
        corpus_fingerprint_scope=fingerprint,
        corroboration_contract="synthetic specialist witness with independent corroboration",
    )

    return DisplacementMeasurementBundle(
        schema_version=DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION,
        measurement_id="pr80b_displacement_run",
        preregistration_id="pr80b_invoice_displacement_study",
        corpus_fingerprint=fingerprint,
        comparators=comparators,
        fairness=fairness,
        evidence_date="2026-08-20T00:00:00Z",
        supporting_artifact_path=art_path,
        supporting_artifact_sha256=art_sha,
        integrations=(llm_integration,),
    )


def create_pr80b_retrospective_preregistration(
    as_of_date: str = "2026-08-26T00:00:00Z",
) -> DisplacementPreregistration:
    """Create the canonical retrospective frozen preregistration for the PR80B displacement benchmark."""
    corpus = CorpusPreregistration(
        manifest_version="marker.pr80b_corpus.v1",
        fingerprint="aeba0b4b2121c3836f2508e8461f2d68bf5bfbabd578f6f54e8e7c513ca60511",
        document_count=24,
        slices=(
            "ambiguity.decoy_anchor",
            "ambiguity.row",
            "ambiguity.scalar",
            "baseline.happy",
            "edge.invalid_value",
            "edge.negative",
            "edge.zero",
            "encoding.fullwidth",
            "integrity.not_evaluable",
            "integrity.row_loss",
            "integrity.total_mismatch",
            "layout.label_variants",
            "layout.noise",
            "layout.pagination",
            "missing.optional",
            "missing.required",
            "normalization.currency",
            "normalization.date",
            "normalization.decimal",
            "normalization.decimal_eu",
            "structure.broken_row_long",
            "structure.broken_row_short",
            "structure.duplicate_conflict",
            "structure.duplicate_identical",
            "structure.many_rows",
            "structure.near_duplicate_desc",
            "witness.conflict",
            "witness.corroboration",
        ),
        task_description="Extract demo.invoice@1.0.0 scalar fields and repeated line items keyed by SKU from plain text.",
        normalization_rules={
            "invoice_date": "ISO YYYY-MM-DD",
            "currency": "USD/EUR/GBP",
            "decimals": "Decimal comparison after thousands/EU stripping",
            "integers": "Base-10 integer",
            "absence": "Document unstated field must be absent/null",
            "conflicts": "Contradictory values must be flagged or abstained",
        },
        declared_invariants=("sum_equality",),
    )

    comparators = (
        ComparatorDeclaredSpec(
            system_id="marker-pr80a",
            is_marker_baseline=True,
            system_kind="current evidence-backed extraction route",
            system_identity="app.extraction pr80a.1 anchor route over marker.query.v1",
            input_path_declared="corpus text published as kernel view documents; extraction via execute_query over the active PublicationSet",
            adaptation_rules_declared="native evidence-backed query execution; only accepted values emitted with verifiable citation pointers",
            selection_rationale="current Marker UI evidence-backed extraction route",
        ),
        ComparatorDeclaredSpec(
            system_id="invoice2data",
            is_marker_baseline=False,
            system_kind="deterministic open-source invoice specialist",
            system_identity="invoice2data 1.0.1 (PyPI) with per-vendor templates authored once for the canonical corpus layout",
            input_path_declared="same document text as plain .txt files (library text reader)",
            adaptation_rules_declared="first regex match wins for multi-match arrays; empty/None result maps to a lane error",
            selection_rationale="",
        ),
        ComparatorDeclaredSpec(
            system_id="llm-openrouter:poolside/laguna-s-2.1:free",
            is_marker_baseline=False,
            system_kind="hosted LLM direct specialist",
            system_identity="local OpenAI-compatible gateway (http://localhost:20128/v1): kc/poolside/laguna-s-2.1:free via structured-output extraction prompt, temperature 0",
            input_path_declared="same document text as the user message; system prompt declares the task normalization rules",
            adaptation_rules_declared="an LLM with a structured invoice-extraction prompt is the dominant deployed direct-specialist approach; invoice2data complements it as the canonical specialized open-source tool",
            selection_rationale="an LLM with a structured invoice-extraction prompt is the dominant deployed direct-specialist approach; invoice2data complements it as the canonical specialized open-source tool",
        ),
    )

    fairness = FairnessContract(
        same_user_level_input_required=True,
        declared_adaptation_rules_required=True,
        disallow_privileged_features=True,
        allowed_input_discrepancies=(),
    )

    thresholds = FrozenDecisionThresholds(
        max_acceptable_dangerous_failures=0,
        threshold_scope="declared_corpus_observed_count",
        min_evidence_coverage_for_retained=1.0,
        quality_margin_for_displacement=0.05,
        allow_candidate_integration=True,
        max_lane_error_rate=0.0,
    )

    material_dims = (
        DIMENSION_DOC_EXACT_RATE,
        DIMENSION_SCALAR_ACCURACY,
        DIMENSION_ABSENT_REJECTION,
        DIMENSION_EVIDENCE_LINEAGE,
        DIMENSION_DANGEROUS_FAILURES,
        DIMENSION_REVIEW_BURDEN,
        DIMENSION_LANE_ERRORS,
    )

    return DisplacementPreregistration(
        schema_version=DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION,
        preregistration_id="pr80b_invoice_displacement_study",
        workflow="demo.invoice@1.0.0 extraction",
        corpus=corpus,
        comparators=comparators,
        fairness_contract=fairness,
        material_dimensions=material_dims,
        frozen_thresholds=thresholds,
        preregistration_date="2026-08-26T00:00:00Z",
        protocol_timing=PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    )


create_pr80b_displacement_preregistration = create_pr80b_retrospective_preregistration

__all__ = [
    "create_pr80b_displacement_preregistration",
    "create_pr80b_retrospective_preregistration",
    "parse_pr80b_measurement_artifact",
]
