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
  * ``compare_marker_liteparse_pdfs`` — Phase 3 PDF corpus gate used by
    ``backend/scripts/run_phase3_pdf_benchmark.py``.

The real engine driver lives in ``backend/scripts`` because it may download or
load Marker models. The package keeps generation, scoring, and validation
unit-testable.
"""

from app.benchmark.phase3_pdf_corpus import generate_phase3_pdf_cases, load_phase3_pdf_cases
from app.benchmark.metrics import (
    BenchmarkScore,
    character_error_rate,
    facts_recall,
    score_outputs,
    table_similarity,
    word_error_rate,
)
from app.benchmark.runner import compare_marker_liteparse_pdfs

__all__ = [
    "BenchmarkScore",
    "character_error_rate",
    "word_error_rate",
    "table_similarity",
    "facts_recall",
    "score_outputs",
    "generate_phase3_pdf_cases",
    "load_phase3_pdf_cases",
    "compare_marker_liteparse_pdfs",
]
