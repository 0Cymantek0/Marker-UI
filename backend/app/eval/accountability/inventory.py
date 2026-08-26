"""Authoritative Invariant-59 Inventory and Excluded Category Policy.

Governing requirement:
"Design one explicit finite authority for in-scope V3.2 shipped/promoted/experimental/non-promoted
capabilities and architecture subsystems; separate population authority from lifecycle/evidence
records without duplicating benchmark truth."

Provides:
- InventorySubject contract and finite in-scope V3.2 subject definitions covering all major
  architecture boundaries (truth/persistence, geometry/patches, verification/risk, runtime/jobs,
  source/retrieval, agent/product, economics/industrial storage, and model catalog candidates).
- Machine-readable ExcludedCategoryPolicy with explicit exclusion rationale.
- Validation functions for inventory sequence (duplicate checking before dict construction),
  individual subjects, source paths, and excluded category policies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

INVENTORY_AUTHORITY_SCHEMA_VERSION = "marker.inventory_authority.v1"
EXCLUDED_CATEGORY_POLICY_SCHEMA_VERSION = "marker.excluded_category_policy.v1"

INVENTORY_CATEGORIES = frozenset(
    {
        "architecture_subsystem",
        "runtime_capability",
        "evaluation_model_candidate",
    }
)


class InventoryAuthorityError(ValueError):
    """Raised when inventory definition or excluded category policy fails closed."""


@dataclass(frozen=True)
class InventorySubject:
    id: str
    name: str
    category: str
    source_paths: tuple[str, ...]
    description: str
    in_scope_v32: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "source_paths": list(self.source_paths),
            "description": self.description,
            "in_scope_v32": self.in_scope_v32,
        }
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out


@dataclass(frozen=True)
class ExcludedCategoryPolicy:
    schema_version: str
    excluded_categories: tuple[str, ...]
    excluded_model_candidates: Mapping[str, str]
    exclusion_reasons: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "excluded_categories": list(self.excluded_categories),
            "excluded_model_candidates": dict(self.excluded_model_candidates),
            "exclusion_reasons": dict(self.exclusion_reasons),
        }


DEFAULT_EXCLUDED_CATEGORY_POLICY = ExcludedCategoryPolicy(
    schema_version=EXCLUDED_CATEGORY_POLICY_SCHEMA_VERSION,
    excluded_categories=(
        "test_harness_and_fixtures",
        "developer_scripts_and_cli",
        "pure_data_and_migrations",
        "third_party_dependencies",
        "unpromoted_legacy_shims",
    ),
    excluded_model_candidates={},
    exclusion_reasons={
        "test_harness_and_fixtures": (
            "Test runners, mock frameworks, and conformance test suites in backend/tests and "
            "backend/conformance support verification but are not serving capabilities."
        ),
        "developer_scripts_and_cli": (
            "Offline evaluation benchmarks, report generators, and audit entrypoints in "
            "backend/scripts produce evidence but are not runtime production subsystems."
        ),
        "pure_data_and_migrations": (
            "Alembic migration scripts and static database fixtures define schema evolution "
            "and are governed by transactional commit authority rather than standalone capabilities."
        ),
        "third_party_dependencies": (
            "External vendor packages and runtime interpreters provide underlying substrate and "
            "are tracked via lockfiles rather than internal subsystem accountability."
        ),
        "unpromoted_legacy_shims": (
            "Deprecated compatibility routes (such as legacy convert stubs) are maintained "
            "for API backward compatibility without active V3.2 utility claims."
        ),
    },
)


def validate_inventory_subject(
    subject: InventorySubject | Mapping[str, Any],
    repo_root: Path | str | None = None,
) -> list[str]:
    """Validate an inventory subject for syntactic correctness and source path existence."""
    errors: list[str] = []

    if isinstance(subject, InventorySubject):
        s_id = subject.id
        name = subject.name
        cat = subject.category
        paths = subject.source_paths
        desc = subject.description
    elif isinstance(subject, Mapping):
        s_id = subject.get("id")
        name = subject.get("name")
        cat = subject.get("category")
        paths = tuple(subject.get("source_paths") or ())
        desc = subject.get("description")
    else:
        return ["inventory subject must be InventorySubject or Mapping"]

    if not isinstance(s_id, str) or not s_id.strip():
        errors.append("inventory subject 'id' must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        errors.append("inventory subject 'name' must be a non-empty string")
    if cat not in INVENTORY_CATEGORIES:
        errors.append(
            f"inventory subject category must be one of {sorted(INVENTORY_CATEGORIES)}, got {cat!r}"
        )
    if not isinstance(desc, str) or not desc.strip():
        errors.append("inventory subject 'description' must be a non-empty string")

    if not isinstance(paths, (list, tuple)) or not paths:
        errors.append(
            f"inventory subject {s_id!r} must declare a non-empty list of source_paths"
        )
    else:
        root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[4]
        for sp in paths:
            if not isinstance(sp, str) or not sp.strip():
                errors.append(
                    f"inventory subject {s_id!r} contains invalid source path: {sp!r}"
                )
            else:
                target = root / sp
                if not target.exists():
                    errors.append(
                        f"inventory subject {s_id!r} source path does not exist: {sp}"
                    )

    return errors


def validate_inventory_sequence(
    subjects: Sequence[InventorySubject | Mapping[str, Any]],
    repo_root: Path | str | None = None,
) -> list[str]:
    """Validate a sequence of inventory subjects, detecting duplicates before dictionary construction."""
    errors: list[str] = []
    if not isinstance(subjects, (list, tuple)):
        return ["inventory subjects sequence must be a list or tuple"]

    seen_ids: set[str] = set()
    for idx, subj in enumerate(subjects):
        s_id = subj.id if isinstance(subj, InventorySubject) else subj.get("id")
        if not isinstance(s_id, str) or not s_id.strip():
            errors.append(f"inventory subject at index {idx} has invalid/missing 'id'")
            continue
        if s_id in seen_ids:
            errors.append(f"duplicate inventory subject id in sequence: {s_id!r}")
        seen_ids.add(s_id)

        subj_errs = validate_inventory_subject(subj, repo_root=repo_root)
        errors.extend(subj_errs)

    return errors


_AUTHORITATIVE_SUBJECTS: tuple[InventorySubject, ...] = (
    # Core Architecture Subsystems (23C.1 - 23C.7)
    InventorySubject(
        id="kernel.commit_authority",
        name="Transactional Kernel Commit Authority",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/commit.py",
            "backend/app/kernel/dialects.py",
            "backend/app/kernel/models.py",
        ),
        description=(
            "Provides transactional commit authority, SQLite/PostgreSQL engine dialect parity, "
            "and atomic publication linearization."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="kernel.anchor_mapping",
        name="Cross-Revision Anchor Mapping Cascade",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/anchor_mapping.py",
            "backend/app/kernel/anchors.py",
            "backend/app/kernel/reading_order.py",
        ),
        description=(
            "Deterministic cascade maintaining cross-revision anchor identity, reading order graph, "
            "and preventing silent citation drift."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="kernel.incremental_rebuild",
        name="Incremental Mutation and Rebuild Engine",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/patches.py",
            "backend/app/kernel/patching.py",
            "backend/app/kernel/rebuild.py",
        ),
        description=(
            "Replays accepted patches with exact equivalence to fresh derivations on document histories."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="source.acquisition_and_convergence",
        name="Source Acquisition and Connector Convergence",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/source_store.py",
            "backend/app/kernel/source_object_store.py",
            "backend/app/kernel/connector_state.py",
        ),
        description=(
            "Source store and connector convergence layer guaranteeing idempotent ingestion, cursor atomicity, "
            "and TOCTOU-safe immutable staging."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="verification.risk_and_dependence",
        name="Verification Risk and Dependency Disclosure",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/proofs.py",
            "backend/app/eval/pr82/dependence.py",
            "backend/app/kernel/assessment_view.py",
        ),
        description=(
            "Proof graph cycle verification, dependency risk disclosure, and shadow correlation-aware evaluation."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="runtime.durable_jobs_fencing",
        name="Durable Execution, Fencing Leases, and Publications",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/fencing.py",
            "backend/app/kernel/publications.py",
            "backend/app/kernel/scheduler.py",
            "backend/app/kernel/outbox.py",
            "backend/app/kernel/events.py",
        ),
        description=(
            "Distributed fencing leases, heartbeat renewals, outbox dispatch, and exactly-one publication guarantees."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="runtime.admission_and_residency",
        name="Runtime Admission and Concurrency Envelope",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/models/schemas.py",
            "backend/app/models/settings.py",
            "backend/app/cli.py",
        ),
        description=(
            "Static worker concurrency limits; dynamic memory-envelope admission and model leases remain unpromoted."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="storage.industrial_topology",
        name="Industrial Storage, Persistence, and Topology",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/kernel/object_store.py",
            "backend/app/kernel/lexical.py",
            "backend/app/kernel/recovery.py",
            "backend/app/kernel/source_object_store.py",
        ),
        description=(
            "PostgreSQL industrial persistence, S3 object storage, lexical query serving, and crash recovery."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="retrieval.context_runtime",
        name="Bounded Context Query and Pagination Runtime",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/context_runtime/executor.py",
            "backend/app/context_runtime/service.py",
            "backend/app/context_runtime/continuation_paging.py",
        ),
        description=(
            "Bounded context retrieval engine supporting snapshot-safe cursor continuation and pagination."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="retrieval.authorization_filter",
        name="Authorization-First Retrieval and Domain ACL Enforcer",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/context_runtime/authorization.py",
            "backend/app/security/auth.py",
            "backend/app/security/scopes.py",
        ),
        description=(
            "Pre-retrieval authorization boundary enforcing domain-level and document-level access control."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="extraction.deterministic_extractor",
        name="Deterministic Evidence-Backed Invoice & Form Extractor",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/extraction/extractor.py",
            "backend/app/extraction/schema.py",
            "backend/app/extraction/validation.py",
        ),
        description=(
            "Deterministic extraction engine producing schema-validated field extractions with exact anchor evidence."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="answer_evidence.publication_service",
        name="Answer Citation and Publication Sets Service",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/answer_evidence/service.py",
            "backend/app/answer_evidence/domain.py",
            "backend/app/answer_evidence/store.py",
        ),
        description=(
            "Publication set assembly service guaranteeing digest-verifiable answer evidence and citations."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="operational.as_of_integrity",
        name="Authoritative As-Of Review Integrity and Replay Engine",
        category="architecture_subsystem",
        source_paths=(
            "backend/app/operational/as_of.py",
            "backend/app/extraction/review_ops.py",
        ),
        description=(
            "Server-authoritative review integrity surface reconciling as-of document state and exports."
        ),
        in_scope_v32=True,
    ),
    # Runtime Capabilities
    InventorySubject(
        id="retrieval.selective_visual_rerank",
        name="Selective Visual Rerank Capability",
        category="runtime_capability",
        source_paths=(
            "backend/app/eval/pr81a/lanes.py",
            "backend/app/eval/pr81a/decision.py",
            "backend/app/eval/pr81b/decision.py",
        ),
        description=(
            "Selective visual reranking lane applied to candidate recall sets under model-gated quality rules."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="retrieval.dense_unselective_visual",
        name="Dense Unselective Full-Document Visual Indexer",
        category="runtime_capability",
        source_paths=(
            "backend/app/eval/pr81a/visual_index.py",
            "backend/app/eval/pr81a/visual_store.py",
        ),
        description=(
            "Decommissioned dense unselective visual indexing path, disabled due to storage amplification."
        ),
        in_scope_v32=True,
    ),
    InventorySubject(
        id="routing.specialist_hybrid_bridge",
        name="Direct Specialist Hybrid Bridge & Candidate Generator",
        category="runtime_capability",
        source_paths=(
            "backend/app/eval/bridge/runner.py",
            "backend/app/eval/bridge/translate.py",
            "backend/app/extraction/specialist.py",
        ),
        description=(
            "Candidate generation bridge translating external specialist outputs into native verification format."
        ),
        in_scope_v32=True,
    ),
    # Model Catalog Evaluation Candidates (1:1 candidate coverage)
    InventorySubject(
        id="model.kr_claude_sonnet_4_5",
        name="Claude Sonnet 4.5 Visual Evaluated Candidate",
        category="evaluation_model_candidate",
        source_paths=(
            "backend/app/eval/model_catalog.default.json",
            "backend/app/eval/model_catalog.py",
        ),
        description=(
            "Frontier VLM candidate evaluated in PR81B model-sensitivity matrix; confirmed holder model."
        ),
        in_scope_v32=True,
        metadata={"catalog_model_id": "kr/claude-sonnet-4.5"},
    ),
    InventorySubject(
        id="model.kr_claude_haiku_4_5",
        name="Claude Haiku 4.5 Visual Evaluated Candidate",
        category="evaluation_model_candidate",
        source_paths=(
            "backend/app/eval/model_catalog.default.json",
            "backend/app/eval/model_catalog.py",
        ),
        description=(
            "Frontier VLM candidate evaluated in PR81B; breached text-easy control, non-promoted."
        ),
        in_scope_v32=True,
        metadata={"catalog_model_id": "kr/claude-haiku-4.5"},
    ),
    InventorySubject(
        id="model.cx_gpt_5_6_luna",
        name="GPT-5.6 Luna Visual Evaluated Candidate",
        category="evaluation_model_candidate",
        source_paths=(
            "backend/app/eval/model_catalog.default.json",
            "backend/app/eval/model_catalog.py",
        ),
        description=(
            "Frontier VLM candidate evaluated in PR81B model-sensitivity matrix; confirmed holder model."
        ),
        in_scope_v32=True,
        metadata={"catalog_model_id": "cx/gpt-5.6-luna"},
    ),
    InventorySubject(
        id="model.free_gemini_3_0_flash",
        name="Gemini 3.0 Flash Visual Evaluated Candidate",
        category="evaluation_model_candidate",
        source_paths=(
            "backend/app/eval/model_catalog.default.json",
            "backend/app/eval/model_catalog.py",
        ),
        description=(
            "Economy VLM candidate evaluated in PR81B model-sensitivity matrix; confirmed holder model."
        ),
        in_scope_v32=True,
        metadata={"catalog_model_id": "free/bbl/gemini-3.0-flash"},
    ),
    InventorySubject(
        id="model.oc_mimo_v2_5_free",
        name="MiMo v2.5 Free Evaluated Candidate",
        category="evaluation_model_candidate",
        source_paths=(
            "backend/app/eval/model_catalog.default.json",
            "backend/app/eval/model_catalog.py",
        ),
        description=(
            "Economy VLM candidate evaluated in PR81B; probe failed due to rate limits, non-promoted."
        ),
        in_scope_v32=True,
        metadata={"catalog_model_id": "oc/mimo-v2.5-free"},
    ),
    InventorySubject(
        id="model.google_gemma_4_26b",
        name="Gemma 4 26B Reference Evaluated Candidate",
        category="evaluation_model_candidate",
        source_paths=(
            "backend/app/eval/model_catalog.default.json",
            "backend/app/eval/model_catalog.py",
        ),
        description=(
            "Reference VLM candidate evaluated in PR81A visual retrieval baseline; non-promoted."
        ),
        in_scope_v32=True,
        metadata={"catalog_model_id": "google/gemma-4-26b-a4b-it:free"},
    ),
)


AUTHORITATIVE_INVENTORY: dict[str, InventorySubject] = {
    s.id: s for s in _AUTHORITATIVE_SUBJECTS
}


def get_authoritative_inventory() -> dict[str, InventorySubject]:
    """Return a fresh shallow copy of the authoritative invariant-59 inventory."""
    return dict(AUTHORITATIVE_INVENTORY)


def get_authoritative_inventory_subjects_tuple() -> tuple[InventorySubject, ...]:
    """Return the raw authoritative inventory subjects tuple before dict construction."""
    return _AUTHORITATIVE_SUBJECTS


def get_excluded_category_policy() -> ExcludedCategoryPolicy:
    """Return the authoritative machine-readable excluded-category policy."""
    return DEFAULT_EXCLUDED_CATEGORY_POLICY


def validate_excluded_category_policy(
    policy: ExcludedCategoryPolicy | Mapping[str, Any],
) -> list[str]:
    """Validate that an excluded category policy complies with the fail-closed contract."""
    errors: list[str] = []
    if isinstance(policy, ExcludedCategoryPolicy):
        p_dict = policy.to_dict()
    elif isinstance(policy, Mapping):
        p_dict = dict(policy)
    else:
        return ["excluded category policy must be ExcludedCategoryPolicy or Mapping"]

    schema = p_dict.get("schema_version")
    if schema != EXCLUDED_CATEGORY_POLICY_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {EXCLUDED_CATEGORY_POLICY_SCHEMA_VERSION!r}, got {schema!r}"
        )

    cats = p_dict.get("excluded_categories")
    if not isinstance(cats, (list, tuple)) or not cats:
        errors.append("excluded_categories must be a non-empty list of strings")
    else:
        # Check duplicate categories
        seen_cats: set[str] = set()
        for cat in cats:
            if not isinstance(cat, str) or not cat.strip():
                errors.append(
                    "excluded_categories contains invalid/empty category string"
                )
            elif cat in seen_cats:
                errors.append(
                    f"excluded_categories contains duplicate category: {cat!r}"
                )
            seen_cats.add(cat)

    reasons = p_dict.get("exclusion_reasons")
    if not isinstance(reasons, Mapping) or not reasons:
        errors.append("exclusion_reasons must be a non-empty mapping")
    else:
        cats_set = set(cats or ())
        # Check every key in exclusion_reasons is a known excluded category
        for rk in reasons:
            if rk not in cats_set:
                errors.append(
                    f"exclusion_reasons contains unknown category key: {rk!r}"
                )

        # Check every excluded category has a non-empty explanation
        for cat in cats or ():
            if cat not in reasons or not str(reasons[cat]).strip():
                errors.append(
                    f"excluded category {cat!r} requires non-empty explanation in exclusion_reasons"
                )

    # Check excluded_model_candidates
    ex_models = p_dict.get("excluded_model_candidates", {})
    if not isinstance(ex_models, Mapping):
        errors.append(
            "excluded_model_candidates must be a mapping of model_id to reason"
        )
    else:
        for m_id, m_reason in ex_models.items():
            if not isinstance(m_id, str) or not m_id.strip():
                errors.append(
                    "excluded_model_candidates contains invalid/empty model ID"
                )
            if not isinstance(m_reason, str) or not m_reason.strip():
                errors.append(
                    f"excluded_model_candidates entry {m_id!r} requires non-empty explanation string"
                )

    return errors
