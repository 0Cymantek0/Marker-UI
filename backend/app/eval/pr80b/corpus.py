"""Corpus/manifest loading and gold-integrity validation for PR80B.

The loader fail-closes on any inconsistency between the manifest, the
document files, and the gold truth (including recomputing every
declared-satisfied sum invariant from gold rows) so a scoring surprise
can never be traced back to a malformed fixture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: Field metadata for the benchmark slice (mirrors demo.invoice@1.0.0).
SCALAR_FIELDS: dict[str, str] = {
    "invoice_number": "string",
    "invoice_date": "date",
    "currency": "enum",
    "po_number": "string",
    "total_due": "decimal",
}
ENUM_VALUES = ("USD", "EUR", "GBP")
ROW_FIELDS: dict[str, str] = {
    "description": "string",
    "quantity": "integer",
    "unit_price": "decimal",
    "amount": "decimal",
}
ROW_IDENTITY = "sku"

FIELD_STATUSES = {"present", "absent", "conflicting"}
INVARIANT_EXPECTATIONS = {"satisfied", "violated", "not_evaluable"}


class CorpusError(ValueError):
    """Raised when the corpus, manifest, or gold truth is inconsistent."""


@dataclass(frozen=True)
class CorpusDoc:
    """One benchmark document with its parts and validated gold."""

    doc_id: str
    slices: tuple[str, ...]
    part_texts: tuple[str, ...]
    gold: dict
    full_text: str
    part_names: tuple[str, ...]

    @property
    def multi_record(self) -> bool:
        return len(self.part_texts) > 1


@dataclass(frozen=True)
class Corpus:
    """The loaded, validated benchmark corpus."""

    root: Path
    manifest_version: str
    docs: tuple[CorpusDoc, ...]
    fingerprint: str
    slice_counts: dict[str, int] = field(default_factory=dict)
    provenance: str = ""
    task: dict = field(default_factory=dict)

    def doc(self, doc_id: str) -> CorpusDoc:
        for entry in self.docs:
            if entry.doc_id == doc_id:
                return entry
        raise CorpusError(f"unknown doc_id {doc_id!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusError(message)


def _validate_gold(doc_id: str, gold: dict) -> None:
    _require(gold.get("doc_id") == doc_id, f"{doc_id}: gold doc_id mismatch")
    _require(isinstance(gold.get("slices"), list) and gold["slices"], f"{doc_id}: gold slices missing")
    fields = gold.get("fields")
    _require(isinstance(fields, dict), f"{doc_id}: gold fields must be a mapping")
    _require(
        set(fields) == set(SCALAR_FIELDS),
        f"{doc_id}: gold scalar fields {sorted(fields)} != {sorted(SCALAR_FIELDS)}",
    )
    for name, entry in fields.items():
        _require(isinstance(entry, dict), f"{doc_id}: field {name} must be a mapping")
        status = entry.get("status")
        _require(status in FIELD_STATUSES, f"{doc_id}: field {name} has bad status {status!r}")
        if status == "present":
            _require("value" in entry, f"{doc_id}: field {name} present without value")
        elif status == "conflicting":
            values = entry.get("values")
            _require(
                isinstance(values, list) and len(values) >= 2,
                f"{doc_id}: field {name} conflicting needs >=2 values",
            )

    items = gold.get("items")
    _require(isinstance(items, dict), f"{doc_id}: gold items must be a mapping")
    _require(items.get("status") in {"rows"}, f"{doc_id}: gold items.status must be 'rows'")
    rows = items.get("rows")
    _require(isinstance(rows, list) and rows, f"{doc_id}: gold rows missing")
    seen_skus: set[str] = set()
    for row in rows:
        sku = row.get(ROW_IDENTITY)
        _require(isinstance(sku, str) and sku, f"{doc_id}: row without sku identity")
        _require(sku not in seen_skus, f"{doc_id}: duplicate gold sku {sku!r}")
        seen_skus.add(sku)
        row_fields = row.get("fields")
        _require(
            isinstance(row_fields, dict) and set(row_fields) == set(ROW_FIELDS),
            f"{doc_id}: row {sku} fields {sorted(row_fields or [])} != {sorted(ROW_FIELDS)}",
        )
        for name, entry in row_fields.items():
            status = entry.get("status")
            _require(status in FIELD_STATUSES, f"{doc_id}: row {sku} field {name} bad status {status!r}")
            if status == "present":
                _require("value" in entry, f"{doc_id}: row {sku} field {name} present without value")

    invariant = gold.get("invariant")
    _require(isinstance(invariant, dict), f"{doc_id}: gold invariant missing")
    expected = invariant.get("expected")
    _require(
        expected in INVARIANT_EXPECTATIONS,
        f"{doc_id}: invariant expected {expected!r} invalid",
    )
    _validate_invariant_math(doc_id, gold, expected)


def _gold_decimal(doc_id: str, value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise CorpusError(f"{doc_id}: {value!r} is not a decimal in gold") from None
    return parsed


def _validate_invariant_math(doc_id: str, gold: dict, expected: str) -> None:
    """Recompute the declared invariant expectation from gold truth."""
    total_entry = gold["fields"]["total_due"]
    row_amounts: list[Decimal] = []
    complete = total_entry.get("status") == "present"
    if complete:
        total = _gold_decimal(doc_id, total_entry["value"])
    for row in gold["items"]["rows"]:
        amount_entry = row["fields"]["amount"]
        if amount_entry.get("status") != "present":
            complete = False
            continue
        row_amounts.append(_gold_decimal(doc_id, amount_entry["value"]))
    if not complete or not row_amounts:
        _require(
            expected == "not_evaluable",
            f"{doc_id}: invariant expected {expected!r} but truth is not evaluable",
        )
        return
    row_sum = sum(row_amounts, Decimal(0))
    matches = abs(total - row_sum) <= Decimal("0.01")
    if matches:
        _require(
            expected == "satisfied",
            f"{doc_id}: gold rows sum {row_sum} == total {total} but expected {expected!r}",
        )
    else:
        _require(
            expected == "violated",
            f"{doc_id}: gold rows sum {row_sum} != total {total} but expected {expected!r}",
        )


def load_corpus(root: Path) -> Corpus:
    """Load and validate the PR80B corpus rooted at ``root``."""
    root = Path(root)
    manifest_path = root / "manifest.json"
    _require(manifest_path.is_file(), f"manifest not found at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require(
        manifest.get("manifest_version") == "marker.pr80b_corpus.v1",
        f"unsupported manifest_version {manifest.get('manifest_version')!r}",
    )
    documents = manifest.get("documents")
    _require(isinstance(documents, list) and documents, "manifest has no documents")

    digest = hashlib.sha256()
    docs: list[CorpusDoc] = []
    slice_counts: dict[str, int] = {}
    for entry in documents:
        doc_id = entry.get("doc_id")
        parts = entry.get("parts")
        gold_path = entry.get("gold")
        slices = entry.get("slices")
        _require(isinstance(doc_id, str) and doc_id, "manifest entry without doc_id")
        _require(isinstance(parts, list) and parts, f"{doc_id}: no parts")
        _require(isinstance(gold_path, str), f"{doc_id}: no gold path")
        _require(isinstance(slices, list) and slices, f"{doc_id}: no slices")
        part_texts: list[str] = []
        part_names: list[str] = []
        for part in parts:
            part_file = root / part
            _require(part_file.is_file(), f"{doc_id}: missing part file {part}")
            text = part_file.read_text(encoding="utf-8")
            part_texts.append(text)
            part_names.append(part)
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(part_file).encode("ascii"))
        gold_file = root / gold_path
        _require(gold_file.is_file(), f"{doc_id}: missing gold file {gold_path}")
        gold = json.loads(gold_file.read_text(encoding="utf-8"))
        _validate_gold(doc_id, gold)
        _require(
            list(gold["slices"]) == list(slices),
            f"{doc_id}: manifest slices {slices} != gold slices {gold['slices']}",
        )
        digest.update(gold_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(gold_file).encode("ascii"))
        for tag in slices:
            slice_counts[tag] = slice_counts.get(tag, 0) + 1
        docs.append(
            CorpusDoc(
                doc_id=doc_id,
                slices=tuple(slices),
                part_texts=tuple(part_texts),
                gold=gold,
                full_text="\n\n".join(part_texts),
                part_names=tuple(parts),
            )
        )
    declared_counts = manifest.get("slice_counts", {})
    _require(
        declared_counts == slice_counts,
        f"manifest slice_counts {declared_counts} != computed {slice_counts}",
    )
    return Corpus(
        root=root,
        manifest_version=manifest["manifest_version"],
        docs=tuple(docs),
        fingerprint=digest.hexdigest(),
        slice_counts=slice_counts,
        provenance=manifest.get("provenance", ""),
        task=manifest.get("task", {}),
    )
