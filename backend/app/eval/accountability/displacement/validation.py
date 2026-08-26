"""Validation rules and verifiers for Invariant-62 displacement evaluation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    _HEX64,
    ALLOWED_INTEGRATION_KINDS,
    DISPLACEMENT_DECISION_SCHEMA_VERSION,
    DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION,
    DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION,
    INTEGRATION_STATUS_VERIFIED_ACTIVE,
    PROTOCOL_PROSPECTIVE_PREREGISTRATION,
    PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    PROTOCOL_TIMINGS,
    DisplacementDecision,
    DisplacementMeasurementBundle,
    DisplacementPreregistration,
    IntegrationVerification,
    _parse_iso_dt,
)


def validate_active_integration(
    integration: IntegrationVerification,
    prereg: DisplacementPreregistration,
    repo_root: Path | str | None,
    as_of_date: str,
) -> list[str]:
    """Validate that an integration contract is active, repo-relative, verified, and bound to declared scope.

    Must not self-certify from strings alone.
    """
    errors: list[str] = []

    if integration.status != INTEGRATION_STATUS_VERIFIED_ACTIVE:
        errors.append(
            f"Integration status is {integration.status!r}, not {INTEGRATION_STATUS_VERIFIED_ACTIVE!r}"
        )
        return errors

    # 1. Integration kind allowlist
    if integration.integration_kind not in ALLOWED_INTEGRATION_KINDS:
        errors.append(
            f"integration_kind {integration.integration_kind!r} not in allowed kinds {sorted(ALLOWED_INTEGRATION_KINDS)}"
        )

    # 2. System ID must be declared non-baseline comparator
    declared_specialist_ids = {c.system_id for c in prereg.get_specialists()}
    if integration.system_id not in declared_specialist_ids:
        errors.append(
            f"system_id {integration.system_id!r} is not a declared non-baseline comparator ({sorted(declared_specialist_ids)})"
        )

    # 3. Workflow scope exact match
    if integration.workflow_scope != prereg.workflow:
        errors.append(
            f"workflow_scope {integration.workflow_scope!r} does not match preregistration workflow {prereg.workflow!r}"
        )

    # 4. Corpus fingerprint scope exact match
    if integration.corpus_fingerprint_scope != prereg.corpus.fingerprint:
        errors.append(
            f"corpus_fingerprint_scope {integration.corpus_fingerprint_scope!r} does not match preregistration corpus fingerprint {prereg.corpus.fingerprint!r}"
        )

    # 5. Corroboration contract non-empty
    if not integration.corroboration_contract.strip():
        errors.append("corroboration_contract must be a non-empty string")

    # 6. verified_at date format and as_of_date boundary
    dt_v = _parse_iso_dt(integration.verified_at, "verified_at", errors)
    if dt_v is not None and as_of_date:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_v > dt_as_of:
            errors.append(
                f"verified_at ({integration.verified_at}) is in future relative to as_of_date ({as_of_date})"
            )

    # 7. Evidence artifact path & raw bytes SHA-256 verification
    p_str = integration.evidence_artifact_path.strip().replace("\\", "/")
    if not p_str.startswith("docs/reference/measurements/"):
        errors.append(
            f"evidence_artifact_path {integration.evidence_artifact_path!r} must be repo-relative under docs/reference/measurements/"
        )
    elif ".." in p_str:
        errors.append(
            f"evidence_artifact_path {integration.evidence_artifact_path!r} cannot contain '..'"
        )

    if not _HEX64.match(integration.evidence_artifact_sha256):
        errors.append(
            f"evidence_artifact_sha256 must be a 64-character lowercase hex string, got {integration.evidence_artifact_sha256!r}"
        )

    if repo_root is None:
        errors.append(
            "repo_root is required to verify integration artifact existence and SHA-256"
        )
    else:
        root_p = Path(repo_root)
        art_path = root_p / p_str
        if not art_path.is_file():
            errors.append(f"Integration evidence artifact does not exist at {art_path}")
        else:
            raw_bytes = art_path.read_bytes()
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha != integration.evidence_artifact_sha256:
                errors.append(
                    f"Integration evidence artifact SHA mismatch: expected {integration.evidence_artifact_sha256}, got {actual_sha}"
                )

    return errors


def validate_displacement_preregistration(
    prereg: DisplacementPreregistration | Mapping[str, Any],
    as_of_date: str | None = None,
) -> list[str]:
    """Validate displacement preregistration against Invariant-62 fail-closed rules."""
    errors: list[str] = []

    if isinstance(prereg, DisplacementPreregistration):
        p_dict = prereg.to_dict()
    elif isinstance(prereg, Mapping):
        p_dict = dict(prereg)
        schema = p_dict.get("schema_version")
        if schema != DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {DISPLACEMENT_PREREGISTRATION_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return [
            "displacement preregistration must be DisplacementPreregistration or Mapping"
        ]

    pid = p_dict.get("preregistration_id")
    if not isinstance(pid, str) or not pid.strip():
        errors.append("preregistration_id must be a non-empty string")

    wf = p_dict.get("workflow")
    if not isinstance(wf, str) or not wf.strip():
        errors.append("workflow must be a non-empty string")

    ptiming = p_dict.get("protocol_timing", PROTOCOL_PROSPECTIVE_PREREGISTRATION)
    if ptiming not in PROTOCOL_TIMINGS:
        errors.append(
            f"protocol_timing must be one of {sorted(PROTOCOL_TIMINGS)}, got {ptiming!r}"
        )

    corpus = p_dict.get("corpus")
    if not isinstance(corpus, Mapping) or not corpus:
        errors.append("corpus must be a non-empty mapping")
    else:
        mver = corpus.get("manifest_version")
        if not isinstance(mver, str) or not mver.strip():
            errors.append("corpus.manifest_version must be a non-empty string")
        fp = corpus.get("fingerprint")
        if not isinstance(fp, str) or not fp.strip():
            errors.append("corpus.fingerprint must be a non-empty string")
        dcount = corpus.get("document_count")
        if not isinstance(dcount, int) or dcount <= 0 or isinstance(dcount, bool):
            errors.append("corpus.document_count must be a positive integer")

    comps = p_dict.get("comparators")
    if not isinstance(comps, (list, tuple)) or len(comps) < 2:
        errors.append("comparators must contain at least 2 comparator specifications")
    else:
        marker_count = 0
        seen_ids: set[str] = set()
        for i, c in enumerate(comps):
            if not isinstance(c, Mapping):
                errors.append(f"comparator [{i}] must be a mapping")
                continue
            sid = c.get("system_id")
            if not isinstance(sid, str) or not sid.strip():
                errors.append(f"comparator [{i}].system_id must be non-empty string")
            elif sid in seen_ids:
                errors.append(f"duplicate comparator system_id {sid!r}")
            else:
                seen_ids.add(sid)

            if c.get("is_marker_baseline") is True:
                marker_count += 1

            ip = c.get("input_path_declared")
            if not isinstance(ip, str) or not ip.strip():
                errors.append(
                    f"comparator {sid!r} must declare non-empty input_path_declared"
                )
            ar = c.get("adaptation_rules_declared")
            if not isinstance(ar, str) or not ar.strip():
                errors.append(
                    f"comparator {sid!r} must declare non-empty adaptation_rules_declared"
                )

        if marker_count != 1:
            errors.append(
                f"comparators must declare exactly 1 is_marker_baseline=True; found {marker_count}"
            )

    mat_dims = p_dict.get("material_dimensions")
    if not isinstance(mat_dims, (list, tuple)) or not mat_dims:
        errors.append("material_dimensions must be a non-empty list of strings")

    thresh = p_dict.get("frozen_thresholds")
    if not isinstance(thresh, Mapping) or not thresh:
        errors.append("frozen_thresholds must be a non-empty mapping")

    pdate_str = p_dict.get("preregistration_date")
    dt_p = _parse_iso_dt(pdate_str, "preregistration_date", errors)
    if as_of_date and dt_p is not None:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_p > dt_as_of:
            errors.append(
                f"preregistration_date is in future relative to as_of_date ({pdate_str} > {as_of_date})"
            )

    return errors


def validate_displacement_measurement_bundle(
    bundle: DisplacementMeasurementBundle | Mapping[str, Any],
    prereg: DisplacementPreregistration | None = None,
    as_of_date: str | None = None,
) -> list[str]:
    """Validate measurement bundle structure, tri-state dimensions, and preregistration binding."""
    errors: list[str] = []

    if isinstance(bundle, DisplacementMeasurementBundle):
        b_dict = bundle.to_dict()
    elif isinstance(bundle, Mapping):
        b_dict = dict(bundle)
        schema = b_dict.get("schema_version")
        if schema != DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {DISPLACEMENT_MEASUREMENT_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return ["measurement bundle must be DisplacementMeasurementBundle or Mapping"]

    mid = b_dict.get("measurement_id")
    if not isinstance(mid, str) or not mid.strip():
        errors.append("measurement_id must be a non-empty string")

    pid = b_dict.get("preregistration_id")
    if not isinstance(pid, str) or not pid.strip():
        errors.append("preregistration_id must be a non-empty string")

    if prereg is not None and pid != prereg.preregistration_id:
        errors.append(
            f"measurement preregistration_id {pid!r} does not match preregistration {prereg.preregistration_id!r}"
        )

    fp = b_dict.get("corpus_fingerprint")
    if not isinstance(fp, str) or not fp.strip():
        errors.append("corpus_fingerprint must be a non-empty string")
    elif prereg is not None and fp != prereg.corpus.fingerprint:
        errors.append(
            f"corpus_fingerprint {fp!r} does not match preregistration corpus {prereg.corpus.fingerprint!r}"
        )

    comps = b_dict.get("comparators")
    if not isinstance(comps, Mapping) or not comps:
        errors.append("comparators must be a non-empty mapping")
    elif prereg is not None:
        declared_ids = {c.system_id for c in prereg.comparators}
        measured_ids = set(comps.keys())
        if declared_ids != measured_ids:
            errors.append(
                f"measured comparators {sorted(measured_ids)} do not match declared comparators {sorted(declared_ids)}"
            )

    edate_str = b_dict.get("evidence_date")
    dt_e = _parse_iso_dt(edate_str, "evidence_date", errors)
    if as_of_date and dt_e is not None:
        dt_as_of = _parse_iso_dt(as_of_date, "as_of_date", errors)
        if dt_as_of is not None and dt_e > dt_as_of:
            errors.append(
                f"evidence_date is in future relative to as_of_date ({edate_str} > {as_of_date})"
            )

    # Validate protocol timing consistency
    if prereg is not None and dt_e is not None:
        dt_p = _parse_iso_dt(prereg.preregistration_date, "preregistration_date", [])
        if (
            dt_p is not None
            and dt_e < dt_p
            and prereg.protocol_timing == PROTOCOL_PROSPECTIVE_PREREGISTRATION
        ):
            errors.append(
                f"Prospective timing lie: evidence_date ({edate_str}) predates preregistration_date ({prereg.preregistration_date}); "
                f"preregistration must declare protocol_timing={PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY!r} for retrospective replays."
            )

    return errors


def validate_persisted_decision(
    persisted: DisplacementDecision | Mapping[str, Any],
    prereg: DisplacementPreregistration,
    bundle: DisplacementMeasurementBundle,
    as_of_date: str,
    repo_root: Path | str | None = None,
) -> list[str]:
    """Validate persisted decision against rederivation to ensure manual flips fail closed."""
    errors: list[str] = []

    if isinstance(persisted, DisplacementDecision):
        p_dict = persisted.to_dict()
    elif isinstance(persisted, Mapping):
        p_dict = dict(persisted)
        schema = p_dict.get("schema_version")
        if schema != DISPLACEMENT_DECISION_SCHEMA_VERSION:
            errors.append(
                f"schema_version must be {DISPLACEMENT_DECISION_SCHEMA_VERSION!r}, got {schema!r}"
            )
    else:
        return ["persisted decision must be DisplacementDecision or Mapping"]

    from .engine import derive_displacement_decision

    rederived = derive_displacement_decision(
        prereg, bundle, as_of_date=as_of_date, repo_root=repo_root
    )

    # 1. Outcome equality
    persisted_outcome = p_dict.get("outcome")
    if persisted_outcome != rederived.outcome:
        errors.append(
            f"Outcome mismatch: persisted={persisted_outcome!r} != rederived={rederived.outcome!r}"
        )

    # 2. Rederivation digest equality
    persisted_digest = p_dict.get("rederivation_digest")
    if persisted_digest != rederived.rederivation_digest:
        errors.append(
            f"Rederivation digest mismatch: persisted={persisted_digest!r} != rederived={rederived.rederivation_digest!r}"
        )

    # 3. Supporting artifact sha256 equality
    persisted_art_sha = p_dict.get("supporting_artifact_sha256")
    if persisted_art_sha != bundle.supporting_artifact_sha256:
        errors.append(
            f"Supporting artifact SHA-256 mismatch: persisted={persisted_art_sha!r} != bundle={bundle.supporting_artifact_sha256!r}"
        )

    # 4. Reason ledger equality
    persisted_ledger = p_dict.get("reason_ledger", [])
    rederived_ledger = [r.to_dict() for r in rederived.reason_ledger]
    if persisted_ledger != rederived_ledger:
        errors.append("Reason ledger mismatch between persisted and rederived decision")

    return errors


__all__ = [
    "validate_active_integration",
    "validate_displacement_measurement_bundle",
    "validate_displacement_preregistration",
    "validate_persisted_decision",
]
