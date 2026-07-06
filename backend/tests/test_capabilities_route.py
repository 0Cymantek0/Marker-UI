from __future__ import annotations

import pytest

from app.routes.capabilities import get_capabilities


@pytest.mark.asyncio
async def test_capabilities_exposes_format_registry_metadata() -> None:
    response = await get_capabilities()
    payload = response.model_dump()

    assert "chunks" in payload["output_formats"]
    assert ".pdf" in payload["marker_multi_format_extensions"]
    assert ".gif" in payload["marker_multi_format_extensions"]
    assert {
        "extensions": [".docx"],
        "engine": "office_docx",
        "label": "Fast Office (Word)",
        "category": "office",
        "needs_marker_models": False,
        "needs_gpu": False,
        "upload_allowed": True,
        "url_allowed": True,
    } in payload["input_formats"]
