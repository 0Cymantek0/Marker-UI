"""Tests for VLM prompt templates: classifier + 17 per-type extractors (Phase 1).

Covers:
- Classifier template lists all 17 ImageType values, demands strict JSON,
  injects heading-chain + ±N paragraphs context window.
- Each of 17 ImageType values has a matching extractor prompt that embeds
  the correct payload submodel schema and ends with JSON-mode discipline.
- Decorative type short-circuits with static "{}" (no second VLM call).
- Unknown / future ImageType falls back to DescriptionPayload prompt.
"""

from __future__ import annotations

import pytest

from app.models.image_understanding import ImageType
from app.prompts.image_classify import build_classify_prompt
from app.prompts.image_extract import (
    DIAGRAM_TYPES,
    EXTRACTOR_PROMPTS,
    build_extract_prompt,
)


# ---------------------------------------------------------------------------
# Classifier prompt
# ---------------------------------------------------------------------------


class TestClassifyPrompt:
    """Classifier template — 17-type taxonomy + context window injection."""

    def test_returns_tuple_of_two_strings(self) -> None:
        result = build_classify_prompt(heading_chain="H1: Foo", surrounding_paragraphs="bar")
        assert isinstance(result, tuple)
        assert len(result) == 2
        system, user = result
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str)

    def test_lists_all_17_types(self) -> None:
        system, _ = build_classify_prompt(heading_chain="H1: Foo", surrounding_paragraphs="bar")
        for image_type in ImageType:
            assert image_type.value in system, f"Missing type in classifier: {image_type.value}"

    def test_ends_with_json_discipline_line(self) -> None:
        system, _ = build_classify_prompt(heading_chain="", surrounding_paragraphs="")
        assert system.rstrip().endswith(
            "Return JSON only, no prose, no code fences, no explanations."
        )

    def test_injects_heading_chain(self) -> None:
        heading = "H1: Revenue > H2: Q4 Results"
        system, user = build_classify_prompt(heading_chain=heading, surrounding_paragraphs="")
        assert heading in system or heading in user

    def test_injects_surrounding_paragraphs(self) -> None:
        para = "The chart below summarizes our quarterly performance."
        system, user = build_classify_prompt(heading_chain="", surrounding_paragraphs=para)
        assert para in system or para in user

    def test_handles_empty_context_without_raising(self) -> None:
        system, user = build_classify_prompt(heading_chain="", surrounding_paragraphs="")
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str)

    def test_explains_local_context_window(self) -> None:
        """System prompt must explain that context is provided to disambiguate."""
        system, _ = build_classify_prompt(heading_chain="", surrounding_paragraphs="")
        assert "context" in system.lower()
        assert "disambiguate" in system.lower() or "ambiguous" in system.lower()

    def test_demands_strict_json_shape(self) -> None:
        """System prompt must show the required JSON output shape."""
        system, _ = build_classify_prompt(heading_chain="", surrounding_paragraphs="")
        assert "image_type" in system
        assert "confidence" in system
        assert "rationale" in system


# ---------------------------------------------------------------------------
# Per-type extractor prompts (17 ImageType values)
# ---------------------------------------------------------------------------


class TestExtractorPromptsPerType:
    """One test per ImageType value — schema fields + JSON discipline + context."""

    HEADING = "H1: Revenue > H2: Q4 Results"
    PARA = "The chart below summarizes our quarterly performance."

    def _build(self, image_type: ImageType) -> tuple[str, str]:
        return build_extract_prompt(
            image_type=image_type,
            heading_chain=self.HEADING,
            surrounding_paragraphs=self.PARA,
        )

    # --- Charts (ChartPayload: title, x_label, y_label, series, points, notes) ---

    def test_extract_prompt_for_chart_bar(self) -> None:
        system, user = self._build(ImageType.chart_bar)
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str)
        assert "series" in system
        assert "points" in system
        assert self.HEADING in system
        assert self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_line(self) -> None:
        system, _ = self._build(ImageType.chart_line)
        assert "series" in system and "points" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_pie(self) -> None:
        system, _ = self._build(ImageType.chart_pie)
        assert "series" in system and "points" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_scatter(self) -> None:
        system, _ = self._build(ImageType.chart_scatter)
        assert "series" in system and "points" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_other(self) -> None:
        system, _ = self._build(ImageType.chart_other)
        assert "series" in system and "points" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Table (TablePayload: caption, headers, rows) ---

    def test_extract_prompt_for_table_image(self) -> None:
        system, _ = self._build(ImageType.table_image)
        assert "caption" in system and "headers" in system and "rows" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Diagrams (DiagramPayload: mermaid, caption) ---

    def test_extract_prompt_for_diagram_flow(self) -> None:
        system, _ = self._build(ImageType.diagram_flow)
        assert "mermaid" in system and "caption" in system
        assert "graph TD" in system or "graph LR" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_sequence(self) -> None:
        system, _ = self._build(ImageType.diagram_sequence)
        assert "mermaid" in system and "caption" in system
        assert "sequenceDiagram" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_state(self) -> None:
        system, _ = self._build(ImageType.diagram_state)
        assert "mermaid" in system and "caption" in system
        assert "stateDiagram-v2" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_class(self) -> None:
        system, _ = self._build(ImageType.diagram_class)
        assert "mermaid" in system and "caption" in system
        assert "classDiagram" in system or "erDiagram" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_architecture(self) -> None:
        system, _ = self._build(ImageType.diagram_architecture)
        assert "mermaid" in system and "caption" in system
        assert "subgraph" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Equation (EquationPayload: latex, caption) ---

    def test_extract_prompt_for_equation(self) -> None:
        system, _ = self._build(ImageType.equation)
        assert "latex" in system and "caption" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Screenshot (ScreenshotPayload: application, area, regions, summary) ---

    def test_extract_prompt_for_screenshot_ui(self) -> None:
        system, _ = self._build(ImageType.screenshot_ui)
        assert "application" in system and "regions" in system and "summary" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Description (DescriptionPayload: alt_text, details) ---

    def test_extract_prompt_for_figure_technical(self) -> None:
        system, _ = self._build(ImageType.figure_technical)
        assert "alt_text" in system and "details" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_photo(self) -> None:
        system, _ = self._build(ImageType.photo)
        assert "alt_text" in system and "details" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_other(self) -> None:
        system, _ = self._build(ImageType.other)
        assert "alt_text" in system and "details" in system
        assert self.HEADING in system and self.PARA in system
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Decorative (special-case: static "{}", no second VLM call) ---

    def test_extract_prompt_for_decorative(self) -> None:
        system, user = self._build(ImageType.decorative)
        # Decorative short-circuits: no schema, no placeholders, no discipline line.
        assert system == "{}"


# ---------------------------------------------------------------------------
# Decorative static-empty-JSON behavior (no second VLM call)
# ---------------------------------------------------------------------------


class TestDecorativeStaticEmpty:
    """Decorative images skip the second VLM call — return static "{}" payload."""

    def test_extract_prompt_decorative_returns_static_empty_json(self) -> None:
        system, user = build_extract_prompt(
            image_type=ImageType.decorative,
            heading_chain="H1: Anything",
            surrounding_paragraphs="Anything.",
        )
        # The returned system prompt is literally the empty JSON object — no
        # format substitution, no schema fields, no discipline line.
        assert system == "{}"
        # And the stored template is also "{}" (no second VLM call needed).
        assert EXTRACTOR_PROMPTS[ImageType.decorative] == "{}"

    def test_extract_prompt_decorative_ignores_context(self) -> None:
        """Decorative never consumes context — should output the same static value."""
        a = build_extract_prompt(
            image_type=ImageType.decorative,
            heading_chain="H1: A",
            surrounding_paragraphs="A",
        )
        b = build_extract_prompt(
            image_type=ImageType.decorative,
            heading_chain="H1: B > H2: Different",
            surrounding_paragraphs="Completely different text.",
        )
        assert a[0] == b[0] == "{}"


# ---------------------------------------------------------------------------
# Defensive: unknown ImageType falls back to DescriptionPayload prompt
# ---------------------------------------------------------------------------


class TestExtractorFallback:
    """Defensive fallback for unknown / future ImageType values."""

    def test_extract_prompt_unknown_type_falls_back_to_description(self) -> None:
        sentinel = object()
        system, user = build_extract_prompt(
            image_type=sentinel,
            heading_chain="H1: Foo",
            surrounding_paragraphs="bar",
        )
        assert isinstance(system, str) and len(system) > 0
        # DescriptionPayload schema fields
        assert "alt_text" in system
        assert "details" in system
        # Fallback is still disciplined
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")


# ---------------------------------------------------------------------------
# Module-level dict exports
# ---------------------------------------------------------------------------


class TestModuleExports:
    """EXTRACTOR_PROMPTS and DIAGRAM_TYPES dicts are exported with expected entries."""

    def test_extractor_prompts_covers_all_types_except_decorative_special_case(self) -> None:
        # EXTRACTOR_PROMPTS has an entry for every ImageType (decorative is "{}").
        for image_type in ImageType:
            assert image_type in EXTRACTOR_PROMPTS, f"Missing extractor for {image_type.value}"

    def test_diagram_types_covers_five_diagram_subtypes(self) -> None:
        diagram_members = {
            ImageType.diagram_flow,
            ImageType.diagram_sequence,
            ImageType.diagram_state,
            ImageType.diagram_class,
            ImageType.diagram_architecture,
        }
        assert set(DIAGRAM_TYPES.keys()) == diagram_members
