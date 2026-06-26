"""Authorization scope constants for REST and MCP surfaces."""

from __future__ import annotations

SCOPE_MARKER_MCP = "marker:mcp"
SCOPE_CAPABILITIES_READ = "capabilities:read"
SCOPE_JOBS_READ = "jobs:read"
SCOPE_JOBS_WRITE = "jobs:write"
SCOPE_OUTPUTS_READ = "outputs:read"
SCOPE_SETTINGS_READ = "settings:read"
SCOPE_SETTINGS_WRITE = "settings:write"

ALL_SCOPES = {
    SCOPE_MARKER_MCP,
    SCOPE_CAPABILITIES_READ,
    SCOPE_JOBS_READ,
    SCOPE_JOBS_WRITE,
    SCOPE_OUTPUTS_READ,
    SCOPE_SETTINGS_READ,
    SCOPE_SETTINGS_WRITE,
}

DEFAULT_MCP_SCOPES = sorted(ALL_SCOPES)
DEFAULT_REST_SCOPES = sorted(ALL_SCOPES - {SCOPE_MARKER_MCP})


def has_scopes(granted: set[str], required: set[str]) -> bool:
    return "*" in granted or required.issubset(granted)
