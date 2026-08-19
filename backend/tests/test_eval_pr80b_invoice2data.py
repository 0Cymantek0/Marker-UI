"""invoice2data specialist adapter tests (matrix U) - real library, offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.eval.pr80b.corpus import load_corpus
from app.eval.pr80b.invoice2data_adapter import Invoice2DataAdapter

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "eval_data" / "pr80b"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS_ROOT)


@pytest.fixture(scope="module")
def adapter():
    return Invoice2DataAdapter()


def _extract(adapter, corpus, doc_id, tmp_path):
    return adapter.extract(doc_id, corpus.doc(doc_id).full_text, tmp_path)


def test_canonical_document_extracts_all_scalars_and_rows(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-001-straightforward", tmp_path)
    assert out.error is None
    assert out.fields["invoice_number"].value == "INV-2026-001"
    assert out.fields["invoice_date"].value == "2026-03-01"
    assert out.fields["currency"].value == "USD"
    assert out.fields["po_number"].value == "PO-4481"
    assert out.fields["total_due"].value == "155.00"
    assert [r.sku for r in out.rows] == ["SKU-1001", "SKU-1002", "SKU-1003"]
    assert out.rows[0].fields["quantity"].value == "4"
    assert out.rows[0].fields["amount"].value == "50.00"


def test_label_variants_document_fails_whole_template(adapter, corpus, tmp_path):
    """No canonical labels: invoice2data's required fields fail closed."""
    out = _extract(adapter, corpus, "inv-018-label-variants", tmp_path)
    assert out.error is not None
    assert "required fields" in out.error or "no template matched" in out.error


def test_missing_required_scalar_fails_whole_document(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-003-missing-required-date", tmp_path)
    assert out.error is not None


def test_broken_short_row_is_dropped(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-013-broken-row-short", tmp_path)
    assert out.error is None
    assert [r.sku for r in out.rows] == ["SKU-7001", "SKU-7003"]


def test_broken_long_row_is_dropped(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-014-broken-row-long", tmp_path)
    assert out.error is None
    assert [r.sku for r in out.rows] == ["SKU-7201", "SKU-7203"]


def test_duplicate_identical_row_is_not_collapsed(adapter, corpus, tmp_path):
    """The library has no witness semantics: duplicates pass through."""
    out = _extract(adapter, corpus, "inv-011-duplicate-sku-identical", tmp_path)
    assert [r.sku for r in out.rows] == ["SKU-5001", "SKU-5001", "SKU-5002"]


def test_decoy_total_is_first_regex_match(adapter, corpus, tmp_path):
    """'Estimated Total Due: 999.99' substring-matches before the real total."""
    out = _extract(adapter, corpus, "inv-019-noise-heavy", tmp_path)
    assert out.error is None
    assert out.fields["total_due"].value == "999.99"


def test_multi_record_document_emits_rows_twice(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-020-two-witness-agree", tmp_path)
    assert [r.sku for r in out.rows] == ["SKU-3100", "SKU-3101", "SKU-3100", "SKU-3101"]


def test_multipage_document_extracts_all_rows(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-024-multipage", tmp_path)
    assert len(out.rows) == 12


def test_fullwidth_colon_labels_supported(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-023-fullwidth-punctuation", tmp_path)
    assert out.fields["invoice_number"].value == "INV-99023"
    assert out.fields["total_due"].value == "210.00"


def test_no_evidence_or_conflict_semantics_claimed(adapter, corpus, tmp_path):
    out = _extract(adapter, corpus, "inv-001-straightforward", tmp_path)
    assert all(not field.has_evidence for field in out.fields.values())
    assert all(not field.self_flagged for field in out.fields.values())
    assert out.invariant_findings is None


def test_adapter_is_deterministic(adapter, corpus, tmp_path):
    first = _extract(adapter, corpus, "inv-010-many-rows", tmp_path / "a")
    second = _extract(adapter, corpus, "inv-010-many-rows", tmp_path / "b")
    assert first.fields == second.fields
    assert [r.sku for r in first.rows] == [r.sku for r in second.rows]
