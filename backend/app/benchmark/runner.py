"""Benchmark runner + regression gate (plan §9.4 / markitdown decision #4).

Aggregates per-sample :class:`BenchmarkScore`s into a corpus-level report and
applies the **≥80% golden-match** regression gate. This is the harness an
operator runs when considering an engine swap: score config A vs config B on the
same corpus, and only swap if the data justifies the added dependency/VRAM.

The sample-production side (running PDFs through each engine) is intentionally
not here — it needs a GPU + provider creds. This module consumes already-scored
samples so the gate logic itself stays pure and unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.benchmark.metrics import BenchmarkScore, score_outputs

# The regression threshold carried over from markitdown-integration-plan
# decision #4: a config must match the golden output at least this well.
GOLDEN_MATCH_THRESHOLD = 0.80

PHASE3_PDF_CLASSES: tuple[str, ...] = (
    "clean_digital",
    "scanned",
    "sandwich",
    "table_heavy",
    "formula_heavy",
)

PHASE3_LITEPARSE_FAST_PATH_CLASSES: tuple[str, ...] = (
    "clean_digital",
    "formula_heavy",
)


@dataclass
class BenchmarkReport:
    """Corpus-level roll-up of per-sample scores for one engine config."""

    config_name: str
    scores: list[BenchmarkScore] = field(default_factory=list)

    @property
    def sample_count(self) -> int:
        return len(self.scores)

    @property
    def mean_combined(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(s.combined for s in self.scores) / len(self.scores), 6)

    @property
    def mean_cer(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(s.cer for s in self.scores) / len(self.scores), 6)

    @property
    def passing(self) -> bool:
        """True when the mean combined score clears the golden-match gate."""
        return self.mean_combined >= GOLDEN_MATCH_THRESHOLD

    def regressions(self, threshold: float = GOLDEN_MATCH_THRESHOLD) -> list[str]:
        """Sample ids whose combined score falls below ``threshold``."""
        return [s.sample_id for s in self.scores if s.combined < threshold]


@dataclass
class BenchmarkSample:
    """One reference/hypothesis pair to score."""

    sample_id: str
    reference_text: str
    hypothesis_text: str
    reference_table: object | None = None
    hypothesis_table: object | None = None


@dataclass
class PdfBenchmarkCase:
    """One Phase 3 PDF case with a golden reference."""

    sample_id: str
    pdf_path: str | Path
    document_class: str
    reference_text: str
    reference_table: object | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PdfEngineOutput:
    """Normalised output from one PDF engine run."""

    text: str
    table: object | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PdfBenchmarkComparison:
    """Phase 3 Marker-vs-LiteParse report across the required PDF classes."""

    marker_report: BenchmarkReport
    liteparse_report: BenchmarkReport
    verdict: dict[str, object]
    covered_classes: tuple[str, ...]

    @property
    def ready_for_phase4(self) -> bool:
        return bool(self.verdict.get("phase3_ready_for_phase4"))


def run_benchmark(
    config_name: str,
    samples: list[BenchmarkSample],
) -> BenchmarkReport:
    """Score every sample for one engine config and roll up into a report."""
    scores = [
        score_outputs(
            sample_id=s.sample_id,
            reference_text=s.reference_text,
            hypothesis_text=s.hypothesis_text,
            reference_table=s.reference_table,
            hypothesis_table=s.hypothesis_table,
        )
        for s in samples
    ]
    return BenchmarkReport(config_name=config_name, scores=scores)


def validate_phase3_pdf_corpus(
    cases: Sequence[PdfBenchmarkCase],
    required_classes: Sequence[str] = PHASE3_PDF_CLASSES,
) -> tuple[str, ...]:
    """Validate that the Phase 3 corpus covers every planned PDF class."""
    covered = {case.document_class for case in cases}
    missing = tuple(cls for cls in required_classes if cls not in covered)
    if missing:
        raise ValueError(
            "Phase 3 benchmark corpus missing required PDF classes: "
            + ", ".join(missing)
        )
    return tuple(cls for cls in required_classes if cls in covered)


def _coerce_pdf_output(output: PdfEngineOutput | dict[str, Any] | str) -> PdfEngineOutput:
    if isinstance(output, PdfEngineOutput):
        return output
    if isinstance(output, str):
        return PdfEngineOutput(text=output)
    if isinstance(output, dict):
        metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
        table = output.get("table") or metadata.get("table")
        return PdfEngineOutput(
            text=str(output.get("text") or ""),
            table=table,
            metadata=dict(metadata),
        )
    raise TypeError(f"Unsupported PDF benchmark output: {type(output).__name__}")


def _run_pdf_engine(
    config_name: str,
    cases: Sequence[PdfBenchmarkCase],
    engine: Callable[[PdfBenchmarkCase], PdfEngineOutput | dict[str, Any] | str],
) -> BenchmarkReport:
    samples: list[BenchmarkSample] = []
    for case in cases:
        output = _coerce_pdf_output(engine(case))
        samples.append(
            BenchmarkSample(
                sample_id=f"{case.document_class}:{case.sample_id}",
                reference_text=case.reference_text,
                hypothesis_text=output.text,
                reference_table=case.reference_table,
                hypothesis_table=output.table,
            )
        )
    return run_benchmark(config_name, samples)


def compare_marker_liteparse_pdfs(
    cases: Sequence[PdfBenchmarkCase],
    marker_engine: Callable[[PdfBenchmarkCase], PdfEngineOutput | dict[str, Any] | str],
    liteparse_engine: Callable[[PdfBenchmarkCase], PdfEngineOutput | dict[str, Any] | str],
    *,
    marker_name: str = "marker_pdf",
    liteparse_name: str = "liteparse_pdf",
    required_classes: Sequence[str] = PHASE3_PDF_CLASSES,
) -> PdfBenchmarkComparison:
    """Run the Phase 3 Marker-vs-LiteParse gate on the existing benchmark logic."""
    covered_classes = validate_phase3_pdf_corpus(cases, required_classes)
    marker_report = _run_pdf_engine(marker_name, cases, marker_engine)
    liteparse_report = _run_pdf_engine(liteparse_name, cases, liteparse_engine)
    verdict = compare_configs(marker_report, liteparse_report)
    fast_path_classes = tuple(
        document_class
        for document_class in PHASE3_LITEPARSE_FAST_PATH_CLASSES
        if document_class in required_classes
    )
    liteparse_fast_path = _class_gate(
        liteparse_report,
        fast_path_classes,
    )
    verdict.update(
        {
            "covered_classes": list(covered_classes),
            "liteparse_fast_path_classes": list(fast_path_classes),
            "liteparse_fast_path_mean": liteparse_fast_path["mean_combined"],
            "liteparse_fast_path_passes_gate": liteparse_fast_path["passes_gate"],
            "liteparse_fast_path_regressions": liteparse_fast_path["regressions"],
            "liteparse_fast_path_missing_classes": liteparse_fast_path["missing_classes"],
            "phase3_ready_for_phase4": (
                marker_report.sample_count == liteparse_report.sample_count
                and marker_report.sample_count >= len(required_classes)
                and liteparse_fast_path["passes_gate"]
            ),
            "marker_regressions": marker_report.regressions(),
            "liteparse_regressions": liteparse_report.regressions(),
        }
    )
    return PdfBenchmarkComparison(
        marker_report=marker_report,
        liteparse_report=liteparse_report,
        verdict=verdict,
        covered_classes=covered_classes,
    )


def compare_configs(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
) -> dict[str, object]:
    """Compare a candidate engine config against the baseline.

    Returns the deltas and a ``should_swap`` verdict: swap only when the
    candidate both clears the golden gate AND beats the baseline mean — the
    §9.4 rule "no swap on benchmark faith".
    """
    delta = round(candidate.mean_combined - baseline.mean_combined, 6)
    return {
        "baseline": baseline.config_name,
        "candidate": candidate.config_name,
        "baseline_mean": baseline.mean_combined,
        "candidate_mean": candidate.mean_combined,
        "delta": delta,
        "candidate_passes_gate": candidate.passing,
        "should_swap": candidate.passing and delta > 0,
    }


def _document_class(sample_id: str) -> str:
    return sample_id.split(":", 1)[0]


def _class_gate(
    report: BenchmarkReport,
    document_classes: Sequence[str],
    threshold: float = GOLDEN_MATCH_THRESHOLD,
) -> dict[str, object]:
    wanted = set(document_classes)
    scores = [
        score
        for score in report.scores
        if _document_class(score.sample_id) in wanted
    ]
    present = {_document_class(score.sample_id) for score in scores}
    missing = [
        document_class
        for document_class in document_classes
        if document_class not in present
    ]
    mean_combined = (
        round(sum(score.combined for score in scores) / len(scores), 6)
        if scores
        else 0.0
    )
    regressions = [score.sample_id for score in scores if score.combined < threshold]
    return {
        "mean_combined": mean_combined,
        "passes_gate": not missing and not regressions,
        "regressions": regressions,
        "missing_classes": missing,
    }
