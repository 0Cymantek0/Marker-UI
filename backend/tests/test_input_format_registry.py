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
from app.conversion.router import ConversionRouter
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
        for mime_type in spec.mime_types:
            assert CONTENT_TYPE_EXTENSION_MAP[mime_type] in spec.extensions

    filename, suffix = extension_for_download(
        "https://example.com/animation.bin",
        Headers({"content-type": "image/gif"}),
        allowed_extensions=set(UPLOAD_ALLOWED_EXTENSIONS),
    )

    assert filename == "animation.gif"
    assert suffix == ".gif"
