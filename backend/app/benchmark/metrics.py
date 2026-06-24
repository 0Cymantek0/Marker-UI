"""Dependency-free scoring metrics for the image-pipeline benchmark (plan §9.4).

Every metric here is pure Python (no Levenshtein C-ext, no lxml, no model) so
the regression gate runs anywhere the test suite runs. They are deliberately
simple, well-defined, and unit-pinned — the point is a *stable, comparable*
number across engine configs, not a research-grade scorer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Edit distance (shared by CER/WER)
# ---------------------------------------------------------------------------

def _levenshtein(a: list[Any], b: list[Any]) -> int:
    """Levenshtein edit distance between two sequences (tokens or chars)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Character Error Rate = edit_distance(chars) / len(reference chars).

    Returns 0.0 when both are empty; 1.0 when only the reference is empty but
    the hypothesis is not (pure insertion). Clamped to [0, 1] (a hypothesis far
    longer than the reference cannot score worse than "completely wrong").
    """
    ref = reference or ""
    hyp = hypothesis or ""
    if not ref and not hyp:
        return 0.0
    if not ref:
        return 1.0
    dist = _levenshtein(list(ref), list(hyp))
    return min(1.0, dist / len(ref))


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word Error Rate = edit_distance(words) / len(reference words)."""
    ref = (reference or "").split()
    hyp = (hypothesis or "").split()
    if not ref and not hyp:
        return 0.0
    if not ref:
        return 1.0
    dist = _levenshtein(ref, hyp)
    return min(1.0, dist / len(ref))


# Short aliases retained for callers that prefer the terse names.
cer = character_error_rate
wer = word_error_rate

# ---------------------------------------------------------------------------
# TEDS-lite (table structure similarity)
# ---------------------------------------------------------------------------

def _normalize_cell(value: Any) -> str:
    return " ".join(str(value or "").split())


def _parse_table(table: Any) -> list[list[str]]:
    """Normalise a table into a list-of-rows-of-cell-strings.

    Accepts either a ``{"headers": [...], "rows": [[...]]}`` dict (our
    TablePayload shape) or a raw list-of-rows. Cells are stripped strings.
    """
    if isinstance(table, dict):
        if isinstance(table.get("grid"), list):
            grid = [list(r) for r in table["grid"]]
        else:
            headers = table.get("headers") or []
            rows = table.get("rows") or []
            grid = ([list(headers)] if headers else []) + [list(r) for r in rows]
    elif isinstance(table, list):
        grid = [list(r) for r in table]
    else:
        grid = []
    return [[_normalize_cell(c) for c in row] for row in grid]


def _looks_like_single_table(table: Any) -> bool:
    if isinstance(table, dict):
        return bool({"headers", "rows", "grid"} & set(table))
    if not isinstance(table, list):
        return False
    if not table:
        return True
    first = table[0]
    return not (
        isinstance(first, dict)
        or (
            isinstance(first, list)
            and first
            and isinstance(first[0], (dict, list))
        )
    )


def _table_collection(table: Any) -> list[Any]:
    if table is None:
        return []
    if isinstance(table, dict) and isinstance(table.get("tables"), list):
        return list(table["tables"])
    if _looks_like_single_table(table):
        return [table]
    if isinstance(table, list):
        return list(table)
    return []


def _single_table_similarity(reference: Any, hypothesis: Any) -> float:
    ref = _parse_table(reference)
    hyp = _parse_table(hypothesis)
    if not ref and not hyp:
        return 1.0
    if not ref or not hyp:
        return 0.0

    hyp_rows = len(hyp)
    total = 0
    matched = 0
    for r, ref_row in enumerate(ref):
        for c, ref_cell in enumerate(ref_row):
            total += 1
            hyp_cell = hyp[r][c] if r < hyp_rows and c < len(hyp[r]) else ""
            if ref_cell == hyp_cell:
                matched += 1
    return round(matched / total, 6) if total else 0.0


def table_similarity(reference: Any, hypothesis: Any) -> float:
    """Cell-match recall for table collections in [0, 1].

    Scores the fraction of **reference** cells reproduced at the same (row, col)
    in the hypothesis. When multiple reference tables exist, each reference
    table is matched to the best remaining hypothesis table and averaged. This
    keeps table-heavy documents from depending on incidental prose or table
    ordering while still requiring the structured cells to be present.
    """
    ref_tables = _table_collection(reference)
    hyp_tables = _table_collection(hypothesis)
    if not ref_tables and not hyp_tables:
        return 1.0
    if not ref_tables or not hyp_tables:
        return 0.0

    unused = set(range(len(hyp_tables)))
    scores: list[float] = []
    for ref_table in ref_tables:
        ranked = sorted(
            (
                (_single_table_similarity(ref_table, hyp_tables[hyp_index]), hyp_index)
                for hyp_index in unused
            ),
            reverse=True,
        )
        if not ranked:
            scores.append(0.0)
            continue
        score, hyp_index = ranked[0]
        unused.remove(hyp_index)
        scores.append(score)
    return round(sum(scores) / len(scores), 6) if scores else 0.0


# ---------------------------------------------------------------------------
# Facts-based recall (ignores cosmetic markdown differences)
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*%?")


def _fact_tokens(text: str) -> set[str]:
    """Extract machine-checkable numeric facts from text.

    Only numbers (integers, decimals, percentages) are treated as checkable
    facts — these are what a chart→table or OCR extraction must get *exactly*
    right. Cosmetic markdown (``*``, ``#``, ``|``, ``-`` separators, prose
    wording, whitespace) is irrelevant and discarded, so a structurally
    different but factually equal output is not penalised (plan §9.4).
    """
    return {m.group(0).rstrip("%") for m in _NUMBER_RE.finditer(text or "")}


def facts_recall(reference: str, hypothesis: str) -> float:
    """Recall of the reference's numeric facts present in the hypothesis.

    Returns the fraction of reference numbers that also appear in the
    hypothesis. 1.0 when the reference has no numeric facts (nothing to miss —
    the text metric carries that sample).
    """
    ref_facts = _fact_tokens(reference)
    if not ref_facts:
        return 1.0
    hyp_facts = _fact_tokens(hypothesis)
    return len(ref_facts & hyp_facts) / len(ref_facts)


# ---------------------------------------------------------------------------
# Combined per-sample score
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkScore:
    """Scored result for one benchmark sample."""

    sample_id: str
    cer: float = 0.0
    wer: float = 0.0
    table_score: float | None = None
    facts: float = 0.0
    combined: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


def score_outputs(
    sample_id: str,
    reference_text: str,
    hypothesis_text: str,
    reference_table: Any = None,
    hypothesis_table: Any = None,
) -> BenchmarkScore:
    """Score one sample across all metrics and blend into a single number.

    Text-only samples use text accuracy (``1 - CER``), word accuracy
    (``1 - WER``), and fact recall. Table samples use structure and fact recall
    as the primary channels because prose renderings and Markdown tables are
    semantically equivalent but text-edit metrics treat them as unrelated.
    """
    c = character_error_rate(reference_text, hypothesis_text)
    w = word_error_rate(reference_text, hypothesis_text)
    f = facts_recall(reference_text, hypothesis_text)
    t: float | None = None
    if reference_table is not None or hypothesis_table is not None:
        t = table_similarity(reference_table, hypothesis_table)

    if t is not None:
        components = [t, f]
        scoring_mode = "table_structured"
    else:
        components = [1.0 - c, 1.0 - w, f]
        scoring_mode = "text"
    combined = round(sum(components) / len(components), 6)

    return BenchmarkScore(
        sample_id=sample_id,
        cer=round(c, 6),
        wer=round(w, 6),
        table_score=t,
        facts=f,
        combined=combined,
        details={
            "reference_len": len(reference_text or ""),
            "hypothesis_len": len(hypothesis_text or ""),
            "scoring_mode": scoring_mode,
        },
    )
