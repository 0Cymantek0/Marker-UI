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

from dataclasses import dataclass, field

from app.benchmark.metrics import BenchmarkScore, score_outputs

# The regression threshold carried over from markitdown-integration-plan
# decision #4: a config must match the golden output at least this well.
GOLDEN_MATCH_THRESHOLD = 0.80


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
