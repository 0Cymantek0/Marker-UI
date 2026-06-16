"""Regression tests for image-understanding config plumbing.

marker's ConfigParser.generate_config_dict() keeps only keys present in its
crawler.attr_set. The image-understanding custom keys are not registered there,
so they are silently dropped unless marker_service.convert_file re-injects them
after the parser runs. These tests pin that behaviour.
"""

from marker.config.parser import ConfigParser

from app.services.marker_service import (
    IMAGE_UNDERSTANDING_CONFIG_KEYS,
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
        },
    )
    assert opts["image_handling_mode"] == "both"
    assert opts["vlm_model"] == "gpt-4o"
    assert opts["max_images_per_doc"] == 10


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
