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


_DESCRIPTION_TEMPLATE = """\
You are extracting a free-form description of the attached image.

Output strict JSON in this exact shape:
{{"alt_text": str, "details": [str]}}

Rules:
- alt_text: 3-6 sentences covering subject, composition, mood, colors, and context.
- details: list of short factual observations (entities, text visible in the image, axis labels, etc.).

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_CHART_BAR_TEMPLATE = """\
You are extracting structured data from a BAR CHART image.

Each bar is one data point: x = category label, y = numeric value. Grouped / \
stacked bars become multiple series.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_CHART_LINE_TEMPLATE = """\
You are extracting structured data from a LINE CHART image.

Each point on a line is one data point. Multiple lines become multiple series. \
Sample interpolated curves at meaningful intervals when points are not labeled.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_CHART_PIE_TEMPLATE = """\
You are extracting structured data from a PIE / DONUT CHART image.

Each slice is one data point: x = slice label, y = value (percentage is optional, put it in y if only percentages are shown).

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_CHART_SCATTER_TEMPLATE = """\
You are extracting structured data from a SCATTER PLOT image.

Each marker is one data point. Multiple marker groups become multiple series.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_CHART_OTHER_TEMPLATE = """\
You are extracting structured data from a chart that is neither bar, line, pie, nor scatter (radar, bubble, heatmap, funnel, etc.).

Approximate the geometry as best you can; encode series and points in the same shape regardless of chart kind.

Output strict JSON in this exact shape:
{{"title": str, "x_label": str, "y_label": str, "series": [{{"name": str, "points": [{{"x": any, "y": any}}]}}], "notes": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_TABLE_TEMPLATE = """\
You are extracting a TABLE from an image.

Preserve the exact structure of the original table. Repeat merged-cell values in every row they span. Use empty strings for blank cells. Do not invent data.

Output strict JSON in this exact shape:
{{"caption": str, "headers": [str], "rows": [[str]]}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_DIAGRAM_FLOW_TEMPLATE = """\
You are converting a FLOWCHART image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `graph TD` (top-down) or `graph LR` (left-right) syntax. Nodes use `id[label]`; edges use `-->`. Use `{{shape}}` for decisions, `[rect]` for steps, `((circle))` for start/end.

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_DIAGRAM_SEQUENCE_TEMPLATE = """\
You are converting a SEQUENCE DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `sequenceDiagram` syntax with `participant`, `Actor->>Actor: message`, `Actor-->>Actor: reply`, and `Note over Actor: text`.

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_DIAGRAM_STATE_TEMPLATE = """\
You are converting a STATE-MACHINE DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `stateDiagram-v2` syntax with `[*]` for start/end, `stateName` for states, and `A --> B: transition label`.

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_DIAGRAM_CLASS_TEMPLATE = """\
You are converting a CLASS / OBJECT / ER DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `classDiagram` syntax (preferred) or `erDiagram` for entity-relationship variants. Include classes, attributes, methods, and relationships (`*--`, `o--`, `<|--`, `-->`).

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_DIAGRAM_ARCHITECTURE_TEMPLATE = """\
You are converting a SYSTEM / COMPONENT ARCHITECTURE DIAGRAM image into Mermaid syntax.

CONSTRAINT: Use valid Mermaid `graph TD` (or `graph LR`) with one `subgraph "SubsystemName"` ... `end` block per major subsystem. Edges between components use `-->`. Label edges with the protocol or data flow when known.

Output strict JSON in this exact shape:
{{"mermaid": str, "caption": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_EQUATION_TEMPLATE = """\
You are extracting a mathematical EQUATION from an image.

Output strict JSON in this exact shape:
{{"latex": str, "caption": str}}

Rules:
- latex: LaTeX source usable inside $$...$$ or \\[...\\]. Use standard amsmath commands. No markdown fences.
- caption: short human-readable description of what the equation states.

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

{discipline_line}\
"""


_SCREENSHOT_TEMPLATE = """\
You are extracting structured data from a SOFTWARE UI SCREENSHOT image.

Identify the application and the visible UI region, then enumerate the meaningful on-screen regions (panels, toolbars, dialogs) with short OCR extracts where text is present.

Output strict JSON in this exact shape:
{{"application": str, "area": str, "regions": [{{"name": str, "description": str, "ocr_text": str}}], "summary": str}}

Document context:
- Heading chain: {heading_chain}
- Surrounding paragraphs: {surrounding_paragraphs}

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
    """
    if image_type == ImageType.decorative:
        return "{}", ""

    template = EXTRACTOR_PROMPTS.get(image_type)
    if template is None:
        template = FALLBACK_TEMPLATE

    system = template.format(
        heading_chain=heading_chain,
        surrounding_paragraphs=surrounding_paragraphs,
        discipline_line=EXTRACT_DISCIPLINE_LINE,
    )
    user = (
        "Extract structured data from the attached image using the JSON schema "
        "described in the system instructions."
    )
    return system, user
