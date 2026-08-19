"""PR74 claim/proof conformance (fixture corpus v1).

Every vector recomputes through the REAL kernel constructors and
validators — committed hashes must not drift, topology verdicts must
stay deterministic, and the corpus must stay self-consistent (unique
ids, declared profile, category coverage). Stdlib + pytest only, like
the canonical conformance suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kernel.proofs import (
    ProofSupportRecord,
    detect_proof_cycle,
    proof_closure_path_to_authority_consumer,
)
from app.kernel.records import (
    ClaimAssertionRecord,
    ClaimAssessmentRecord,
)
from app.utils.canonical import (
    CANONICALIZATION_PROFILE,
    record_identity_hash,
    to_json_ready,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "claim_proof_vectors_v1.json"
)

CONSTRUCTORS = {
    "claim_assertion": ClaimAssertionRecord,
    "claim_assessment": ClaimAssessmentRecord,
    "proof_support": ProofSupportRecord,
}


def load_corpus() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def vector_identity_hash(record_class: str, payload: dict) -> str:
    record = CONSTRUCTORS[record_class].from_payload(
        payload, record_id="conformance-record"
    )
    return record_identity_hash(
        record_type=record.record_type,
        schema_version=record.schema_version,
        payload=to_json_ready(record.identity_payload()),
    )


def test_corpus_header_declares_contract():
    corpus = load_corpus()
    assert corpus["$schema"] == "marker.claim_proof.fixtures.v1"
    assert corpus["canonicalization_profile"] == CANONICALIZATION_PROFILE
    for section in ("identity_cases", "topology_cases", "grounding_cases"):
        assert corpus[section], f"{section} must not be empty"


def test_corpus_has_no_duplicate_case_ids():
    corpus = load_corpus()
    ids = [
        case["id"]
        for section in ("identity_cases", "topology_cases", "grounding_cases")
        for case in corpus[section]
    ]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "case_id",
    [case["id"] for case in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["identity_cases"]],
)
def test_identity_case_is_drift_free(case_id: str):
    case = next(
        c for c in load_corpus()["identity_cases"] if c["id"] == case_id
    )
    base_hash = vector_identity_hash(case["record_class"], case["payload"])
    assert base_hash == case["expect"]["identity_hash"]
    # Determinism: recompute must be byte-identical.
    assert vector_identity_hash(case["record_class"], case["payload"]) == base_hash
    for variant in case["variants"]:
        variant_hash = vector_identity_hash(
            case["record_class"], variant["payload"]
        )
        if case["variant_expectation"] == "same":
            assert variant_hash == base_hash, variant["id"]
        else:
            assert variant_hash != base_hash, variant["id"]


@pytest.mark.parametrize(
    "case_id",
    [case["id"] for case in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["topology_cases"]],
)
def test_topology_case_deterministic(case_id: str):
    case = next(c for c in load_corpus()["topology_cases"] if c["id"] == case_id)
    supports = [tuple(pair) for pair in case["supports"]]
    derived = [tuple(pair) for pair in case["derived_edges"]]
    cycle = detect_proof_cycle(supports, derived)
    if case["expect"] == "acyclic":
        assert cycle is None, case["id"]
    else:
        assert cycle is not None and len(cycle) >= 2, case["id"]
        # The reported path closes on itself.
        assert cycle[0] == cycle[-1], case["id"]
    # Determinism: same inputs, same verdict.
    assert detect_proof_cycle(supports, derived) == cycle


@pytest.mark.parametrize(
    "case_id",
    [case["id"] for case in json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["grounding_cases"]],
)
def test_grounding_case_deterministic(case_id: str):
    case = next(c for c in load_corpus()["grounding_cases"] if c["id"] == case_id)
    supports = [tuple(pair) for pair in case["supports"]]
    derived = [tuple(pair) for pair in case["derived_edges"]]
    path = proof_closure_path_to_authority_consumer(
        case["start"], supports, derived, case["classes"]
    )
    if case["expect"] == "grounded":
        assert path is None, case["id"]
    else:
        assert path is not None, case["id"]
        assert path[0] == case["start"]
        reached = path[-1]
        assert case["classes"][reached] in {
            "claim_assertion",
            "claim_assessment",
            "decision",
        }
    assert proof_closure_path_to_authority_consumer(
        case["start"], supports, derived, case["classes"]
    ) == path


def test_corpus_covers_required_behaviors():
    corpus = load_corpus()
    topology = {case["id"] for case in corpus["topology_cases"]}
    grounding = {case["id"] for case in corpus["grounding_cases"]}
    assert any(c["expect"] == "acyclic" for c in corpus["topology_cases"])
    assert any(
        c["id"].startswith("two-node") for c in corpus["topology_cases"]
    ) or "two-node-self-support" in topology
    assert "summary-of-assessed-claim" in grounding
    assert "grounded-derivation-chain" in grounding
