"""Deterministic anchor-based candidate generation (PR80A).

This route proves the extraction contract without a model: every
candidate comes from a literal anchored match inside one served
evidence unit, and carries that unit's full citation. Retrieval is NOT
entailment — a candidate existing only means text was delivered and
parsed; whether it survives validation and reconciliation is decided
elsewhere.

Rerunning this route over the same packet yields byte-identical
candidates: no clocks, no randomness, no ambient state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.extraction.results import CandidateView, EvidenceCitation
from app.extraction.schema import ExtractionSchema
from app.extraction.validation import parse_typed

#: Route identity recorded in every candidate's derivation.
ANCHOR_ROUTE = "anchor.v1"

#: Value separator between an anchor label and its value.
_ANCHOR_SEPARATOR = r"[:\uff1a]"

#: Column separator inside a line-item row unit.
_ROW_COLUMN_SEPARATOR = "|"


def _citation(unit: Any, packet_identity_id: str) -> EvidenceCitation:
    locator = unit.locator
    return EvidenceCitation(
        record_id=locator.record_id,
        revision_ref=locator.revision_ref,
        text_hash=locator.text_hash,
        node_id=locator.node_id,
        publication_set_id=locator.publication_set_id,
        materialized_generation_id=locator.materialized_generation_id,
        packet_identity_id=packet_identity_id,
        op=unit.op,
    )


@dataclass(frozen=True)
class ItemCandidate:
    """One parsed line-item row grounded in one evidence unit."""

    identity: Mapping[str, Any]
    fields: Mapping[str, CandidateView]

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "fields": {name: c.to_dict() for name, c in self.fields.items()},
        }


@dataclass(frozen=True)
class CandidateSet:
    """All candidates a route produced for one extraction run."""

    scalars: Mapping[str, tuple[CandidateView, ...]] = field(default_factory=dict)
    items: Mapping[str, tuple[ItemCandidate, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scalars": {
                name: [c.to_dict() for c in cands]
                for name, cands in self.scalars.items()
            },
            "items": {
                name: [item.to_dict() for item in items]
                for name, items in self.items.items()
            },
        }


def _scalar_candidates(
    unit: Any, packet_identity_id: str, schema: ExtractionSchema
) -> dict[str, CandidateView]:
    """Extract every scalar field whose anchor matches this unit once."""
    found: dict[str, CandidateView] = {}
    for spec in schema.fields:
        pattern = re.compile(
            re.escape(spec.anchor) + _ANCHOR_SEPARATOR + r"\s*(?P<value>[^\n]+)"
        )
        match = pattern.search(unit.text)
        if match is None:
            continue
        raw = match.group("value")
        parsed = parse_typed(raw, spec)
        found[spec.name] = CandidateView(
            raw_text=raw,
            value=parsed.value,
            evidence=(_citation(unit, packet_identity_id),),
            derivation={
                "route": ANCHOR_ROUTE,
                "anchor": spec.anchor,
                "field": spec.name,
            },
            parse_error=parsed.error,
        )
    return found


def _row_candidates(
    unit: Any, packet_identity_id: str, schema: ExtractionSchema
) -> dict[str, ItemCandidate]:
    """Extract every line-item row this unit represents (at most one each)."""
    found: dict[str, ItemCandidate] = {}
    stripped = unit.text.strip()
    for item_spec in schema.line_items:
        row_prefix = item_spec.anchor
        if not stripped.startswith(row_prefix):
            continue
        body = (
            stripped[len(row_prefix) :].strip().lstrip(_ROW_COLUMN_SEPARATOR).strip()
        )
        columns = [col.strip() for col in body.split(_ROW_COLUMN_SEPARATOR)]
        if len(columns) != len(item_spec.fields):
            # A structurally wrong row is not a candidate; reconciliation
            # sees only rows the identity rule can actually evaluate.
            continue
        field_views: dict[str, CandidateView] = {}
        for spec, raw in zip(item_spec.fields, columns):
            parsed = parse_typed(raw, spec)
            field_views[spec.name] = CandidateView(
                raw_text=raw,
                value=parsed.value,
                evidence=(_citation(unit, packet_identity_id),),
                derivation={
                    "route": ANCHOR_ROUTE,
                    "anchor": item_spec.anchor,
                    "field": f"{item_spec.name}.{spec.name}",
                },
                parse_error=parsed.error,
            )
        identity: dict[str, Any] = {}
        for key in item_spec.identity_keys:
            identity[key] = field_views[key].value
        found[item_spec.name] = ItemCandidate(identity=identity, fields=field_views)
    return found


def extract_candidates(packet: Any, schema: ExtractionSchema) -> CandidateSet:
    """Run the deterministic anchor route over one evidence packet."""
    scalars: dict[str, list[CandidateView]] = {spec.name: [] for spec in schema.fields}
    items: dict[str, list[ItemCandidate]] = {
        item.name: [] for item in schema.line_items
    }
    for unit in packet.evidence:
        for name, candidate in _scalar_candidates(unit, packet.identity_id, schema).items():
            scalars[name].append(candidate)
        for name, item in _row_candidates(unit, packet.identity_id, schema).items():
            items[name].append(item)
    return CandidateSet(
        scalars={name: tuple(cands) for name, cands in scalars.items()},
        items={name: tuple(rows) for name, rows in items.items()},
    )
