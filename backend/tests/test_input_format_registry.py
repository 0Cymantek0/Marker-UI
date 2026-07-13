"""Input format registry parity across backend surfaces."""

from __future__ import annotations

from httpx import Headers

from app.agent_api import ALLOWED_EXTENSIONS as AGENT_ALLOWED_EXTENSIONS
from app.conversion.formats import (
    CONTENT_TYPE_EXTENSION_MAP,
    INPUT_FORMAT_BY_EXTENSION,
    INPUT_FORMATS,
    UPLOAD_ALLOWED_EXTENSIONS,
)
from app.conversion.router import ConversionRouter, _ENGINE_META
from app.conversion.stream_info import StreamInfo
from app.routes.convert import ALLOWED_EXTENSIONS as REST_ALLOWED_EXTENSIONS
from app.services.safe_url_fetcher import extension_for_download


def test_upload_agent_and_router_extensions_share_registry() -> None:
    assert ".gif" in UPLOAD_ALLOWED_EXTENSIONS
    assert REST_ALLOWED_EXTENSIONS == UPLOAD_ALLOWED_EXTENSIONS
    assert AGENT_ALLOWED_EXTENSIONS == UPLOAD_ALLOWED_EXTENSIONS

    for ext, spec in INPUT_FORMAT_BY_EXTENSION.items():
        plan = ConversionRouter.plan(
            StreamInfo(
                path=f"/tmp/input{ext}",
                extension=ext,
                mime_type="application/octet-stream",
                size=1,
                sample=b"",
            ),
            {},
        )
        assert plan.engine == spec.engine


def test_url_content_type_map_uses_registry_for_gif() -> None:
    assert CONTENT_TYPE_EXTENSION_MAP["image/gif"] == ".gif"
    for spec in INPUT_FORMATS:
        expected_extensions = spec.content_type_extensions or (spec.extensions[0],) * len(spec.mime_types)
        assert len(expected_extensions) == len(spec.mime_types)
        for mime_type in spec.mime_types:
            assert CONTENT_TYPE_EXTENSION_MAP[mime_type] in spec.extensions
        for mime_type, extension in zip(spec.mime_types, expected_extensions):
            assert CONTENT_TYPE_EXTENSION_MAP[mime_type] == extension

    filename, suffix = extension_for_download(
        "https://example.com/animation.bin",
        Headers({"content-type": "image/gif"}),
        allowed_extensions=set(UPLOAD_ALLOWED_EXTENSIONS),
    )

    assert filename == "animation.gif"
    assert suffix == ".gif"


def test_router_engine_override_metadata_comes_from_input_registry() -> None:
    first_engine_specs = {}
    for spec in INPUT_FORMATS:
        first_engine_specs.setdefault(spec.engine, spec)

    assert set(first_engine_specs).issubset(_ENGINE_META)
    for engine, spec in first_engine_specs.items():
        assert _ENGINE_META[engine] == (
            spec.label,
            spec.needs_marker_models,
            spec.needs_gpu,
            spec.confidence,
        )
