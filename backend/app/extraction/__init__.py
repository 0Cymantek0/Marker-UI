"""Evidence-backed structured extraction (PR80A).

One versioned extraction program over published kernel evidence:
anchor-grounded candidates, deterministic validation, versioned
reconciliation, honest missing/unresolved/review outcomes, and a
review seam with stale-context protection. Accepted values commit as
kernel claim/assessment/proof-support records — extraction never
creates a second truth or proof authority.

The slice's contract, non-claims, and reproduction evidence are
documented in ``docs/reference/evidence-backed-extraction.md``.
"""

from __future__ import annotations

import importlib
from typing import Any

# Exports resolve lazily (PEP 562), mirroring the kernel package: the
# pure contract modules (schema, results, reconciliation) must stay
# importable without dragging the ORM/service stack along.

_EXPORT_MODULES: dict[str, str] = {
    "CandidateSet": "app.extraction.extractor",
    "CandidateView": "app.extraction.results",
    "EvidenceCitation": "app.extraction.results",
    "ExtractionContext": "app.extraction.results",
    "ExtractionRequest": "app.extraction.contract",
    "ExtractionRequestError": "app.extraction.contract",
    "ExtractionResult": "app.extraction.results",
    "ExtractionSchema": "app.extraction.schema",
    "ExtractionSchemaError": "app.extraction.schema",
    "ExtractionService": "app.extraction.service",
    "FieldOutcome": "app.extraction.results",
    "InvariantFinding": "app.extraction.results",
    "ItemOutcome": "app.extraction.results",
    "ModelIdentity": "app.extraction.provider",
    "OpenAICompatProvider": "app.extraction.provider",
    "ProposalView": "app.extraction.results",
    "ProviderResult": "app.extraction.provider",
    "ReplayProvider": "app.extraction.provider",
    "ReviewDecision": "app.extraction.review",
    "ReviewError": "app.extraction.review",
    "SpecialistLane": "app.extraction.specialist",
    "SpecialistLaneReport": "app.extraction.results",
    "SpecialistLaneResult": "app.extraction.specialist",
    "SpecialistProposal": "app.extraction.specialist",
    "SpecialistProvenance": "app.extraction.results",
    "SpecialistProvider": "app.extraction.provider",
    "SpecialistRuntime": "app.extraction.results",
    "StaleReviewError": "app.extraction.review",
    "HYBRID_POLICY_ID": "app.extraction.reconciliation",
    "HYBRID_POLICY_VERSION": "app.extraction.reconciliation",
    "INVOICE_SCHEMA": "app.extraction.contract",
    "RECONCILE_POLICY_ID": "app.extraction.reconciliation",
    "RECONCILE_POLICY_VERSION": "app.extraction.reconciliation",
    "extract_candidates": "app.extraction.extractor",
    "parse_typed": "app.extraction.validation",
    "reconcile": "app.extraction.reconciliation",
    "register_schema": "app.extraction.contract",
    "resolve_schema": "app.extraction.contract",
    "result_from_dict": "app.extraction.results",
}

__all__ = sorted(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    module_path = _EXPORT_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    return getattr(module, name)
