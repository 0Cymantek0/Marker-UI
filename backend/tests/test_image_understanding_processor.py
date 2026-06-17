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


class FakeFigure(FakeBlock):
    """A marker Figure block (charts/diagrams land here, not in Picture).

    Carries a pre-set ``description`` to mimic marker's native
    LLMImageDescriptionProcessor having already run; the test asserts our
    processor overwrites it with structured ``html``.
    """

    def __init__(self):
        from marker.schema import BlockTypes

        super().__init__(BlockTypes.Figure)
        self.html = None
        self.description = "Native marker prose description."
        self.ignore_for_output = False
        self.block_id = 7
        self.id = FakeBlockId("_page_1_Figure_7")

    def get_image(self, document):
        return Image.new("RGB", (24, 24), color="white")


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
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
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
    # Augment gate (§6): chart is NOT safe to replace, so even in understanding
    # mode the original image is kept alongside the extracted table.
    assert "original_image" in picture.html
    assert '<img src="_page_0_Picture_42.jpeg" />' in picture.html
    assert len(vlm.calls) == 2

    # Sidecar metadata channel: pairs to the emitted image filename.
    assert proc.image_meta == [
        {
            "image_name": "_page_0_Picture_42.jpeg",
            "image_type": "chart_bar",
            "confidence": 0.91,
            "model": "gpt-4o",
            "omitted": False,
            "cost_usd": 0.0,
        }
    ]


def test_equation_replaces_image_in_understanding_mode():
    """Augment gate (§6/decision #4): equation->LaTeX IS safe to replace, so the
    original image is dropped in understanding mode (unlike charts/diagrams)."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM(
        image_type=ImageType.equation,
        payload={"latex": "a^2 + b^2 = c^2", "caption": "Pythagoras"},
    )

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    assert picture.html is not None
    assert "a^2 + b^2 = c^2" in picture.html
    # Safe-to-replace type: no kept original.
    assert "original_image" not in picture.html
    assert "<img" not in picture.html


def test_processor_processes_figure_blocks_not_just_pictures():
    """Regression: charts/diagrams are marker Figure blocks, not Picture blocks.

    The processor used to iterate only BlockTypes.Picture, so every Figure
    (the actual charts and diagrams in a paper) bypassed the VLM and fell back
    to marker's native ``Image <id> description:`` prose. The processor must
    now also pick up Figure blocks and overwrite the native description with
    structured html.
    """
    from marker.schema import BlockTypes

    h1 = FakeBlock(BlockTypes.SectionHeader, "Results", heading_level=1)
    figure = FakeFigure()
    document = FakeDocument([h1, figure])
    vlm = FakeVLM()  # default chart_bar payload

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    # VLM actually ran on the Figure (classify + extract).
    assert len(vlm.calls) == 2
    # Structured html replaces marker's native prose description.
    assert figure.html is not None
    assert "<table>" in figure.html
    assert "marker-ui image-understanding: type=chart_bar model=gpt-4o" in figure.html
    # Badge metadata pairs to the Figure's emitted filename.
    assert proc.image_meta[0]["image_name"] == "_page_1_Figure_7.jpeg"
    assert proc.image_meta[0]["image_type"] == "chart_bar"


def test_processor_both_mode_collects_sidecar_meta_for_description_type():
    """both-mode + photo sets html with comments and original img tag."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM(
        image_type=ImageType.photo,
        payload={"alt_text": "A detailed office photo.", "details": ["Bright room"]},
    )

    proc = ImageUnderstandingProcessor(
        {"image_handling_mode": "both", "allow_cloud_vlm": True},
        vlm_service=vlm,
    )
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
        {
            "image_handling_mode": "both",
            "include_original_ref": False,
            "allow_cloud_vlm": True,
        },
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
        {"image_handling_mode": "understanding", "allow_cloud_vlm": True},
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


def test_processor_logs_start_and_done_tally(caplog):
    """ISSUE-2: processor must log entry count and exit tally so an operator
    can tell 'no pictures' from 'all failed' from 'never ran'."""
    import logging

    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    with caplog.at_level(logging.INFO, logger="app.processors.image_understanding"):
        ImageUnderstandingProcessor(
            {
                "image_handling_mode": "understanding",
                "vlm_model": "gpt-4o",
                "allow_cloud_vlm": True,
            },
            vlm_service=vlm,
        )(document)

    messages = [r.getMessage() for r in caplog.records]
    start = next(m for m in messages if "ImageUnderstanding start" in m)
    done = next(m for m in messages if "ImageUnderstanding done" in m)
    assert "pictures=1" in start
    assert "model=gpt-4o" in start
    assert "processed=1" in done
    assert "failed=0" in done
    assert "total=1" in done


def test_processor_logs_failed_tally_when_vlm_raises(caplog):
    """A VLM that raises must be counted as failed, not silently dropped."""
    import logging

    document, picture = _doc_with_picture()

    class BoomVLM:
        def classify(self, *a, **k):
            raise RuntimeError("vlm down")

        def extract(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("extract should not run")

    with caplog.at_level(logging.INFO, logger="app.processors.image_understanding"):
        ImageUnderstandingProcessor(
            {"image_handling_mode": "understanding", "allow_cloud_vlm": True},
            vlm_service=BoomVLM(),
        )(document)

    done = next(
        r.getMessage() for r in caplog.records if "ImageUnderstanding done" in r.getMessage()
    )
    assert "processed=0" in done
    assert "failed=1" in done


def test_processor_counts_extraction_error_as_failed(caplog):
    """ISSUE-4: a 503 surfaces as ExtractionResult.error (not a raise). The
    processor must count that error branch as failed and leave the picture
    un-mutated, so the tally reflects the real outcome."""
    import logging

    from app.models.image_understanding import ClassificationResult, ExtractionResult

    document, picture = _doc_with_picture()

    class ErrorVLM:
        def classify(self, *a, **k):
            return ClassificationResult(
                image_type=ImageType.chart_bar, confidence=0.9, rationale="x"
            )

        def extract(self, *a, **k):
            return ExtractionResult(
                image_type=ImageType.chart_bar,
                payload={},
                raw_response="",
                confidence=0.0,
                error="503 Service Unavailable: model overloaded",
            )

    with caplog.at_level(logging.INFO, logger="app.processors.image_understanding"):
        proc = ImageUnderstandingProcessor(
            {"image_handling_mode": "understanding", "allow_cloud_vlm": True},
            vlm_service=ErrorVLM(),
        )
        proc(document)

    done = next(
        r.getMessage() for r in caplog.records if "ImageUnderstanding done" in r.getMessage()
    )
    assert "processed=0" in done
    assert "failed=1" in done
    # Picture left untouched; no sidecar meta for a failed extraction.
    assert picture.html is None
    assert proc.image_meta == []


def test_with_image_understanding_processor_appends_for_understanding_modes():
    assert with_image_understanding_processor({}, None) is None

    # Explicit caller-supplied list is honoured verbatim, just with ours added.
    processors = with_image_understanding_processor(
        {"image_handling_mode": "both"},
        "marker.processors.order.OrderProcessor",
    )

    assert processors is not None
    assert "marker.processors.order.OrderProcessor" in processors
    assert IMAGE_UNDERSTANDING_PROCESSOR in processors


def test_with_image_understanding_processor_preserves_default_pipeline():
    """ISSUE-1 regression: with NO explicit processor list, marker would replace
    its entire default pipeline (dropping every built-in LLM processor) the
    moment we pass a non-None list. We must expand the default pipeline and
    append ours so use_llm refinement still runs alongside image understanding.

    Bug 3: the native LLMImageDescriptionProcessor is the one exception — it
    handles the same Picture + Figure blocks ours does and runs earlier, so we
    drop it to avoid a discarded paid LLM call per image.
    """
    processors = with_image_understanding_processor(
        {"image_handling_mode": "understanding"},
        None,
    )

    assert processors is not None
    parts = processors.split(",")
    # Our processor is present...
    assert IMAGE_UNDERSTANDING_PROCESSOR in parts
    # ...and so are marker's other default LLM + structural processors.
    assert any("LLMTableProcessor" in p for p in parts)
    assert any("LLMEquationProcessor" in p for p in parts)
    assert any("TableProcessor" in p for p in parts)
    # ...but the redundant native image-description processor is dropped.
    assert not any("LLMImageDescriptionProcessor" in p for p in parts)
    # Ours runs last so Picture/Figure blocks are final before it mutates them.
    assert parts[-1] == IMAGE_UNDERSTANDING_PROCESSOR


def test_build_marker_options_wires_processor_when_mode_is_both():
    llm_cfg = {"providers": [], "active": {"provider_id": "none", "model_id": ""}}

    opts = build_marker_options(llm_cfg, {"image_handling_mode": "both"})

    parts = opts["processors"].split(",")
    assert IMAGE_UNDERSTANDING_PROCESSOR in parts
    # Default pipeline preserved (ISSUE-1): built-in LLM processors still wired.
    assert any("LLMTableProcessor" in p for p in parts)
    assert parts[-1] == IMAGE_UNDERSTANDING_PROCESSOR


class _RoutingDetectionModel:
    """Fake surya DetectionPredictor returning a fixed text-coverage profile."""

    def __init__(self, coverage_boxes, image_side=100):
        self._boxes = coverage_boxes
        self._side = image_side

    def __call__(self, images):
        from dataclasses import dataclass, field

        @dataclass
        class _Box:
            polygon: list

        @dataclass
        class _Result:
            bboxes: list
            image_bbox: list = field(
                default_factory=lambda: [0, 0, self._side, self._side]
            )

        return [_Result(bboxes=[_Box(polygon=b) for b in self._boxes])]


def test_processor_skips_decorative_route_without_vlm_call():
    """Tier-0: a decorative image (no detected text) is omitted locally with no
    VLM call — the §2 skip path that saves the most expensive resource."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
        detection_model=_RoutingDetectionModel(coverage_boxes=[]),  # no text
    )
    proc(document)

    # No VLM call happened — the router short-circuited locally.
    assert vlm.calls == []
    # Picture omitted as decorative.
    assert picture.html is not None
    assert "Decorative element omitted" in picture.html
    assert proc.image_meta == [
        {
            "image_name": "_page_0_Picture_42.jpeg",
            "image_type": "decorative",
            "confidence": 1.0,
            "model": "gpt-4o",
            "omitted": True,
            "cost_usd": 0.0,
        }
    ]


def test_local_only_decorative_route_does_not_construct_vlm_service():
    """Local-only privacy mode can omit decorative images without configured VLM."""
    document, picture = _doc_with_picture()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "allow_cloud_vlm": False,
        },
        detection_model=_RoutingDetectionModel(coverage_boxes=[]),
    )
    proc(document)

    assert "Decorative element omitted" in (picture.html or "")
    assert proc.image_meta[0]["model"] == "local-only"


def test_processor_routes_sparse_text_graphic_to_vlm():
    """A chart (sparse axis labels) still escalates to the VLM under the router."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    # Two tiny label boxes -> low density -> vlm route.
    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
        detection_model=_RoutingDetectionModel(
            coverage_boxes=[
                [[2, 2], [12, 2], [12, 8], [2, 8]],
                [[80, 90], [92, 90], [92, 96], [80, 96]],
            ]
        ),
    )
    proc(document)

    # VLM ran (classify + extract) and produced the chart table.
    assert len(vlm.calls) == 2
    assert "<table>" in (picture.html or "")


def test_router_disabled_preserves_legacy_path():
    """router_enabled=False -> every image hits the VLM (legacy behaviour),
    even with a detection model that would otherwise route to decorative."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "router_enabled": False,
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
        detection_model=_RoutingDetectionModel(coverage_boxes=[]),  # would skip
    )
    proc(document)

    # Router off: the decorative-looking image still went to the VLM.
    assert len(vlm.calls) == 2
    assert "<table>" in (picture.html or "")


class _OcrTextLine:
    def __init__(self, text):
        self.text = text


class _OcrResultObj:
    def __init__(self, lines):
        self.text_lines = [_OcrTextLine(t) for t in lines]
        self.image_bbox = [0, 0, 100, 100]


class _FakeRecognitionModel:
    def __init__(self, lines):
        self._lines = lines

    def __call__(self, **kwargs):
        return [_OcrResultObj(self._lines)]


def _dense_text_boxes():
    """Five wide boxes covering ~60% of a 100x100 image -> OCR route."""
    return [
        [[0, 0], [100, 0], [100, 12], [0, 12]],
        [[0, 14], [100, 14], [100, 26], [0, 26]],
        [[0, 28], [100, 28], [100, 40], [0, 40]],
        [[0, 42], [100, 42], [100, 54], [0, 54]],
        [[0, 56], [100, 56], [100, 68], [0, 68]],
    ]


def test_processor_text_image_routes_to_local_ocr_no_vlm():
    """Tier-2: a text-dense image is transcribed locally with NO VLM call —
    the line-227 fix (describe -> transcribe)."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
        detection_model=_RoutingDetectionModel(coverage_boxes=_dense_text_boxes()),
        recognition_model=_FakeRecognitionModel(
            ["Section 4.1 Results", "The model achieved 94% accuracy."]
        ),
    )
    proc(document)

    # No cloud call — transcription was local and deterministic.
    assert vlm.calls == []
    assert "The model achieved 94% accuracy." in (picture.html or "")
    assert "Section 4.1 Results" in (picture.html or "")
    assert proc.image_meta[0]["model"] == "local-ocr"
    assert proc.image_meta[0]["omitted"] is False


def test_processor_ocr_empty_escalates_to_vlm():
    """When local OCR recovers no text, the image escalates to the VLM rather
    than emitting nothing (plan §5 escalation gate)."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
        detection_model=_RoutingDetectionModel(coverage_boxes=_dense_text_boxes()),
        recognition_model=_FakeRecognitionModel([]),  # OCR recovers nothing
    )
    proc(document)

    # Fell through to the VLM.
    assert len(vlm.calls) == 2
    assert "<table>" in (picture.html or "")


class _CountingVLM(FakeVLM):
    """FakeVLM that records how many extract calls ran (for dedup assertions)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.extract_count = 0

    def extract(self, *a, **k):
        self.extract_count += 1
        return super().extract(*a, **k)


def _doc_with_two_identical_pictures():
    from marker.schema import BlockTypes

    h1 = FakeBlock(BlockTypes.SectionHeader, "Report", heading_level=1)
    p1 = FakePicture()
    p2 = FakePicture()
    # Distinct dataclass field values so FakeBlock.__eq__ (and thus
    # list.index in FakeDocument) treats them as different blocks — without
    # this, identical dataclasses collide and FakeDocument navigation loops.
    # The image itself (get_image) is identical, which is what dedup keys on.
    p1.text = "first"
    p2.text = "second"
    # Give the second a distinct emitted name so we can prove the fan-out
    # regenerates the per-block image link rather than copying the first's.
    p2.id = FakeBlockId("_page_5_Picture_99")
    return FakeDocument([h1, p1, p2]), p1, p2


def test_dedup_collapses_identical_images_to_one_vlm_call():
    """Plan §8a: two identical images -> one extraction, fanned to both blocks
    with each block's own image name regenerated."""
    document, p1, p2 = _doc_with_two_identical_pictures()
    vlm = _CountingVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "both",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    # Only ONE extract call despite two identical pictures.
    assert vlm.extract_count == 1
    # Both blocks got the table...
    assert "<table>" in (p1.html or "")
    assert "<table>" in (p2.html or "")
    # ...but each kept its OWN image link (fan-out regenerated the name).
    assert "_page_0_Picture_42.jpeg" in p1.html
    assert "_page_5_Picture_99.jpeg" in p2.html


def test_dedup_disabled_processes_each_image():
    document, p1, p2 = _doc_with_two_identical_pictures()
    vlm = _CountingVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "both",
            "vlm_model": "gpt-4o",
            "dedup_enabled": False,
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    # Dedup off: both images extracted independently.
    assert vlm.extract_count == 2


class _BatchVLM:
    """Fake VLM exposing the batch surface; records batch sizes seen."""

    model_id = "gpt-4o"

    def __init__(self):
        self.batch_calls = []

    def classify_and_extract_batch(self, items, max_retries=2):
        from app.models.image_understanding import ExtractionResult, ImageType

        self.batch_calls.append(len(items))
        return [
            ExtractionResult(
                image_type=ImageType.chart_bar,
                payload={"title": "T", "series": [{"name": "s", "points": [{"x": "Q1", "y": 1}]}]},
                raw_response="{}",
                confidence=0.9,
            )
            for _ in items
        ]


class _OcrSufficientBatchVLM:
    """Batch VLM says local OCR is better for this ambiguous image."""

    model_id = "gpt-4o"

    def __init__(self):
        self.batch_calls = []

    def classify_and_extract_batch(self, items, max_retries=2):
        from app.models.image_understanding import ExtractionResult, ImageType

        self.batch_calls.append(len(items))
        return [
            ExtractionResult(
                image_type=ImageType.other,
                payload={},
                raw_response="{}",
                confidence=0.8,
                route="ocr_sufficient",
            )
            for _ in items
        ]


def _doc_with_two_distinct_pictures():
    from marker.schema import BlockTypes
    from PIL import Image

    h1 = FakeBlock(BlockTypes.SectionHeader, "Report", heading_level=1)
    p1 = FakePicture()
    p1.text = "first"
    p2 = FakePicture()
    p2.text = "second"
    p2.id = FakeBlockId("_page_5_Picture_99")
    # Distinct image content so dedup does NOT collapse them.
    p2.get_image = lambda document: Image.new("RGB", (40, 40), color="black")
    return FakeDocument([h1, p1, p2]), p1, p2


def test_processor_batches_vlm_images_in_one_call():
    """Plan §3: two distinct VLM-routed images go out in a SINGLE batch call."""
    document, p1, p2 = _doc_with_two_distinct_pictures()
    vlm = _BatchVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "dedup_enabled": False,
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    # One batch call carrying both images (no router model -> both vlm-routed).
    assert vlm.batch_calls == [2]
    assert "<table>" in (p1.html or "")
    assert "<table>" in (p2.html or "")


def test_processor_spills_vlm_ocr_sufficient_route_to_local_ocr():
    document, picture = _doc_with_picture()
    vlm = _OcrSufficientBatchVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "dedup_enabled": False,
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
        recognition_model=_FakeRecognitionModel(["Ambiguous crop text."]),
    )
    proc(document)

    assert vlm.batch_calls == [1]
    assert "Ambiguous crop text." in (picture.html or "")
    assert proc.image_meta[0]["model"] == "local-ocr"


def test_processor_batch_disabled_uses_serial():
    """batch_enabled=False -> legacy per-image two-call path."""
    document, picture = _doc_with_picture()
    vlm = FakeVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "batch_enabled": False,
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    assert len(vlm.calls) == 2  # classify + extract
    assert "<table>" in (picture.html or "")


class _CostBatchVLM:
    """Batch VLM that reports a per-image cost on each ExtractionResult."""

    model_id = "gpt-4o"

    def classify_and_extract_batch(self, items, max_retries=2):
        from app.models.image_understanding import ExtractionResult, ImageType

        return [
            ExtractionResult(
                image_type=ImageType.photo,
                payload={"alt_text": "x", "details": []},
                raw_response="{}",
                confidence=0.9,
                cost_usd=0.0123,
            )
            for _ in items
        ]


def test_processor_attributes_cost_to_badge_meta():
    """Plan §6: per-image cost flows into the badge sidecar + the HTML comment,
    closing the cost_usd=0 gap."""
    document, picture = _doc_with_picture()
    vlm = _CostBatchVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "dedup_enabled": False,
            "allow_cloud_vlm": True,
        },
        vlm_service=vlm,
    )
    proc(document)

    assert proc.image_meta[0]["cost_usd"] == 0.0123
    assert "cost_usd=0.012300" in (picture.html or "")


def test_privacy_gate_blocks_cloud_when_disabled():
    """Plan §11a: allow_cloud_vlm=False -> no cloud call; local OCR runs instead."""
    document, picture = _doc_with_picture()
    vlm = _BatchVLM()  # records batch calls

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": False,
        },
        vlm_service=vlm,
        # No detection model -> router would pick vlm; the privacy gate must
        # still prevent the cloud send and fall back to local OCR.
        recognition_model=_FakeRecognitionModel(["Local text only."]),
    )
    proc(document)

    # No cloud batch call happened.
    assert vlm.batch_calls == []
    # Local OCR produced the text instead.
    assert "Local text only." in (picture.html or "")
    assert proc.image_meta[0]["model"] == "local-ocr"


def test_privacy_gate_skips_when_no_local_ocr():
    """Cloud disabled AND no local OCR available -> image skipped, not sent."""
    document, picture = _doc_with_picture()
    vlm = _BatchVLM()

    proc = ImageUnderstandingProcessor(
        {
            "image_handling_mode": "understanding",
            "vlm_model": "gpt-4o",
            "allow_cloud_vlm": False,
        },
        vlm_service=vlm,
        # No recognition model -> no local OCR fallback.
    )
    proc(document)

    assert vlm.batch_calls == []
    assert picture.html is None  # untouched, never sent to cloud
