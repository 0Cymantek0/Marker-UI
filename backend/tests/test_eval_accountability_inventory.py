"""Adversarial, completeness, and semantic alignment tests for Invariant-59 inventory & accountability population."""

from __future__ import annotations

import copy
from pathlib import Path
from app.eval.accountability import (
    DEFAULT_EXCLUDED_CATEGORY_POLICY,
    DISPOSITION_PROMOTED,
    EVIDENCE_STALE,
    EVIDENCE_SUPERSEDED,
    ExcludedCategoryPolicy,
    InventorySubject,
    get_authoritative_capability_matrix,
    get_authoritative_capability_records_tuple,
    get_authoritative_inventory,
    get_authoritative_inventory_subjects_tuple,
    get_authoritative_rollback_verification_nodes,
    get_excluded_category_policy,
    get_promoted_rollback_verification_nodes,
    validate_accountability_completeness,
    validate_capability_records_sequence,
    validate_excluded_category_policy,
    validate_inventory_sequence,
    validate_inventory_subject,
)
from app.eval.model_catalog import ModelCatalog, ModelSpec, ProviderSpec

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_authoritative_inventory_and_population_validates_cleanly():
    """Verify that all 22 committed in-scope inventory subjects and population pass completeness rules."""
    inv = get_authoritative_inventory()
    assert len(inv) == 22
    records = get_authoritative_capability_matrix()
    assert len(records) == 22

    errors = validate_accountability_completeness(
        inventory=inv,
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
        verify_evidence_digests=True,
    )
    assert errors == []


def test_runner_facing_rollback_verification_nodes():
    all_nodes = get_authoritative_rollback_verification_nodes()
    assert len(all_nodes) >= 15
    for node in all_nodes:
        assert "::" in node
        assert node.startswith("backend/tests/") or node.startswith("backend/conformance/")

    promoted_nodes = get_promoted_rollback_verification_nodes()
    assert len(promoted_nodes) == 11
    for node in promoted_nodes:
        assert "::" in node
        assert node.startswith("backend/tests/") or node.startswith("backend/conformance/")


def test_authoritative_raw_tuples_have_no_duplicates():
    raw_inv = get_authoritative_inventory_subjects_tuple()
    assert len(raw_inv) == 22
    inv_errs = validate_inventory_sequence(raw_inv, repo_root=REPO_ROOT)
    assert inv_errs == []

    raw_rec = get_authoritative_capability_records_tuple()
    assert len(raw_rec) == 22
    rec_errs = validate_capability_records_sequence(raw_rec, as_of_date="2026-08-26T00:00:00Z")
    assert rec_errs == []


def test_forbidden_overclaim_phrases_absent_from_procedures():
    """Assert that fictional or overclaiming rollback phrases are absent from authoritative records."""
    forbidden_phrases = (
        "postgresql pool to sqlite",
        "switch engine dialect from postgresql pool",
        "s3 object store is unavailable",
        "s3 to local",
        "filesystem source store when s3",
        "fallback to local disk filesystem source store",
        "fallback to local sqlite single-instance",
        "provisional set discard",
        "discards provisional draft and retains previous",
        "route to human review queue",
    )
    records = get_authoritative_capability_matrix()
    for cid, rec in records.items():
        if rec.rollback and rec.rollback.procedure:
            proc_lower = rec.rollback.procedure.lower()
            for phrase in forbidden_phrases:
                assert phrase not in proc_lower, (
                    f"Capability {cid!r} contains forbidden overclaim phrase {phrase!r} in procedure: {rec.rollback.procedure}"
                )


def test_exact_promoted_procedure_semantic_alignment():
    """Verify specific promoted capabilities have exact semantic alignment with test proof."""
    records = get_authoritative_capability_matrix()

    # kernel.commit_authority -> transaction_retry & whole-operation retry
    commit_rec = records["kernel.commit_authority"]
    assert commit_rec.disposition == DISPOSITION_PROMOTED
    assert commit_rec.rollback.mechanism == "transaction_retry"
    assert "whole-operation retry on classified contention" in commit_rec.rollback.procedure.lower()
    assert commit_rec.rollback.verification_node == "backend/tests/test_kernel_dialects.py::test_contention_budget_retries_whole_operation_then_converges"

    # source.acquisition_and_convergence -> CAS artifact healing
    src_rec = records["source.acquisition_and_convergence"]
    assert src_rec.disposition == DISPOSITION_PROMOTED
    assert "cas" in src_rec.rollback.mechanism.lower() or "corrupted" in src_rec.rollback.mechanism.lower()
    assert "heal" in src_rec.rollback.procedure.lower()
    assert src_rec.rollback.verification_node == "backend/tests/test_kernel_source_store.py::test_corrupted_existing_artifact_is_healed_on_reacquisition"

    # runtime.durable_jobs_fencing -> takeover advances fence
    fence_rec = records["runtime.durable_jobs_fencing"]
    assert fence_rec.disposition == DISPOSITION_PROMOTED
    assert "takeover" in fence_rec.rollback.procedure.lower()
    assert "advances durable fence" in fence_rec.rollback.procedure.lower()
    assert fence_rec.rollback.verification_node == "backend/tests/test_kernel_fencing.py::test_takeover_after_expiry_advances_the_durable_fence"

    # storage.industrial_topology -> standby promotion preserving acknowledged truth
    storage_rec = records["storage.industrial_topology"]
    assert storage_rec.disposition == DISPOSITION_PROMOTED
    assert "standby failover promotion" in storage_rec.rollback.procedure.lower()
    assert "sqlite" not in storage_rec.rollback.procedure.lower()
    assert storage_rec.rollback.verification_node == "backend/tests/test_kernel_pg_failover_promotion.py::test_acknowledged_truth_survives_primary_loss_and_promotion"

    # extraction.deterministic_extractor -> reject ungrounded candidate
    ext_rec = records["extraction.deterministic_extractor"]
    assert ext_rec.disposition == DISPOSITION_PROMOTED
    assert "reject ungrounded" in ext_rec.rollback.procedure.lower()
    assert ext_rec.rollback.verification_node == "backend/tests/test_extraction_review.py::test_cannot_accept_a_field_with_no_grounded_candidate"

    # answer_evidence.publication_service -> reject conflicting context on same answer ref
    ans_rec = records["answer_evidence.publication_service"]
    assert ans_rec.disposition == DISPOSITION_PROMOTED
    assert "conflicting context" in ans_rec.rollback.procedure.lower()
    assert ans_rec.rollback.verification_node == "backend/tests/test_answer_evidence.py::test_same_answer_ref_with_different_context_conflicts"


def test_adversarial_duplicate_in_inventory_sequence_fails_before_dict():
    raw_inv = list(get_authoritative_inventory_subjects_tuple())
    raw_inv.append(raw_inv[0])
    errs = validate_inventory_sequence(raw_inv, repo_root=REPO_ROOT)
    assert any(f"duplicate inventory subject id in sequence: '{raw_inv[0].id}'" in e for e in errs)

    completeness_errs = validate_accountability_completeness(
        inventory=raw_inv,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any(f"duplicate inventory subject id in sequence: '{raw_inv[0].id}'" in e for e in completeness_errs)


def test_adversarial_duplicate_in_records_sequence_fails_before_dict():
    raw_rec = list(get_authoritative_capability_records_tuple())
    raw_rec.append(raw_rec[0])
    errs = validate_capability_records_sequence(raw_rec, as_of_date="2026-08-26T00:00:00Z")
    assert any(f"duplicate capability record id in sequence: '{raw_rec[0].id}'" in e for e in errs)

    completeness_errs = validate_accountability_completeness(
        records=raw_rec,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any(f"duplicate capability record id in sequence: '{raw_rec[0].id}'" in e for e in completeness_errs)


def test_inventory_subject_validation():
    root = REPO_ROOT
    valid_sub = InventorySubject(
        id="test.valid_subsystem",
        name="Test Valid Subsystem",
        category="architecture_subsystem",
        source_paths=("backend/app/kernel/commit.py",),
        description="A valid test subsystem",
        in_scope_v32=True,
    )
    assert validate_inventory_subject(valid_sub, repo_root=root) == []

    bad_sub = InventorySubject(
        id="",
        name="",
        category="unknown_category",
        source_paths=(),
        description="",
    )
    errs = validate_inventory_subject(bad_sub, repo_root=root)
    assert any("subject 'id' must be" in e for e in errs)
    assert any("subject 'name' must be" in e for e in errs)
    assert any("subject category must be one of" in e for e in errs)
    assert any("must declare a non-empty list of source_paths" in e for e in errs)
    assert any("subject 'description' must be" in e for e in errs)


def test_inventory_subject_missing_source_path_fails():
    root = REPO_ROOT
    bad_path_sub = InventorySubject(
        id="test.bad_path",
        name="Test Bad Path",
        category="architecture_subsystem",
        source_paths=("backend/app/kernel/non_existent_file_xyz123.py",),
        description="Test missing path",
    )
    errs = validate_inventory_subject(bad_path_sub, repo_root=root)
    assert any("source path does not exist: backend/app/kernel/non_existent_file_xyz123.py" in e for e in errs)


def test_excluded_category_policy_validation():
    pol = get_excluded_category_policy()
    assert validate_excluded_category_policy(pol) == []

    bad_pol = ExcludedCategoryPolicy(
        schema_version="wrong.version",
        excluded_categories=(),
        excluded_model_candidates={},
        exclusion_reasons={},
    )
    errs = validate_excluded_category_policy(bad_pol)
    assert any("schema_version must be" in e for e in errs)
    assert any("excluded_categories must be a non-empty list" in e for e in errs)

    bad_pol2 = ExcludedCategoryPolicy(
        schema_version=DEFAULT_EXCLUDED_CATEGORY_POLICY.schema_version,
        excluded_categories=("test_category",),
        excluded_model_candidates={},
        exclusion_reasons={},
    )
    errs2 = validate_excluded_category_policy(bad_pol2)
    assert any("exclusion_reasons must be a non-empty mapping" in e for e in errs2)

    bad_pol_dup = ExcludedCategoryPolicy(
        schema_version=DEFAULT_EXCLUDED_CATEGORY_POLICY.schema_version,
        excluded_categories=("dup_category", "dup_category"),
        excluded_model_candidates={},
        exclusion_reasons={"dup_category": "Some reason"},
    )
    errs_dup = validate_excluded_category_policy(bad_pol_dup)
    assert any("excluded_categories contains duplicate category: 'dup_category'" in e for e in errs_dup)

    bad_pol_unk = ExcludedCategoryPolicy(
        schema_version=DEFAULT_EXCLUDED_CATEGORY_POLICY.schema_version,
        excluded_categories=("cat_a",),
        excluded_model_candidates={},
        exclusion_reasons={"cat_a": "Reason A", "cat_b_unknown": "Reason B"},
    )
    errs_unk = validate_excluded_category_policy(bad_pol_unk)
    assert any("exclusion_reasons contains unknown category key: 'cat_b_unknown'" in e for e in errs_unk)

    bad_pol_model = ExcludedCategoryPolicy(
        schema_version=DEFAULT_EXCLUDED_CATEGORY_POLICY.schema_version,
        excluded_categories=("cat_a",),
        excluded_model_candidates={"": "Empty key", "valid_id": ""},
        exclusion_reasons={"cat_a": "Reason A"},
    )
    errs_model = validate_excluded_category_policy(bad_pol_model)
    assert any("excluded_model_candidates contains invalid/empty model ID" in e for e in errs_model)
    assert any("excluded_model_candidates entry 'valid_id' requires non-empty explanation string" in e for e in errs_model)


def test_adversarial_remove_subject_fails_bijection():
    inv = get_authoritative_inventory()
    records = get_authoritative_capability_matrix()

    del records["kernel.commit_authority"]
    errs = validate_accountability_completeness(
        inventory=inv,
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("missing accountability record for in-scope inventory subject: 'kernel.commit_authority'" in e for e in errs)


def test_adversarial_extra_record_fails_bijection():
    inv = get_authoritative_inventory()
    records = get_authoritative_capability_matrix()

    extra_rec = copy.deepcopy(records["kernel.commit_authority"])
    extra_dict = extra_rec.to_dict()
    extra_dict["id"] = "extra.unauthorized_subsystem"
    records["extra.unauthorized_subsystem"] = extra_dict

    errs = validate_accountability_completeness(
        inventory=inv,
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("extra accountability record not declared in in-scope inventory: 'extra.unauthorized_subsystem'" in e for e in errs)


def test_adversarial_missing_source_path_fails_completeness():
    inv = get_authoritative_inventory()
    sub = inv["kernel.commit_authority"]
    tampered_sub = InventorySubject(
        id=sub.id,
        name=sub.name,
        category=sub.category,
        source_paths=("backend/app/kernel/deleted_source_file.py",),
        description=sub.description,
        in_scope_v32=True,
    )
    inv["kernel.commit_authority"] = tampered_sub

    errs = validate_accountability_completeness(
        inventory=inv,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("source path does not exist: backend/app/kernel/deleted_source_file.py" in e for e in errs)


def test_adversarial_catalog_candidate_omission_fails():
    inv = get_authoritative_inventory()
    pol = get_excluded_category_policy()

    dummy_provider = ProviderSpec(
        id="local-gateway",
        transport="openai_chat",
        base_url_env="MARKER_LLM_BASE_URL",
        api_key_env="MARKER_LLM_API_KEY",
    )
    synthetic_catalog = ModelCatalog(
        providers={"local-gateway": dummy_provider},
        models={
            "untracked/new-candidate-model-v1": ModelSpec(
                id="untracked/new-candidate-model-v1",
                provider="local-gateway",
                context_window=128000,
                max_output=4096,
            )
        },
    )

    errs = validate_accountability_completeness(
        inventory=inv,
        catalog=synthetic_catalog,
        excluded_policy=pol,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("model catalog entry 'untracked/new-candidate-model-v1' not covered by inventory candidates" in e for e in errs)

    covered_policy = ExcludedCategoryPolicy(
        schema_version=pol.schema_version,
        excluded_categories=pol.excluded_categories,
        excluded_model_candidates={"untracked/new-candidate-model-v1": "Experimental test model excluded from evaluation matrix"},
        exclusion_reasons=pol.exclusion_reasons,
    )
    errs2 = validate_accountability_completeness(
        inventory=inv,
        catalog=synthetic_catalog,
        excluded_policy=covered_policy,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert not any("untracked/new-candidate-model-v1" in e for e in errs2)


def test_adversarial_tamper_evidence_digest_fails():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["utility_basis"]["evidence_sha256"] = "f" * 64
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
        verify_evidence_digests=True,
    )
    assert any("evidence artifact SHA-256 digest mismatch" in e for e in errs)


def test_adversarial_tamper_rollback_verification_digest_fails():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["rollback"]["verification_sha256"] = "f" * 64
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
        verify_evidence_digests=True,
    )
    assert any("rollback verification evidence SHA-256 mismatch" in e for e in errs)


def test_adversarial_rollback_node_filename_only_rejected():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["rollback"]["verification_node"] = "backend/tests/test_kernel_dialects.py"
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("must be an exact pytest node ID" in e for e in errs)


def test_adversarial_rollback_node_unapproved_path_rejected():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["rollback"]["verification_node"] = "docs/reference/test_mock.py::test_fn"
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("must be in approved test directories" in e for e in errs)


def test_adversarial_rollback_node_nonexistent_file_fails():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["rollback"]["verification_node"] = "backend/tests/test_nonexistent_file_abc.py::test_fn"
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("rollback verification node file not found" in e for e in errs)


def test_adversarial_rollback_node_nonexistent_ast_function_fails():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["rollback"]["verification_node"] = "backend/tests/test_kernel_dialects.py::test_nonexistent_fn_xyz123"
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("rollback verification node function 'test_nonexistent_fn_xyz123' not found in AST" in e for e in errs)


def test_adversarial_promoted_expiry_deadline_fails():
    records = get_authoritative_capability_matrix()
    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2029-01-01T00:00:00Z",
    )
    assert any("is expired (retest deadline" in e for e in errs)


def test_adversarial_promoted_triggered_kill_fails():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["kill_condition"]["triggered"] = True
    rec_dict["kill_condition"]["trigger_reason"] = "Corruption threshold breached in telemetry"
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("kill condition is triggered" in e for e in errs)


def test_adversarial_promoted_stale_or_superseded_evidence_fails():
    for lc in (EVIDENCE_STALE, EVIDENCE_SUPERSEDED):
        records = get_authoritative_capability_matrix()
        rec_dict = records["kernel.commit_authority"].to_dict()
        rec_dict["utility_basis"]["lifecycle"] = lc
        records["kernel.commit_authority"] = rec_dict

        errs = validate_accountability_completeness(
            records=records,
            repo_root=REPO_ROOT,
            as_of_date="2026-08-26T00:00:00Z",
        )
        assert any(f"cannot rest on {lc} evidence" in e for e in errs)


def test_adversarial_promoted_unverified_rollback_fails():
    records = get_authoritative_capability_matrix()
    rec_dict = records["kernel.commit_authority"].to_dict()
    rec_dict["rollback"]["verified"] = False
    records["kernel.commit_authority"] = rec_dict

    errs = validate_accountability_completeness(
        records=records,
        repo_root=REPO_ROOT,
        as_of_date="2026-08-26T00:00:00Z",
    )
    assert any("requires verified rollback path" in e for e in errs)
