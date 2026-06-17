"""Tests for image understanding Pydantic schemas (Phase 1)."""

import pytest
from pydantic import ValidationError

from app.models.image_understanding import (
    ChartPayload,
    ClassificationResult,
    DescriptionPayload,
    DiagramPayload,
    EquationPayload,
    ExtractionResult,
    ImageHandlingMode,
    ImageType,
    ImageUnderstandingConfig,
    Point,
    ScreenshotPayload,
    Series,
    TablePayload,
)


# ---------------------------------------------------------------------------
# ImageType enum
# ---------------------------------------------------------------------------


class TestImageType:
    """17-value taxonomy locked for Phase 1."""

    EXPECTED_MEMBERS = {
        "chart_bar",
        "chart_line",
        "chart_pie",
        "chart_scatter",
        "chart_other",
        "table_image",
        "diagram_flow",
        "diagram_sequence",
        "diagram_state",
        "diagram_class",
        "diagram_architecture",
        "equation",
        "screenshot_ui",
        "figure_technical",
        "photo",
        "decorative",
        "other",
    }

    def test_has_17_values(self) -> None:
        members = set(ImageType.__members__)
        assert len(members) == 17
        assert members == self.EXPECTED_MEMBERS

    def test_all_str_values(self) -> None:
        for name in self.EXPECTED_MEMBERS:
            assert ImageType[name].value == name


# ---------------------------------------------------------------------------
# ImageHandlingMode enum
# ---------------------------------------------------------------------------


class TestImageHandlingMode:
    """3-way UX toggle per m0104."""

    def test_has_3_values(self) -> None:
        members = set(ImageHandlingMode.__members__)
        assert len(members) == 3
        assert "understanding" in members
        assert "extraction" in members
        assert "both" in members

    def test_str_values(self) -> None:
        assert ImageHandlingMode.understanding.value == "understanding"
        assert ImageHandlingMode.extraction.value == "extraction"
        assert ImageHandlingMode.both.value == "both"

    def test_default_is_extraction(self) -> None:
        assert ImageHandlingMode.extraction == ImageHandlingMode("extraction")


# ---------------------------------------------------------------------------
# ClassificationResult
# ---------------------------------------------------------------------------


class TestClassificationResult:
    """VLM classification output."""

    def test_requires_image_type(self) -> None:
        with pytest.raises(ValidationError):
            ClassificationResult()  # missing required field

    def test_defaults(self) -> None:
        r = ClassificationResult(image_type=ImageType.photo)
        assert r.image_type == ImageType.photo
        assert r.confidence == 0.0
        assert r.rationale == ""

    def test_confidence_bounds(self) -> None:
        r1 = ClassificationResult(image_type=ImageType.photo, confidence=0.0)
        assert r1.confidence == 0.0
        r2 = ClassificationResult(image_type=ImageType.photo, confidence=1.0)
        assert r2.confidence == 1.0
        with pytest.raises(ValidationError):
            ClassificationResult(image_type=ImageType.photo, confidence=-0.1)
        with pytest.raises(ValidationError):
            ClassificationResult(image_type=ImageType.photo, confidence=1.1)


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------


class TestExtractionResult:
    """VLM extraction output with optional debug context_window."""

    def test_requires_image_type(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult()

    def test_defaults(self) -> None:
        r = ExtractionResult(image_type=ImageType.table_image)
        assert r.image_type == ImageType.table_image
        assert r.payload == {}
        assert r.raw_response == ""
        assert r.confidence == 0.0
        assert r.error is None
        assert r.context_window is None

    def test_with_context_window(self) -> None:
        cw: dict = {"model": "gemini-2.5-flash", "tokens": 1024}
        r = ExtractionResult(image_type=ImageType.diagram_flow, context_window=cw)
        assert r.context_window == cw

    def test_with_payload(self) -> None:
        r = ExtractionResult(
            image_type=ImageType.equation,
            payload={"latex": "E=mc^2"},
            raw_response="raw VLM text",
            confidence=0.95,
            error=None,
        )
        assert r.payload["latex"] == "E=mc^2"
        assert r.raw_response == "raw VLM text"
        assert r.confidence == 0.95


# ---------------------------------------------------------------------------
# Chart payload submodel
# ---------------------------------------------------------------------------


class TestChartPayload:
    """Chart-specific structured data."""

    def test_defaults(self) -> None:
        p = ChartPayload()
        assert p.title == ""
        assert p.x_label == ""
        assert p.y_label == ""
        assert p.series == []
        assert p.notes == ""

    def test_with_series(self) -> None:
        p = ChartPayload(
            title="Revenue",
            series=[
                Series(
                    name="2024",
                    points=[Point(x=1.0, y=100.0), Point(x=2.0, y=200.0)],
                ),
            ],
        )
        assert p.title == "Revenue"
        assert len(p.series) == 1
        assert p.series[0].name == "2024"
        assert p.series[0].points[0].x == 1.0


# ---------------------------------------------------------------------------
# Table payload submodel
# ---------------------------------------------------------------------------


class TestTablePayload:
    """Table extraction payload."""

    def test_defaults(self) -> None:
        t = TablePayload()
        assert t.caption == ""
        assert t.headers == []
        assert t.rows == []

    def test_with_rows(self) -> None:
        t = TablePayload(
            caption="Sales Data",
            headers=["Name", "Amount"],
            rows=[["Alice", "100"], ["Bob", "200"]],
        )
        assert t.caption == "Sales Data"
        assert t.headers == ["Name", "Amount"]
        assert len(t.rows) == 2


# ---------------------------------------------------------------------------
# Diagram payload submodel
# ---------------------------------------------------------------------------


class TestDiagramPayload:
    """Diagram extraction payload (mermaid)."""

    def test_defaults(self) -> None:
        d = DiagramPayload()
        assert d.mermaid == ""
        assert d.caption == ""

    def test_with_mermaid(self) -> None:
        d = DiagramPayload(mermaid="graph TD; A-->B;", caption="Flow diagram")
        assert "graph TD" in d.mermaid
        assert d.caption == "Flow diagram"


# ---------------------------------------------------------------------------
# Equation payload submodel
# ---------------------------------------------------------------------------


class TestEquationPayload:
    """Equation extraction payload (LaTeX)."""

    def test_defaults(self) -> None:
        e = EquationPayload()
        assert e.latex == ""
        assert e.caption == ""

    def test_with_latex(self) -> None:
        e = EquationPayload(latex="E=mc^2", caption="Mass-energy equivalence")
        assert e.latex == "E=mc^2"
        assert e.caption == "Mass-energy equivalence"


# ---------------------------------------------------------------------------
# Screenshot payload submodel
# ---------------------------------------------------------------------------


class TestScreenshotPayload:
    """Screenshot / UI region extraction payload."""

    def test_defaults(self) -> None:
        s = ScreenshotPayload()
        assert s.application == ""
        assert s.area == ""
        assert s.regions == []
        assert s.summary == ""

    def test_with_regions(self) -> None:
        s = ScreenshotPayload(
            application="VS Code",
            area="editor",
            regions=[
                {"name": "sidebar", "description": "File explorer", "ocr_text": "src/"},
            ],
            summary="IDE screenshot with file explorer",
        )
        assert s.application == "VS Code"
        assert len(s.regions) == 1
        assert s.regions[0].name == "sidebar"


# ---------------------------------------------------------------------------
# Description payload submodel
# ---------------------------------------------------------------------------


class TestDescriptionPayload:
    """Alt-text / free-form image description."""

    def test_defaults(self) -> None:
        d = DescriptionPayload()
        assert d.alt_text == ""
        assert d.details == []

    def test_with_details(self) -> None:
        d = DescriptionPayload(
            alt_text="A bar chart showing revenue growth",
            details=["X-axis: quarters", "Y-axis: USD"],
        )
        assert "bar chart" in d.alt_text
        assert len(d.details) == 2


# ---------------------------------------------------------------------------
# ImageUnderstandingConfig — no cost cap (decision #5)
# ---------------------------------------------------------------------------


class TestImageUnderstandingConfig:
    """Pipeline configuration (no cost cap per decision #5)."""

    def test_defaults(self) -> None:
        c = ImageUnderstandingConfig()
        assert c.enabled is False
        assert c.image_handling_mode == ImageHandlingMode.extraction
        assert c.vlm_model is None
        assert c.max_images_per_doc == 50
        assert c.skip_decorative is True
        assert c.include_original_ref is True
        assert c.confidence_threshold == 0.7
        assert c.allow_cloud_vlm is False

    def test_no_cost_cap(self) -> None:
        """Decision #5: NO max_job_budget_usd field."""
        c = ImageUnderstandingConfig()
        assert not hasattr(c, "max_job_budget_usd")
        assert getattr(c, "max_job_budget_usd", None) is None
        with pytest.raises(AttributeError):
            c.max_job_budget_usd  # noqa: B018

    def test_rejects_negative_max_images(self) -> None:
        with pytest.raises(ValidationError):
            ImageUnderstandingConfig(max_images_per_doc=-1)

    def test_rejects_zero_max_images(self) -> None:
        with pytest.raises(ValidationError):
            ImageUnderstandingConfig(max_images_per_doc=0)

    def test_image_handling_mode_custom(self) -> None:
        c = ImageUnderstandingConfig(image_handling_mode=ImageHandlingMode.both)
        assert c.image_handling_mode == ImageHandlingMode.both

    def test_image_handling_mode_default(self) -> None:
        c = ImageUnderstandingConfig()
        assert c.image_handling_mode == ImageHandlingMode.extraction

    def test_confidence_threshold_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ImageUnderstandingConfig(confidence_threshold=-0.1)
        with pytest.raises(ValidationError):
            ImageUnderstandingConfig(confidence_threshold=1.1)
        c1 = ImageUnderstandingConfig(confidence_threshold=0.0)
        assert c1.confidence_threshold == 0.0
        c2 = ImageUnderstandingConfig(confidence_threshold=1.0)
        assert c2.confidence_threshold == 1.0

    def test_skip_decorative_default_true(self) -> None:
        c = ImageUnderstandingConfig()
        assert c.skip_decorative is True

    def test_include_original_ref_default_true(self) -> None:
        c = ImageUnderstandingConfig()
        assert c.include_original_ref is True

    def test_max_images_per_doc_custom(self) -> None:
        c = ImageUnderstandingConfig(max_images_per_doc=5)
        assert c.max_images_per_doc == 5
