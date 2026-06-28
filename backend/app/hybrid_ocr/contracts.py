"""Contracts for the local Hybrid OCR refinement pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TargetKind(str, Enum):
    TEXT = "text"
    DEGRADED_TEXT = "degraded_text"
    TABLE = "table"
    FORMULA = "formula"
    FIGURE_TEXT = "figure_text"
    FULL_PAGE_SCAN = "full_page_scan"
    SEAL_OR_STAMP = "seal_or_stamp"
    CHART_TEXT = "chart_text"


class HybridEngine(str, Enum):
    SURYA = "surya"
    GLM_OCR = "glm_ocr"
    PADDLEOCR_VL = "paddleocr_vl"


class ReplacementPolicy(str, Enum):
    NO_CHANGE = "no_change"
    REPLACE_BLOCK = "replace_block"
    AUGMENT_BLOCK = "augment_block"
    REPLACE_IMAGE_WITH_TEXT = "replace_image_with_text"
    APPEND_NOTE = "append_note"


@dataclass(frozen=True)
class HybridTarget:
    target_id: str
    document_id: str
    page_index: int
    page_number: int
    block_id: str | None
    block_type: str
    target_kind: TargetKind
    bbox: list[float] | None
    polygon: list[list[float]] | None
    crop_path: str
    crop_width: int
    crop_height: int
    baseline_text: str
    baseline_html: str
    baseline_confidence: float | None
    baseline_source: str
    route_hints: dict[str, Any] = field(default_factory=dict)
    surrounding_text: str = ""
    heading_chain: str = ""
    fingerprint: str | None = None
    block_ref: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ValidationReport:
    accepted: bool
    score: float
    checks: dict[str, bool]
    reasons: list[str]
    normalized_text_len: int
    output_shape: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HybridResult:
    target_id: str
    engine: HybridEngine
    status: str
    output_kind: TargetKind
    text: str
    markdown: str
    html: str
    json_payload: dict[str, Any]
    confidence: float | None
    duration_ms: int
    validation: ValidationReport
    replacement_policy: ReplacementPolicy
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

