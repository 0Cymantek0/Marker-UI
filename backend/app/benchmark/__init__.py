"""Image-pipeline benchmark harness (plan §9.4).

The §9.4 gate: any engine swap (Surya -> GLM-OCR / PaddleOCR-VL / Mistral OCR)
must be justified by measured quality on the real corpus, never by spec-sheet
faith. This package provides the *scoring* half of that gate — pure-Python,
dependency-free metrics so a swap can be A/B-scored locally:

  * ``cer`` / ``wer`` — character / word error rate for plain text (OCR quality).
  * ``teds`` — Tree-Edit-Distance-based Similarity for table structure.
  * ``facts_score`` — machine-checkable-facts recall that ignores cosmetic
    markdown differences (per the research report: raw diff over-penalises
    structurally-equivalent-but-textually-different VLM output).
  * ``score_outputs`` — combine the above into one weighted record per sample.

The driver that actually runs PDFs through alternate engines is intentionally
out of scope here (it needs GPU + provider creds); this module is the part that
is unit-testable and that pins the metric semantics the gate depends on.
"""

from app.benchmark.metrics import (
    BenchmarkScore,
    character_error_rate,
    facts_recall,
    score_outputs,
    table_similarity,
    word_error_rate,
)

__all__ = [
    "BenchmarkScore",
    "character_error_rate",
    "word_error_rate",
    "table_similarity",
    "facts_recall",
    "score_outputs",
]
