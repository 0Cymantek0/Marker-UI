"""PR82 adversarial Quality Lab evaluation package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY: dict[str, str] = {
    # preregistration names
    "PREREGISTERED_QUESTIONS": "preregistration",
    "PREREGISTRATION_SCHEMA_VERSION": "preregistration",
    "DECISION_VOCABULARY": "preregistration",
    "STATUS_VOCABULARY": "preregistration",
    "MODE_VOCABULARY": "preregistration",
    "preregistration_identity": "preregistration",
    "question_by_id": "preregistration",
    # evidence names
    "RELEASE_EVIDENCE_SCHEMA_VERSION": "evidence",
    "PR83_RECOMMENDATION_VOCABULARY": "evidence",
    "ReleaseEvidenceError": "evidence",
    "answer_question": "evidence",
    "validate_release_bundle": "evidence",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    try:
        module_name = _LAZY[name]
    except KeyError as exc:  # pragma: no cover - defensive
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(f"app.eval.pr82.{module_name}")
    return getattr(module, name)
