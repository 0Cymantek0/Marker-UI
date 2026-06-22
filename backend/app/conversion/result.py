"""Core types for the universal conversion layer.

These types flow through every converter and the orchestration service.
``UniversalConversionResult`` is the canonical output; ``to_legacy_envelope``
converts it to the dict shape that ``_finalize_job`` already knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Which executor a plan should run on. Office/text jobs never need marker
# models or a GPU, so they route to the lightweight CPU thread pool and avoid
# spawning (or warming) a GPU process worker. Marker/scanned jobs route to the
# marker worker pool when one is configured.
ExecutionBackend = Literal["cpu_thread", "marker_worker"]


@dataclass(frozen=True)
class Asset:
    """A non-image sidecar produced by a converter (e.g. CSV export).

    Used by spreadsheet/archive converters in later PRs.  Defined here so
    the result type is complete from the start.
    """

    name: str  # relative path, e.g. "sheets/Sheet1.csv"
    media_type: str
    data: bytes | None = None
    pil: Any | None = None  # PIL.Image instance


@dataclass
class ConverterPlan:
    """The routing decision for one file: which engine, why, what it needs."""

    engine: str  # e.g. "marker_pdf", "office_docx"
    label: str  # human-readable, e.g. "Marker PDF"
    confidence: float  # 0.0–1.0
    reasons: list[str]
    needs_marker_models: bool
    needs_gpu: bool
    execution_backend: ExecutionBackend = "marker_worker"
    needs_cloud: bool = False
    optional_dependencies: list[str] = field(default_factory=list)
    fallback_chain: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for metadata/JSON responses."""
        return {
            "engine": self.engine,
            "label": self.label,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "needs_marker_models": self.needs_marker_models,
            "needs_gpu": self.needs_gpu,
            "execution_backend": self.execution_backend,
            "needs_cloud": self.needs_cloud,
            "optional_dependencies": self.optional_dependencies,
            "fallback_chain": self.fallback_chain,
            "warnings": self.warnings,
        }


@dataclass
class UniversalConversionResult:
    """Canonical converter output.

    Every converter returns this.  ``to_legacy_envelope`` adapts it to the
    ``{text, extension, images, metadata}`` dict that ``_finalize_job`` and
    the process-boundary ``WorkerEvent.result`` already expect.
    """

    text: str
    extension: str = "md"
    images: dict[str, Any] = field(default_factory=dict)  # name → PIL|bytes
    metadata: dict[str, Any] = field(default_factory=dict)
    assets: list[Asset] = field(default_factory=list)

    def to_legacy_envelope(self) -> dict[str, Any]:
        """Convert to the dict shape ``_finalize_job`` expects."""
        return {
            "text": self.text,
            "extension": self.extension,
            "images": self.images,
            "metadata": self.metadata,
        }
