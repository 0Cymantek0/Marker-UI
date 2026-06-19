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
# SmartRouterLevel — the Tier-0 routing "brain" (3 levels)
# ---------------------------------------------------------------------------

class SmartRouterLevel(str, Enum):
    """How much local intelligence the Tier-0 router applies per crop.

    * ``disabled`` — density-only heuristic (text-box area + line count). No
      extra layout pass: cheapest and fastest, but mis-routes text-heavy charts
      to OCR and may drop textless graphics.
    * ``smart`` — re-run Surya ``layout_model`` on each crop and route on its
      label (Table/Equation/Text/Code/Form/Picture/...). Big accuracy gain at
      one extra *local* forward pass per crop (no API cost).
    * ``beeg_brain`` — layout + density fusion with conservative escalation:
      when the label and density disagree, escalate to the VLM instead of
      guessing, and require both signals to agree before dropping a decorative.
      Highest accuracy and near-zero catastrophic drops, at the most local GPU
      and more VLM escalations.
    """

    disabled = "disabled"
    smart = "smart"
    beeg_brain = "beeg_brain"


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
# Router decision (Tier-0 local pre-pass — plan §1/§2)
# ---------------------------------------------------------------------------

class RouteKind(str, Enum):
    """Where the graded router sends an image.

    * ``skip_decorative`` — no text, tiny / low-content: omit, zero cost.
    * ``ocr`` — text-as-image: deterministic local Surya OCR, zero cloud cost,
      no hallucination (the openskill.md line-227 class of bug).
    * ``vlm`` — genuine visual understanding (chart/diagram/photo): escalate to
      the (batched) cloud VLM.
    """

    skip_decorative = "skip_decorative"
    ocr = "ocr"
    vlm = "vlm"


class RouteDecision(BaseModel):
    """Result of the Tier-0 local pre-pass for one image.

    Carries the chosen route plus the cheap local signals that justified it, so
    the ``reason`` can be logged and the thresholds tuned against real output
    (plan §7: tune via logged ``reason`` metadata, not a fixed split up front).
    """

    route: RouteKind
    reason: str = ""
    layout_label: str = Field(
        default="",
        description=(
            "Surya layout label of the crop that drove the route (smart/"
            "beeg_brain levels). Empty when density-only routing was used."
        ),
    )
    text_density: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of image area covered by detected text boxes.",
    )
    line_count: int = Field(default=0, ge=0, description="Detected text line count.")


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
    route: Optional[str] = Field(
        default=None,
        description="Batch route verdict: vlm_required, ocr_sufficient, or decorative.",
    )
    cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated USD cost attributed to this extraction (plan §6).",
    )
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

    # --- Tier-0 router (plan §1/§2/§7) -----------------------------------
    router_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the graded Tier-0 router. When False the "
            "processor uses the legacy per-image classify+extract path "
            "(escape hatch — plan §7/§10)."
        ),
    )
    smart_router_level: SmartRouterLevel = Field(
        default=SmartRouterLevel.smart,
        description=(
            "Tier-0 routing brain: 'disabled' = density-only heuristic; "
            "'smart' = route on a per-crop Surya layout label (default); "
            "'beeg_brain' = layout+density fusion with conservative VLM "
            "escalation on disagreement. Higher levels cost more local GPU but "
            "route more accurately."
        ),
    )
    decorative_max_text_density: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        description=(
            "Images with text-box area fraction at or below this AND no "
            "meaningful detected lines route to skip_decorative."
        ),
    )
    ocr_min_text_density: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description=(
            "Images with text-box area fraction at or above this route to "
            "deterministic local OCR (text-as-image) instead of the cloud VLM."
        ),
    )
    ocr_min_lines: int = Field(
        default=3,
        ge=1,
        description="Minimum detected text lines to consider the OCR route.",
    )
    allow_cloud_vlm: bool = Field(
        default=False,
        description=(
            "Allow escalation to the cloud VLM (plan §11a). False by default "
            "so a fresh install stays local-only until the user explicitly "
            "opts in to sending image crops to a configured provider."
        ),
    )
    dedup_enabled: bool = Field(
        default=True,
        description=(
            "Collapse repeated identical images (logos, recurring figures) to "
            "a single extraction, fanned back to every duplicate (plan §8a)."
        ),
    )
    dedup_max_distance: int = Field(
        default=0,
        ge=0,
        le=64,
        description=(
            "Max aHash Hamming distance treated as a duplicate. 0 = exact "
            "fingerprint match (safest; never collapses distinct figures)."
        ),
    )
    downscale_vlm_crops: bool = Field(
        default=True,
        description=(
            "Downscale image crops before the VLM send to land in the cheap "
            "vision-token tile band (plan §8, the biggest non-obvious cost "
            "lever). Off preserves full-resolution sends."
        ),
    )
    vlm_crop_max_px: int = Field(
        default=768,
        ge=64,
        le=4096,
        description="Longest-side pixel cap applied to a crop before VLM send.",
    )
    batch_enabled: bool = Field(
        default=True,
        description=(
            "Route+extract images in batched structured-output calls instead "
            "of two serial calls per image (plan §3). Off = legacy per-image "
            "classify+extract."
        ),
    )
    vlm_batch_size: int = Field(
        default=8,
        ge=1,
        le=64,
        description="Images per batched VLM call (clamped per provider, plan §3.1).",
    )
    max_batch_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Max extra batch calls to recover missing/garbled indices.",
    )
    ocr_engine: str = Field(
        default="surya",
        description=(
            "Local OCR engine behind the pluggable OCREngine seam (plan §5). "
            "Only 'surya' ships today; glm_ocr / paddleocr_vl / mistral_ocr are "
            "gated behind the §9.4 benchmark."
        ),
    )
