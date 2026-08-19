"""Agent-surface registry facts for the PR79B query/events tools."""

from __future__ import annotations

from app.agent_surface import (
    MCP_MINIMAL_TOOL_NAMES,
    MCP_TOOL_SPEC_BY_NAME,
    tool_names_for_profile,
)
from app.security.scopes import (
    ALL_SCOPES,
    DEFAULT_MCP_SCOPES,
    SCOPE_EVENTS_READ,
    SCOPE_QUERIES_READ,
)


def test_query_and_events_are_canonical_v2_tools() -> None:
    assert "marker_query" in MCP_MINIMAL_TOOL_NAMES
    assert "marker_events" in MCP_MINIMAL_TOOL_NAMES


def test_query_and_events_appear_in_every_profile() -> None:
    for profile in ("minimal", "full", "admin"):
        names = tool_names_for_profile(profile)
        assert "marker_query" in names
        assert "marker_events" in names


def test_query_spec_declares_read_only_annotations_and_scope() -> None:
    spec = MCP_TOOL_SPEC_BY_NAME["marker_query"]
    assert spec.annotations["readOnlyHint"] is True
    assert spec.annotations["destructiveHint"] is False
    assert spec.scopes == (SCOPE_QUERIES_READ,)
    assert spec.profile == "minimal"
    assert not spec.deprecated
    assert spec.aliases == ()


def test_events_spec_declares_read_only_annotations_and_scope() -> None:
    spec = MCP_TOOL_SPEC_BY_NAME["marker_events"]
    assert spec.annotations["readOnlyHint"] is True
    assert spec.annotations["destructiveHint"] is False
    assert spec.scopes == (SCOPE_EVENTS_READ,)
    assert spec.profile == "minimal"
    assert not spec.deprecated
    assert spec.aliases == ()


def test_query_scopes_are_part_of_default_grants() -> None:
    assert SCOPE_QUERIES_READ in ALL_SCOPES
    assert SCOPE_EVENTS_READ in ALL_SCOPES
    assert SCOPE_QUERIES_READ in DEFAULT_MCP_SCOPES
    assert SCOPE_EVENTS_READ in DEFAULT_MCP_SCOPES
