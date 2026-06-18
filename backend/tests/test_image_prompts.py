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
        assert self.HEADING in user
        assert self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_line(self) -> None:
        system, user = self._build(ImageType.chart_line)
        assert "series" in system and "points" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_pie(self) -> None:
        system, user = self._build(ImageType.chart_pie)
        assert "series" in system and "points" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_scatter(self) -> None:
        system, user = self._build(ImageType.chart_scatter)
        assert "series" in system and "points" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_chart_other(self) -> None:
        system, user = self._build(ImageType.chart_other)
        assert "series" in system and "points" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Table (TablePayload: caption, headers, rows) ---

    def test_extract_prompt_for_table_image(self) -> None:
        system, user = self._build(ImageType.table_image)
        assert "caption" in system and "headers" in system and "rows" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Diagrams (DiagramPayload: mermaid, caption) ---

    def test_extract_prompt_for_diagram_flow(self) -> None:
        system, user = self._build(ImageType.diagram_flow)
        assert "mermaid" in system and "caption" in system
        assert "graph TD" in system or "graph LR" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_sequence(self) -> None:
        system, user = self._build(ImageType.diagram_sequence)
        assert "mermaid" in system and "caption" in system
        assert "sequenceDiagram" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_state(self) -> None:
        system, user = self._build(ImageType.diagram_state)
        assert "mermaid" in system and "caption" in system
        assert "stateDiagram-v2" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_class(self) -> None:
        system, user = self._build(ImageType.diagram_class)
        assert "mermaid" in system and "caption" in system
        assert "classDiagram" in system or "erDiagram" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_diagram_architecture(self) -> None:
        system, user = self._build(ImageType.diagram_architecture)
        assert "mermaid" in system and "caption" in system
        assert "subgraph" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Equation (EquationPayload: latex, caption) ---

    def test_extract_prompt_for_equation(self) -> None:
        system, user = self._build(ImageType.equation)
        assert "latex" in system and "caption" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Screenshot (ScreenshotPayload: application, area, regions, summary) ---

    def test_extract_prompt_for_screenshot_ui(self) -> None:
        system, user = self._build(ImageType.screenshot_ui)
        assert "application" in system and "regions" in system and "summary" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    # --- Description (DescriptionPayload: alt_text, details) ---

    def test_extract_prompt_for_figure_technical(self) -> None:
        system, user = self._build(ImageType.figure_technical)
        assert "alt_text" in system and "details" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_photo(self) -> None:
        system, user = self._build(ImageType.photo)
        assert "alt_text" in system and "details" in system
        assert self.HEADING in user and self.PARA in user
        assert system.rstrip().endswith("Return JSON only, no prose, no code fences.")

    def test_extract_prompt_for_other(self) -> None:
        system, user = self._build(ImageType.other)
        assert "alt_text" in system and "details" in system
        assert self.HEADING in user and self.PARA in user
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


# ---------------------------------------------------------------------------
# Prompt-caching: stable system prefix (plan §8)
# ---------------------------------------------------------------------------


class TestCacheableSystemPrefix:
    """The system prompt must be byte-identical across images of the same type
    so provider prompt-caching hits; per-image context lives in the user prompt.
    """

    def test_extract_system_prompt_is_context_invariant(self) -> None:
        sys_a, user_a = build_extract_prompt(
            ImageType.chart_bar,
            heading_chain="H1: Revenue",
            surrounding_paragraphs="First doc context.",
        )
        sys_b, user_b = build_extract_prompt(
            ImageType.chart_bar,
            heading_chain="H1: Costs > H2: Detail",
            surrounding_paragraphs="A totally different paragraph.",
        )
        # System prefix identical -> cacheable.
        assert sys_a == sys_b
        # The variable context moved to the user prompt.
        assert "Revenue" in user_a and "Revenue" not in sys_a
        assert "different paragraph" in user_b and "different paragraph" not in sys_b

    def test_classify_system_prompt_is_context_invariant(self) -> None:
        sys_a, user_a = build_classify_prompt(
            heading_chain="H1: A", surrounding_paragraphs="alpha"
        )
        sys_b, user_b = build_classify_prompt(
            heading_chain="H1: B", surrounding_paragraphs="beta"
        )
        assert sys_a == sys_b
        assert "alpha" in user_a and "beta" in user_b

    def test_extract_user_prompt_carries_context(self) -> None:
        _, user = build_extract_prompt(
            ImageType.table_image,
            heading_chain="H1: Results",
            surrounding_paragraphs="Table caption text.",
        )
        assert "H1: Results" in user
        assert "Table caption text." in user


# ---------------------------------------------------------------------------
# Universal anti-fabrication guardrail (PDF-agnostic regression lock).
#
# The openskill baseline showed the VLM inventing mermaid edges for a non-graph
# comparison figure. The fix is a soft guardrail that must bind EVERY diagram
# type on EVERY document, plus its mirror in the batched path. These tests lock
# the contract so the rule can never silently regress — they assert the
# constraint exists for all diagram_* types, not that any specific figure
# converts a specific way (which would overfit to one PDF).
# ---------------------------------------------------------------------------


# Phrases that, together, encode "reproduce only what is drawn; don't invent
# structure; comparison panels are not a flow". Matched case-insensitively.
_FIDELITY_MARKERS = ("only", "drawn", "never invent")
_PANEL_ESCAPE_MARKERS = ("comparison", "panel")


class TestDiagramFidelityGuardrail:
    """Every diagram_* extractor prompt must carry the no-fabrication rule."""

    @pytest.mark.parametrize("image_type", list(DIAGRAM_TYPES.keys()))
    def test_each_diagram_prompt_binds_to_only_what_is_drawn(
        self, image_type: ImageType
    ) -> None:
        system, _ = build_extract_prompt(
            image_type, heading_chain="", surrounding_paragraphs=""
        )
        low = system.lower()
        for marker in _FIDELITY_MARKERS:
            assert marker in low, (
                f"{image_type} extract prompt missing fidelity marker {marker!r}"
            )

    @pytest.mark.parametrize("image_type", list(DIAGRAM_TYPES.keys()))
    def test_each_diagram_prompt_has_comparison_panel_escape(
        self, image_type: ImageType
    ) -> None:
        system, _ = build_extract_prompt(
            image_type, heading_chain="", surrounding_paragraphs=""
        )
        low = system.lower()
        assert any(m in low for m in _PANEL_ESCAPE_MARKERS), (
            f"{image_type} prompt lacks the comparison-panel escape hatch"
        )

    def test_non_diagram_prompt_does_not_carry_diagram_fidelity(self) -> None:
        # The chart path has its own discipline; the diagram fidelity block is
        # diagram-only so the cacheable prefixes stay distinct.
        system, _ = build_extract_prompt(
            ImageType.chart_bar, heading_chain="", surrounding_paragraphs=""
        )
        assert "never invent an edge" not in system.lower()


class TestBatchFidelityGuardrail:
    """The batched classify+extract prompt mirrors the same guardrail."""

    def test_batch_system_prompt_binds_diagram_fidelity(self) -> None:
        from app.prompts.image_batch import build_batch_system_prompt

        low = build_batch_system_prompt().lower()
        for marker in _FIDELITY_MARKERS:
            assert marker in low, f"batch prompt missing fidelity marker {marker!r}"

    def test_batch_system_prompt_has_comparison_panel_escape(self) -> None:
        from app.prompts.image_batch import build_batch_system_prompt

        low = build_batch_system_prompt().lower()
        assert any(m in low for m in _PANEL_ESCAPE_MARKERS), (
            "batch prompt lacks the comparison-panel escape hatch"
        )


class TestAugmentGateIsConservative:
    """Hard safety backbone: a fallible extraction must never DESTROY the source.

    Only provably-lossless conversions (equation->LaTeX) and omissions
    (decorative) may replace the original image. Every visual type — charts,
    ALL diagrams, tables, technical figures, photos, screenshots — must keep the
    source image alongside the extraction, so a wrong mermaid/table misleads at
    worst but never loses the real figure. This holds for any PDF.
    """

    def test_only_equation_and_decorative_replace_destructively(self) -> None:
        from app.processors.image_understanding import _safe_to_replace

        replaceable = {t for t in ImageType if _safe_to_replace(t)}
        assert replaceable == {ImageType.equation, ImageType.decorative}

    @pytest.mark.parametrize("image_type", list(DIAGRAM_TYPES.keys()))
    def test_no_diagram_type_is_destructively_replaceable(
        self, image_type: ImageType
    ) -> None:
        from app.processors.image_understanding import _safe_to_replace

        assert not _safe_to_replace(image_type)
