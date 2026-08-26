"""Evidence runner: execute ledger bindings and record machine results.

Test-family bindings run once as a single batched pytest invocation
(junit XML gives per-node outcomes, including environment-gated skips).
Measurement bindings are digested and their expected proving values are
re-checked against the artifact content. The written evidence-run
artifact is the only execution record the auditor trusts.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import EVIDENCE_RUN_SCHEMA
from .auditor import _binding_key, _resolve_dot_path
from .gitmeta import GitMeta
from .ledger import LedgerEntry

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped_env_gated"


class EvidenceRunError(RuntimeError):
    """Raised when the evidence run cannot be produced honestly."""


def _node_matches(node: str, collected_id: str) -> bool:
    """A bound node selects its parametrized children (``test_x`` covers
    ``test_x[param]``) and class children (``TestCls`` covers
    ``TestCls::test_x``) exactly as pytest argument selection does."""

    return (
        collected_id == node
        or collected_id.startswith(node + "[")
        or collected_id.startswith(node + "::")
    )


def _junit_node_outcomes(junit_path: Path) -> dict[str, dict[str, str]]:
    """Map pytest node id -> {"outcome": ..., "detail": ...} from junit XML."""

    root = ET.parse(junit_path).getroot()
    outcomes: dict[str, dict[str, str]] = {}
    for testcase in root.iter("testcase"):
        file_attr = testcase.get("file")
        name = testcase.get("name", "")
        classname = testcase.get("classname", "")
        if not file_attr or not name:
            continue
        node = file_attr.replace("\\", "/")
        module = node[:-3].replace("/", ".") if node.endswith(".py") else node.replace("/", ".")
        if classname.startswith(module + "."):
            tail = classname[len(module) + 1 :]
            node = f"{node}::{tail}::{name}"
        else:
            node = f"{node}::{name}"

        child_tags = {child.tag: child for child in testcase}
        if "failure" in child_tags or "error" in child_tags:
            tag = child_tags.get("error") or child_tags.get("failure")
            outcomes[node] = {"outcome": OUTCOME_FAILED, "detail": (tag.text or "")[:300]}
        elif "skipped" in child_tags:
            skipped = child_tags["skipped"]
            outcomes[node] = {
                "outcome": OUTCOME_SKIPPED,
                "detail": (skipped.get("message") or skipped.text or "")[:300],
            }
        else:
            outcomes[node] = {"outcome": OUTCOME_PASSED, "detail": ""}
    return outcomes


class EvidenceRunner:
    def __init__(self, repo_root: Path, ledger_entries: tuple[LedgerEntry, ...]) -> None:
        self._repo_root = Path(repo_root)
        self._entries = ledger_entries
        self._meta = GitMeta(self._repo_root)

    def run(self) -> dict:
        test_nodes = self._collect_test_nodes()
        node_outcomes: dict[str, dict[str, str]] = {}
        if test_nodes:
            node_outcomes = self._run_pytest(test_nodes)

        all_scope = sorted(
            {scope for entry in self._entries for binding in entry.bindings for scope in binding.scope_files}
        )
        sha_map = self._meta.content_shas(all_scope)

        results = []
        for entry in self._entries:
            for binding in entry.bindings:
                results.append(
                    self._result_for_binding(entry, binding, node_outcomes, sha_map)
                )

        return {
            "schema": EVIDENCE_RUN_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_head": self._meta.head(),
            "runner": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "results": sorted(results, key=lambda r: r["binding_key"]),
        }

    # ------------------------------------------------------------------

    def _collect_test_nodes(self) -> list[str]:
        nodes: list[str] = []
        seen: set[str] = set()
        for entry in self._entries:
            for binding in entry.bindings:
                if binding.kind == "measurement":
                    continue
                for node in binding.nodes:
                    if node not in seen:
                        seen.add(node)
                        nodes.append(node)
        return nodes

    def _run_pytest(self, nodes: list[str]) -> dict[str, dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="pr84a-junit-") as tmp:
            junit_path = Path(tmp) / "junit.xml"
            # The full bound-node population exceeds the Windows CreateProcess
            # 32k command-line cap, so node ids travel via a pytest @argsfile
            # (one argument per line) instead of inline argv.
            args_path = Path(tmp) / "pytest-nodes.txt"
            args_path.write_text("\n".join(nodes) + "\n", encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    f"@{args_path}",
                    f"--junit-xml={junit_path}",
                    "-o",
                    "junit_family=xunit1",
                    "-q",
                    "--no-header",
                    "-p",
                    "no:cacheprovider",
                    "--tb=no",
                ],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if not junit_path.is_file():
                raise EvidenceRunError(
                    f"pytest did not produce a junit report (exit {proc.returncode}): "
                    f"{proc.stdout[-2000:]} {proc.stderr[-2000:]}"
                )
            outcomes = _junit_node_outcomes(junit_path)
        for node in nodes:
            if not any(_node_matches(node, collected) for collected in outcomes):
                raise EvidenceRunError(f"pytest outcomes missing for bound node: {node}")
        return outcomes

    def _result_for_binding(
        self,
        entry: LedgerEntry,
        binding,
        node_outcomes: dict[str, dict[str, str]],
        sha_map: dict[str, str],
    ) -> dict:
        scope_blobs = {scope: sha_map[scope] for scope in binding.scope_files if scope in sha_map}
        result: dict = {
            "binding_key": _binding_key(entry.id, binding),
            "kind": binding.kind,
            "scope_blobs": scope_blobs,
        }

        if binding.kind == "measurement":
            artifact_path = self._repo_root / binding.artifact
            result["artifact"] = binding.artifact
            result["artifact_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            document = json.loads(artifact_path.read_text(encoding="utf-8"))
            checks = {}
            all_ok = True
            for pointer, expected in binding.artifact_expect.items():
                found, actual = _resolve_dot_path(document, pointer)
                ok = found and actual == expected
                all_ok = all_ok and ok
                checks[pointer] = {"expected": expected, "actual": actual if found else None, "ok": ok}
            result["checks"] = checks
            result["outcome"] = OUTCOME_PASSED if all_ok else OUTCOME_FAILED
            result["detail"] = "" if all_ok else "artifact expectation mismatch"
            return result

        node_results: dict[str, dict[str, str]] = {}
        for node in binding.nodes:
            for collected, outcome in node_outcomes.items():
                if _node_matches(node, collected):
                    node_results[collected] = outcome
        result["nodes"] = list(binding.nodes)
        result["node_outcomes"] = {node: nr["outcome"] for node, nr in node_results.items()}
        outcomes = [nr["outcome"] for nr in node_results.values()]
        if OUTCOME_FAILED in outcomes:
            result["outcome"] = OUTCOME_FAILED
            result["detail"] = "; ".join(
                f"{node}: {nr['detail']}" for node, nr in node_results.items() if nr["outcome"] == OUTCOME_FAILED
            )[:600]
        elif outcomes and all(o == OUTCOME_PASSED for o in outcomes):
            result["outcome"] = OUTCOME_PASSED
            result["detail"] = ""
        else:
            result["outcome"] = OUTCOME_SKIPPED
            result["detail"] = "; ".join(
                f"{node}: {nr['detail']}" for node, nr in node_results.items() if nr["outcome"] == OUTCOME_SKIPPED
            )[:600]
        return result
