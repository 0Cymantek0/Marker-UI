"""PR85 executable documentation contract (readiness invariant 48).

Invariant 48: "Marker UI documentation states that already disclosed
external-agent context cannot be revoked." Prose alone is not the
evidence — this test is: it fails when the canonical statement is
removed or watered down, on every documentation surface agents actually
read (reference doc, MCP guide, runtime agent-guide resource, tool
descriptions).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app import mcp_resources, mcp_server
from app.agent_surface import MCP_TOOL_SPEC_BY_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DOC = REPO_ROOT / "docs" / "reference" / "pr85-answer-evidence.md"
MCP_GUIDE = REPO_ROOT / "docs" / "usage" / "mcp.md"


def _assert_contract(text: str, surface: str) -> None:
    """The invariant-48 contract checker.

    Requires the three-part distinction in substance, not a keyword:
    future disclosure stops, past external disclosure is irreversible,
    and local deletion is not remote revocation.
    """

    lowered = text.lower()
    assert "cannot be retroactively revoked" in lowered or (
        "cannot" in lowered
        and "retroactively" in lowered
        and ("revok" in lowered or "unsee" in lowered or "un-copied" in lowered)
    ), (
        f"{surface}: the non-revocability statement is missing or no longer "
        "says that already-disclosed external-agent context cannot be "
        "retroactively revoked"
    )
    assert "external agent" in lowered or "external model" in lowered, (
        f"{surface}: the statement must name the external agent/model as the "
        "holder of the already-disclosed context"
    )
    assert "future" in lowered or "going forward" in lowered or (
        "stops future" in lowered or "stops disclosing" in lowered
    ), (
        f"{surface}: the statement must scope revocation to future disclosure"
    )


def test_reference_doc_states_non_revocability_contract() -> None:
    _assert_contract(
        REFERENCE_DOC.read_text(encoding="utf-8"),
        str(REFERENCE_DOC.relative_to(REPO_ROOT)),
    )


def test_mcp_guide_states_non_revocability_contract() -> None:
    text = MCP_GUIDE.read_text(encoding="utf-8")
    _assert_contract(text, str(MCP_GUIDE.relative_to(REPO_ROOT)))
    # The guide's Answer Evidence section must exist and mention the seam.
    assert "## Answer Evidence" in text
    assert "marker_answer_trace" in text
    assert "marker_answer_assessment" in text


def test_runtime_agent_guide_resource_states_the_boundary() -> None:
    source = inspect.getsource(mcp_resources)
    _assert_contract(source, "mcp_resources.agent-guide resource")


def test_tool_descriptions_state_the_boundary() -> None:
    trace_doc = inspect.getdoc(
        mcp_server.marker_answer_trace.__wrapped__
        if hasattr(mcp_server.marker_answer_trace, "__wrapped__")
        else mcp_server.marker_answer_trace
    )
    assert trace_doc, "marker_answer_trace must carry a description"
    _assert_contract(trace_doc, "marker_answer_trace tool description")
    spec = MCP_TOOL_SPEC_BY_NAME["marker_answer_trace"]
    assert spec.scopes, "registry spec must declare scopes"


def test_negative_control_checker_rejects_mutated_text() -> None:
    """AE-19: prove the checker itself fails on weakened wording."""

    import pytest

    weakened_variants = [
        # Vague marketing promise instead of the honest limit.
        "Revoking access removes all prior access everywhere.",
        # True statement about local records only — misses remote truth.
        "Marker UI can delete its local disclosure records retroactively.",
        # Mentions revocation but claims it undoes external disclosure.
        "Marker UI revokes future disclosure and unsees past external context.",
        # Empty.
        "",
    ]
    for variant in weakened_variants:
        with pytest.raises(AssertionError):
            _assert_contract(variant, "negative-control")
    # The honest statement passes.
    _assert_contract(
        "Context already disclosed to an external agent cannot be "
        "retroactively revoked; revocation only stops future disclosure "
        "going forward.",
        "positive-control",
    )
