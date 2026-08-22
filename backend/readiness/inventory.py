"""Canonical V3.2 readiness-invariant inventory (amendment 23C, items 1-62).

The JSON data file is the single canonical copy of the governing wording.
``extract_masterplan_invariants`` re-derives the same 62 items straight
from the master plan so tests can prove the inventory never drifts from
the governing text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import INVENTORY_SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY_PATH = Path(__file__).resolve().parent / "readiness_invariants.json"
# The governing master plan lives in the gitignored planning/ tree, so audits
# bind to the committed release-governing extract instead. Regenerate it from
# a real master plan with backend/scripts/readiness_governing_extract.py;
# hand edits surface as inventory drift in --mode integrity.
DEFAULT_MASTERPLAN_PATH = (
    Path(__file__).resolve().parent / "governing" / "masterplan-amendment-23c.md"
)

EXPECTED_COUNT = 62
EXPECTED_IDS = frozenset(range(1, EXPECTED_COUNT + 1))

_SECTION_HEADER = "## V3.2 amendment 23C - Adversarially hardened readiness definition"
_ITEM_RE = re.compile(r"^(\d+)\. (.+)$")
_GROUP_RE = re.compile(r"^### (23C\.\d) (.+)$")


class InventoryError(ValueError):
    """Raised when the canonical inventory is malformed or incomplete."""


@dataclass(frozen=True)
class Invariant:
    id: int
    group: str
    group_name: str
    label: str
    text: str


def parse_inventory(data: dict) -> list[Invariant]:
    """Validate raw inventory JSON and return invariants ordered by id."""

    if not isinstance(data, dict):
        raise InventoryError("inventory must be a JSON object")
    schema = data.get("schema")
    if schema != INVENTORY_SCHEMA:
        raise InventoryError(f"unsupported inventory schema: {schema!r}")
    groups = data.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise InventoryError("inventory must declare a non-empty groups map")
    entries = data.get("invariants")
    if not isinstance(entries, list) or not entries:
        raise InventoryError("inventory must declare a non-empty invariants list")

    seen: set[int] = set()
    invariants: list[Invariant] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise InventoryError("each invariant entry must be an object")
        inv_id = entry.get("id")
        if not isinstance(inv_id, int) or isinstance(inv_id, bool):
            raise InventoryError(f"invariant id must be an integer, got {inv_id!r}")
        if inv_id in seen:
            raise InventoryError(f"duplicate invariant id: {inv_id}")
        seen.add(inv_id)
        group = entry.get("group")
        if group not in groups:
            raise InventoryError(f"invariant {inv_id} has unknown group {group!r}")
        label = entry.get("label")
        text = entry.get("text")
        if not isinstance(label, str) or not label.strip():
            raise InventoryError(f"invariant {inv_id} must have a non-empty label")
        if not isinstance(text, str) or not text.strip():
            raise InventoryError(f"invariant {inv_id} must have non-empty governing text")
        invariants.append(
            Invariant(
                id=inv_id,
                group=group,
                group_name=groups[group],
                label=label,
                text=text.strip(),
            )
        )

    invariants.sort(key=lambda inv: inv.id)
    missing = EXPECTED_IDS - seen
    if missing:
        raise InventoryError(f"inventory is missing governing invariants: {sorted(missing)}")
    unknown = seen - EXPECTED_IDS
    if unknown:
        raise InventoryError(f"inventory contains non-governing invariant ids: {sorted(unknown)}")
    return invariants


def load_inventory(path: str | Path | None = None) -> list[Invariant]:
    inventory_path = Path(path) if path is not None else DEFAULT_INVENTORY_PATH
    try:
        data = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot load inventory from {inventory_path}: {exc}") from exc
    return parse_inventory(data)


def extract_masterplan_invariants(path: str | Path | None = None) -> dict[int, str]:
    """Extract the governing 1..62 item texts from the V3.2 master plan.

    Returns ``{id: text}`` for amendment 23C. Numbered items may wrap onto
    continuation lines; continuation lines are joined with a single space.
    """

    masterplan_path = Path(path) if path is not None else DEFAULT_MASTERPLAN_PATH
    lines = masterplan_path.read_text(encoding="utf-8").splitlines()

    in_section = False
    items: dict[int, str] = {}
    last_id: int | None = None
    for line in lines:
        if not in_section:
            if line.strip() == _SECTION_HEADER:
                in_section = True
            continue
        if line.startswith("## "):
            break  # next top-level section ends amendment 23C
        if _GROUP_RE.match(line) or line.startswith("---"):
            last_id = None
            continue
        match = _ITEM_RE.match(line)
        if match:
            last_id = int(match.group(1))
            items[last_id] = match.group(2).strip()
        elif last_id is not None and line.strip():
            items[last_id] = items[last_id] + " " + line.strip()
    return items


def masterplan_drift(invariants: list[Invariant], path: str | Path | None = None) -> list[str]:
    """Describe any wording drift between the inventory and the master plan."""

    extracted = extract_masterplan_invariants(path)
    problems: list[str] = []
    for inv in invariants:
        governing = extracted.get(inv.id)
        if governing is None:
            problems.append(f"invariant {inv.id} not found in master plan amendment 23C")
        elif governing.strip() != inv.text:
            problems.append(f"invariant {inv.id} text drifted from master plan wording")
    extra = set(extracted) - {inv.id for inv in invariants}
    if extra:
        problems.append(f"master plan contains non-governing numbered items: {sorted(extra)}")
    return problems
