"""Readiness auditor: validate evidence bindings and derive statuses.

Trust boundary: humans assert *coverage* (full/partial + rationale);
the machine verifies execution, freshness, identity, and result
semantics. A ``proven`` status always traces to an executed binding
whose evidence identity still matches the current tree. Prose, docs,
and unexecuted or skipped strict proofs can never produce ``proven``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import EVIDENCE_RUN_SCHEMA
from .gitmeta import GitMeta, StaticResolver
from .inventory import Invariant
from .ledger import LedgerEntry

VERDICT_READY = "READY"
VERDICT_NOT_READY = "NOT_READY"

STATUS_PROVEN = "proven"
STATUS_FAILED = "failed"
STATUS_NO_EVIDENCE = "no_evidence"

REASON_NONE_BOUND = "none_bound"
REASON_STALE_OR_INVALID = "stale_or_invalid_evidence"
REASON_PARTIAL_ONLY = "partial_coverage_only"
REASON_ENV_LIMITED = "environment_limited"
REASON_DOCS_ONLY = "docs_only_no_executable_binding"


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning"
    code: str
    message: str


@dataclass(frozen=True)
class BindingEvaluation:
    binding_key: str
    valid: bool
    outcome: str | None  # passed | failed | skipped_env_gated | None when invalid
    support: str  # full | partial | none | failure
    stale_reason: str | None = None


@dataclass(frozen=True)
class InvariantResult:
    id: int
    group: str
    group_name: str
    label: str
    derived_status: str
    reason: str | None
    environments: tuple[str, ...]
    gap_type: str | None
    gap_note: str | None
    bindings: tuple[BindingEvaluation, ...]
    context_docs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditResult:
    verdict: str
    git_head: str
    invariants: tuple[InvariantResult, ...]
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def counts(self) -> dict[str, int]:
        counts = {STATUS_PROVEN: 0, STATUS_FAILED: 0, STATUS_NO_EVIDENCE: 0}
        for result in self.invariants:
            counts[result.derived_status] += 1
        return counts


def _resolve_dot_path(document: object, pointer: str):
    """Resolve a ``a.b.c`` dot-path; returns (found, value)."""

    current = document
    for part in pointer.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _binding_key(inv_id: int, binding) -> str:
    return f"{inv_id}:{binding.binding_key}"


def _check_measurement_expectations(
    entry: LedgerEntry, binding, artifact_path: Path, findings: list[Finding]
) -> bool:
    try:
        document = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            Finding(
                "error",
                "artifact_unreadable",
                f"invariant {entry.id}: measurement artifact {binding.artifact} "
                f"cannot be parsed: {exc}",
            )
        )
        return False
    ok = True
    for pointer, expected in binding.artifact_expect.items():
        found, actual = _resolve_dot_path(document, pointer)
        if not found or actual != expected:
            findings.append(
                Finding(
                    "error",
                    "artifact_expectation_mismatch",
                    f"invariant {entry.id}: artifact {binding.artifact} field "
                    f"{pointer!r} expected {expected!r}, found {actual!r}",
                )
            )
            ok = False
    return ok


class Auditor:
    """Derives readiness statuses from the ledger + executed evidence run."""

    def __init__(self, resolver: GitMeta | StaticResolver, repo_root: Path) -> None:
        self._resolver = resolver
        self._repo_root = Path(repo_root)

    def audit(
        self,
        inventory: list[Invariant],
        ledger_entries: tuple[LedgerEntry, ...],
        evidence_run: dict,
    ) -> AuditResult:
        findings: list[Finding] = []
        self._check_evidence_run(evidence_run, ledger_entries, findings)

        results_by_key = {
            result.get("binding_key"): result
            for result in evidence_run.get("results", [])
            if isinstance(result, dict)
        }

        all_scope_files = sorted(
            {
                scope
                for entry in ledger_entries
                for binding in entry.bindings
                for scope in binding.scope_files
            }
        )
        tracked_map = self._resolver.tracked(all_scope_files)
        sha_map = self._resolver.content_shas(
            [path for path in all_scope_files if tracked_map.get(path)]
        )

        invariant_results: list[InvariantResult] = []
        inventory_by_id = {inv.id: inv for inv in inventory}
        for entry in ledger_entries:
            invariant = inventory_by_id[entry.id]
            invariant_results.append(
                self._evaluate_entry(
                    entry, invariant, results_by_key, tracked_map, sha_map, findings
                )
            )

        counts = {STATUS_PROVEN: 0, STATUS_FAILED: 0, STATUS_NO_EVIDENCE: 0}
        for result in invariant_results:
            counts[result.derived_status] += 1
        verdict = (
            VERDICT_READY
            if counts[STATUS_PROVEN] == len(invariant_results)
            else VERDICT_NOT_READY
        )
        return AuditResult(
            verdict=verdict,
            git_head=evidence_run.get("git_head", ""),
            invariants=tuple(invariant_results),
            findings=tuple(findings),
        )

    # ------------------------------------------------------------------
    # structural evidence-run checks
    # ------------------------------------------------------------------

    def _check_evidence_run(
        self, evidence_run: dict, ledger_entries: tuple[LedgerEntry, ...], findings: list[Finding]
    ) -> None:
        if not isinstance(evidence_run, dict):
            findings.append(
                Finding("error", "evidence_run_malformed", "evidence run must be a JSON object")
            )
            return
        schema = evidence_run.get("schema")
        if schema != EVIDENCE_RUN_SCHEMA:
            findings.append(
                Finding(
                    "error",
                    "evidence_run_schema",
                    f"unsupported evidence-run schema: {schema!r}",
                )
            )
            return
        if not isinstance(evidence_run.get("git_head"), str) or not evidence_run.get("git_head"):
            findings.append(
                Finding("error", "evidence_run_identity", "evidence run must record git_head")
            )
        results = evidence_run.get("results")
        if not isinstance(results, list):
            findings.append(
                Finding("error", "evidence_run_results", "evidence run must carry a results list")
            )
            return

        expected_keys = {
            _binding_key(entry.id, binding)
            for entry in ledger_entries
            for binding in entry.bindings
        }
        actual_keys: set[str] = set()
        for result in results:
            if not isinstance(result, dict):
                findings.append(
                    Finding("error", "evidence_run_result", "each result must be an object")
                )
                continue
            key = result.get("binding_key")
            outcome = result.get("outcome")
            if outcome not in ("passed", "failed", "skipped_env_gated"):
                findings.append(
                    Finding(
                        "error",
                        "evidence_run_outcome",
                        f"result {key!r} has unsupported outcome {outcome!r}",
                    )
                )
            if not isinstance(result.get("scope_blobs"), dict) or not result.get("scope_blobs"):
                findings.append(
                    Finding(
                        "error",
                        "evidence_run_scope",
                        f"result {key!r} must record non-empty scope_blobs",
                    )
                )
            if key in actual_keys:
                findings.append(
                    Finding(
                        "error", "evidence_run_duplicate", f"duplicate result key {key!r}"
                    )
                )
            actual_keys.add(key)

        for missing in sorted(expected_keys - actual_keys):
            findings.append(
                Finding(
                    "error",
                    "dangling_binding",
                    f"ledger binding {missing!r} has no executed result in the evidence run",
                )
            )
        for orphan in sorted(actual_keys - expected_keys):
            findings.append(
                Finding(
                    "error",
                    "orphan_result",
                    f"evidence run result {orphan!r} does not correspond to any ledger binding",
                )
            )

    # ------------------------------------------------------------------
    # per-invariant derivation
    # ------------------------------------------------------------------

    def _evaluate_entry(
        self,
        entry: LedgerEntry,
        invariant: Invariant,
        results_by_key: dict,
        tracked_map: dict,
        sha_map: dict,
        findings: list[Finding],
    ) -> InvariantResult:
        evaluations: list[BindingEvaluation] = []
        for binding in entry.bindings:
            evaluations.append(
                self._evaluate_binding(entry, binding, results_by_key, tracked_map, sha_map, findings)
            )

        derived, reason = self._derive_status(entry, evaluations, findings)
        environments: list[str] = []
        for binding, evaluation in zip(entry.bindings, evaluations):
            if evaluation.valid and evaluation.support == "full":
                if binding.environment not in environments:
                    environments.append(binding.environment)

        return InvariantResult(
            id=invariant.id,
            group=invariant.group,
            group_name=invariant.group_name,
            label=invariant.label,
            derived_status=derived,
            reason=reason,
            environments=tuple(environments),
            gap_type=entry.gap_type,
            gap_note=entry.gap_note,
            bindings=tuple(evaluations),
            context_docs=entry.context_docs,
        )

    def _evaluate_binding(
        self,
        entry: LedgerEntry,
        binding,
        results_by_key: dict,
        tracked_map: dict,
        sha_map: dict,
        findings: list[Finding],
    ) -> BindingEvaluation:
        key = _binding_key(entry.id, binding)
        result = results_by_key.get(key)
        if result is None:
            return BindingEvaluation(key, False, None, "none", "no executed result")

        # freshness: tracked + working-tree content identity unchanged
        for scope in binding.scope_files:
            if not tracked_map.get(scope, False):
                findings.append(
                    Finding(
                        "error",
                        "untracked_scope",
                        f"invariant {entry.id}: scope file {scope} is not tracked by git; "
                        "untracked evidence has no stable identity",
                    )
                )
                return BindingEvaluation(key, False, None, "none", f"untracked scope {scope}")
            if scope not in sha_map:
                return BindingEvaluation(key, False, None, "none", f"missing scope {scope}")
            if sha_map[scope] != result.get("scope_blobs", {}).get(scope):
                findings.append(
                    Finding(
                        "error",
                        "stale_scope",
                        f"invariant {entry.id}: scope file {scope} changed since the "
                        "evidence run; the recorded result certifies a different subject",
                    )
                )
                return BindingEvaluation(key, False, None, "none", f"stale scope {scope}")

        if binding.kind == "measurement":
            artifact_path = self._repo_root / binding.artifact
            if not artifact_path.is_file():
                findings.append(
                    Finding(
                        "error",
                        "missing_artifact",
                        f"invariant {entry.id}: measurement artifact {binding.artifact} is missing",
                    )
                )
                return BindingEvaluation(key, False, None, "none", "missing artifact")
            digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            if digest != result.get("artifact_sha256"):
                findings.append(
                    Finding(
                        "error",
                        "artifact_digest_mismatch",
                        f"invariant {entry.id}: measurement artifact {binding.artifact} "
                        "content no longer matches the digested evidence",
                    )
                )
                return BindingEvaluation(key, False, None, "none", "artifact digest mismatch")
            if not _check_measurement_expectations(entry, binding, artifact_path, findings):
                return BindingEvaluation(key, False, None, "none", "artifact expectation mismatch")

        outcome = result.get("outcome")
        if outcome == "failed":
            support = "failure"
        elif outcome == "passed":
            support = "full" if binding.coverage == "full" else "partial"
        else:
            support = "none"
        return BindingEvaluation(key, True, outcome, support)

    def _derive_status(
        self,
        entry: LedgerEntry,
        evaluations: list[BindingEvaluation],
        findings: list[Finding],
    ) -> tuple[str, str | None]:
        executable = [e for e in evaluations if e.valid]
        if any(e.support == "failure" for e in executable):
            derived, reason = STATUS_FAILED, None
        elif any(e.support == "full" for e in executable):
            derived, reason = STATUS_PROVEN, None
        elif not entry.bindings:
            derived = STATUS_NO_EVIDENCE
            reason = (
                REASON_DOCS_ONLY
                if entry.context_docs
                else REASON_NONE_BOUND
            )
        elif executable and any(
            e.outcome == "skipped_env_gated" for e in executable
        ):
            derived, reason = STATUS_NO_EVIDENCE, REASON_ENV_LIMITED
        elif executable and any(e.support == "partial" for e in executable):
            derived, reason = STATUS_NO_EVIDENCE, REASON_PARTIAL_ONLY
        elif evaluations:
            derived, reason = STATUS_NO_EVIDENCE, REASON_STALE_OR_INVALID
        else:
            derived, reason = STATUS_NO_EVIDENCE, REASON_NONE_BOUND

        if derived != entry.status_claim:
            findings.append(
                Finding(
                    "error",
                    "claim_mismatch",
                    f"invariant {entry.id}: ledger claims {entry.status_claim!r} but the "
                    f"evidence derives {derived!r}"
                    + (f" (reason: {reason})" if reason else ""),
                )
            )
        return derived, reason
