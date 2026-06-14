"""Pydantic schemas for image understanding (VLM classification + extraction)."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ImageType taxonomy (locked for Phase 1 — 16 types)
# ---------------------------------------------------------------------------

class ImageType(str, Enum):
    # --- Charts ---
    chart_bar = "chart_bar"
    chart_line = "chart_line"
    chart_pie = "chart_pie"
    chart_scatter = "chart_scatter"
    chart_other = "chart_other"

    # --- Tables ---
    table_image = "table_image"

    # --- Diagrams ---
    diagram_flow = "diagram_flow"
    diagram_sequence = "diagram_sequence"
    diagram_state = "diagram_state"
    diagram_class = "diagram_class"
    diagram_architecture = "diagram_architecture"

    # --- Technical / Special ---
    equation = "equation"
    screenshot_ui = "screenshot_ui"
    figure_technical = "figure_technical"

    # --- General ---
    photo = "photo"
    decorative = "decorative"
    other = "other"


# ---------------------------------------------------------------------------
# ImageHandlingMode (3-way UX toggle per m0104)
# ---------------------------------------------------------------------------

class ImageHandlingMode(str, Enum):
    understanding = "understanding"
    extraction = "extraction"
    both = "both"


# ---------------------------------------------------------------------------
# Per-type payload sub-models  (plan sec 6.4)
# ---------------------------------------------------------------------------

class Point(BaseModel):
    x: float = 0.0
    y: float = 0.0


class Series(BaseModel):
    name: str = ""
    points: list[Point] = Field(default_factory=list)


class ChartPayload(BaseModel):
    title: str = ""
    x_label: str = ""
    y_label: str = ""
    series: list[Series] = Field(default_factory=list)
    notes: str = ""


class TablePayload(BaseModel):
    caption: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class DiagramPayload(BaseModel):
    mermaid: str = ""
    caption: str = ""


class EquationPayload(BaseModel):
    latex: str = ""
    caption: str = ""


class Region(BaseModel):
    name: str = ""
    description: str = ""
    ocr_text: str = ""


class ScreenshotPayload(BaseModel):
    application: str = ""
    area: str = ""
    regions: list[Region] = Field(default_factory=list)
    summary: str = ""


class DescriptionPayload(BaseModel):
    alt_text: str = ""
    details: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

class ClassificationResult(BaseModel):
    image_type: ImageType
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


# ---------------------------------------------------------------------------
# Extraction result
# ---------------------------------------------------------------------------

class ExtractionResult(BaseModel):
    image_type: ImageType
    payload: dict = Field(default_factory=dict, description="Type-specific JSON per plan sec 6.4")
    raw_response: str = Field(default="", description="Raw VLM response for debugging")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    error: Optional[str] = None
    context_window: Optional[dict] = Field(
        default=None,
        description="Debug info about the VLM context window used (injected at prompt layer)",
    )


# ---------------------------------------------------------------------------
# Image Understanding config  (NO cost cap — decision #5)
# ---------------------------------------------------------------------------

class ImageUnderstandingConfig(BaseModel):
    """Configuration for the image understanding pipeline (Phase 1).

    No cost-cap field per decision #5 (m0108). Context window is injected
    at the prompt layer, not in the schema.
    """

    enabled: bool = Field(default=False, description="Master toggle — opt-in per conversion")
    image_handling_mode: ImageHandlingMode = Field(
        default=ImageHandlingMode.extraction,
        description="3-way UX toggle: understanding / extraction (default) / both",
    )
    vlm_model: Optional[str] = Field(default=None, description="Override VLM model name")
    max_images_per_doc: int = Field(default=50, ge=1, le=1000)
    skip_decorative: bool = Field(
        default=True,
        description="Skip classification of decorative images (save VLM calls)",
    )
    include_original_ref: bool = Field(
        default=True,
        description="Preserve original image link below textual representation when mode=both or understanding",
    )
    confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum confidence for the ~approximate cell marker",
    )
