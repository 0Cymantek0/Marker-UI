"""Tests for /api/capabilities endpoint."""

from __future__ import annotations

from unittest.mock import patch
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_capabilities_returns_200(client: AsyncClient):
    """Querying capabilities returns 200 and all engines."""
    resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    
    body = resp.json()
    assert "engines" in body
    engines = body["engines"]
    
    # Assert standard engines are represented
    assert "marker_pdf" in engines
    assert "office_docx" in engines
    assert "office_pptx" in engines
    assert "spreadsheet" in engines
    assert "text_data" in engines
    assert "html" in engines
    
    # Since text_data needs no extra dependencies, it must be "ready"
    assert engines["text_data"] == "ready"


@pytest.mark.asyncio
async def test_get_capabilities_with_mocked_dependencies(client: AsyncClient):
    """Capabilities endpoint responds correctly to missing dependencies."""
    with patch("app.conversion.dependencies.is_dependency_available") as mock_dep:
        # Mock dependencies so office_docx looks ready but office_pptx looks missing
        def side_effect(name):
            if name in ("mammoth", "markdownify"):
                return True
            if name == "python-pptx":
                return False
            return True

        mock_dep.side_effect = side_effect

        resp = await client.get("/api/capabilities")
        assert resp.status_code == 200
        engines = resp.json()["engines"]
        assert engines["office_docx"] == "ready"
        assert engines["office_pptx"] == "missing_optional_dependency"
