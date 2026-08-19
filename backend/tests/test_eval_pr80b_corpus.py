"""Corpus loader validation tests for the PR80B benchmark (matrix L)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.eval.pr80b.corpus import CorpusError, load_corpus

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "eval_data" / "pr80b"


def _load_real():
    return load_corpus(CORPUS_ROOT)


def test_real_corpus_loads_with_expected_shape():
    corpus = _load_real()
    assert len(corpus.docs) == 24
    assert corpus.manifest_version == "marker.pr80b_corpus.v1"
    assert len(corpus.slice_counts) == 28
    assert corpus.provenance


def test_fingerprint_is_stable_across_loads():
    assert _load_real().fingerprint == _load_real().fingerprint


def test_multi_part_documents():
    corpus = _load_real()
    agree = corpus.doc("inv-020-two-witness-agree")
    conflict = corpus.doc("inv-021-two-witness-conflict")
    assert agree.multi_record and len(agree.part_texts) == 2
    assert conflict.multi_record and len(conflict.part_texts) == 2
    assert "155.00" in agree.full_text
    assert "159.97" in conflict.full_text
    single = corpus.doc("inv-001-straightforward")
    assert not single.multi_record


def test_every_doc_gold_id_and_slices_match():
    corpus = _load_real()
    for doc in corpus.docs:
        assert doc.gold["doc_id"] == doc.doc_id
        assert list(doc.gold["slices"]) == list(doc.slices)


def _write_minimal_corpus(tmp_path: Path, gold_mutator=None, manifest_mutator=None) -> Path:
    """One-doc corpus factory; mutators inject inconsistencies."""
    doc_text = (
        "Invoice Number: INV-1\n"
        "Invoice Date: 2026-01-01\n"
        "Currency: USD\n"
        "Total Due: 30.00\n"
        "LINEITEM | SKU-A | Thing | 2 | 10.00 | 20.00\n"
        "LINEITEM | SKU-B | Other | 1 | 10.00 | 10.00\n"
    )
    gold = {
        "doc_id": "doc-1",
        "slices": ["baseline.happy"],
        "fields": {
            "invoice_number": {"status": "present", "value": "INV-1"},
            "invoice_date": {"status": "present", "value": "2026-01-01"},
            "currency": {"status": "present", "value": "USD"},
            "po_number": {"status": "absent"},
            "total_due": {"status": "present", "value": "30.00"},
        },
        "items": {
            "status": "rows",
            "rows": [
                {
                    "sku": "SKU-A",
                    "fields": {
                        "description": {"status": "present", "value": "Thing"},
                        "quantity": {"status": "present", "value": 2},
                        "unit_price": {"status": "present", "value": "10.00"},
                        "amount": {"status": "present", "value": "20.00"},
                    },
                },
                {
                    "sku": "SKU-B",
                    "fields": {
                        "description": {"status": "present", "value": "Other"},
                        "quantity": {"status": "present", "value": 1},
                        "unit_price": {"status": "present", "value": "10.00"},
                        "amount": {"status": "present", "value": "10.00"},
                    },
                },
            ],
        },
        "invariant": {"expected": "satisfied"},
        "notes": "",
    }
    if gold_mutator is not None:
        gold_mutator(gold)
    manifest = {
        "manifest_version": "marker.pr80b_corpus.v1",
        "generated": "test",
        "provenance": "test",
        "task": {},
        "documents": [
            {
                "doc_id": "doc-1",
                "parts": ["corpus/doc-1.txt"],
                "gold": "gold/doc-1.json",
                "slices": ["baseline.happy"],
            }
        ],
        "slice_counts": {"baseline.happy": 1},
    }
    if manifest_mutator is not None:
        manifest_mutator(manifest)
    (tmp_path / "corpus").mkdir()
    (tmp_path / "gold").mkdir()
    (tmp_path / "corpus" / "doc-1.txt").write_text(doc_text, encoding="utf-8")
    (tmp_path / "gold" / "doc-1.json").write_text(
        json.dumps(gold, indent=2), encoding="utf-8"
    )
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_minimal_corpus_loads(tmp_path):
    load_corpus(_write_minimal_corpus(tmp_path))


def test_gold_doc_id_mismatch_rejected(tmp_path):
    root = _write_minimal_corpus(
        tmp_path, gold_mutator=lambda gold: gold.update(doc_id="wrong")
    )
    with pytest.raises(CorpusError, match="doc_id mismatch"):
        load_corpus(root)


def test_declared_satisfied_but_sum_wrong_rejected(tmp_path):
    def mutate(gold):
        gold["fields"]["total_due"]["value"] = "99.00"

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="!= total"):
        load_corpus(root)


def test_declared_violated_but_sum_right_rejected(tmp_path):
    def mutate(gold):
        gold["invariant"]["expected"] = "violated"

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="== total"):
        load_corpus(root)


def test_declared_satisfied_but_row_missing_rejected(tmp_path):
    def mutate(gold):
        gold["items"]["rows"][1]["fields"]["amount"] = {"status": "absent"}

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="not evaluable"):
        load_corpus(root)


def test_conflicting_total_must_be_not_evaluable(tmp_path):
    def mutate(gold):
        gold["fields"]["total_due"] = {
            "status": "conflicting",
            "values": ["30.00", "31.00"],
        }
        gold["invariant"]["expected"] = "satisfied"

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="not evaluable"):
        load_corpus(root)


def test_duplicate_gold_sku_rejected(tmp_path):
    def mutate(gold):
        gold["items"]["rows"][1]["sku"] = "SKU-A"

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="duplicate gold sku"):
        load_corpus(root)


def test_missing_scalar_field_rejected(tmp_path):
    def mutate(gold):
        del gold["fields"]["po_number"]

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="gold scalar fields"):
        load_corpus(root)


def test_bad_field_status_rejected(tmp_path):
    def mutate(gold):
        gold["fields"]["currency"]["status"] = "maybe"

    root = _write_minimal_corpus(tmp_path, gold_mutator=mutate)
    with pytest.raises(CorpusError, match="bad status"):
        load_corpus(root)


def test_missing_part_file_rejected(tmp_path):
    root = _write_minimal_corpus(tmp_path)
    (root / "corpus" / "doc-1.txt").unlink()
    with pytest.raises(CorpusError, match="missing part file"):
        load_corpus(root)


def test_manifest_slice_mismatch_rejected(tmp_path):
    root = _write_minimal_corpus(
        tmp_path, manifest_mutator=lambda m: m["documents"][0]["slices"].append("other")
    )
    with pytest.raises(CorpusError, match="slices"):
        load_corpus(root)


def test_manifest_slice_counts_mismatch_rejected(tmp_path):
    root = _write_minimal_corpus(
        tmp_path,
        manifest_mutator=lambda m: m["slice_counts"].update({"baseline.happy": 5}),
    )
    with pytest.raises(CorpusError, match="slice_counts"):
        load_corpus(root)


def test_unknown_manifest_version_rejected(tmp_path):
    root = _write_minimal_corpus(
        tmp_path,
        manifest_mutator=lambda m: m.update(manifest_version="marker.pr80b_corpus.v2"),
    )
    with pytest.raises(CorpusError, match="unsupported manifest_version"):
        load_corpus(root)


def test_gold_deep_copy_isolation(tmp_path):
    """Mutating one gold never leaks into another load (no shared state)."""
    root = _write_minimal_corpus(tmp_path)
    first = load_corpus(root)
    original = copy.deepcopy(first.doc("doc-1").gold)
    first.doc("doc-1").gold["fields"]["total_due"]["value"] = "tampered"
    second = load_corpus(root)
    assert second.doc("doc-1").gold["fields"]["total_due"]["value"] == original["fields"]["total_due"]["value"]
