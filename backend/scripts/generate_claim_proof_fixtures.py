"""Generate the PR74 claim/proof conformance vector corpus.

Deterministic: recomputes every identity hash through the real kernel
constructors and writes
``backend/conformance/fixtures/claim_proof_vectors_v1.json``.

Run from the repository root:

    python backend/scripts/generate_claim_proof_fixtures.py --write
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.kernel.proofs import ProofSupportRecord  # noqa: E402
from app.kernel.records import (  # noqa: E402
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
)
from app.utils.canonical import (  # noqa: E402
    CANONICALIZATION_PROFILE,
    record_identity_hash,
    to_json_ready,
)

FIXTURE_PATH = BACKEND / "conformance" / "fixtures" / "claim_proof_vectors_v1.json"

CONSTRUCTORS = {
    "claim_assertion": ClaimAssertionRecord,
    "claim_assessment": ClaimAssessmentRecord,
    "proof_support": ProofSupportRecord,
}


def identity_hash(record_class: str, payload: dict) -> str:
    record = CONSTRUCTORS[record_class].from_payload(
        payload, record_id="fixture-record"
    )
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


ASSERTION_BASE = {
    "claim_key": "invoice-42.total",
    "subject": "doc:invoice-42",
    "predicate": "total_amount",
    "value": "1250.00",
    "qualifiers": {"currency": "USD", "basis": "net"},
}

ASSESSMENT_BASE = {
    "assertion_ref": "assertion-1",
    "outcome": "verified",
    "policy": {"policy_id": "policy.default", "revision": "rev-3"},
    "evidence_refs": ["obs-1", "obs-2"],
    "snapshot_commit_id": 2,
    "workflow_class": "standard.v1",
    "declared_context": {"as_of": "fixture", "locale": "en"},
}

SUPPORT_BASE = {
    "holder_ref": "assessment-1",
    "evidence_ref": "obs-1",
    "role": "witness",
    "authority_rule": "policy.default/rev-3:witness-v1",
}


def identity_case(
    case_id: str, record_class: str, base: dict, variants: list[dict],
    expectation: str,
) -> dict:
    return {
        "id": case_id,
        "record_class": record_class,
        "payload": base,
        "variants": variants,
        "variant_expectation": expectation,
        "expect": {"identity_hash": identity_hash(record_class, base)},
    }


def build_corpus() -> dict:
    return {
        "$schema": "marker.claim_proof.fixtures.v1",
        "description": (
            "PR74 claim assertion/assessment/proof-support identity "
            "vectors and proof-topology determinism vectors. Identity "
            "hashes are committed constants computed through the real "
            "kernel constructors (regenerate with "
            "backend/scripts/generate_claim_proof_fixtures.py --write)."
        ),
        "canonicalization_profile": CANONICALIZATION_PROFILE,
        "identity_cases": [
            identity_case(
                "assertion-qualifier-key-order",
                "claim_assertion",
                ASSERTION_BASE,
                [
                    {
                        "id": "qualifiers-reordered",
                        "payload": {
                            **ASSERTION_BASE,
                            "qualifiers": {"basis": "net", "currency": "USD"},
                        },
                    }
                ],
                "same",
            ),
            identity_case(
                "assertion-claim-key-scopes-identity",
                "claim_assertion",
                ASSERTION_BASE,
                [
                    {
                        "id": "renamed-claim-key",
                        "payload": {
                            **ASSERTION_BASE, "claim_key": "invoice-42.grand-total"
                        },
                    }
                ],
                "different",
            ),
            identity_case(
                "assertion-value-change",
                "claim_assertion",
                ASSERTION_BASE,
                [
                    {
                        "id": "value-changed",
                        "payload": {**ASSERTION_BASE, "value": "1251.00"},
                    }
                ],
                "different",
            ),
            identity_case(
                "assertion-raw-unicode-not-folded",
                "claim_assertion",
                {**ASSERTION_BASE, "value": "cafe\u0301"},
                [
                    {
                        "id": "precomposed-twin",
                        "payload": {**ASSERTION_BASE, "value": "caf\u00e9"},
                    }
                ],
                "different",
            ),
            identity_case(
                "assessment-evidence-set-order",
                "claim_assessment",
                ASSESSMENT_BASE,
                [
                    {
                        "id": "evidence-reordered",
                        "payload": {
                            **ASSESSMENT_BASE,
                            "evidence_refs": ["obs-2", "obs-1"],
                        },
                    }
                ],
                "same",
            ),
            identity_case(
                "assessment-policy-revision",
                "claim_assessment",
                ASSESSMENT_BASE,
                [
                    {
                        "id": "revision-bumped",
                        "payload": {
                            **ASSESSMENT_BASE,
                            "policy": {"policy_id": "policy.default", "revision": "rev-4"},
                        },
                    }
                ],
                "different",
            ),
            identity_case(
                "assessment-snapshot-cut",
                "claim_assessment",
                ASSESSMENT_BASE,
                [
                    {
                        "id": "later-cut",
                        "payload": {**ASSESSMENT_BASE, "snapshot_commit_id": 3},
                    }
                ],
                "different",
            ),
            identity_case(
                "assessment-workflow-class",
                "claim_assessment",
                ASSESSMENT_BASE,
                [
                    {
                        "id": "fast-class",
                        "payload": {
                            **ASSESSMENT_BASE, "workflow_class": "fast.v1"
                        },
                    }
                ],
                "different",
            ),
            identity_case(
                "assessment-declared-context-order",
                "claim_assessment",
                ASSESSMENT_BASE,
                [
                    {
                        "id": "context-reordered",
                        "payload": {
                            **ASSESSMENT_BASE,
                            "declared_context": {"locale": "en", "as_of": "fixture"},
                        },
                    }
                ],
                "same",
            ),
            identity_case(
                "proof-support-role-and-rule",
                "proof_support",
                SUPPORT_BASE,
                [
                    {
                        "id": "role-changed",
                        "payload": {
                            **SUPPORT_BASE, "role": "derived"
                        },
                    },
                    {
                        "id": "rule-changed",
                        "payload": {
                            **SUPPORT_BASE,
                            "authority_rule": "policy.default/rev-3:derived-v1",
                        },
                    },
                ],
                "different",
            ),
        ],
        "topology_cases": [
            {
                "id": "valid-witness-proof",
                "expect": "acyclic",
                "supports": [["assessment-1", "obs-1"]],
                "derived_edges": [],
            },
            {
                "id": "derived-chain-proof",
                "expect": "acyclic",
                "supports": [["assessment-1", "obs-crop"]],
                "derived_edges": [
                    ["obs-crop", "obs-page"],
                    ["obs-page", "obs-source"],
                ],
            },
            {
                "id": "wide-witness-fan",
                "expect": "acyclic",
                "supports": [
                    ["assessment-1", "obs-1"],
                    ["assessment-1", "obs-2"],
                    ["assessment-1", "obs-3"],
                ],
                "derived_edges": [],
            },
            {
                "id": "two-node-self-support",
                "expect": "cycle",
                "supports": [["assessment-1", "obs-1"]],
                "derived_edges": [["obs-1", "assessment-1"]],
            },
            {
                "id": "three-node-reconciliation-loop",
                "expect": "cycle",
                "supports": [["assessment-1", "obs-1"]],
                "derived_edges": [
                    ["obs-1", "obs-summary"],
                    ["obs-summary", "assessment-1"],
                ],
            },
            {
                "id": "loop-through-second-consumer",
                "expect": "cycle",
                "supports": [
                    ["assessment-1", "obs-1"],
                    ["assessment-2", "obs-2"],
                ],
                "derived_edges": [
                    ["obs-1", "obs-2"],
                    ["obs-2", "assessment-1"],
                ],
            },
        ],
        "grounding_cases": [
            {
                "id": "grounded-witness",
                "expect": "grounded",
                "start": "assessment-1",
                "supports": [["assessment-1", "obs-1"]],
                "derived_edges": [],
                "classes": {
                    "assessment-1": "claim_assessment",
                    "obs-1": "observation",
                },
            },
            {
                "id": "grounded-derivation-chain",
                "expect": "grounded",
                "start": "assessment-1",
                "supports": [["assessment-1", "obs-crop"]],
                "derived_edges": [["obs-crop", "obs-page"]],
                "classes": {
                    "assessment-1": "claim_assessment",
                    "obs-crop": "observation",
                    "obs-page": "observation",
                },
            },
            {
                "id": "summary-of-assessed-claim",
                "expect": "reaches_authority_consumer",
                "start": "assessment-1",
                "supports": [["assessment-1", "obs-summary"]],
                "derived_edges": [["obs-summary", "assertion-1"]],
                "classes": {
                    "assessment-1": "claim_assessment",
                    "obs-summary": "observation",
                    "assertion-1": "claim_assertion",
                },
            },
            {
                "id": "laundering-through-peer-assessment",
                "expect": "reaches_authority_consumer",
                "start": "assessment-1",
                "supports": [["assessment-1", "obs-1"]],
                "derived_edges": [["obs-1", "assessment-2"]],
                "classes": {
                    "assessment-1": "claim_assessment",
                    "obs-1": "observation",
                    "assessment-2": "claim_assessment",
                },
            },
        ],
    }


def main() -> None:
    corpus = build_corpus()
    text = json.dumps(corpus, indent=2, ensure_ascii=False) + "\n"
    if "--write" in sys.argv:
        FIXTURE_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {FIXTURE_PATH}")
    else:
        current = (
            FIXTURE_PATH.read_text(encoding="utf-8") if FIXTURE_PATH.exists() else ""
        )
        status = "in-sync" if current == text else "DRIFT"
        print(f"fixture corpus: {status}")
        raise SystemExit(0 if status == "in-sync" else 1)


if __name__ == "__main__":
    main()
