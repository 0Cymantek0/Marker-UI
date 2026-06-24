"""Format benchmark gate for native converters vs Markitdown.

This module scores already-produced outputs. It does not import Markitdown or
run converters, so the gate stays deterministic and dependency-light. Operators
can feed native outputs, Markitdown outputs, or both for the same cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.benchmark.metrics import BenchmarkScore, score_outputs
from app.benchmark.runner import BenchmarkReport, GOLDEN_MATCH_THRESHOLD, compare_configs
from app.conversion.table_evidence import attach_table_evidence


_LINK_RE = re.compile(r"https?://[^\s)>\]\"']+")
_PRIVATE_PATH_RE = re.compile(
    r"([A-Za-z]:\\Users\\|/Users/|/home/|\\\\[^\\\s]+\\[^\\\s]+)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(api[_-]?key|secret|token|password)\s*[:=]\s*[A-Za-z0-9_\-]{8,}",
    re.IGNORECASE,
)


@dataclass
class FormatBenchmarkCase:
    """One non-PDF format fixture with explicit fidelity expectations."""

    sample_id: str
    format_class: str
    source_path: str | Path
    reference_text: str
    reference_tables: object | None = None
    expected_links: tuple[str, ...] = ()
    expected_metadata: dict[str, Any] = field(default_factory=dict)
    require_local: bool = True


@dataclass
class FormatEngineOutput:
    """Normalised output for one native/Markitdown format conversion."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float | None = None
    memory_mb: float | None = None


@dataclass
class FormatBenchmarkScore:
    """Scored result for one format case."""

    sample_id: str
    format_class: str
    content: BenchmarkScore
    link_score: float = 1.0
    metadata_score: float = 1.0
    privacy_score: float = 1.0
    combined: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FormatBenchmarkReport:
    """Corpus-level report for one format engine."""

    config_name: str
    scores: list[FormatBenchmarkScore] = field(default_factory=list)

    @property
    def sample_count(self) -> int:
        return len(self.scores)

    @property
    def mean_combined(self) -> float:
        if not self.scores:
            return 0.0
        return round(sum(score.combined for score in self.scores) / len(self.scores), 6)

    @property
    def passing(self) -> bool:
        return self.mean_combined >= GOLDEN_MATCH_THRESHOLD

    def regressions(self, threshold: float = GOLDEN_MATCH_THRESHOLD) -> list[str]:
        return [score.sample_id for score in self.scores if score.combined < threshold]

    def as_benchmark_report(self) -> BenchmarkReport:
        """Adapt to generic config-comparison gate."""
        return BenchmarkReport(
            config_name=self.config_name,
            scores=[
                BenchmarkScore(
                    sample_id=score.sample_id,
                    cer=score.content.cer,
                    wer=score.content.wer,
                    table_score=score.content.table_score,
                    facts=score.content.facts,
                    combined=score.combined,
                    details=score.details,
                )
                for score in self.scores
            ],
        )


@dataclass
class FormatBenchmarkComparison:
    """Native-vs-Markitdown comparison result."""

    native_report: FormatBenchmarkReport
    markitdown_report: FormatBenchmarkReport | None
    verdict: dict[str, Any]


def extract_links(text: str) -> set[str]:
    return {match.group(0).rstrip(".,") for match in _LINK_RE.finditer(text or "")}


def _metadata_get(metadata: dict[str, Any], path: str) -> Any:
    current: Any = metadata
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _metadata_score(expected: dict[str, Any], metadata: dict[str, Any]) -> tuple[float, list[str]]:
    if not expected:
        return 1.0, []
    matched = 0
    missing: list[str] = []
    for path, expected_value in expected.items():
        actual = _metadata_get(metadata, path)
        if actual == expected_value:
            matched += 1
        else:
            missing.append(path)
    return round(matched / len(expected), 6), missing


def _link_score(expected_links: tuple[str, ...], text: str) -> tuple[float, list[str]]:
    if not expected_links:
        return 1.0, []
    found = extract_links(text)
    missing = [link for link in expected_links if link not in found]
    matched = len(expected_links) - len(missing)
    return round(matched / len(expected_links), 6), missing


def _privacy_score(
    case: FormatBenchmarkCase,
    output: FormatEngineOutput,
) -> tuple[float, list[str]]:
    violations: list[str] = []
    metadata_text = str(output.metadata)
    combined = f"{output.text}\n{metadata_text}"
    if _PRIVATE_PATH_RE.search(combined):
        violations.append("private_path")
    if _SECRET_RE.search(combined):
        violations.append("secret_like_value")
    if case.require_local and _metadata_get(output.metadata, "engine.needs_cloud") is True:
        violations.append("needs_cloud")
    return (0.0 if violations else 1.0), violations


def _safe_path_for_report(path: str | Path) -> str:
    raw = Path(path)
    parts = raw.parts
    for marker in ("backend", "tests", "fixtures"):
        if marker in parts:
            index = parts.index(marker)
            return str(Path(*parts[index:]))
    return raw.name


def score_format_output(
    case: FormatBenchmarkCase,
    output: FormatEngineOutput,
) -> FormatBenchmarkScore:
    metadata_with_tables = attach_table_evidence(output.metadata, output.text)
    hypothesis_tables = metadata_with_tables.get("tables") or metadata_with_tables.get("table")
    content = score_outputs(
        sample_id=case.sample_id,
        reference_text=case.reference_text,
        hypothesis_text=output.text,
        reference_table=case.reference_tables,
        hypothesis_table=hypothesis_tables,
    )
    link_score, missing_links = _link_score(case.expected_links, output.text)
    metadata_score, missing_metadata = _metadata_score(
        case.expected_metadata,
        output.metadata,
    )
    privacy_score, privacy_violations = _privacy_score(case, output)
    components = [content.combined, link_score, metadata_score, privacy_score]
    combined = round(sum(components) / len(components), 6)
    return FormatBenchmarkScore(
        sample_id=case.sample_id,
        format_class=case.format_class,
        content=content,
        link_score=link_score,
        metadata_score=metadata_score,
        privacy_score=privacy_score,
        combined=combined,
        details={
            "source_path": _safe_path_for_report(case.source_path),
            "reference_len": len(case.reference_text or ""),
            "hypothesis_len": len(output.text or ""),
            "missing_links": missing_links,
            "missing_metadata": missing_metadata,
            "privacy_violations": privacy_violations,
            "elapsed_s": output.elapsed_s,
            "memory_mb": output.memory_mb,
            "content_scoring_mode": content.details.get("scoring_mode"),
        },
    )


def run_format_benchmark(
    config_name: str,
    cases: list[FormatBenchmarkCase],
    outputs: dict[str, FormatEngineOutput],
) -> FormatBenchmarkReport:
    missing = [case.sample_id for case in cases if case.sample_id not in outputs]
    if missing:
        raise ValueError(
            "Format benchmark missing outputs for cases: " + ", ".join(missing)
        )
    return FormatBenchmarkReport(
        config_name=config_name,
        scores=[
            score_format_output(case, outputs[case.sample_id])
            for case in cases
        ],
    )


def compare_native_markitdown_formats(
    cases: list[FormatBenchmarkCase],
    native_outputs: dict[str, FormatEngineOutput],
    markitdown_outputs: dict[str, FormatEngineOutput] | None = None,
) -> FormatBenchmarkComparison:
    native_report = run_format_benchmark("native", cases, native_outputs)
    if markitdown_outputs is None:
        verdict = {
            "comparison_available": False,
            "native_mean": native_report.mean_combined,
            "native_passes_gate": native_report.passing,
            "native_regressions": native_report.regressions(),
        }
        return FormatBenchmarkComparison(native_report, None, verdict)

    markitdown_report = run_format_benchmark("markitdown", cases, markitdown_outputs)
    verdict = compare_configs(
        markitdown_report.as_benchmark_report(),
        native_report.as_benchmark_report(),
    )
    verdict.update(
        {
            "comparison_available": True,
            "native_regressions": native_report.regressions(),
            "markitdown_regressions": markitdown_report.regressions(),
            "native_passes_gate": native_report.passing,
            "markitdown_passes_gate": markitdown_report.passing,
        }
    )
    return FormatBenchmarkComparison(native_report, markitdown_report, verdict)
