"""Canonical-inventory tests: completeness, uniqueness, determinism, drift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readiness.inventory import (
    DEFAULT_INVENTORY_PATH,
    DEFAULT_MASTERPLAN_PATH,
    InventoryError,
    extract_masterplan_invariants,
    load_inventory,
    masterplan_drift,
    parse_inventory,
)


def _raw_inventory() -> dict:
    return json.loads(DEFAULT_INVENTORY_PATH.read_text(encoding="utf-8"))


def test_inventory_covers_exactly_ids_1_through_62() -> None:
    invariants = load_inventory()
    assert [inv.id for inv in invariants] == list(range(1, 63))


def test_inventory_ids_unique() -> None:
    ids = [inv.id for inv in load_inventory()]
    assert len(ids) == len(set(ids)) == 62


def test_inventory_groups_are_the_seven_amendment_groups() -> None:
    invariants = load_inventory()
    groups = {inv.group for inv in invariants}
    assert groups == {f"23C.{n}" for n in range(1, 8)}
    for inv in invariants:
        assert inv.group_name
        assert inv.label
        assert inv.text


def test_inventory_group_membership_counts() -> None:
    counts: dict[str, int] = {}
    for inv in load_inventory():
        counts[inv.group] = counts.get(inv.group, 0) + 1
    assert counts == {
        "23C.1": 9,
        "23C.2": 9,
        "23C.3": 9,
        "23C.4": 11,
        "23C.5": 10,
        "23C.6": 8,
        "23C.7": 6,
    }


def test_inventory_load_is_deterministic() -> None:
    assert load_inventory() == load_inventory()


def test_inventory_wording_matches_governing_masterplan_exactly() -> None:
    drift = masterplan_drift(load_inventory())
    assert drift == []


def test_masterplan_extraction_finds_all_62_items() -> None:
    extracted = extract_masterplan_invariants()
    assert set(extracted) == set(range(1, 63))


def test_missing_invariant_is_rejected() -> None:
    raw = _raw_inventory()
    raw["invariants"] = [e for e in raw["invariants"] if e["id"] != 30]
    with pytest.raises(InventoryError, match=r"missing governing invariants: \[30\]"):
        parse_inventory(raw)


def test_duplicate_invariant_is_rejected() -> None:
    raw = _raw_inventory()
    raw["invariants"].append(dict(raw["invariants"][0]))
    with pytest.raises(InventoryError, match="duplicate invariant id: 1"):
        parse_inventory(raw)


def test_unknown_invariant_is_rejected() -> None:
    raw = _raw_inventory()
    raw["invariants"].append({"id": 63, "group": "23C.7", "label": "x", "text": "y"})
    with pytest.raises(InventoryError, match="non-governing invariant ids: \[63\]"):
        parse_inventory(raw)


def test_unsupported_schema_is_rejected() -> None:
    raw = _raw_inventory()
    raw["schema"] = "marker.pr84a_readiness_inventory.v0"
    with pytest.raises(InventoryError, match="unsupported inventory schema"):
        parse_inventory(raw)


def test_unknown_group_is_rejected() -> None:
    raw = _raw_inventory()
    raw["invariants"][0]["group"] = "23C.9"
    with pytest.raises(InventoryError, match="unknown group"):
        parse_inventory(raw)


def test_missing_invariant_cannot_hide_behind_a_complete_report_shape() -> None:
    """A ledger consumer must not be able to report 'complete' with 61 items."""
    raw = _raw_inventory()
    raw["invariants"] = raw["invariants"][:61]
    with pytest.raises(InventoryError):
        parse_inventory(raw)


def test_masterplan_extraction_joins_wrapped_item_lines(tmp_path: Path) -> None:
    masterplan = tmp_path / "masterplan.md"
    masterplan.write_text(
        "## V3.2 amendment 23C - Adversarially hardened readiness definition\n"
        "intro\n"
        "### 23C.1 Truth and persistence\n"
        "1. First governing item that wraps\nonto a second line.\n"
        "2. Second item.\n"
        "## 24. Next section\n"
        "3. Not in the amendment.\n",
        encoding="utf-8",
    )
    extracted = extract_masterplan_invariants(masterplan)
    assert extracted == {
        1: "First governing item that wraps onto a second line.",
        2: "Second item.",
    }


def test_drift_detection_reports_reworded_invariant(tmp_path: Path) -> None:
    masterplan = tmp_path / "masterplan.md"
    masterplan.write_text(
        "## V3.2 amendment 23C - Adversarially hardened readiness definition\n"
        "1. Reworded governing item.\n",
        encoding="utf-8",
    )
    invariants = load_inventory()
    drift = masterplan_drift(invariants, masterplan)
    assert any("text drifted" in problem for problem in drift)
    assert DEFAULT_MASTERPLAN_PATH.exists()


def test_default_governing_source_is_committed_in_the_repository() -> None:
    """Integrity audits must be hermetic from a clean checkout (PR69 preflight).

    The governing source is the committed release-governing extract, not the
    gitignored planning tree. If this path ever moves back under ``planning/``,
    ``--mode integrity`` breaks on every fresh clone.
    """
    resolved = DEFAULT_MASTERPLAN_PATH.resolve()
    repo_parts = resolved.parts
    assert "planning" not in repo_parts
    assert DEFAULT_MASTERPLAN_PATH.is_file()
    # And it lives under backend/readiness/governing/, which is tracked.
    assert resolved.parent.name == "governing"


def test_governing_extract_is_parseable_and_complete() -> None:
    """The committed extract itself yields exactly the 62 governing items."""
    extracted = extract_masterplan_invariants(DEFAULT_MASTERPLAN_PATH)
    assert set(extracted) == set(range(1, 63))


def test_hand_edited_extract_fails_drift_loudly(tmp_path: Path) -> None:
    """A tampered committed extract must not silently become the new truth.

    Editing one invariant's wording in the extract makes the committed
    inventory JSON drift from it — the exact anti-drift guarantee the
    integrity mode exists to enforce.
    """
    text = DEFAULT_MASTERPLAN_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("30. "):
            lines[i] = "30. Tampered admission wording."
            break
    else:
        pytest.fail("invariant 30 not found in governing extract")
    tampered = tmp_path / "tampered.md"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    drift = masterplan_drift(load_inventory(), tampered)
    assert any("30" in problem and "drifted" in problem for problem in drift)
