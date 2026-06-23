"""Tests for ConversionRouter — extension/mime → engine routing decisions.

Data-driven test covering every row in the §2.2 decision table, plus
edge cases (unknown extension, missing extension, case sensitivity).
"""

from __future__ import annotations

import pytest

from app.conversion.router import ConversionRouter
from app.conversion.stream_info import StreamInfo


def _make_stream_info(ext: str, path: str = "") -> StreamInfo:
    """Build a minimal StreamInfo with the given extension."""
    if not path:
        path = f"/tmp/test_file{ext}"
    return StreamInfo(
        path=path,
        extension=ext,
        mime_type="application/octet-stream",
        size=1024,
        sample=b"",
    )


def _safe_clean_pdf_probe(**overrides):
    probe = {
        "page_count": 2,
        "text_layer_score": 0.9,
        "text_quality_score": 0.95,
        "scan_likelihood": 0.05,
        "sandwich_likelihood": 0.1,
        "layout_complexity_score": 0.1,
        "visual_complexity_score": 0.0,
        "recommended_engine": "liteparse",
        "reasons": ["strong extractable text layer"],
        "sampled_image_count": 0,
    }
    probe.update(overrides)
    return probe


class TestConversionRouter:
    """Exhaustive routing decision table tests."""

    # (extension, expected_engine, expected_needs_marker, expected_needs_gpu, min_confidence)
    ROUTING_TABLE = [
        # PDF without bytes/probe stays conservative and preliminary.
        (".pdf", "marker_pdf", True, True, 0.75),
        # Images → marker for OCR
        (".jpg", "marker_pdf", True, True, 1.0),
        (".jpeg", "marker_pdf", True, True, 1.0),
        (".png", "marker_pdf", True, True, 1.0),
        (".webp", "marker_pdf", True, True, 1.0),
        (".tiff", "marker_pdf", True, True, 1.0),
        (".bmp", "marker_pdf", True, True, 1.0),
        (".gif", "marker_pdf", True, True, 1.0),
        # EPUB
        (".epub", "marker_pdf", True, True, 1.0),
        # Office
        (".docx", "office_docx", False, False, 0.95),
        (".pptx", "office_pptx", False, False, 0.95),
        (".xlsx", "spreadsheet", False, False, 0.95),
        # Data formats
        (".csv", "text_data", False, False, 0.95),
        (".json", "text_data", False, False, 0.95),
        (".jsonl", "text_data", False, False, 0.95),
        # XML/RSS
        (".xml", "xml_rss", False, False, 0.90),
        (".rss", "xml_rss", False, False, 0.90),
        (".atom", "xml_rss", False, False, 0.90),
        # HTML
        (".html", "html", False, False, 0.90),
        (".htm", "html", False, False, 0.90),
        # Text
        (".txt", "text_data", False, False, 1.0),
        (".md", "text_data", False, False, 1.0),
        (".rst", "text_data", False, False, 1.0),
        (".log", "text_data", False, False, 1.0),
        # Notebook
        (".ipynb", "notebook", False, False, 0.95),
        # Archive
        (".zip", "archive", False, False, 0.90),
    ]

    @pytest.mark.parametrize(
        "ext,engine,needs_marker,needs_gpu,min_conf",
        ROUTING_TABLE,
        ids=[row[0] for row in ROUTING_TABLE],
    )
    def test_routing_table(
        self,
        ext: str,
        engine: str,
        needs_marker: bool,
        needs_gpu: bool,
        min_conf: float,
    ) -> None:
        """Every extension in the decision table maps to the correct engine."""
        stream_info = _make_stream_info(ext)
        plan = ConversionRouter.plan(stream_info, {})

        assert plan.engine == engine
        assert plan.needs_marker_models == needs_marker
        assert plan.needs_gpu == needs_gpu
        assert plan.confidence >= min_conf
        assert len(plan.reasons) > 0

    def test_unknown_extension_falls_back(self) -> None:
        """Unknown extensions get a low-confidence marker_pdf fallback."""
        stream_info = _make_stream_info(".xyz")
        plan = ConversionRouter.plan(stream_info, {})

        assert plan.engine == "marker_pdf"
        assert plan.confidence == 0.3
        assert plan.needs_marker_models is True
        assert plan.needs_gpu is True
        assert len(plan.warnings) > 0
        assert "No dedicated converter" in plan.warnings[0]

    def test_empty_extension_falls_back(self) -> None:
        """Empty extension (no suffix) falls back gracefully."""
        stream_info = _make_stream_info("")
        plan = ConversionRouter.plan(stream_info, {})

        assert plan.engine == "marker_pdf"
        assert plan.confidence == 0.3
        assert len(plan.warnings) > 0

    def test_plan_returns_reasons(self) -> None:
        """Plan always includes at least one reason."""
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(stream_info, {})

        assert len(plan.reasons) >= 1
        assert "PDF complexity" in plan.reasons[0]

    def test_plan_is_pure(self) -> None:
        """Calling plan twice with the same input produces identical results."""
        stream_info = _make_stream_info(".docx")
        plan1 = ConversionRouter.plan(stream_info, {})
        plan2 = ConversionRouter.plan(stream_info, {})

        assert plan1.engine == plan2.engine
        assert plan1.confidence == plan2.confidence
        assert plan1.needs_marker_models == plan2.needs_marker_models

    def test_plan_to_dict_serializable(self) -> None:
        """ConverterPlan.to_dict() produces a JSON-serializable dict."""
        import json

        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(stream_info, {})
        d = plan.to_dict()

        # Must not raise
        serialized = json.dumps(d)
        assert '"engine"' in serialized
        assert '"marker_pdf"' in serialized

    def test_uppercase_extension_routes_correctly(self) -> None:
        """ISSUE-K: a raw uppercase extension must route the same as lowercase.

        Production paths (from_path, plan_by_metadata) lower-case the extension,
        but a caller building a StreamInfo directly with an upper-case suffix
        should still hit the routing table, not the low-confidence fallback.
        """
        upper = _make_stream_info(".PDF", path="/tmp/REPORT.PDF")
        lower = _make_stream_info(".pdf")
        plan_upper = ConversionRouter.plan(upper, {})
        plan_lower = ConversionRouter.plan(lower, {})

        assert plan_upper.engine == plan_lower.engine == "marker_pdf"
        assert plan_upper.confidence == plan_lower.confidence == 0.75
        assert plan_upper.needs_marker_models is True

    def test_uppercase_office_extension_routes_to_office(self) -> None:
        """An uppercase .DOCX routes to office_docx, not the marker fallback."""
        upper = _make_stream_info(".DOCX", path="/tmp/REPORT.DOCX")
        plan = ConversionRouter.plan(upper, {})

        assert plan.engine == "office_docx"
        assert plan.needs_marker_models is False
        assert plan.execution_backend == "cpu_thread"

    def test_clean_digital_pdf_probe_routes_to_liteparse(self) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(stream_info, {
            "probe_result": _safe_clean_pdf_probe(),
            "image_handling_mode": "both",
        })

        assert plan.engine == "liteparse_pdf"
        assert plan.needs_marker_models is False
        assert plan.execution_backend == "cpu_thread"
        assert plan.fallback_chain == ["liteparse_pdf", "marker_pdf"]

    def test_scanned_pdf_probe_routes_to_marker(self) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(stream_info, {
            "probe_result": {
                "page_count": 2,
                "text_layer_score": 0.0,
                "text_quality_score": 0.0,
                "scan_likelihood": 0.95,
                "sandwich_likelihood": 0.55,
                "layout_complexity_score": 0.0,
                "visual_complexity_score": 0.9,
                "recommended_engine": "marker",
                "reasons": ["weak or missing extractable text layer"],
                "sampled_image_count": 2,
            }
        })

        assert plan.engine == "marker_pdf"
        assert plan.needs_marker_models is True
        assert plan.execution_backend == "marker_worker"

    @pytest.mark.parametrize(
        "probe_overrides,expected_reason",
        [
            ({"sandwich_likelihood": 0.6, "visual_complexity_score": 0.4, "sampled_image_count": 1}, "sandwich"),
            ({"layout_complexity_score": 0.8}, "layout"),
            ({"visual_complexity_score": 0.8, "sampled_image_count": 3}, "image"),
            ({"text_quality_score": 0.7}, "text quality"),
        ],
    )
    def test_pdf_probe_risk_scores_route_to_marker(
        self,
        probe_overrides,
        expected_reason: str,
    ) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(
            stream_info,
            {"probe_result": _safe_clean_pdf_probe(**probe_overrides)},
        )

        assert plan.engine == "marker_pdf"
        assert any(expected_reason.lower() in reason.lower() for reason in plan.reasons)

    def test_fast_profile_routes_risky_pdf_to_liteparse_with_warning(self) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(
            stream_info,
            {
                "conversion_profile": "fast",
                "probe_result": _safe_clean_pdf_probe(
                    scan_likelihood=0.95,
                    sandwich_likelihood=0.65,
                    visual_complexity_score=0.9,
                    sampled_image_count=3,
                    recommended_engine="marker",
                ),
            },
        )

        assert plan.engine == "liteparse_pdf"
        assert plan.execution_backend == "cpu_thread"
        assert plan.fallback_chain == ["liteparse_pdf", "marker_pdf"]
        assert any("Fast profile forces LiteParse" in warning for warning in plan.warnings)

    def test_fast_profile_preliminary_pdf_routes_to_liteparse(self) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(stream_info, {"conversion_profile": "fast"})

        assert plan.engine == "liteparse_pdf"
        assert plan.confidence == 0.55
        assert any("Preliminary" in warning for warning in plan.warnings)

    def test_fast_profile_does_not_override_marker_required_options(self) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(
            stream_info,
            {
                "conversion_profile": "fast",
                "force_ocr": True,
                "probe_result": _safe_clean_pdf_probe(),
            },
        )

        assert plan.engine == "marker_pdf"
        assert any("force_ocr" in reason for reason in plan.reasons)

    def test_image_understanding_both_routes_marker_only_when_images_exist(self) -> None:
        stream_info = _make_stream_info(".pdf")
        safe_probe = _safe_clean_pdf_probe()
        with_no_images = ConversionRouter.plan(stream_info, {
            "probe_result": safe_probe,
            "image_handling_mode": "both",
        })
        with_images = ConversionRouter.plan(stream_info, {
            "probe_result": {**safe_probe, "sampled_image_count": 1, "visual_complexity_score": 0.2},
            "image_handling_mode": "both",
        })

        assert with_no_images.engine == "liteparse_pdf"
        assert with_images.engine == "marker_pdf"

    @pytest.mark.parametrize(
        "force_config,expected_reason",
        [
            ({"profile": "high_accuracy"}, "High Accuracy"),
            ({"conversion_profile": "high-accuracy"}, "High Accuracy"),
            ({"force_ocr": True}, "force_ocr"),
            ({"converter_cls": "TableConverter"}, "TableConverter"),
            ({"converter_cls": "marker.converters.table.TableConverter"}, "TableConverter"),
            ({"converter_cls": "OCRConverter"}, "OCRConverter"),
        ],
    )
    def test_force_marker_pdf_configs_override_clean_probe(
        self,
        force_config,
        expected_reason: str,
    ) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(
            stream_info,
            {"probe_result": _safe_clean_pdf_probe(), **force_config},
        )

        assert plan.engine == "marker_pdf"
        assert any(expected_reason in reason for reason in plan.reasons)

    def test_pdf_engine_override_can_select_marker_over_liteparse(self) -> None:
        stream_info = _make_stream_info(".pdf")
        plan = ConversionRouter.plan(stream_info, {
            "engine_override": "marker_pdf",
            "probe_result": _safe_clean_pdf_probe(),
        })

        assert plan.engine == "marker_pdf"
        assert "User selected engine override" in plan.reasons[0]

    def test_incompatible_engine_override_is_ignored(self) -> None:
        stream_info = _make_stream_info(".png")
        plan = ConversionRouter.plan(stream_info, {"engine_override": "liteparse_pdf"})

        assert plan.engine == "marker_pdf"
        assert plan.label == "Marker Image OCR"

    def test_legacy_xls_does_not_claim_xlsx_native_path(self) -> None:
        stream_info = _make_stream_info(".xls")
        plan = ConversionRouter.plan(stream_info, {})

        assert plan.engine == "marker_pdf"
        assert any("No dedicated converter" in warning for warning in plan.warnings)
