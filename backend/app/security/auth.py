"""Closed-by-configuration auth helpers for REST and MCP."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import HTTPException, Request
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.security.scopes import DEFAULT_MCP_SCOPES, DEFAULT_REST_SCOPES, has_scopes
from app.services.audit import record_audit_event


@dataclass(frozen=True)
class Principal:
    token: str
    scopes: frozenset[str]
    client_id: str = "static-token"


@dataclass(frozen=True)
class OIDCSettings:
    issuer_url: str
    audience: str
    jwks_url: str | None = None


def oidc_settings() -> OIDCSettings | None:
    issuer = os.getenv("MARKER_OIDC_ISSUER", "").strip()
    audience = os.getenv("MARKER_OIDC_AUDIENCE", "").strip()
    if not issuer or not audience:
        return None
    return OIDCSettings(
        issuer_url=issuer,
        audience=audience,
        jwks_url=os.getenv("MARKER_OIDC_JWKS_URL", "").strip() or None,
    )


def validate_oidc_jwt_skeleton(token: str) -> Principal | None:
    """Future OIDC/JWT hook.

    Static tokens remain the implemented path for UCM-009. This function makes
    OIDC configuration explicit without accepting unsigned or unverified JWTs.
    """

    if oidc_settings() is None:
        return None
    raise HTTPException(
        status_code=501,
        detail="OIDC/JWT validation is configured but not implemented in this build",
    )


def parse_scope_list(raw: str | None, *, default: list[str]) -> list[str]:
    if raw is None or not raw.strip():
        return list(default)
    return [item for item in raw.replace(",", " ").split() if item]


def configured_static_tokens(*, surface: str) -> dict[str, list[str]]:
    tokens: dict[str, list[str]] = {}
    for token, scopes in _parse_token_map(os.getenv("MARKER_AUTH_TOKENS", "")):
        tokens[token] = scopes
    if surface == "mcp":
        token = os.getenv("MARKER_MCP_AUTH_TOKEN", "").strip()
        if token:
            tokens[token] = parse_scope_list(os.getenv("MARKER_MCP_AUTH_SCOPES"), default=DEFAULT_MCP_SCOPES)
    if surface == "rest":
        token = (os.getenv("MARKER_REST_AUTH_TOKEN") or os.getenv("MARKER_AUTH_TOKEN") or "").strip()
        if token:
            tokens[token] = parse_scope_list(os.getenv("MARKER_REST_AUTH_SCOPES"), default=DEFAULT_REST_SCOPES)
    return tokens


def principal_for_token(token: str, *, surface: str) -> Principal | None:
    for expected, scopes in configured_static_tokens(surface=surface).items():
        if secrets.compare_digest(token, expected):
            return Principal(token=token, scopes=frozenset(scopes), client_id=f"marker-{surface}")
    return validate_oidc_jwt_skeleton(token)


def principal_from_authorization(header: str | None, *, surface: str) -> Principal | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return principal_for_token(token.strip(), surface=surface)


def rest_auth_enabled() -> bool:
    return bool(configured_static_tokens(surface="rest"))


def require_principal_scopes(principal: Principal, required: set[str]) -> None:
    if not has_scopes(set(principal.scopes), required):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "required": sorted(required),
            },
        )


def require_rest_scopes(*required: str) -> Callable[[Request], Principal]:
    required_set = set(required)

    def dependency(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            principal = principal_from_authorization(request.headers.get("Authorization"), surface="rest")
        if principal is None:
            if rest_auth_enabled():
                raise HTTPException(status_code=401, detail="Missing or invalid bearer token")
            principal = Principal(token="", scopes=frozenset(DEFAULT_REST_SCOPES), client_id="anonymous-local")
        require_principal_scopes(principal, required_set)
        return principal

    return dependency


def require_mcp_scopes(*required: str) -> None:
    access_token = get_access_token()
    if access_token is None:
        return
    granted = set(access_token.scopes or [])
    required_set = set(required)
    if not has_scopes(granted, required_set):
        raise PermissionError(f"Missing MCP scope(s): {', '.join(sorted(required_set - granted))}")


class ScopedStaticTokenVerifier:
    def __init__(self, token_scopes: dict[str, list[str]]) -> None:
        self._token_scopes = token_scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = principal_for_token(token, surface="mcp")
        if principal is None:
            for expected, scopes in self._token_scopes.items():
                if secrets.compare_digest(token, expected):
                    principal = Principal(token=token, scopes=frozenset(scopes), client_id="marker-mcp")
                    break
        if principal is None:
            return None
        return AccessToken(token=token, client_id=principal.client_id, scopes=sorted(principal.scopes))


class RestAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not rest_auth_enabled() or request.url.path == "/api/health":
            return await call_next(request)
        principal = principal_from_authorization(request.headers.get("Authorization"), surface="rest")
        if principal is None:
            await record_audit_event(
                None,
                event_type="auth.denied",
                surface="rest",
                resource_type="http_request",
                resource_id=request.url.path,
                status="denied",
                payload={"path": request.url.path, "method": request.method},
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.principal = principal
        return await call_next(request)


def _parse_token_map(raw: str) -> list[tuple[str, list[str]]]:
    entries: list[tuple[str, list[str]]] = []
    for part in raw.replace("\n", ";").split(";"):
        item = part.strip()
        if not item:
            continue
        if "=" in item:
            token, scopes_raw = item.split("=", 1)
        else:
            token, scopes_raw = item, ""
        token = token.strip()
        if token:
            entries.append((token, parse_scope_list(scopes_raw, default=DEFAULT_REST_SCOPES)))
    return entries
