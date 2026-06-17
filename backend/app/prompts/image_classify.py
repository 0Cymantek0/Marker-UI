"""Classifier prompt template for VLM image-type classification (Phase 1).

Builds the system + user prompt pair sent to the VLM on the FIRST call to
classify an image into one of 17 ImageType values. The system prompt carries
the full taxonomy + JSON-mode discipline; the user prompt carries the local
context (heading chain + ±N paragraphs) that the model uses to disambiguate
when image content alone is ambiguous (decision #1).
"""

from __future__ import annotations

from app.models.image_understanding import ImageType


CLASSIFY_DISCIPLINE_LINE = "Return JSON only, no prose, no code fences, no explanations."


TYPE_DEFINITIONS: dict[ImageType, str] = {
    ImageType.chart_bar: "chart_bar — bar chart; discrete categories on one axis, quantitative values on the other.",
    ImageType.chart_line: "chart_line — line chart showing trend over a continuous variable such as time.",
    ImageType.chart_pie: "chart_pie — pie / donut chart showing proportions of a whole.",
    ImageType.chart_scatter: "chart_scatter — scatter plot showing correlation between two continuous variables.",
    ImageType.chart_other: "chart_other — chart that does not fit bar/line/pie/scatter (radar, bubble, heatmap, etc.).",
    ImageType.table_image: "table_image — a table rendered as an image (rows and columns of cells).",
    ImageType.diagram_flow: "diagram_flow — flowchart showing steps and decision branches.",
    ImageType.diagram_sequence: "diagram_sequence — UML-style sequence diagram showing message flow between actors.",
    ImageType.diagram_state: "diagram_state — UML-style state-machine diagram.",
    ImageType.diagram_class: "diagram_class — UML-style class / object / ER diagram.",
    ImageType.diagram_architecture: "diagram_architecture — system / component architecture diagram showing boxes and connections.",
    ImageType.equation: "equation — a standalone mathematical equation or formula.",
    ImageType.screenshot_ui: "screenshot_ui — screenshot of a software UI (desktop, web, mobile, terminal).",
    ImageType.figure_technical: "figure_technical — technical figure (circuit, mechanical drawing, map, plot in a paper).",
    ImageType.photo: "photo — photographic image (real-world scene, product shot, headshot).",
    ImageType.decorative: "decorative — purely decorative image (icon, ornament, background, divider) with no informational content.",
    ImageType.other: "other — image that does not fit any of the above categories.",
}


_CLASSIFY_SYSTEM_TEMPLATE = """\
You are an image classification system for technical documents. Classify the \
attached image into exactly one of the following 17 types.

TYPES:
{type_definitions}

Document context (heading chain + surrounding text) is provided in the user \
prompt to disambiguate. Use it to refine classification when image content \
alone is ambiguous.

Output strict JSON in this exact shape and nothing else:
{{"image_type": "<one of the 17 type values>", "confidence": <float 0.0-1.0>, "rationale": "<short justification>"}}

{discipline_line}\
"""


def build_classify_prompt(
    heading_chain: str,
    surrounding_paragraphs: str,
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the classifier VLM call.

    Args:
        heading_chain: Pre-formatted heading breadcrumb, e.g.
            ``"H1: Revenue > H2: Q4 Results"``. May be empty.
        surrounding_paragraphs: Pre-formatted string of ±N paragraphs around
            the image (typically ±2). May be empty.

    Returns:
        Tuple of (system_prompt, user_prompt). The system prompt carries the
        taxonomy + JSON discipline; the user prompt carries the local context
        and the implicit image attachment.

    Prompt-caching note (plan §8): the per-image document context lives in the
    **user** prompt only. The system prompt (taxonomy + schema, several KB) is
    byte-identical across every image in the document, so provider
    prompt-caching actually hits the same cached prefix on each request.
    """
    type_defs_block = "\n".join(f"- {line}" for line in TYPE_DEFINITIONS.values())
    system = _CLASSIFY_SYSTEM_TEMPLATE.format(
        type_definitions=type_defs_block,
        discipline_line=CLASSIFY_DISCIPLINE_LINE,
    )
    user = (
        "Classify the attached image into one of the 17 types listed in the "
        "system instructions.\n\n"
        f"Heading chain:\n{heading_chain}\n\n"
        f"Surrounding paragraphs (±2 around the image):\n{surrounding_paragraphs}"
    )
    return system, user
