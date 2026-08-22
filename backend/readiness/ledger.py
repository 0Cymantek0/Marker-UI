"""Readiness evidence-ledger schema v1.

The ledger binds each governing invariant to executable evidence and
records the human gap classification for non-proven invariants. Statuses
are *claims*: the auditor derives the true status from executed evidence
and any claim/derivation mismatch is an integrity failure. Markdown,
comments, screenshots, and manual statements are context only
(``context_docs``) and can never turn an invariant into ``proven``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import LEDGER_SCHEMA

STATUSES = ("proven", "failed", "no_evidence")
GAP_TYPES = ("A", "B", "C", "D", "E", "F", "G")
GAP_TYPE_LABELS = {
    "A": "implementation missing",
    "B": "behavior appears present, executable proof missing",
    "C": "proof exists but cannot currently be trusted (stale/corrupt/unsupported)",
    "D": "evidence valid but narrower than the invariant",
    "E": "compatibility/public boundary unresolved",
    "F": "measurement/economics/operations closure missing",
    "G": "governing applicability needs clarification",
}
BINDING_KINDS = ("test", "measurement", "failure_injection", "conformance")
EXECUTABLE_KINDS = frozenset(BINDING_KINDS)


class LedgerError(ValueError):
    """Raised when the evidence ledger is malformed."""


@dataclass(frozen=True)
class Binding:
    kind: str
    coverage: str
    environment: str
    rationale: str
    nodes: tuple[str, ...] = ()
    scope_files: tuple[str, ...] = ()
    artifact: str | None = None
    artifact_expect: dict = field(default_factory=dict)

    @property
    def key_target(self) -> str:
        if self.kind == "measurement":
            return self.artifact or ""
        return self.nodes[0] if self.nodes else ""

    @property
    def binding_key(self) -> str:
        return f"{self.kind}:{self.key_target}"


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    status_claim: str
    bindings: tuple[Binding, ...]
    gap_type: str | None = None
    gap_note: str | None = None
    context_docs: tuple[str, ...] = ()


def _require_string(value: object, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LedgerError(f"{where} must be a string, got {value!r}")
    if not allow_empty and not value.strip():
        raise LedgerError(f"{where} must be a non-empty string")
    return value


def parse_binding(raw: dict, inv_id: int, index: int) -> Binding:
    where = f"invariant {inv_id} binding #{index}"
    if not isinstance(raw, dict):
        raise LedgerError(f"{where} must be an object")
    kind = raw.get("kind")
    if kind not in BINDING_KINDS:
        raise LedgerError(f"{where} has unsupported kind {kind!r}")
    coverage = raw.get("coverage")
    if coverage not in ("full", "partial"):
        raise LedgerError(f"{where} coverage must be 'full' or 'partial', got {coverage!r}")
    environment = _require_string(raw.get("environment"), f"{where} environment")
    rationale = _require_string(raw.get("rationale"), f"{where} rationale")

    nodes_raw = raw.get("nodes", [])
    if not isinstance(nodes_raw, list) or any(not isinstance(n, str) or not n for n in nodes_raw):
        raise LedgerError(f"{where} nodes must be a list of non-empty strings")
    nodes = tuple(nodes_raw)

    scope_raw = raw.get("scope_files", [])
    if not isinstance(scope_raw, list) or not scope_raw:
        raise LedgerError(f"{where} must declare non-empty scope_files")
    if any(not isinstance(s, str) or not s for s in scope_raw):
        raise LedgerError(f"{where} scope_files must be non-empty strings")
    scope_files = tuple(sorted(set(scope_raw)))

    artifact = raw.get("artifact")
    expect = raw.get("artifact_expect", {})

    if kind == "measurement":
        artifact = _require_string(artifact, f"{where} artifact")
        if not isinstance(expect, dict) or not expect:
            raise LedgerError(
                f"{where} measurement bindings must declare non-empty artifact_expect"
            )
        for pointer, expected in expect.items():
            if not isinstance(pointer, str) or not pointer.strip():
                raise LedgerError(f"{where} artifact_expect keys must be dot-paths")
            if isinstance(expected, dict) or isinstance(expected, list):
                raise LedgerError(
                    f"{where} artifact_expect values must be scalars for exact comparison"
                )
        if artifact not in scope_files:
            raise LedgerError(
                f"{where} measurement artifact must be listed in its own scope_files"
            )
    else:
        if artifact is not None or expect:
            raise LedgerError(f"{where} non-measurement bindings must not declare artifacts")
        if not nodes:
            raise LedgerError(f"{where} executable bindings need at least one pytest node id")
        for node in nodes:
            node_file = node.split("::", 1)[0]
            repo_path = f"backend/{node_file}"
            if repo_path not in scope_files:
                raise LedgerError(
                    f"{where} node {node} targets {repo_path} which is missing from scope_files"
                )

    return Binding(
        kind=kind,
        coverage=coverage,
        environment=environment,
        rationale=rationale,
        nodes=nodes,
        scope_files=scope_files,
        artifact=artifact,
        artifact_expect=dict(expect),
    )


def parse_ledger(data: dict, expected_ids: frozenset[int]) -> tuple[LedgerEntry, ...]:
    if not isinstance(data, dict):
        raise LedgerError("ledger must be a JSON object")
    schema = data.get("schema")
    if schema != LEDGER_SCHEMA:
        raise LedgerError(f"unsupported ledger schema: {schema!r}")
    entries_raw = data.get("invariants")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise LedgerError("ledger must declare a non-empty invariants list")

    seen: set[int] = set()
    entries: list[LedgerEntry] = []
    for raw in entries_raw:
        if not isinstance(raw, dict):
            raise LedgerError("each ledger entry must be an object")
        inv_id = raw.get("id")
        if not isinstance(inv_id, int) or isinstance(inv_id, bool):
            raise LedgerError(f"ledger entry id must be an integer, got {inv_id!r}")
        if inv_id in seen:
            raise LedgerError(f"duplicate ledger invariant id: {inv_id}")
        seen.add(inv_id)
        claim = raw.get("status_claim")
        if claim not in STATUSES:
            raise LedgerError(
                f"invariant {inv_id} status_claim must be one of {STATUSES}, got {claim!r}"
            )
        gap_type = raw.get("gap_type")
        gap_note = raw.get("gap_note")
        if claim == "proven":
            if gap_type is not None or gap_note is not None:
                raise LedgerError(
                    f"invariant {inv_id} claims proven but declares a gap; "
                    "gap fields are only for non-proven invariants"
                )
        else:
            if gap_type not in GAP_TYPES:
                raise LedgerError(
                    f"invariant {inv_id} must classify its gap with gap_type A-G, "
                    f"got {gap_type!r}"
                )
            if not isinstance(gap_note, str) or not gap_note.strip():
                raise LedgerError(f"invariant {inv_id} must explain its gap in gap_note")

        bindings_raw = raw.get("bindings", [])
        if not isinstance(bindings_raw, list):
            raise LedgerError(f"invariant {inv_id} bindings must be a list")
        bindings = tuple(
            parse_binding(raw_binding, inv_id, i)
            for i, raw_binding in enumerate(bindings_raw, start=1)
        )
        if claim == "proven" and not bindings:
            raise LedgerError(
                f"invariant {inv_id} claims proven without any executable binding; "
                "proven cannot rest on context_docs or prose"
            )
        context_raw = raw.get("context_docs", [])
        if not isinstance(context_raw, list) or any(
            not isinstance(c, str) or not c for c in context_raw
        ):
            raise LedgerError(f"invariant {inv_id} context_docs must be a list of paths")

        entries.append(
            LedgerEntry(
                id=inv_id,
                status_claim=claim,
                bindings=bindings,
                gap_type=gap_type,
                gap_note=gap_note,
                context_docs=tuple(context_raw),
            )
        )

    entries.sort(key=lambda entry: entry.id)
    missing = set(expected_ids) - seen
    if missing:
        raise LedgerError(f"ledger is missing governing invariants: {sorted(missing)}")
    unknown = seen - set(expected_ids)
    if unknown:
        raise LedgerError(f"ledger contains non-governing invariant ids: {sorted(unknown)}")
    return tuple(entries)


def load_ledger(path: str | Path, expected_ids: frozenset[int]) -> tuple[LedgerEntry, ...]:
    ledger_path = Path(path)
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot load ledger from {ledger_path}: {exc}") from exc
    return parse_ledger(data, expected_ids)
