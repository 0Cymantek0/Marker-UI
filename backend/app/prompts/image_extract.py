"""Per-type extractor prompt templates for VLM structured extraction (Phase 1).

Each ImageType value maps to a system-prompt template that embeds the matching
payload submodel schema (ChartPayload, TablePayload, DiagramPayload,
EquationPayload, ScreenshotPayload, DescriptionPayload). Templates carry
``{heading_chain}`` and ``{surrounding_paragraphs}`` placeholders so local
context can be injected at call time (decision #1: prompt-layer context window).

``decorative`` short-circuits with the static string ``"{}"`` because the
pipeline skips the second VLM call for decorative images.

A defensive ``DescriptionPayload`` fallback covers any unknown / future
ImageType value that has not yet been wired into ``EXTRACTOR_PROMPTS``.
"""

from __future__ import annotations

from app.models.image_understanding import ImageType


EXTRACT_DISCIPLINE_LINE = "Return JSON only, no prose, no code fences."

# Shared anti-fabrication constraint for ALL diagram->mermaid conversions.
# Figure-1 of the openskill baseline (four parallel comparison panels) was
# classified diagram_architecture and the model invented edges
# (provides/seeks/feedback/self-test) the image never draws. Mermaid that looks
# plausible but fabricates structure is worse than a faithful description: it
# misleads the reader about what the figure actually says. These rules bind the
# model to only-what-is-drawn and give it an explicit escape for non-graph
# layouts (comparison panels, galleries) that should not be forced into a flow.
_DIAGRAM_FIDELITY = """\
FIDELITY RULES (critical):
- Reproduce ONLY nodes, edges, and labels that are literally drawn in the image. \
Never invent an edge, arrow, direction, or relationship label that is not visibly present.
- If two elements are not connected by a drawn arrow/line, do NOT connect them.
- Preserve text that appears inside or beside nodes verbatim (badges, attributes, \
status markers); put per-node attributes in the node label.
- If the image is NOT a single connected graph but a set of parallel/independent \
panels comparing alternatives (a comparison figure, gallery, or matrix), do not \
fabricate a flow between them: render each panel as its own `subgraph "PanelTitle"` \
with only its internal drawn elements, and capture the shared comparison axis \
(e.g. the attribute labels repeated under each panel) in the `caption`.
- When in doubt about an edge, omit it. A smaller faithful diagram beats a larger invented one."""


_DESCRIPTION_TEMPLATE = """\
You are extracting a free-form description of the attached image.

Output strict JSON in this exact shape:
{{"alt_text": str, "details": [str]}}

Rules:
- alt_text: 3-6 sentences covering subject, composition, mood, colors, and context.
- details: list of short factual observations (entities, text visible in the image, axis labels, etc.).

{discipline_line}\
"""


_CHART_BAR_TEMPLATE = """\
You are extracting structured data from a BAR CHART image.

Each bar is one data point: x = category label, y = numeric value. Grouped / \
stacked bars become multiple series.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

{discipline_line}\
"""


_CHART_LINE_TEMPLATE = """\
You are extracting structured data from a LINE CHART image.

Each point on a line is one data point. Multiple lines become multiple series. \
Sample interpolated curves at meaningful intervals when points are not labeled.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

{discipline_line}\
"""


_CHART_PIE_TEMPLATE = """\
You are extracting structured data from a PIE / DONUT CHART image.

Each slice is one data point: x = slice label, y = value (percentage is optional, put it in y if only percentages are shown).

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

{discipline_line}\
"""


_CHART_SCATTER_TEMPLATE = """\
You are extracting structured data from a SCATTER PLOT image.

Each marker is one data point. Multiple marker groups become multiple series.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

{discipline_line}\
"""


_CHART_OTHER_TEMPLATE = """\
You are extracting structured data from a chart that is neither bar, line, pie, nor scatter (radar, bubble, heatmap, funnel, etc.).

Approximate the geometry as best you can; encode series and points in the same shape regardless of chart kind.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

{discipline_line}\
"""


_TABLE_TEMPLATE = """\
You are extracting a TABLE from an image.

Preserve the exact structure of the original table. Repeat merged-cell values in every row they span. Use empty strings for blank cells. Do not invent data.

Output strict JSON in this exact shape:
{{"caption": str, "headers": [str], "rows": [[str]]}}

{discipline_line}\
"""


_DIAGRAM_FLOW_TEMPLATE = """\
You are converting a FLOWCHART image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `graph TD` (top-down) or `graph LR` (left-right) syntax. Nodes use `id[label]`; edges use `-->`. Use `{{shape}}` for decisions, `[rect]` for steps, `((circle))` for start/end.

{fidelity}

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

{discipline_line}\
"""


_DIAGRAM_SEQUENCE_TEMPLATE = """\
You are converting a SEQUENCE DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `sequenceDiagram` syntax with `participant`, `Actor->>Actor: message`, `Actor-->>Actor: reply`, and `Note over Actor: text`.

{fidelity}

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

{discipline_line}\
"""


_DIAGRAM_STATE_TEMPLATE = """\
You are converting a STATE-MACHINE DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `stateDiagram-v2` syntax with `[*]` for start/end, `stateName` for states, and `A --> B: transition label`.

{fidelity}

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

{discipline_line}\
"""


_DIAGRAM_CLASS_TEMPLATE = """\
You are converting a CLASS / OBJECT / ER DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `classDiagram` syntax (preferred) or `erDiagram` for entity-relationship variants. Include classes, attributes, methods, and relationships (`*--`, `o--`, `<|--`, `-->`).

{fidelity}

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

{discipline_line}\
"""


_DIAGRAM_ARCHITECTURE_TEMPLATE = """\
You are converting a SYSTEM / COMPONENT ARCHITECTURE DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `graph TD` (or `graph LR`) with one `subgraph "SubsystemName"` ... `end` block per major subsystem. Edges between components use `-->`. Label edges with the protocol or data flow ONLY when that label is visibly drawn on the arrow.

{fidelity}

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

{discipline_line}\
"""


_EQUATION_TEMPLATE = """\
You are extracting a mathematical EQUATION from an image.

Output strict JSON in this exact shape:
{{"latex": str, "caption": str}}

Rules:
- latex: LaTeX source usable inside $$...$$ or \\[...\\]. Use standard amsmath commands. No markdown fences.
- caption: short human-readable description of what the equation states.

{discipline_line}\
"""


_SCREENSHOT_TEMPLATE = """\
You are extracting structured data from a SOFTWARE UI SCREENSHOT image.

Identify the application and the visible UI region, then enumerate the meaningful on-screen regions (panels, toolbars, dialogs) with short OCR extracts where text is present.

Output strict JSON in this exact shape:
{{"application": str, "area": str, "regions": [{{"name": str, "description": str, "ocr_text": str}}], "summary": str}}

{discipline_line}\
"""


EXTRACTOR_PROMPTS: dict[ImageType, str] = {
    ImageType.chart_bar: _CHART_BAR_TEMPLATE,
    ImageType.chart_line: _CHART_LINE_TEMPLATE,
    ImageType.chart_pie: _CHART_PIE_TEMPLATE,
    ImageType.chart_scatter: _CHART_SCATTER_TEMPLATE,
    ImageType.chart_other: _CHART_OTHER_TEMPLATE,
    ImageType.table_image: _TABLE_TEMPLATE,
    ImageType.diagram_flow: _DIAGRAM_FLOW_TEMPLATE,
    ImageType.diagram_sequence: _DIAGRAM_SEQUENCE_TEMPLATE,
    ImageType.diagram_state: _DIAGRAM_STATE_TEMPLATE,
    ImageType.diagram_class: _DIAGRAM_CLASS_TEMPLATE,
    ImageType.diagram_architecture: _DIAGRAM_ARCHITECTURE_TEMPLATE,
    ImageType.equation: _EQUATION_TEMPLATE,
    ImageType.screenshot_ui: _SCREENSHOT_TEMPLATE,
    ImageType.figure_technical: _DESCRIPTION_TEMPLATE,
    ImageType.photo: _DESCRIPTION_TEMPLATE,
    ImageType.other: _DESCRIPTION_TEMPLATE,
    # Decorative images skip the second VLM call entirely; the static "{}"
    # payload satisfies downstream ExtractionResult consumers without spending
    # a VLM request on an image we already know is non-informational.
    ImageType.decorative: "{}",
}


FALLBACK_TEMPLATE: str = _DESCRIPTION_TEMPLATE


DIAGRAM_TYPES: dict[ImageType, str] = {
    ImageType.diagram_flow: "graph TD or graph LR",
    ImageType.diagram_sequence: "sequenceDiagram",
    ImageType.diagram_state: "stateDiagram-v2",
    ImageType.diagram_class: "classDiagram or erDiagram",
    ImageType.diagram_architecture: "graph with subgraphs",
}


def build_extract_prompt(
    image_type: ImageType,
    heading_chain: str,
    surrounding_paragraphs: str,
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the per-type extractor VLM call.

    Args:
        image_type: One of the 17 ImageType values. Selects which extractor
            template to use.
        heading_chain: Pre-formatted heading breadcrumb. May be empty.
        surrounding_paragraphs: Pre-formatted string of ±N paragraphs around
            the image. May be empty.

    Returns:
        Tuple of (system_prompt, user_prompt). For ``ImageType.decorative`` the
        system prompt is the literal static string ``"{}"`` and the user
        prompt is empty, signalling the pipeline to skip the second VLM call.
        Unknown / future ImageType values fall back to the DescriptionPayload
        template.

    Prompt-caching note (plan §8): the per-image document context lives in the
    **user** prompt, never the system prompt. The system prompt is therefore
    byte-stable per ``image_type`` across the whole document, so provider
    prompt-caching (OpenAI automatic at 1024+ tokens, up to ~90% off input
    cost) actually hits — the cached prefix is the same bytes every request.
    """
    if image_type == ImageType.decorative:
        return "{}", ""

    template = EXTRACTOR_PROMPTS.get(image_type)
    if template is None:
        template = FALLBACK_TEMPLATE

    # Diagram templates carry a {fidelity} placeholder; others don't. Pass it
    # unconditionally — str.format ignores unreferenced kwargs.
    system = template.format(
        discipline_line=EXTRACT_DISCIPLINE_LINE,
        fidelity=_DIAGRAM_FIDELITY,
    )
    user = _context_user_prompt(
        "Extract structured data from the attached image using the JSON schema "
        "described in the system instructions.",
        heading_chain,
        surrounding_paragraphs,
    )
    return system, user


def _context_user_prompt(
    instruction: str,
    heading_chain: str,
    surrounding_paragraphs: str,
) -> str:
    """Compose a user prompt carrying the per-image (variable) document context.

    Kept out of the system prompt so the cacheable system prefix stays
    byte-stable across images (plan §8 prompt-caching).
    """
    return (
        f"{instruction}\n\n"
        f"Document context:\n"
        f"- Heading chain: {heading_chain}\n"
        f"- Surrounding paragraphs: {surrounding_paragraphs}"
    )
