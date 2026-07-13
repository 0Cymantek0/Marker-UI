"""Authorization and scope tests (UCM-009)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.auth.provider import AccessToken

from app.security.auth import (
    principal_from_authorization,
    require_mcp_scopes,
    rest_scopes_for_request,
    validate_oidc_jwt_skeleton,
)
from app.security.scopes import (
    SCOPE_CAPABILITIES_READ,
    SCOPE_JOBS_WRITE,
    SCOPE_OUTPUTS_READ,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
)
from app.services.output_writer import write_conversion_output


def test_static_bearer_token_maps_to_configured_scopes(monkeypatch):
    monkeypatch.setenv("MARKER_REST_AUTH_TOKEN", "read-token")
    monkeypatch.setenv("MARKER_REST_AUTH_SCOPES", f"{SCOPE_OUTPUTS_READ},{SCOPE_SETTINGS_READ}")

    principal = principal_from_authorization("Bearer read-token", surface="rest")

    assert principal is not None
    assert principal.scopes == frozenset({SCOPE_OUTPUTS_READ, SCOPE_SETTINGS_READ})


def test_oidc_jwt_skeleton_refuses_unverified_tokens(monkeypatch):
    monkeypatch.setenv("MARKER_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("MARKER_OIDC_AUDIENCE", "marker")

    with pytest.raises(Exception) as exc_info:
        validate_oidc_jwt_skeleton("header.payload.signature")

    assert getattr(exc_info.value, "status_code", None) == 501


def test_mcp_scope_check_denies_missing_scope(monkeypatch):
    monkeypatch.setattr(
        "app.security.auth.get_access_token",
        lambda: AccessToken(token="limited", client_id="test", scopes=[SCOPE_SETTINGS_READ]),
    )

    with pytest.raises(PermissionError):
        require_mcp_scopes(SCOPE_SETTINGS_WRITE)


def test_rest_scope_mapping_covers_core_surfaces():
    assert rest_scopes_for_request("GET", "/api/capabilities") == {SCOPE_CAPABILITIES_READ}
    assert rest_scopes_for_request("POST", "/api/convert/upload") == {SCOPE_JOBS_WRITE}
    assert rest_scopes_for_request("GET", "/api/convert/download/job-1") == {SCOPE_OUTPUTS_READ}
    assert rest_scopes_for_request("GET", "/api/convert/assets/job-1/chart.png") == {SCOPE_OUTPUTS_READ}
    assert rest_scopes_for_request("GET", "/api/settings/") == {SCOPE_SETTINGS_READ}
    assert rest_scopes_for_request("PUT", "/api/settings/") == {SCOPE_SETTINGS_WRITE}


@pytest.mark.asyncio
async def test_mcp_token_without_settings_write_cannot_set_settings(monkeypatch):
    import app.mcp_server as mcp_server

    monkeypatch.setattr(
        "app.security.auth.get_access_token",
        lambda: AccessToken(token="limited", client_id="test", scopes=[SCOPE_SETTINGS_READ]),
    )

    with pytest.raises(PermissionError):
        await mcp_server.marker_set_setting("some_key", "some_value")


@pytest.mark.asyncio
async def test_mcp_token_with_outputs_read_can_read_output(monkeypatch, tmp_path: Path):
    import app.mcp_server as mcp_server

    monkeypatch.setattr(
        "app.security.auth.get_access_token",
        lambda: AccessToken(token="reader", client_id="test", scopes=[SCOPE_OUTPUTS_READ]),
    )
    written = write_conversion_output(
        {"text": "hello", "extension": "md"},
        source_name="doc.tsv",
        output_base=tmp_path,
        output_format="markdown",
    )

    payload = await mcp_server.marker_read_output(str(written.text_path), limit=20)

    assert payload["text"] == "hello"


@pytest.mark.asyncio
async def test_rest_middleware_requires_bearer_when_configured(monkeypatch):
    from app.main import app

    monkeypatch.setenv("MARKER_REST_AUTH_TOKEN", "rest-token")
    monkeypatch.setenv("MARKER_REST_AUTH_SCOPES", SCOPE_OUTPUTS_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.get("/api/capabilities")
        allowed = await client.get("/api/capabilities", headers={"Authorization": "Bearer rest-token"})
        denied_write = await client.put(
            "/api/settings/",
            headers={"Authorization": "Bearer rest-token"},
            json={"key": "x", "value": "y", "category": "general"},
        )

    assert missing.status_code == 401
    assert allowed.status_code == 403
    assert denied_write.status_code == 403


@pytest.mark.asyncio
async def test_rest_middleware_allows_matching_scope_when_configured(monkeypatch):
    from app.main import app

    monkeypatch.setenv("MARKER_REST_AUTH_TOKEN", "rest-token")
    monkeypatch.setenv("MARKER_REST_AUTH_SCOPES", SCOPE_CAPABILITIES_READ)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.get("/api/capabilities", headers={"Authorization": "Bearer rest-token"})
        denied_settings = await client.get("/api/settings/", headers={"Authorization": "Bearer rest-token"})

    assert allowed.status_code == 200
    assert denied_settings.status_code == 403


@pytest.mark.asyncio
async def test_rest_asset_endpoint_requires_outputs_read_scope(monkeypatch):
    from app.main import app

    monkeypatch.setenv("MARKER_AUTH_TOKENS", "outputs-token=outputs:read;caps-token=capabilities:read")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        scoped = await client.get(
            "/api/convert/assets/missing-job/chart.png",
            headers={"Authorization": "Bearer outputs-token"},
        )
        denied = await client.get(
            "/api/convert/assets/missing-job/chart.png",
            headers={"Authorization": "Bearer caps-token"},
        )

    assert scoped.status_code == 404
    assert denied.status_code == 403
