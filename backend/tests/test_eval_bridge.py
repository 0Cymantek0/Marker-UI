"""Specialist-bridge benchmark harness tests (translation + one real doc).

The translation layer adapts committed PR80B recordings to the
production output contract; it must be deterministic, lossless for
string values, and fail closed on junk. The lane test runs the real
authorities over the known inv-013 fabrication case offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.bridge.translate import (
    build_corpus_lookup,
    extract_prompt_document,
    translate_recorded_content,
)
from app.eval.pr80b.corpus import load_corpus
from app.eval.pr80b.llm import cache_key

pytestmark = pytest.mark.asyncio

CORPUS_ROOT = Path(__file__).resolve().parents[1] / "eval_data" / "pr80b"
CACHE_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "reference"
    / "measurements"
    / "pr80b-llm-cache.json"
)


class TestTranslateRecordedContent:
    def test_flat_invoice_maps_to_output_contract(self):
        raw = json.dumps(
            {
                "invoice_number": "INV-1",
                "invoice_date": "2026-03-15",
                "currency": None,
                "po_number": "PO-9",
                "total_due": "2.045,00",
                "items": [
                    {
                        "sku": "SKU-1",
                        "description": "Widget",
                        "quantity": "2",
                        "unit_price": None,
                        "amount": "19,98",
                    }
                ],
                "flags": ["total_due_conflict"],
            }
        )
        translated = json.loads(translate_recorded_content(raw))
        assert translated["contract_version"] == "marker.specialist.output.v1"
        assert translated["fields"]["invoice_number"] == "INV-1"
        assert translated["fields"]["currency"] is None
        assert translated["items"][0]["identity"] == {"sku": "SKU-1"}
        assert translated["items"][0]["fields"]["unit_price"] is None
        assert translated["items"][0]["fields"]["amount"] == "19,98"
        assert translated["flags"] == ["total_due_conflict"]

    def test_non_string_values_become_null_not_coerced(self):
        raw = json.dumps({"invoice_number": 42, "items": "junk", "flags": "junk"})
        translated = json.loads(translate_recorded_content(raw))
        assert translated["fields"]["invoice_number"] is None
        assert translated["items"] == []
        assert translated["flags"] == []

    def test_fenced_content_is_handled(self):
        raw = '```json\n{"invoice_number": "INV-2"}\n```'
        translated = json.loads(translate_recorded_content(raw))
        assert translated["fields"]["invoice_number"] == "INV-2"

    def test_non_json_fails_closed(self):
        assert translate_recorded_content("not json") is None
        assert translate_recorded_content("[1, 2]") is None


class TestCorpusLookup:
    def _corpus_and_cache(self):
        corpus = load_corpus(CORPUS_ROOT)
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return corpus, cache

    def test_subset_prompt_matches_its_document(self):
        corpus, cache = self._corpus_and_cache()
        model = cache["model_chain"][0]
        doc = corpus.docs[0]
        # the query serves only anchor-matching lines: simulate that
        served = [
            line
            for line in doc.full_text.splitlines()
            if line.startswith(("Invoice", "Currency", "PO", "Total", "LINEITEM"))
        ]
        user_text = (
            "[extraction-context fingerprint=abc]\n"
            "<document>\n" + "\n".join(served) + "\n</document>"
        )
        lookup = build_corpus_lookup(corpus, cache["responses"], model=model)
        content = lookup(model, user_text)
        assert content is not None
        translated = json.loads(content)
        assert translated["contract_version"] == "marker.specialist.output.v1"
        envelope = cache["responses"][cache_key(model, doc.full_text)]
        recorded = json.loads(envelope["content_raw"])
        assert (
            translated["fields"]["invoice_number"]
            == recorded["invoice_number"]
        )

    def test_unknown_prompt_is_miss_never_guessed(self):
        corpus, cache = self._corpus_and_cache()
        model = cache["model_chain"][0]
        lookup = build_corpus_lookup(corpus, cache["responses"], model=model)
        user_text = "<document>\nNo Such Line Anywhere\n</document>"
        assert lookup(model, user_text) is None

    def test_wrong_model_key_is_miss(self):
        corpus, cache = self._corpus_and_cache()
        doc = corpus.docs[0]
        lookup = build_corpus_lookup(corpus, cache["responses"], model="other-model")
        user_text = f"<document>\n{doc.full_text}\n</document>"
        assert lookup("other-model", user_text) is None

    def test_extract_prompt_document_round_trip(self):
        body = "line one\nline two"
        user = f"[extraction-context fingerprint=x]\n<document>\n{body}\n</document>"
        assert extract_prompt_document(user) == body


class TestBridgeLaneOnKnownFabricationDoc:
    async def test_inv013_stays_fabrication_free_offline(self, tmp_path):
        from app.eval.bridge.runner import run_bridge_lane

        corpus = load_corpus(CORPUS_ROOT)
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        model = cache["model_chain"][0]
        lookup = build_corpus_lookup(corpus, cache["responses"], model=model)
        doc = next(d for d in corpus.docs if d.doc_id == "inv-013-broken-row-short")

        _output, metrics = await run_bridge_lane(
            doc, tmp_path / "first", lookup, model=model
        )
        assert metrics["lane_status"] == "ok"
        assert metrics["false_authority_events"] == []
        # the derived 29.99 unit_price never reached authority: the
        # broken row exists only as a proposal-only review row.
        assert "items[sku=SKU-7002].unit_price" not in metrics["accepted_fields"]
        assert metrics["rows"]["proposal_only"] == 1
        assert metrics["rows"]["accepted"] == 2

        # deterministic rerun over a fresh kernel with the same source
        # state and the same replayed response: identical identity.
        _second, second_metrics = await run_bridge_lane(
            doc, tmp_path / "second", lookup, model=model
        )
        assert second_metrics["result_identity"] == metrics["result_identity"]
