"""PR84C evidence generator: invariants 59, 60, 62 (23C.7 readiness closure).

Executes the three accountability audits in-process against the live tree and
digests their results into three committed measurement artifacts:

* invariant 59 — capability/subsystem accountability: authoritative inventory
  vs capability-record bijection, fail-closed completeness validation (owner,
  rollback, expiry, kill condition, evidence digests, AST verification nodes),
  and promoted rollback coverage;
* invariant 60 — leadership claim discipline: registry audit including
  SHA-256 evidence-binding verification, catastrophic-budget/review-burden
  honesty, and the release-docs leadership-verb scan;
* invariant 62 — final rational-user displacement test: PR80B frozen replay
  through the preregistered decision engine with fairness gate, explicit
  reason-to-leave ledger, and re-derivation validation.

Artifacts record counts and named scenario outcomes so the readiness auditor
binds exact expectations instead of trusting prose. The script fails loudly
(non-zero exit) if any scenario does not pass.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
MEASUREMENTS_DIR = REPO_ROOT / "docs" / "reference" / "measurements"
sys.path.insert(0, str(BACKEND_DIR))

from app.eval.accountability.claims import audit_leadership_claims  # noqa: E402
from app.eval.accountability.displacement import (  # noqa: E402
    OUTCOME_MARKER_RETAINED,
    PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY,
    REASON_STATUS_CONCEDED,
    REASON_STATUS_INTEGRATED,
    REASON_STATUS_MEASURED,
    create_pr80b_retrospective_preregistration,
    derive_displacement_decision,
    parse_pr80b_measurement_artifact,
    validate_persisted_decision,
)
from app.eval.accountability.inventory import (  # noqa: E402
    get_authoritative_inventory_subjects_tuple,
)
from app.eval.accountability.population import (  # noqa: E402
    get_authoritative_capability_records_tuple,
    get_promoted_rollback_verification_nodes,
    validate_accountability_completeness,
)

AS_OF_DATE = "2026-08-26T00:00:00Z"
PR80B_ARTIFACT = (
    MEASUREMENTS_DIR / "pr80b-direct-specialist-displacement.json"
)

CAPABILITY_SCHEMA = "marker.pr84c.capability_accountability_evidence.v1"
CLAIMS_SCHEMA = "marker.pr84c.leadership_claims_evidence.v1"
DISPLACEMENT_SCHEMA = "marker.pr84c.displacement_decision_evidence.v1"

_VERDICT_59 = "capability_accountability_matrix_proven"
_VERDICT_60 = "leadership_claim_registry_audit_proven"
_VERDICT_62 = "final_rational_user_displacement_test_executed"

_REASON_TERMINAL = frozenset(
    {REASON_STATUS_INTEGRATED, REASON_STATUS_MEASURED, REASON_STATUS_CONCEDED}
)


def _scenario(condition: bool) -> str:
    return "passed" if condition else "failed"


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_capability_accountability_evidence() -> dict:
    """Invariant 59: inventory bijection + fail-closed completeness audit."""
    records = get_authoritative_capability_records_tuple()
    subjects = get_authoritative_inventory_subjects_tuple()
    errors = validate_accountability_completeness(
        repo_root=REPO_ROOT, as_of_date=AS_OF_DATE
    )
    dispositions = dict(Counter(r.disposition for r in records))
    promoted = dispositions.get("promoted", 0)
    rollback_nodes = get_promoted_rollback_verification_nodes()

    scenarios = {
        "inventory_record_bijection": _scenario(
            {s.id for s in subjects} == {r.id for r in records}
        ),
        "fail_closed_completeness": _scenario(errors == []),
        "promoted_rollback_nodes_ast_verified": _scenario(
            len(rollback_nodes) == promoted and promoted > 0
        ),
        "non_promotion_legitimate": _scenario(
            dispositions.get("non_promoted", 0) + dispositions.get("disabled", 0)
            + dispositions.get("experimental_shadow", 0) > 0
        ),
    }
    return {
        "schema": CAPABILITY_SCHEMA,
        "generated_at": _generated_at(),
        "counts": {
            "inventory_subjects": len(subjects),
            "capability_records": len(records),
            **{f"disposition_{k}": v for k, v in sorted(dispositions.items())},
            "promoted_rollback_nodes": len(rollback_nodes),
        },
        "scenarios": scenarios,
        "errors": list(errors),
        "as_of_date": AS_OF_DATE,
        "verdict": _VERDICT_59 if all(v == "passed" for v in scenarios.values()) and errors == [] else "failed",
    }


def build_leadership_claims_evidence() -> dict:
    """Invariant 60: registry + deep binding + release-prose audit."""
    report = audit_leadership_claims(repo_root=REPO_ROOT, as_of_date=AS_OF_DATE)
    scan_clean = not any(
        "unregistered leadership verb" in e for e in report.errors
    )

    scenarios = {
        "registry_non_vacuous": _scenario(report.claims_count >= 3),
        "all_claims_fully_scoped": _scenario(
            report.errors == () and report.claims_count > 0
        ),
        "fail_closed_withholding": _scenario(
            report.claims_by_disposition.get("withheld", 0) == report.claims_count
        ),
        "evidence_bindings_sha256_verified": _scenario(
            report.evidence_bindings_verified >= 2 and report.errors == ()
        ),
        "release_docs_verb_scan_clean": _scenario(scan_clean),
    }
    return {
        "schema": CLAIMS_SCHEMA,
        "generated_at": _generated_at(),
        "counts": {
            "claims_registered": report.claims_count,
            "claims_withheld": report.claims_by_disposition.get("withheld", 0),
            "evidence_bindings_verified": report.evidence_bindings_verified,
            "release_docs_scanned": len(report.source_files_scanned),
            "leadership_verbs_found": report.verb_occurrences_found,
            "verbs_allowlisted": report.allowlisted_occurrences_count,
        },
        "scenarios": scenarios,
        "errors": list(report.errors),
        "as_of_date": AS_OF_DATE,
        "verdict": _VERDICT_60 if report.passed and all(v == "passed" for v in scenarios.values()) else "failed",
    }


def build_displacement_decision_evidence() -> dict:
    """Invariant 62: final rational-user displacement test on the PR80B workflow."""
    prereg = create_pr80b_retrospective_preregistration(as_of_date=AS_OF_DATE)
    bundle = parse_pr80b_measurement_artifact(PR80B_ARTIFACT)
    decision = derive_displacement_decision(
        prereg, bundle, as_of_date=AS_OF_DATE, repo_root=REPO_ROOT
    )
    rederiv_errors = validate_persisted_decision(
        decision, prereg, bundle, as_of_date=AS_OF_DATE, repo_root=REPO_ROOT
    )

    reasons_by_status = dict(Counter(r.status for r in decision.reason_ledger))
    scenarios = {
        "preregistration_frozen_before_interpretation": _scenario(
            decision.protocol_timing == PROTOCOL_RETROSPECTIVE_FROZEN_REPLAY
        ),
        "fairness_gate_passed": _scenario(decision.fairness_passed),
        "no_blockers": _scenario(decision.blockers == ()),
        "reason_ledger_terminal": _scenario(
            len(decision.reason_ledger) > 0
            and all(r.status in _REASON_TERMINAL for r in decision.reason_ledger)
        ),
        "rederivation_matches": _scenario(rederiv_errors == []),
        "supporting_artifact_bound": _scenario(
            len(decision.supporting_artifact_sha256) == 64
        ),
    }
    return {
        "schema": DISPLACEMENT_SCHEMA,
        "generated_at": _generated_at(),
        "decision": {
            "decision_id": decision.decision_id,
            "preregistration_id": decision.preregistration_id,
            "workflow": decision.workflow,
            "outcome": decision.outcome,
            "protocol_timing": decision.protocol_timing,
            "fairness_passed": decision.fairness_passed,
            "supporting_artifact_sha256": decision.supporting_artifact_sha256,
        },
        "counts": {
            "comparators_evaluated": len(decision.comparator_evaluations),
            "reasons_integrated": reasons_by_status.get(REASON_STATUS_INTEGRATED, 0),
            "reasons_measured": reasons_by_status.get(REASON_STATUS_MEASURED, 0),
            "reasons_conceded": reasons_by_status.get(REASON_STATUS_CONCEDED, 0),
        },
        "scenarios": scenarios,
        "errors": list(decision.blockers) + list(rederiv_errors),
        "as_of_date": AS_OF_DATE,
        "verdict": _VERDICT_62 if all(v == "passed" for v in scenarios.values()) else "failed",
    }


def _write_artifact(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    artifacts = {
        "pr84c-capability-accountability-evidence.json": build_capability_accountability_evidence(),
        "pr84c-leadership-claims-evidence.json": build_leadership_claims_evidence(),
        "pr84c-displacement-decision-evidence.json": build_displacement_decision_evidence(),
    }

    failed = False
    for name, payload in artifacts.items():
        _write_artifact(MEASUREMENTS_DIR / name, payload)
        verdict = payload["verdict"]
        print(f"{name}: verdict={verdict}")
        if verdict == "failed":
            failed = True
            for err in payload.get("errors", []):
                print(f"  error: {err}")

    if failed:
        print("PR84C evidence generation FAILED", file=sys.stderr)
        return 1
    print(
        f"PR84C evidence written to {MEASUREMENTS_DIR} "
        f"(invariants 59/60/62; decision outcome={OUTCOME_MARKER_RETAINED})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
