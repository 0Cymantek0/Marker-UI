"""Adversarial agent/query evaluation tests (PR82A Q9/Q10)."""

from __future__ import annotations

import pytest

from app.eval.pr82.agent import (
    HOSTILE_PAYLOADS,
    evaluate_agent,
    evaluate_mcp_compat,
)


@pytest.mark.asyncio
async def test_hostile_documents_never_manufacture_authority(payload_env):
    factory, _store, _service = payload_env
    result = await evaluate_agent(factory, run_id="hostile")
    assert result.violations == ()
    # Every OWASP-derived class was actually exercised.
    assert set(result.hostile_checks) == set(HOSTILE_PAYLOADS)
    for name, checks in result.hostile_checks.items():
        assert all(checks.values()), (name, checks)


@pytest.mark.asyncio
async def test_revision_and_deny_during_task_are_structured(payload_env):
    factory, _store, _service = payload_env
    result = await evaluate_agent(factory, run_id="revision")
    assert result.revision_checks == {
        "inflight_packet_pinned_to_original": True,
        "new_query_sees_new_publication": True,
        "revised_text_only_in_new": True,
        "denied_content_not_delivered_stale": True,
        "post_deny_is_no_hit_not_stale": True,
    }


def test_mcp_compat_records_the_current_spec_era_honestly():
    compat = evaluate_mcp_compat()
    assert compat["sdk_version"]  # the dependency is installed and pinned
    assert compat["sdk_protocol_revision"]
    assert compat["verdict"] == "aligned_deprecated_era"
    assert compat["spec_latest_era"] == "2026-07-28"
    assert "next_cursor" in compat["state_handle"]
    assert "PR84" in compat["follow_up"]
