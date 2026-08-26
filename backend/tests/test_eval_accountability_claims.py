"""Focused and adversarial tests for Invariant-60 leadership claim registry and audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.eval.accountability.claims import (
    CLAIM_AUDIT_REPORT_SCHEMA_VERSION,
    audit_leadership_claims,
    get_authoritative_claim_by_id,
    get_authoritative_leadership_claims,
    resolve_metric_pointer,
    scan_release_claim_sources,
    validate_claims_inventory,
    verify_catastrophic_budget,
    verify_evidence_binding,
    verify_review_burden,
)
from app.eval.accountability.leadership_claim import (
    CANONICAL_UNIVERSAL_DISCLAIMER,
    CLAIM_BEATS,
    CLAIM_ROUTES_TO,
    CLAIM_TIES_REDUCING_BURDEN,
    CLAIM_WITHHELD,
    EVIDENCE_CURRENT,
    EVIDENCE_STALE,
    EVIDENCE_SUPERSEDED,
    CatastrophicBudget,
    ClaimEvidenceBinding,
    LeadershipClaim,
    ReviewBurden,
    calculate_one_sided_95_upper_bound,
    validate_leadership_claim,
)


def _repo_root() -> Path:
    cwd = Path.cwd()
    if cwd.name == "backend":
        return cwd.parent
    return cwd


# -----------------------------------------------------------------------------
# 1. Authoritative Inventory & Default Audit
# -----------------------------------------------------------------------------


def test_authoritative_claims_inventory_non_empty_and_valid():
    claims = get_authoritative_leadership_claims()
    assert len(claims) >= 3

    # All seeded claims must be withheld under fail-closed discipline
    for claim in claims:
        assert claim.disposition == CLAIM_WITHHELD
        assert claim.universal_disclaimer == CANONICAL_UNIVERSAL_DISCLAIMER

    # The universal-superiority withholding carries no evidence binding on purpose:
    # no finite artifact can bound an unbounded population, so a binding would
    # launder provenance into support.
    universal = get_authoritative_claim_by_id("claim.universal_document_superiority")
    assert universal.evidence_bindings == ()

    # Claim comparators must be the exact system identities present in the
    # bound measurement artifacts, never renamed aliases.
    pr81a = get_authoritative_claim_by_id("claim.pr81a_visual_retrieval_gain")
    for comp in pr81a.competitors:
        assert comp in pr81a.evidence_bindings[0].comparator_scope

    errors = validate_claims_inventory(
        claims,
        as_of_date="2026-08-26T00:00:00Z",
        check_bindings_on_disk=True,
        repo_root=_repo_root(),
    )
    assert errors == []


def test_claim_lookup_by_id():
    pr80b_claim = get_authoritative_claim_by_id(
        "claim.pr80b_invoice_extraction_authority"
    )
    assert pr80b_claim.workflow == "extraction.invoice_authority"
    assert pr80b_claim.disposition == CLAIM_WITHHELD

    with pytest.raises(KeyError):
        get_authoritative_claim_by_id("claim.nonexistent_claim_id")


def test_audit_leadership_claims_clean_run():
    report = audit_leadership_claims(
        repo_root=_repo_root(),
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert report.schema_version == CLAIM_AUDIT_REPORT_SCHEMA_VERSION
    assert report.passed is True
    assert report.errors == ()
    assert report.claims_count == 3
    assert report.claims_by_disposition[CLAIM_WITHHELD] == 3
    assert report.verb_occurrences_found == 19
    assert report.allowlisted_occurrences_count == 19
    assert report.evidence_bindings_verified == 2
    assert len(report.source_files_scanned) >= 50


# -----------------------------------------------------------------------------
# 2. Adversarial Tests: Inventory & Schema Rules
# -----------------------------------------------------------------------------


def test_adversarial_empty_inventory_cannot_pass():
    errors = validate_claims_inventory([])
    assert any("claims inventory must be non-empty" in err for err in errors)


def test_adversarial_duplicate_claim_id_rejected():
    base = get_authoritative_leadership_claims()[0]
    errors = validate_claims_inventory([base, base])
    assert any(f"duplicate claim_id {base.claim_id!r}" in err for err in errors)


def test_adversarial_broad_universalization_rejected():
    claim = get_authoritative_leadership_claims()[0]
    claim_dict = claim.to_dict()

    for bad_phrase in (
        "Marker UI achieves global superiority across all tasks",
        "This is the universal standard for document extraction",
        "Marker UI is the best overall extraction engine",
        "It dominates all existing tools",
    ):
        claim_dict["universal_disclaimer"] = bad_phrase
        errs = validate_leadership_claim(claim_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("universal_disclaimer" in e for e in errs)


# -----------------------------------------------------------------------------
# 3. Adversarial Tests: Deep Evidence Binding & Scopes
# -----------------------------------------------------------------------------


def test_adversarial_artifact_tamper_fails_sha256(tmp_path: Path):
    claim = get_authoritative_leadership_claims()[0]
    binding = claim.evidence_bindings[0]

    # Create dummy file with mismatching content
    fake_artifact = tmp_path / binding.artifact_path
    fake_artifact.parent.mkdir(parents=True, exist_ok=True)
    fake_artifact.write_text('{"tampered": true}', encoding="utf-8")

    errs = verify_evidence_binding(binding, claim, repo_root=tmp_path)
    assert any("SHA-256 mismatch" in e for e in errs)


def test_adversarial_missing_metric_pointer(tmp_path: Path):
    claim = get_authoritative_leadership_claims()[0]
    binding = claim.evidence_bindings[0]

    # Create dummy artifact with valid hash but missing metric pointers
    fake_artifact = tmp_path / binding.artifact_path
    fake_artifact.parent.mkdir(parents=True, exist_ok=True)
    fake_data = {
        "schema_version": "marker.pr80b_displacement_evidence.v1",
        "systems": {
            "invoice2data": {},
            "llm-openrouter:poolside/laguna-s-2.1:free": {},
        },
        "corpus": {"documents": 24},
    }
    raw = json.dumps(fake_data).encode("utf-8")
    fake_sha = hashlib.sha256(raw).hexdigest()
    fake_artifact.write_bytes(raw)

    tampered_binding = ClaimEvidenceBinding(
        artifact_path=binding.artifact_path,
        artifact_sha256=fake_sha,
        lifecycle=EVIDENCE_CURRENT,
        workflow_scope=binding.workflow_scope,
        corpus_scope=binding.corpus_scope,
        comparator_scope=binding.comparator_scope,
        metric_pointers={"missing_metric": "metrics.marker-pr80a.docs.nonexistent"},
    )

    errs = verify_evidence_binding(tampered_binding, claim, repo_root=tmp_path)
    assert any("missing metric pointer" in e for e in errs)


def test_adversarial_comparator_mismatch(tmp_path: Path):
    claim = get_authoritative_leadership_claims()[0]
    binding = claim.evidence_bindings[0]

    # Binding with comparator subset / mismatch
    mismatched_binding = ClaimEvidenceBinding(
        artifact_path=binding.artifact_path,
        artifact_sha256=binding.artifact_sha256,
        lifecycle=EVIDENCE_CURRENT,
        workflow_scope=binding.workflow_scope,
        corpus_scope=binding.corpus_scope,
        comparator_scope=("invoice2data",),  # omitted LLM comparator
        metric_pointers=binding.metric_pointers,
    )

    errs = verify_evidence_binding(mismatched_binding, claim, repo_root=_repo_root())
    assert any(
        "comparator_scope must match claim competitors exactly" in e for e in errs
    )


def test_adversarial_comparator_absent_from_artifact_population(tmp_path: Path):
    claim = get_authoritative_leadership_claims()[0]
    binding = claim.evidence_bindings[0]

    # Artifact population that omits one declared comparator entirely
    fake_artifact = tmp_path / binding.artifact_path
    fake_artifact.parent.mkdir(parents=True, exist_ok=True)
    fake_data = {
        "schema_version": "marker.pr80b_displacement_evidence.v1",
        "systems": {
            "invoice2data": {},
            "marker-pr80a": {},
            # LLM comparator deliberately absent
        },
        "metrics": {
            "invoice2data": {},
            "marker-pr80a": {},
        },
    }
    raw = json.dumps(fake_data).encode("utf-8")
    fake_sha = hashlib.sha256(raw).hexdigest()
    fake_artifact.write_bytes(raw)

    tampered_binding = ClaimEvidenceBinding(
        artifact_path=binding.artifact_path,
        artifact_sha256=fake_sha,
        lifecycle=EVIDENCE_CURRENT,
        workflow_scope=binding.workflow_scope,
        corpus_scope=binding.corpus_scope,
        comparator_scope=binding.comparator_scope,
        metric_pointers=binding.metric_pointers,
    )

    errs = verify_evidence_binding(tampered_binding, claim, repo_root=tmp_path)
    assert any(
        "does not contain declared comparators" in e for e in errs
    )


def test_adversarial_artifact_without_population_cannot_verify_comparators(
    tmp_path: Path,
):
    claim = get_authoritative_leadership_claims()[0]
    binding = claim.evidence_bindings[0]

    # Artifact with no comparator population keys at all: comparator scope is
    # unverifiable and must not silently pass (prevents flipping a withheld
    # claim to beats on top of population-free provenance).
    fake_artifact = tmp_path / binding.artifact_path
    fake_artifact.parent.mkdir(parents=True, exist_ok=True)
    fake_data = {
        "schema_version": "marker.pr80b_displacement_evidence.v1",
        "notes": "no systems, metrics, or comparators here",
        "metrics_marker_pr80a": {"docs": {"doc_exact": 17}},
    }
    raw = json.dumps(fake_data).encode("utf-8")
    fake_sha = hashlib.sha256(raw).hexdigest()
    fake_artifact.write_bytes(raw)

    tampered_binding = ClaimEvidenceBinding(
        artifact_path=binding.artifact_path,
        artifact_sha256=fake_sha,
        lifecycle=EVIDENCE_CURRENT,
        workflow_scope=binding.workflow_scope,
        corpus_scope=binding.corpus_scope,
        comparator_scope=binding.comparator_scope,
        metric_pointers={},
    )

    errs = verify_evidence_binding(tampered_binding, claim, repo_root=tmp_path)
    assert any(
        "exposes no comparator population" in e for e in errs
    )


def test_adversarial_workflow_and_corpus_mismatch():
    claim = get_authoritative_leadership_claims()[0]
    binding = claim.evidence_bindings[0]

    wf_mismatched = ClaimEvidenceBinding(
        artifact_path=binding.artifact_path,
        artifact_sha256=binding.artifact_sha256,
        lifecycle=EVIDENCE_CURRENT,
        workflow_scope="wrong.workflow",
        corpus_scope=binding.corpus_scope,
        comparator_scope=binding.comparator_scope,
        metric_pointers=binding.metric_pointers,
    )
    errs = verify_evidence_binding(wf_mismatched, claim, repo_root=_repo_root())
    assert any("workflow_scope" in e for e in errs)

    corpus_mismatched = ClaimEvidenceBinding(
        artifact_path=binding.artifact_path,
        artifact_sha256=binding.artifact_sha256,
        lifecycle=EVIDENCE_CURRENT,
        workflow_scope=binding.workflow_scope,
        corpus_scope="unrelated_corpus",
        comparator_scope=binding.comparator_scope,
        metric_pointers=binding.metric_pointers,
    )
    errs2 = verify_evidence_binding(corpus_mismatched, claim, repo_root=_repo_root())
    assert any("corpus_scope" in e for e in errs2)


def test_adversarial_stale_evidence_cannot_support_beats_or_routes():
    for disp in (CLAIM_BEATS, CLAIM_TIES_REDUCING_BURDEN, CLAIM_ROUTES_TO):
        claim_dict = get_authoritative_leadership_claims()[0].to_dict()
        claim_dict["disposition"] = disp
        claim_dict["evidence_bindings"][0]["lifecycle"] = EVIDENCE_STALE

        errs = validate_leadership_claim(claim_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("cannot rest on stale evidence" in e for e in errs)

        claim_dict["evidence_bindings"][0]["lifecycle"] = EVIDENCE_SUPERSEDED
        errs2 = validate_leadership_claim(claim_dict, as_of_date="2026-08-26T00:00:00Z")
        assert any("cannot rest on superseded evidence" in e for e in errs2)


# -----------------------------------------------------------------------------
# 4. Adversarial Tests: Catastrophic Budget & Math Derivations
# -----------------------------------------------------------------------------


def test_calculate_one_sided_95_upper_bound_methods():
    # Rule of Three on 24 trials
    ub_rot = calculate_one_sided_95_upper_bound(24, 0, "rule_of_three")
    assert 0.124 < ub_rot <= 0.125

    # Exact binomial on 24 trials (1 - 0.05^(1/24))
    ub_bin = calculate_one_sided_95_upper_bound(24, 0, "exact_binomial")
    assert 0.117 < ub_bin < 0.118

    # Clopper-Pearson on 454 trials
    ub_cp = calculate_one_sided_95_upper_bound(454, 0, "clopper_pearson_exact")
    assert 0.0065 < ub_cp < 0.0066

    # Wilson score on 60 trials
    ub_wil = calculate_one_sided_95_upper_bound(60, 0, "wilson_score")
    assert 0.04 < ub_wil < 0.05

    # Non-zero events
    ub_events = calculate_one_sided_95_upper_bound(100, 2, "exact_binomial")
    assert 0.05 < ub_events < 0.07


def test_adversarial_zero_risk_lie_rejected():
    claim = get_authoritative_leadership_claims()[0]

    # 1. upper_bound_95 = 0.0 with 0 observed events
    bad_budget1 = CatastrophicBudget(
        max_acceptable_rate=0.15,
        observed_rate=0.0,
        bound_method="rule_of_three",
        upper_bound_95=0.0,
        trials=24,
        zero_is_not_zero_risk_acknowledged=True,
    )
    bad_claim1 = LeadershipClaim(
        claim_id=claim.claim_id,
        workflow=claim.workflow,
        source_profile=claim.source_profile,
        policy_profile=claim.policy_profile,
        hardware_profile=claim.hardware_profile,
        competitors=claim.competitors,
        evidence_date=claim.evidence_date,
        catastrophic_budget=bad_budget1,
        review_burden=claim.review_burden,
        unresolved_limits=claim.unresolved_limits,
        disposition=claim.disposition,
        evidence_bindings=claim.evidence_bindings,
        corpus_scope=claim.corpus_scope,
    )
    errs1 = verify_catastrophic_budget(bad_claim1)
    assert any("zero observed is not zero risk" in e for e in errs1)

    # 2. zero_is_not_zero_risk_acknowledged = False
    bad_budget2 = CatastrophicBudget(
        max_acceptable_rate=0.15,
        observed_rate=0.0,
        bound_method="rule_of_three",
        upper_bound_95=0.125,
        trials=24,
        zero_is_not_zero_risk_acknowledged=False,
    )
    bad_claim2 = LeadershipClaim(
        claim_id=claim.claim_id,
        workflow=claim.workflow,
        source_profile=claim.source_profile,
        policy_profile=claim.policy_profile,
        hardware_profile=claim.hardware_profile,
        competitors=claim.competitors,
        evidence_date=claim.evidence_date,
        catastrophic_budget=bad_budget2,
        review_burden=claim.review_burden,
        unresolved_limits=claim.unresolved_limits,
        disposition=claim.disposition,
        evidence_bindings=claim.evidence_bindings,
        corpus_scope=claim.corpus_scope,
    )
    errs2 = verify_catastrophic_budget(bad_claim2)
    assert any("zero_is_not_zero_risk_acknowledged must be True" in e for e in errs2)


def test_adversarial_wrong_upper_bound_rejected():
    claim = get_authoritative_leadership_claims()[0]

    # Declaring upper bound far lower than mathematical bound
    bad_budget = CatastrophicBudget(
        max_acceptable_rate=0.15,
        observed_rate=0.0,
        bound_method="rule_of_three",
        upper_bound_95=0.01,  # actual bound for 24 trials is ~0.125
        trials=24,
        zero_is_not_zero_risk_acknowledged=True,
    )
    bad_claim = LeadershipClaim(
        claim_id=claim.claim_id,
        workflow=claim.workflow,
        source_profile=claim.source_profile,
        policy_profile=claim.policy_profile,
        hardware_profile=claim.hardware_profile,
        competitors=claim.competitors,
        evidence_date=claim.evidence_date,
        catastrophic_budget=bad_budget,
        review_burden=claim.review_burden,
        unresolved_limits=claim.unresolved_limits,
        disposition=claim.disposition,
        evidence_bindings=claim.evidence_bindings,
        corpus_scope=claim.corpus_scope,
    )
    errs = verify_catastrophic_budget(bad_claim)
    assert any("lower than derived mathematical 95% bound" in e for e in errs)


def test_adversarial_unsupported_bound_method():
    claim = get_authoritative_leadership_claims()[0]
    bad_budget = CatastrophicBudget(
        max_acceptable_rate=0.15,
        observed_rate=0.0,
        bound_method="magic_oracle_bound",
        upper_bound_95=0.05,
        trials=24,
        zero_is_not_zero_risk_acknowledged=True,
    )
    bad_claim = LeadershipClaim(
        claim_id=claim.claim_id,
        workflow=claim.workflow,
        source_profile=claim.source_profile,
        policy_profile=claim.policy_profile,
        hardware_profile=claim.hardware_profile,
        competitors=claim.competitors,
        evidence_date=claim.evidence_date,
        catastrophic_budget=bad_budget,
        review_burden=claim.review_burden,
        unresolved_limits=claim.unresolved_limits,
        disposition=claim.disposition,
        evidence_bindings=claim.evidence_bindings,
        corpus_scope=claim.corpus_scope,
    )
    errs = verify_catastrophic_budget(bad_claim)
    assert any("unsupported bound_method" in e for e in errs)


# -----------------------------------------------------------------------------
# 5. Adversarial Tests: Review Burden Discipline
# -----------------------------------------------------------------------------


def test_adversarial_review_burden_unavailable_requires_reason():
    claim = get_authoritative_leadership_claims()[0]

    bad_rb = ReviewBurden(status="unavailable", reason="")
    bad_claim = LeadershipClaim(
        claim_id=claim.claim_id,
        workflow=claim.workflow,
        source_profile=claim.source_profile,
        policy_profile=claim.policy_profile,
        hardware_profile=claim.hardware_profile,
        competitors=claim.competitors,
        evidence_date=claim.evidence_date,
        catastrophic_budget=claim.catastrophic_budget,
        review_burden=bad_rb,
        unresolved_limits=claim.unresolved_limits,
        disposition=claim.disposition,
        evidence_bindings=claim.evidence_bindings,
        corpus_scope=claim.corpus_scope,
    )
    errs = verify_review_burden(bad_claim)
    assert any("requires non-empty reason" in e for e in errs)


def test_adversarial_review_burden_measured_requires_counts():
    claim = get_authoritative_leadership_claims()[0]

    bad_rb = ReviewBurden(
        status="measured", self_flagged_count=None, unverified_emitted_count=0
    )
    bad_claim = LeadershipClaim(
        claim_id=claim.claim_id,
        workflow=claim.workflow,
        source_profile=claim.source_profile,
        policy_profile=claim.policy_profile,
        hardware_profile=claim.hardware_profile,
        competitors=claim.competitors,
        evidence_date=claim.evidence_date,
        catastrophic_budget=claim.catastrophic_budget,
        review_burden=bad_rb,
        unresolved_limits=claim.unresolved_limits,
        disposition=claim.disposition,
        evidence_bindings=claim.evidence_bindings,
        corpus_scope=claim.corpus_scope,
    )
    errs = verify_review_burden(bad_claim)
    assert any("requires non-negative self_flagged_count" in e for e in errs)


# -----------------------------------------------------------------------------
# 6. Adversarial Tests: Release Source Scanner & Overstatements
# -----------------------------------------------------------------------------


def test_adversarial_unregistered_verb_occurrence_fails_scan(tmp_path: Path):
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir(parents=True, exist_ok=True)
    fake_md = fake_docs / "overstatement.md"
    fake_md.write_text(
        "# Fast Conversion\nMarker UI beats all external alternatives consistently.\n",
        encoding="utf-8",
    )

    errs, occs = scan_release_claim_sources(
        repo_root=tmp_path,
        registered_claim_ids=["claim.pr80b_invoice_extraction_authority"],
        custom_allowlist=(),
    )
    assert any("unregistered leadership verb 'beats'" in e for e in errs)
    assert any(o.status == "unregistered_overstatement" for o in occs)


def test_inline_claim_marker_satisfies_source_scanner(tmp_path: Path):
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir(parents=True, exist_ok=True)
    fake_md = fake_docs / "claimed_section.md"
    fake_md.write_text(
        "<!-- claim: claim.pr80b_invoice_extraction_authority -->\n"
        "On synthetic invoice benchmarks, our anchor route beats specialist regex parsers.\n",
        encoding="utf-8",
    )

    errs, occs = scan_release_claim_sources(
        repo_root=tmp_path,
        registered_claim_ids=["claim.pr80b_invoice_extraction_authority"],
        custom_allowlist=(),
    )
    assert errs == []
    assert any(o.status == "registered_claim" for o in occs)


# -----------------------------------------------------------------------------
# 7. Metric Pointer Resolution
# -----------------------------------------------------------------------------


def test_resolve_metric_pointer_nested_dicts_and_lists():
    data = {
        "metrics": {
            "marker-pr80a": {
                "docs": {"doc_exact": 17, "total": 24},
                "scalar": {"counts": {"correct": 94}},
            }
        },
        "scores": [0.95, 0.98, 1.0],
    }

    ok, val = resolve_metric_pointer(data, "metrics.marker-pr80a.docs.doc_exact")
    assert ok is True and val == 17

    ok2, val2 = resolve_metric_pointer(
        data, "/metrics/marker-pr80a/scalar/counts/correct"
    )
    assert ok2 is True and val2 == 94

    ok3, val3 = resolve_metric_pointer(data, "scores.1")
    assert ok3 is True and val3 == 0.98

    ok_bad, _ = resolve_metric_pointer(data, "metrics.missing.field")
    assert ok_bad is False
