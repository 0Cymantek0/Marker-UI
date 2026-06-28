"""Regression tests for image-understanding config plumbing.

marker's ConfigParser.generate_config_dict() keeps only keys present in its
crawler.attr_set. The image-understanding custom keys are not registered there,
so they are silently dropped unless marker_service.convert_file re-injects them
after the parser runs. These tests pin that behaviour.
"""

import threading
import time

from marker.config.parser import ConfigParser

from app.services.marker_service import (
    IMAGE_UNDERSTANDING_CONFIG_KEYS,
    MarkerService,
    build_marker_options,
)


def test_custom_keys_are_not_in_marker_crawler_attr_set():
    """Pin the precondition: marker does not know about our custom keys.

    If this fails, marker started registering these keys and the re-injection
    in convert_file becomes redundant (harmless but worth noticing).
    """
    from marker.config.crawler import crawler

    unknown = [k for k in IMAGE_UNDERSTANDING_CONFIG_KEYS if k in crawler.attr_set]
    assert unknown == [], f"keys unexpectedly registered in marker: {unknown}"


def test_config_parser_strips_custom_keys():
    """ConfigParser alone drops every custom key (the original bug)."""
    options = {
        "image_handling_mode": "both",
        "vlm_model": "gpt-4o",
        "max_images_per_doc": 10,
        "context_window_size": 3,
        "include_original_ref": False,
    }
    config_dict = ConfigParser(options).generate_config_dict()
    for k in IMAGE_UNDERSTANDING_CONFIG_KEYS:
        assert k not in config_dict, f"{k} unexpectedly survived ConfigParser"


def test_build_marker_options_preserves_custom_keys():
    """build_marker_options must keep the custom keys in the returned dict.

    They flow into convert_file as the `options` arg, which is the re-injection
    source. If this regresses, the keys never reach the re-injection loop.
    """
    opts = build_marker_options(
        {
            "providers": [
                {
                    "id": "openai",
                    "type": "openai",
                    "label": "OpenAI",
                    "api_key": "openai-key",
                    "models": [{"model_id": "gpt-4o"}],
                }
            ],
            "active": {"provider_id": "openai", "model_id": "gpt-4o"},
        },
        {
            "image_handling_mode": "both",
            "vlm_model": "gpt-4o",
            "max_images_per_doc": 10,
            "router_enabled": False,
            "smart_router_level": "beeg_brain",
            "ocr_engine": "surya",
            "vlm_batch_size": 16,
            "ocr_min_text_density": 0.6,
        },
    )
    assert opts["image_handling_mode"] == "both"
    assert opts["vlm_model"] == "gpt-4o"
    assert opts["max_images_per_doc"] == 10
    assert opts["router_enabled"] is False
    assert opts["smart_router_level"] == "beeg_brain"
    assert opts["ocr_engine"] == "surya"
    assert opts["vlm_batch_size"] == 16
    assert opts["ocr_min_text_density"] == 0.6


def test_marker_service_serializes_shared_model_conversions(monkeypatch):
    """Shared marker/surya predictors are stateful, so convert calls must not overlap."""
    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output

    active = 0
    max_active = 0
    active_lock = threading.Lock()
    start_gate = threading.Barrier(2)

    class _FakeRendered:
        metadata = {}

    class _FakeConverter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def build_document(self, filepath):
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with active_lock:
                active -= 1
            return object()

        def resolve_dependencies(self, cls):
            class _Renderer:
                def __call__(self, document):
                    return _FakeRendered()
            return _Renderer()

    def fake_text_from_rendered(rendered):
        return "ok", "md", {}

    monkeypatch.setattr(
        marker_service_mod,
        "_CONVERTERS",
        {"PdfConverter": _FakeConverter},
    )
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}
    errors = []

    def run_one(path):
        try:
            start_gate.wait()
            return service.convert_file(
                path,
                {"converter_cls": "PdfConverter", "output_format": "markdown"},
            )
        except Exception as exc:  # noqa: BLE001 - asserted below from parent thread
            errors.append(exc)

    threads = [
        threading.Thread(target=run_one, args=("one.pdf",)),
        threading.Thread(target=run_one, args=("two.pdf",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert max_active == 1


def test_reinjection_restores_custom_keys_after_config_parser():
    """Simulate the convert_file re-injection step end-to-end.

    This is the exact logic added to marker_service.convert_file: parse, then
    re-inject from the original options dict. The processor reads from the
    resulting config_dict, so these keys must survive.
    """
    options = {
        "image_handling_mode": "understanding",
        "vlm_model": "gpt-4o",
        "max_images_per_doc": 25,
        "context_window_size": 4,
        "include_original_ref": False,
        "force_ocr": True,  # known marker key — should survive on its own
    }
    config_dict = ConfigParser(options).generate_config_dict()

    # Mirror the convert_file re-injection loop verbatim.
    for _k in IMAGE_UNDERSTANDING_CONFIG_KEYS:
        if _k in options:
            config_dict[_k] = options[_k]

    assert config_dict["image_handling_mode"] == "understanding"
    assert config_dict["vlm_model"] == "gpt-4o"
    assert config_dict["max_images_per_doc"] == 25
    assert config_dict["context_window_size"] == 4
    assert config_dict["include_original_ref"] is False
    # marker-known keys still flow through untouched.
    assert config_dict.get("force_ocr") is True


def test_reinjection_skips_absent_keys():
    """Absent custom keys must not be added as None values."""
    options = {"image_handling_mode": "both"}
    config_dict = ConfigParser(options).generate_config_dict()
    for _k in IMAGE_UNDERSTANDING_CONFIG_KEYS:
        if _k in options:
            config_dict[_k] = options[_k]

    assert config_dict["image_handling_mode"] == "both"
    assert "vlm_model" not in config_dict
    assert "max_images_per_doc" not in config_dict


def test_collect_image_understanding_meta_reads_processor_stash():
    """marker_service reads the per-image sidecar from the processor instance."""
    from app.services.marker_service import _collect_image_understanding_meta

    class ImageUnderstandingProcessor:  # name match is intentional (lookup key)
        image_meta = [{"image_name": "x.jpeg", "image_type": "chart_bar"}]

    class _OtherProc:
        pass

    class _FakeConverter:
        processor_list = [_OtherProc(), ImageUnderstandingProcessor()]

    assert _collect_image_understanding_meta(_FakeConverter()) == [
        {"image_name": "x.jpeg", "image_type": "chart_bar"}
    ]


def test_collect_image_understanding_meta_empty_when_no_processor():
    """No ImageUnderstandingProcessor -> empty sidecar, no crash."""
    from app.services.marker_service import _collect_image_understanding_meta

    class _FakeConverter:
        processor_list = []

    assert _collect_image_understanding_meta(_FakeConverter()) == []
    # Missing processor_list attr entirely.
    assert _collect_image_understanding_meta(object()) == []


# ---------------------------------------------------------------------------
# OCRConverter renderer-restore (markdown + OCR engine bug)
# ---------------------------------------------------------------------------

def test_ocr_converter_restores_markdown_renderer(monkeypatch):
    """Regression: Markdown + OCR engine must NOT silently emit JSON.

    marker's ``OCRConverter.__init__`` hard-forces
    ``self.renderer = OCRJSONRenderer`` after construction, clobbering the
    markdown renderer we pass in. ``convert_file`` must resolve the
    user-chosen renderer as a class (not a string) and restore it.
    """
    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output
    from marker.renderers.markdown import MarkdownRenderer

    OCR_JSON = "marker.renderers.ocr_json.OCRJSONRenderer"
    FORCED_RENDERER = {}

    class _FakeRendered:
        metadata = {}

    class _FakeOCRConverter:
        """Mimics marker's OCRConverter forcing OCRJSONRenderer in __init__."""

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            # This is the exact line that caused the bug.
            self.renderer = OCR_JSON

        def build_document(self, filepath):
            return object()

        def resolve_dependencies(self, cls):
            FORCED_RENDERER["renderer"] = self.renderer
            class _Renderer:
                def __call__(self, document):
                    return _FakeRendered()
            return _Renderer()

    def fake_text_from_rendered(rendered):
        return "ok", "md", {}

    monkeypatch.setattr(
        marker_service_mod,
        "_CONVERTERS",
        {"OCRConverter": _FakeOCRConverter, "PdfConverter": _FakeOCRConverter},
    )
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}

    service.convert_file(
        "scan.pdf",
        {"converter_cls": "OCRConverter", "output_format": "markdown"},
    )

    # The converter must have its renderer restored to the MarkdownRenderer
    # CLASS, not the OCRJSONRenderer string that OCRConverter.__init__ forced.
    assert FORCED_RENDERER["renderer"] is MarkdownRenderer


def test_ocr_converter_keeps_json_renderer(monkeypatch):
    """When the user genuinely asks for JSON + OCR, leave OCRJSONRenderer alone."""
    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output

    OCR_JSON = "marker.renderers.ocr_json.OCRJSONRenderer"
    FORCED_RENDERER = {}

    class _FakeRendered:
        metadata = {}

    class _FakeOCRConverter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.renderer = OCR_JSON

        def build_document(self, filepath):
            return object()

        def resolve_dependencies(self, cls):
            FORCED_RENDERER["renderer"] = self.renderer
            class _Renderer:
                def __call__(self, document):
                    return _FakeRendered()
            return _Renderer()

    def fake_text_from_rendered(rendered):
        return "ok", "json", {}

    monkeypatch.setattr(
        marker_service_mod,
        "_CONVERTERS",
        {"PdfConverter": _FakeOCRConverter, "OCRConverter": _FakeOCRConverter},
    )
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}

    service.convert_file(
        "scan.pdf",
        {"converter_cls": "OCRConverter", "output_format": "json"},
    )

    assert FORCED_RENDERER["renderer"] == OCR_JSON


def test_ocr_converter_renderer_survives_double_resolve(monkeypatch):
    """Regression: renderer must be a CLASS so __call__'s resolve_dependencies works.

    marker's OCRConverter.__call__ calls resolve_dependencies(self.renderer)
    which inspects cls.__init__ then does cls(**kwargs). If self.renderer is
    already an instance (not a class), cls(**kwargs) hits __call__ instead of
    __init__, causing TypeError on unexpected 'config' kwarg.
    """
    import inspect

    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output
    from marker.renderers.markdown import MarkdownRenderer

    OCR_JSON = "marker.renderers.ocr_json.OCRJSONRenderer"

    class _FakeRendered:
        metadata = {}

    class _FakeOCRConverter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.config = kwargs.get("config", {})
            self.artifact_dict = kwargs.get("artifact_dict", {})
            self.renderer = OCR_JSON

        def build_document(self, filepath):
            return object()

        def resolve_dependencies(self, cls):
            init_sig = inspect.signature(cls.__init__)
            params = init_sig.parameters
            resolved = {}
            for name, param in params.items():
                if name == "self":
                    continue
                elif name == "config":
                    resolved[name] = self.config
                elif name in self.artifact_dict:
                    resolved[name] = self.artifact_dict[name]
                elif param.default != inspect.Parameter.empty:
                    resolved[name] = param.default
                else:
                    raise ValueError(f"Cannot resolve: {name}")
            cls(**resolved)

            class _Renderer:
                def __call__(self, document):
                    return _FakeRendered()

            return _Renderer()

    def fake_text_from_rendered(rendered):
        return "ok", "md", {}

    monkeypatch.setattr(
        marker_service_mod,
        "_CONVERTERS",
        {"OCRConverter": _FakeOCRConverter, "PdfConverter": _FakeOCRConverter},
    )
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}

    service.convert_file(
        "scan.pdf",
        {"converter_cls": "OCRConverter", "output_format": "markdown"},
    )


# ---------------------------------------------------------------------------
# Multi-format render (convert_file_formats): one document parse -> N renders
# ---------------------------------------------------------------------------


def test_renderer_string_for_format():
    """Each canonical format maps to its stock marker renderer.

    Markdown swaps to the image-understanding renderer only in understanding/
    both mode; every other format is stock so behaviour never silently shifts.
    """
    from app.services.marker_service import _renderer_string_for_format

    assert _renderer_string_for_format("markdown", {}) == "marker.renderers.markdown.MarkdownRenderer"
    assert _renderer_string_for_format("markdown", {"image_handling_mode": "extraction"}).endswith("MarkdownRenderer")
    assert _renderer_string_for_format("markdown", {"image_handling_mode": "both"}).endswith("ImageUnderstandingRenderer")
    assert _renderer_string_for_format("json", {}) == "marker.renderers.json.JSONRenderer"
    assert _renderer_string_for_format("html", {}) == "marker.renderers.html.HTMLRenderer"
    assert _renderer_string_for_format("chunks", {}) == "marker.renderers.chunk.ChunkRenderer"
    # Unknown format falls back to markdown, never crashes.
    assert _renderer_string_for_format("bogus", {}).endswith("MarkdownRenderer")


def test_convert_file_formats_parses_once_renders_each(monkeypatch):
    """The document is built ONCE; each requested format renders from it.

    This is the no-reconversion contract: N formats cost one layout/OCR pass.
    We assert build_document ran exactly once and one render per format ran.
    """
    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output
    import marker.util as marker_util

    build_calls = {"n": 0}
    render_calls: list[str] = []

    class _FakeDocument:
        pass

    class _FakeRendered:
        def __init__(self, fmt):
            self.metadata = {"format": fmt}

    class _FakeRenderer:
        def __init__(self, name):
            self.name = name

        def __call__(self, document):
            render_calls.append(self.name)
            return _FakeRendered(self.name)

    def fake_resolve(cls):
        return _FakeRenderer(cls.__name__)

    def fake_strings_to_classes(paths):
        class _Stub:
            def __init__(self, p):
                self._p = p
        return [_Stub(p) for p in paths]

    class _FakeConverter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.renderer = None

        def build_document(self, filepath):
            build_calls["n"] += 1
            return _FakeDocument()

        def resolve_dependencies(self, cls):
            return _FakeRenderer(cls.__name__)

    def fake_text_from_rendered(rendered):
        return rendered.metadata["format"], rendered.metadata["format"], {}

    monkeypatch.setattr(
        marker_service_mod,
        "_CONVERTERS",
        {"PdfConverter": _FakeConverter},
    )
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}

    out = service.convert_file_formats(
        "doc.pdf",
        {"converter_cls": "PdfConverter", "output_format": "markdown"},
        ["markdown", "json", "html"],
    )

    # One parse, three renders.
    assert build_calls["n"] == 1
    assert len(render_calls) == 3
    # Every requested format is present with text derived from its renderer.
    assert set(out.keys()) == {"markdown", "json", "html"}
    assert out["markdown"]["text"] == "MarkdownRenderer"
    assert out["json"]["text"] == "JSONRenderer"
    assert out["html"]["text"] == "HTMLRenderer"


def test_convert_file_formats_dedupes_and_drops_unknown(monkeypatch):
    """Duplicates collapse and unknown formats are filtered to markdown only."""
    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output

    class _FakeDocument:
        pass

    class _FakeRendered:
        metadata = {}

    class _FakeConverter:
        def __init__(self, **kwargs):
            pass

        def build_document(self, filepath):
            return _FakeDocument()

        def resolve_dependencies(self, cls):
            class _R:
                def __call__(self, doc):
                    return _FakeRendered()
            return _R()

    def fake_text_from_rendered(rendered):
        return "ok", "md", {}

    monkeypatch.setattr(marker_service_mod, "_CONVERTERS", {"PdfConverter": _FakeConverter})
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}

    out = service.convert_file_formats(
        "doc.pdf",
        {"converter_cls": "PdfConverter"},
        ["markdown", "markdown", "bogus", ""],
    )
    # Deduped markdown survives; bogus/empty dropped.
    assert list(out.keys()) == ["markdown"]


def test_convert_file_uses_build_refine_render_seam(monkeypatch):
    """Single-format conversion must use the same build -> hybrid -> render seam."""
    import app.services.marker_service as marker_service_mod
    import marker.output as marker_output

    build_calls = {"n": 0}

    class _Block:
        block_type = "Table"

        def __init__(self):
            self.text = "| old | value |"

    class _Document:
        def __init__(self):
            self.pages = [type("_Page", (), {"children": [_Block()]})()]

    class _FakeRendered:
        def __init__(self, document):
            self.document = document
            self.metadata = {"format": "markdown"}

    class _FakeConverter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def build_document(self, filepath):
            build_calls["n"] += 1
            return _Document()

        def resolve_dependencies(self, cls):
            class _Renderer:
                def __call__(self, document):
                    return _FakeRendered(document)
            return _Renderer()

    def fake_text_from_rendered(rendered):
        block = rendered.document.pages[0].children[0]
        return block.text, "md", {}

    monkeypatch.setenv("MARKER_GLM_PYTHON", "python")
    monkeypatch.setattr(marker_service_mod, "_CONVERTERS", {"PdfConverter": _FakeConverter})
    monkeypatch.setattr(marker_output, "text_from_rendered", fake_text_from_rendered)

    service = MarkerService()
    service._initialized = True
    service._model_dict = {"recognition": object()}

    out = service.convert_file(
        "doc.pdf",
        {
            "converter_cls": "PdfConverter",
            "output_format": "markdown",
            "ocr_engine": "hybrid_ocr",
            "hybrid_ocr_mock_results": {
                "p1_table_01": {
                    "engine": "glm_ocr",
                    "markdown": "| new | value |\n|---|---|\n| 1 | 2 |",
                    "replacement_policy": "replace_block",
                }
            },
        },
    )

    assert build_calls["n"] == 1
    assert out["text"] == "| new | value |\n|---|---|\n| 1 | 2 |"
    assert out["metadata"]["hybrid_ocr"]["specialist_results"]["accepted"] == 1


# ---------------------------------------------------------------------------
# OOM safety net (ISSUE-5): catch CUDA OOM -> empty_cache -> halve batch -> retry
# ---------------------------------------------------------------------------


class _FakePredictor:
    """Minimal stand-in for a surya predictor with a batch_size knob."""

    def __init__(self, batch_size):
        self.batch_size = batch_size

    def get_batch_size(self):
        return self.batch_size if self.batch_size is not None else 256


def test_run_with_oom_retry_halves_batch_then_succeeds():
    """First call raises CUDA OOM, second succeeds. Batch sizes halved once."""
    from app.services.marker_service import run_with_oom_retry

    model_dict = {
        "recognition_model": _FakePredictor(256),
        "detection_model": _FakePredictor(32),
    }
    calls = {"n": 0}

    def convert():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate ...")
        return "rendered-ok"

    result = run_with_oom_retry(convert, model_dict)

    assert result == "rendered-ok"
    assert calls["n"] == 2
    assert model_dict["recognition_model"].batch_size == 128
    assert model_dict["detection_model"].batch_size == 16


def test_run_with_oom_retry_descends_into_foundation_predictor():
    """Layout/Recognition wrap a foundation predictor; its batch must shrink too."""
    from app.services.marker_service import run_with_oom_retry

    foundation = _FakePredictor(256)

    class _Wrapper:
        batch_size = 8
        foundation_predictor = foundation

    model_dict = {"recognition_model": _Wrapper()}
    calls = {"n": 0}

    def convert():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CUDA out of memory")
        return "ok"

    run_with_oom_retry(convert, model_dict)

    assert foundation.batch_size == 128
    assert model_dict["recognition_model"].batch_size == 4


def test_run_with_oom_retry_non_oom_error_propagates_immediately():
    """A non-OOM error must NOT be retried — it propagates on the first call."""
    from app.services.marker_service import run_with_oom_retry

    model_dict = {"recognition_model": _FakePredictor(256)}
    calls = {"n": 0}

    def convert():
        calls["n"] += 1
        raise ValueError("unrelated failure")

    import pytest

    with pytest.raises(ValueError, match="unrelated failure"):
        run_with_oom_retry(convert, model_dict)

    assert calls["n"] == 1
    # Batch sizes untouched on a non-OOM failure.
    assert model_dict["recognition_model"].batch_size == 256


def test_run_with_oom_retry_gives_up_at_minimum_batch():
    """Persistent OOM with batches already at the floor re-raises, no infinite loop."""
    from app.services.marker_service import run_with_oom_retry

    model_dict = {"recognition_model": _FakePredictor(1)}
    calls = {"n": 0}

    def convert():
        calls["n"] += 1
        raise RuntimeError("CUDA out of memory")

    import pytest

    with pytest.raises(RuntimeError, match="out of memory"):
        run_with_oom_retry(convert, model_dict)

    # Floor batch can't shrink, so we bail after the first failed attempt.
    assert calls["n"] == 1


def test_run_with_oom_retry_exhausts_limit():
    """Even with shrinkable batches, stop after `limit` attempts."""
    from app.services.marker_service import run_with_oom_retry

    model_dict = {"recognition_model": _FakePredictor(1024)}
    calls = {"n": 0}

    def convert():
        calls["n"] += 1
        raise RuntimeError("CUDA out of memory")

    import pytest

    with pytest.raises(RuntimeError, match="out of memory"):
        run_with_oom_retry(convert, model_dict, limit=3)

    assert calls["n"] == 3
