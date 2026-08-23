"""Visual economics artifact honesty tests (invariant 58).

The committed OFF/ON artifact must validate against the envelope
contract, the OFF arm must carry zero visual state, and the recorded
disposition must be reproducible by re-executing the predeclared rule
over the artifact's own measured inputs — so a hand-edited artifact
that flips the disposition or hides visual cost fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.eval.pr81a.economics_decision import (
    ALLOWED_DISPOSITIONS,
    evaluate_economics_disposition,
)
from app.eval.economics.validate import validate_envelope

REPO = Path(__file__).resolve().parent.parent.parent
ARTIFACT = REPO / "docs" / "reference" / "measurements" / "pr87c-visual-economics.json"


def _artifact() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def test_committed_artifact_passes_envelope_validation():
    errors = validate_envelope(_artifact())
    assert errors == []


def test_off_arm_carries_zero_visual_state():
    breakdown = _artifact()["dimensions"]["disabled_state_proof"]["breakdown"]
    assert breakdown == {
        "off_embedding_bytes": 0,
        "off_visual_index_entries": 0,
        "off_visual_lanes_executed": 0,
        "off_rerank_vlm_calls": 0,
    }
    assert _artifact()["dimensions"]["disabled_state_proof"]["value"] is True


def test_no_security_dangers_in_recorded_runs():
    dangers = _artifact()["dangers"]
    assert dangers.get("forbidden_delivered", 0) == 0
    assert dangers.get("stale_revision_delivered", 0) == 0
    assert dangers.get("unresolvable_source", 0) == 0


def test_recorded_disposition_is_reproducible_from_measured_inputs():
    artifact = _artifact()
    quality = artifact["dimensions"]["quality_gain"]["breakdown"]
    model = artifact["dimensions"]["model_service_delta"]["breakdown"]
    storage = artifact["dimensions"]["storage_delta"]["breakdown"]
    build = artifact["dimensions"]["build_delta"]["breakdown"]
    query = artifact["dimensions"]["query_delta"]
    acl = artifact["dimensions"]["acl_complexity"]["breakdown"]

    off_quality = {
        "visual_hard_task_success_rate": quality["off_visual_hard_rate"],
        "text_easy_task_success_rate": quality["off_text_easy_rate"],
    }
    on_hybrid_quality = {
        "visual_hard_task_success_rate": quality["on_hybrid_visual_hard_rate"],
        "text_easy_task_success_rate": quality["on_hybrid_text_easy_rate"],
    }
    on_dense_quality = {
        "visual_hard_task_success_rate": quality["on_dense_visual_hard_rate"],
        "text_easy_task_success_rate": None,
    }
    on_economics = {
        "avg_render_bytes_per_page": storage["avg_render_bytes_per_page_on"],
        "avg_embedding_bytes_per_page": storage["avg_embedding_bytes_per_page_on"],
        "warm_query_ms_p50": query["value"],
        "vlm_calls": model["on_vlm_calls"],
        "index_build_ms": build["on_index_build_ms"],
        "revision_rebuild_ms": build["on_revision_total_ms"],
    }
    off_economics = {
        "vlm_calls": model["off_vlm_calls"],
        "revision_rebuild_ms": build["off_revision_total_ms"],
    }
    replay = evaluate_economics_disposition(
        off_quality=off_quality,
        on_dense_quality=on_dense_quality,
        on_hybrid_quality=on_hybrid_quality,
        off_economics=off_economics,
        on_economics=on_economics,
        acl_cost={
            "visual_partitions": acl["visual_partitions"],
            "partition_duplicate_bytes": acl["partition_duplicate_bytes"],
            "partition_build_ms": acl["partition_build_ms"],
            "deny_to_effective_ms": acl["deny_to_effective_ms"],
            "denied_rebuilds_required": acl["denied_rebuilds_required"],
            "authorized_universe_filter_calls": acl["authorized_universe_filter_calls"],
        },
        danger_totals=artifact["dangers"],
    )
    recorded = artifact["disposition"]["disposition"]
    assert recorded in ALLOWED_DISPOSITIONS
    assert replay["disposition"] == recorded, (
        "committed disposition is not reproducible from the artifact's own "
        "measured inputs — the decision no longer follows the predeclared rule"
    )
    assert replay["hybrid_gain"] == artifact["disposition"]["hybrid_gain"]


def test_acl_vector_is_complete_and_deny_needs_zero_rebuilds():
    breakdown = _artifact()["dimensions"]["acl_complexity"]["breakdown"]
    for field in (
        "visual_partitions",
        "partition_duplicate_bytes",
        "partition_build_ms",
        "deny_to_effective_ms",
        "denied_rebuilds_required",
        "authorized_universe_filter_calls",
    ):
        assert field in breakdown, f"ACL vector missing {field}"
        assert isinstance(breakdown[field], (int, float))
    assert breakdown["denied_rebuilds_required"] == 0
    assert breakdown["visual_partitions"] >= 1
    assert breakdown["authorized_universe_filter_calls"] > 0
