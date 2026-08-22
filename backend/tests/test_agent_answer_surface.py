"""PR85 agent-surface registry facts for the answer-evidence tools."""

from __future__ import annotations

from app.agent_surface import (
    MCP_ANSWER_TOOL_NAMES,
    MCP_MINIMAL_TOOL_NAMES,
    MCP_TOOL_SPEC_BY_NAME,
    tool_names_for_profile,
)
from app.security.scopes import (
    ALL_SCOPES,
    DEFAULT_MCP_SCOPES,
    DEFAULT_REST_SCOPES,
    SCOPE_ANSWERS_WRITE,
)


def test_answer_tools_are_registered_but_not_minimal() -> None:
    """The minimal profile's bounded 10-tool surface stays intact; the
    answer-evidence workflow is an explicit full/admin opt-in."""

    assert MCP_ANSWER_TOOL_NAMES == ("marker_answer_trace", "marker_answer_assessment")
    for name in MCP_ANSWER_TOOL_NAMES:
        assert name not in MCP_MINIMAL_TOOL_NAMES
        assert name in tool_names_for_profile("full")
        assert name in tool_names_for_profile("admin")


def test_answer_tool_specs_declare_write_annotations_and_scope() -> None:
    for name in MCP_ANSWER_TOOL_NAMES:
        spec = MCP_TOOL_SPEC_BY_NAME[name]
        assert spec.annotations["readOnlyHint"] is False
        assert spec.annotations["destructiveHint"] is False
        assert spec.annotations["idempotentHint"] is True
        assert spec.annotations["openWorldHint"] is False
        assert spec.scopes == (SCOPE_ANSWERS_WRITE,)
        assert spec.profile == "full"
        assert not spec.deprecated
        assert spec.aliases == ()


def test_answers_write_scope_is_part_of_default_grants() -> None:
    assert SCOPE_ANSWERS_WRITE in ALL_SCOPES
    assert SCOPE_ANSWERS_WRITE in DEFAULT_MCP_SCOPES
    assert SCOPE_ANSWERS_WRITE in DEFAULT_REST_SCOPES
