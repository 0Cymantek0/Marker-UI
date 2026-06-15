from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from app.models.image_understanding import ClassificationResult, ExtractionResult, ImageType
from app.processors.image_understanding import (
    ImageUnderstandingProcessor,
    gather_local_context,
    render_extraction,
)
from app.services.marker_service import (
    IMAGE_UNDERSTANDING_PROCESSOR,
    build_marker_options,
    with_image_understanding_processor,
)


@dataclass
class FakeBlock:
    block_type: object
    text: str = ""
    heading_level: int = 2

    def raw_text(self, document):
        return self.text


class FakeBlockId:
    """Mimics marker.schema.blocks.BlockId.to_path()."""

    def __init__(self, path="_page_0_Picture_42"):
        self._path = path

    def to_path(self) -> str:
        return self._path


class FakePicture(FakeBlock):
    def __init__(self):
        from marker.schema import BlockTypes

        super().__init__(BlockTypes.Picture)
        self.html = None
        self.description = None
        self.ignore_for_output = False
        self.block_id = 42
        self.id = FakeBlockId()

    def get_image(self, document):
        return Image.new("RGB", (12, 12), color="white")


class FakeDocument:
    def __init__(self, blocks):
        self.blocks = blocks

    def contained_blocks(self, block_types):
        return [b for b in self.blocks if b.block_type in block_types]

    def get_prev_block(self, block):
        idx = self.blocks.index(block)
        return self.blocks[idx - 1] if idx > 0 else None

    def get_next_block(self, block):
        idx = self.blocks.index(block)
        return self.blocks[idx + 1] if idx + 1 < len(self.blocks) else None


class FakeVLM:
    def __init__(self, image_type=ImageType.chart_bar, payload=None):
        self.image_type = image_type
        self.payload = payload or {
            "title": "Revenue",
            "series": [
                {
                    "name": "FY26",
                    "points": [{"x": "Q1", "y": 10}, {"x": "Q2", "y": 14}],
                }
            ],
            "notes": "Values read from chart.",
        }
        self.calls = []

    def classify(self, image_bytes, mime_type, heading_chain, surrounding_paragraphs):
        self.calls.append(("classify", heading_chain, surrounding_paragraphs))
        return ClassificationResult(
            image_type=self.image_type,
            confidence=0.91,
            rationale="test",
        )

    def extract(self, image_bytes, mime_type, image_type, heading_chain, surrounding_paragraphs):
        self.calls.append(("extract", heading_chain, surrounding_paragraphs))
        return ExtractionResult(
            image_type=image_type,
            payload=self.payload,
            raw_response="{}",
            confidence=0.9,
        )


def _doc_with_picture():
    from marker.schema import BlockTypes

    h1 = FakeBlock(BlockTypes.SectionHeader, "Quarterly report", heading_level=1)
    before = FakeBlock(BlockTypes.Text, "Revenue rose before the chart.")
    picture = FakePicture()
    after = FakeBlock(BlockTypes.Text, "The chart shows Q2 growth.")
    return FakeDocument([h1, before, picture, after]), picture


def test_gather_local_context_uses_heading_chain_and_surrounding_text():
    document, picture = _doc_with_picture()

    heading_chain, surrounding = gather_local_context(document, picture, n=2)

    assert heading_chain == "Quarterly report"
    assert "Revenue rose before the chart." in surrounding
    assert "The chart shows Q2 growth." in surrounding


def test_gather_local_context_handles_none_heading_level():
    """Real marker SectionHeader blocks can carry heading_level=None.

    Regression: ``getattr(prev, "heading_level", 0)`` returned None (the
    attribute exists but is None), and ``None <= 1`` raised TypeError, aborting
    the whole conversion. Guard must treat None as "no level".
    """
    from marker.schema import BlockTypes

    h = FakeBlock(BlockTypes.SectionHeader, "Untitled section", heading_level=None)
    before = FakeBlock(BlockTypes.Text, "Some text before.")
    picture = FakePicture()
    document = FakeDocument([h, before, picture])

    # Must not raise.
    heading_chain, surrounding = gather_local_context(document, picture, n=2)

    assert heading_chain == "Untitled section"
    assert "Some text before." in surrounding


def test_extraction_mode_leaves_picture_untouched_and_skips_vlm():
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    ImageUnderstandingProcessor(
        {"image_handling_mode": "extraction"},
        vlm_service=vlm,
    )(document)

    assert picture.html is None
    assert picture.description is None
    assert vlm.calls == []


def test_processor_mutates_picture_in_place_for_chart_html():
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    proc = ImageUnderstandingProcessor(
        {"image_handling_mode": "understanding", "vlm_model": "gpt-4o"},
        vlm_service=vlm,
    )
    proc(document)

    assert picture.html is not None
    assert "marker-ui image-understanding: type=chart_bar model=gpt-4o confidence=0.91" in picture.html
    # Output is HTML (markdownify converts it to a Markdown table downstream).
    assert "<table>" in picture.html
    assert "<th>x</th>" in picture.html
    assert "<th>FY26</th>" in picture.html
    assert "<td>Q2</td>" in picture.html
    assert "<td>14</td>" in picture.html
    assert "original_image" not in picture.html
    assert len(vlm.calls) == 2

    # Sidecar metadata channel: pairs to the emitted image filename.
    assert proc.image_meta == [
        {
            "image_name": "_page_0_Picture_42.jpeg",
            "image_type": "chart_bar",
            "confidence": 0.91,
            "model": "gpt-4o",
            "omitted": False,
        }
    ]


def test_processor_both_mode_collects_sidecar_meta_for_description_type():
    """both-mode + photo sets html with comments and original img tag."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM(
        image_type=ImageType.photo,
        payload={"alt_text": "A detailed office photo.", "details": ["Bright room"]},
    )

    proc = ImageUnderstandingProcessor({"image_handling_mode": "both"}, vlm_service=vlm)
    proc(document)

    assert picture.description is None
    assert picture.html is not None
    assert "marker-ui image-understanding: type=photo" in picture.html
    assert "original_image: _page_0_Picture_42.jpeg" in picture.html
    assert "<p>A detailed office photo.</p>" in picture.html
    assert "<li>Bright room</li>" in picture.html
    assert '<img src="_page_0_Picture_42.jpeg" />' in picture.html
    assert proc.image_meta[0]["image_type"] == "photo"
    assert proc.image_meta[0]["omitted"] is False


def test_processor_both_mode_respects_include_original_ref_false():
    """When include_original_ref is off, both-mode drops the original <img>."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM(
        image_type=ImageType.photo,
        payload={"alt_text": "A photo.", "details": []},
    )

    proc = ImageUnderstandingProcessor(
        {"image_handling_mode": "both", "include_original_ref": False},
        vlm_service=vlm,
    )
    proc(document)

    assert picture.html is not None
    assert "<img" not in picture.html
    assert "original_image" not in picture.html


def test_processor_decorative_omits_picture_output():
    document, picture = _doc_with_picture()
    vlm = FakeVLM(image_type=ImageType.decorative, payload={})

    proc = ImageUnderstandingProcessor(
        {"image_handling_mode": "understanding"},
        vlm_service=vlm,
    )
    proc(document)

    assert picture.ignore_for_output is False
    assert "marker-ui image-understanding: type=decorative" in picture.html
    assert "Decorative element omitted." in picture.html
    assert "original_image" not in picture.html
    assert "<img" not in picture.html
    # Decorative images are flagged in the sidecar so the badge can show "omitted".
    assert proc.image_meta[0]["image_type"] == "decorative"
    assert proc.image_meta[0]["omitted"] is True


def test_render_extraction_diagram_outputs_mermaid_code_block():
    rendered = render_extraction(
        ImageType.diagram_flow,
        {"caption": "Flow", "mermaid": "graph TD\n    A-->B\n    B-->C"},
    )

    assert '<code class="language-mermaid">' in rendered
    assert "graph TD" in rendered


# ---------------------------------------------------------------------------
# Round-trip regression: render the processor HTML through marker's own
# Markdownify and assert the *final Markdown* is correct. This is the test
# that catches markdownify escaping LaTeX / Mermaid / bold — the bug that a
# unit test on picture.html alone misses.
# ---------------------------------------------------------------------------

def _markdownify():
    from marker.renderers.markdown import Markdownify

    return Markdownify(
        paginate_output=False,
        page_separator="-" * 48,
        inline_math_delimiters=("$", "$"),
        block_math_delimiters=("$$", "$$"),
        html_tables_in_markdown=False,
        heading_style="ATX",
        bullets="-",
        escape_misc=False,
        escape_underscores=True,
        escape_asterisks=True,
        escape_dollars=True,
        sub_symbol="<sub>",
        sup_symbol="<sup>",
    )


def test_roundtrip_chart_html_becomes_clean_markdown_table():
    html = render_extraction(
        ImageType.chart_bar,
        {
            "title": "Revenue_2024",
            "series": [{"name": "series_1", "points": [{"x": "Q1", "y": 100}]}],
            "notes": "",
        },
    )
    md = _markdownify().convert(html)
    # Underscores in headers survive (not escaped to series\_1 inside a table).
    assert "series_1" in md
    assert "| Q1" in md and "100" in md


def test_roundtrip_diagram_html_keeps_mermaid_fence_and_underscores():
    html = render_extraction(
        ImageType.diagram_flow,
        {"caption": "", "mermaid": "graph TD\n  start_node --> end_node"},
    )
    md = _markdownify().convert(html)
    assert "```mermaid" in md
    # Node ids keep their underscores — markdownify must not escape them.
    assert "start_node --> end_node" in md
    assert "\\_" not in md


def test_roundtrip_equation_html_becomes_valid_block_math():
    html = render_extraction(
        ImageType.equation,
        {"caption": "Pythagoras", "latex": "a_1 + b_2 = c^2"},
    )
    md = _markdownify().convert(html)
    # $$ delimiters survive unescaped and the subscripts are intact.
    assert "$$" in md
    assert "\\$" not in md
    assert "a_1 + b_2 = c^2" in md


def test_with_image_understanding_processor_appends_for_understanding_modes():
    assert with_image_understanding_processor({}, None) is None

    processors = with_image_understanding_processor(
        {"image_handling_mode": "both"},
        "marker.processors.order.OrderProcessor",
    )

    assert processors is not None
    assert "marker.processors.order.OrderProcessor" in processors
    assert IMAGE_UNDERSTANDING_PROCESSOR in processors


def test_build_marker_options_wires_processor_when_mode_is_both():
    llm_cfg = {"providers": [], "active": {"provider_id": "none", "model_id": ""}}

    opts = build_marker_options(llm_cfg, {"image_handling_mode": "both"})

    assert opts["processors"] == IMAGE_UNDERSTANDING_PROCESSOR
