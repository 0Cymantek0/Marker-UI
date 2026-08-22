"""Machine and human readiness reports from one derived audit result.

Reports are pure functions of the audit result — no wall-clock, no
environment noise — so regeneration from unchanged inputs is
byte-identical, and any hand-edit of a committed report is detectable.
"""

from __future__ import annotations

import json

from . import REPORT_SCHEMA
from .auditor import (
    REASON_DOCS_ONLY,
    REASON_ENV_LIMITED,
    REASON_NONE_BOUND,
    REASON_PARTIAL_ONLY,
    REASON_STALE_OR_INVALID,
    STATUS_FAILED,
    STATUS_NO_EVIDENCE,
    STATUS_PROVEN,
    AuditResult,
)
from .ledger import GAP_TYPE_LABELS

REASON_LABELS = {
    REASON_NONE_BOUND: "no executable evidence bound",
    REASON_DOCS_ONLY: "only non-executable context (docs/prose) bound",
    REASON_STALE_OR_INVALID: "bound proof is stale or invalid for the current tree",
    REASON_PARTIAL_ONLY: "executed proof covers only part of the invariant wording",
    REASON_ENV_LIMITED: "proof exists but was environment-gated in the recorded run",
}


def build_machine_report(audit: AuditResult) -> dict:
    group_counts: dict[str, dict[str, int]] = {}
    for result in audit.invariants:
        bucket = group_counts.setdefault(
            result.group, {"proven": 0, "failed": 0, "no_evidence": 0, "total": 0}
        )
        bucket[result.derived_status] += 1
        bucket["total"] += 1

    gap_type_counts: dict[str, int] = {}
    for result in audit.invariants:
        if result.derived_status != STATUS_PROVEN and result.gap_type:
            gap_type_counts[result.gap_type] = gap_type_counts.get(result.gap_type, 0) + 1

    ranked_groups = sorted(
        (
            group
            for group in group_counts
            if group_counts[group][STATUS_NO_EVIDENCE] or group_counts[group][STATUS_FAILED]
        ),
        key=lambda g: (
            -(group_counts[g][STATUS_NO_EVIDENCE] + group_counts[g][STATUS_FAILED]),
            g,
        ),
    )

    return {
        "schema": REPORT_SCHEMA,
        "subject": {"git_head": audit.git_head},
        "verdict": audit.verdict,
        "counts": audit.counts,
        "group_counts": {g: group_counts[g] for g in sorted(group_counts)},
        "gap_type_counts": {t: gap_type_counts[t] for t in sorted(gap_type_counts)},
        "residual_gap_ranking": ranked_groups,
        "integrity_findings": [
            {"severity": f.severity, "code": f.code, "message": f.message}
            for f in audit.findings
        ],
        "invariants": [
            {
                "id": r.id,
                "group": r.group,
                "label": r.label,
                "status": r.derived_status,
                "reason": r.reason,
                "environments": list(r.environments),
                "gap_type": r.gap_type,
                "gap_note": r.gap_note,
            }
            for r in audit.invariants
        ],
    }


def render_machine_report(audit: AuditResult) -> str:
    return json.dumps(build_machine_report(audit), indent=2, sort_keys=False, ensure_ascii=False) + "\n"


_STATUS_MARKS = {
    STATUS_PROVEN: "proven",
    STATUS_FAILED: "FAILED",
    STATUS_NO_EVIDENCE: "no-evidence",
}


def render_markdown_report(audit: AuditResult) -> str:
    counts = audit.counts
    lines: list[str] = []
    lines.append("# PR84A V3.2 Readiness Report")
    lines.append("")
    lines.append(f"**Overall verdict: {audit.verdict}** (mechanically derived; never hand-set)")
    lines.append("")
    lines.append(f"- Audited source head: `{audit.git_head}`")
    lines.append(f"- Invariants proven: **{counts[STATUS_PROVEN]} / 62**")
    lines.append(f"- Failed: **{counts[STATUS_FAILED]}**")
    lines.append(f"- No acceptable evidence: **{counts[STATUS_NO_EVIDENCE]}**")
    if audit.errors:
        lines.append(f"- Integrity errors: **{len(audit.errors)}** (see machine report)")
    lines.append("")

    lines.append("## Group summary")
    lines.append("")
    lines.append("| Group | Name | Proven | Failed | No evidence |")
    lines.append("|---|---|---:|---:|---:|")
    group_counts: dict[str, dict[str, int]] = {}
    for result in audit.invariants:
        bucket = group_counts.setdefault(
            result.group, {"proven": 0, "failed": 0, "no_evidence": 0}
        )
        bucket[result.derived_status] += 1
    for group in sorted(group_counts):
        names = {r.group_name for r in audit.invariants if r.group == group}
        bucket = group_counts[group]
        lines.append(
            f"| {group} | {next(iter(names))} | {bucket['proven']} | "
            f"{bucket['failed']} | {bucket['no_evidence']} |"
        )
    lines.append("")

    lines.append("## Invariant statuses")
    lines.append("")
    lines.append("| ID | Group | Status | Evidence environments / reason |")
    lines.append("|---:|---|---|---|")
    for result in audit.invariants:
        if result.derived_status == STATUS_PROVEN:
            detail = ", ".join(result.environments)
        elif result.reason:
            detail = REASON_LABELS.get(result.reason, result.reason)
        else:
            detail = ""
        lines.append(
            f"| {result.id} | {result.group} | {_STATUS_MARKS[result.derived_status]} | {detail} |"
        )
    lines.append("")

    non_proven = [r for r in audit.invariants if r.derived_status != STATUS_PROVEN]
    if non_proven:
        lines.append("## Residual gap map")
        lines.append("")
        lines.append("Gap types: " + "; ".join(f"**{t}** — {GAP_TYPE_LABELS[t]}" for t in sorted(GAP_TYPE_LABELS)))
        lines.append("")
        by_type: dict[str, list] = {}
        for result in non_proven:
            by_type.setdefault(result.gap_type or "?", []).append(result)
        for gap_type in sorted(by_type):
            lines.append(f"### Type {gap_type} — {GAP_TYPE_LABELS.get(gap_type, 'unclassified')}")
            lines.append("")
            for result in by_type[gap_type]:
                reason = (
                    f" Reason: {REASON_LABELS.get(result.reason, result.reason)}."
                    if result.reason
                    else ""
                )
                lines.append(
                    f"- **Inv {result.id}** ({result.group}, {result.label}): "
                    f"{result.gap_note}{reason}"
                )
            lines.append("")

        lines.append("### Next-slice ranking (groups with most non-proven invariants)")
        lines.append("")
        ranked = sorted(
            group_counts,
            key=lambda g: (
                -(group_counts[g]["no_evidence"] + group_counts[g]["failed"]),
                g,
            ),
        )
        for group in ranked:
            bucket = group_counts[group]
            gap = bucket["no_evidence"] + bucket["failed"]
            if gap == 0:
                continue
            names = {r.group_name for r in audit.invariants if r.group == group}
            lines.append(f"- {group} {next(iter(names))}: {gap} non-proven")
        lines.append("")

    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append("# from repository root")
    lines.append("python backend/scripts/readiness_audit.py --mode integrity")
    lines.append("# re-execute bound evidence and regenerate the snapshot:")
    lines.append("python backend/scripts/readiness_audit.py --mode run-evidence")
    lines.append("```")
    lines.append("")
    lines.append(
        "This report is generated from the canonical ledger and executed evidence; "
        "manual edits are detected by `--mode integrity`. An honest NOT READY verdict "
        "with valid evidence integrity is an accepted repository state."
    )
    lines.append("")
    return "\n".join(lines)
