"""PR81A corpus loader and generator tests.

Matrix letter W (corpus): fail-closed loading, fingerprint stability,
revision chains, gold/oracle consistency, and byte-level generator
determinism for the committed corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.pr81a.corpus import CorpusError, load_corpus
from app.eval.pr81a.corpus_gen import GENERATOR_VERSION, QUERIES, build_all
from app.eval.pr81a.normalize import NormalizeError, normalize_answer

BACKEND = Path(__file__).resolve().parent.parent
CORPUS_ROOT = BACKEND / "eval_data" / "pr81a"


@pytest.fixture(scope="module")
def corpus() -> object:
    return load_corpus(CORPUS_ROOT)


class TestRealCorpus:
    def test_manifest_shape_and_counts(self, corpus):
        assert corpus.manifest_version == "marker.pr81a_corpus.v1"
        assert corpus.generator_version == GENERATOR_VERSION
        assert len(corpus.docs) == 15
        assert len(corpus.queries) == 35
        assert sum(doc.current.page_count for doc in corpus.docs) == 27

    def test_fingerprint_stable_across_loads(self, corpus):
        again = load_corpus(CORPUS_ROOT)
        assert again.fingerprint == corpus.fingerprint

    def test_slice_counts(self, corpus):
        assert dict(corpus.slice_counts) == {
            "text.easy_control": 6,
            "chart.appearance": 5,
            "chart.value_read": 3,
            "table.cell_grid": 4,
            "form.label_placement": 3,
            "layout.column_bind": 3,
            "near_duplicate.decoy": 4,
            "revision.change": 4,
            "authz.revocation": 3,
        }

    def test_revision_chain(self, corpus):
        doc = corpus.doc("doc-rev-01")
        assert [r.revision for r in doc.revisions] == ["v3", "v4"]
        v3 = doc.revision("v3")
        assert v3.superseded_by == "v4"
        assert doc.current.revision == "v4"
        assert v3.pdf_sha256 != doc.current.pdf_sha256
        assert doc.blob_key("v3") != doc.blob_key()

    def test_selective_admission_declared(self, corpus):
        not_admitted = sorted(d.doc_id for d in corpus.docs if not d.visual_admitted)
        assert not_admitted == ["doc-hr-01", "doc-leg-01"]

    def test_restricted_domain_documents(self, corpus):
        restricted = sorted(d.doc_id for d in corpus.docs if d.domain == "restricted")
        assert restricted == ["doc-sec-01", "doc-sec-02"]

    def test_every_answer_query_gold_value_exists_on_gold_page(self, corpus):
        for query in corpus.queries:
            if query.expectation != "answer":
                continue
            doc = corpus.doc(query.doc_id)
            revision = (
                doc.revision("v3")
                if query.phase in ("pre_revision", "pinned_pre_revision")
                else doc.current
            )
            joined = " | ".join(revision.page(query.page_number)).lower()
            assert query.answer.lower() in joined, (
                f"{query.query_id}: gold {query.answer!r} missing from "
                f"{query.doc_id} p{query.page_number}"
            )

    def test_no_delivery_and_phases_declared(self, corpus):
        no_delivery = [q.query_id for q in corpus.queries if q.expectation == "no_delivery"]
        assert no_delivery == ["q34"]
        phases = {q.phase for q in corpus.queries}
        assert phases == {
            "baseline",
            "pre_revision",
            "post_revision",
            "pinned_pre_revision",
        }

    def test_oracle_nodes_nonempty_per_page(self, corpus):
        for doc in corpus.docs:
            for revision in doc.revisions:
                for _number, nodes in revision.pages:
                    assert nodes, f"{doc.doc_id}/{revision.revision} empty page"


class TestGeneratorDeterminism:
    def test_build_all_is_byte_identical(self):
        first = build_all()
        second = build_all()
        assert sorted(first) == sorted(second)
        for doc_key, artifact in first.items():
            assert artifact.pdf_bytes == second[doc_key].pdf_bytes, doc_key

    def test_queries_reference_realized_answers(self):
        artifacts = build_all()
        assert len(QUERIES) == 35
        assert len(artifacts) == 16  # 15 docs + doc-rev-01 carries two revisions
        # every gold answer is drawn from a corpus_gen constant: spot-check
        # that the drawn pages actually contain the numeric gold values.
        by_key = {key: art for key, art in artifacts.items()}
        fin01_p2 = " | ".join(by_key["doc-fin-01"].pages[1].nodes)
        assert "4.0" in fin01_p2
        fin03_p2 = " | ".join(by_key["doc-fin-03"].pages[1].nodes)
        assert "3.8" in fin03_p2


class TestNormalize:
    def test_string_kind(self):
        assert normalize_answer("  Omar  Haddad. ", "string") == "omar haddad"

    def test_decimal_kinds(self):
        assert normalize_answer("18.5", "decimal") == "18.5"
        assert normalize_answer("$2,450 USD", "decimal") == "2450"
        assert normalize_answer("4.0", "decimal") == "4"
        assert normalize_answer("2.4500", "decimal") == "2.45"

    def test_count_kind(self):
        assert normalize_answer("47", "count") == "47"
        assert normalize_answer(" 3 ", "count") == "3"

    def test_percent_kind(self):
        assert normalize_answer("87.4%", "percent") == "87.4"

    def test_date_kind(self):
        assert normalize_answer("2026-08-02", "date") == "2026-08-02"
        with pytest.raises(NormalizeError):
            normalize_answer("Jun-24", "date")

    def test_money_million_kind(self):
        assert normalize_answer("2.4M", "money_million") == "2.4"
        assert normalize_answer("2.4M USD", "money_million") == "2.4"

    def test_failures(self):
        with pytest.raises(NormalizeError):
            normalize_answer("abc", "decimal")
        with pytest.raises(NormalizeError):
            normalize_answer("2.5", "count")
        with pytest.raises(NormalizeError):
            normalize_answer("", "string")
        with pytest.raises(NormalizeError):
            normalize_answer("5", "bogus")


def _write_minimal_corpus(tmp_path: Path, *, manifest_mutator=None, query_mutator=None):
    """One-doc corpus fixture with injectable inconsistencies."""
    doc_dir = tmp_path / "corpus"
    gold_dir = tmp_path / "oracle"
    doc_dir.mkdir()
    gold_dir.mkdir()
    (doc_dir / "doc-a.pdf").write_bytes(b"%PDF-fake-a")
    (gold_dir / "doc-a.json").write_text(
        json.dumps(
            {
                "doc_id": "doc-a",
                "revision": "v1",
                "pages": [{"page_number": 1, "nodes": ["alpha beta", "gamma"]}],
            }
        ),
        encoding="utf-8",
    )
    queries = [
        {
            "query_id": "q01",
            "text": "where is alpha?",
            "slice_tag": "text.easy_control",
            "doc_id": "doc-a",
            "page_number": 1,
            "answer": "gamma",
            "answer_kind": "string",
        }
    ]
    if query_mutator:
        query_mutator(queries)
    (tmp_path / "queries.json").write_text(json.dumps(queries), encoding="utf-8")
    manifest = {
        "manifest_version": "marker.pr81a_corpus.v1",
        "provenance": "synthetic test corpus",
        "generator_version": GENERATOR_VERSION,
        "documents": [
            {
                "doc_id": "doc-a",
                "domain": "general",
                "slices": ["text.plain"],
                "visual_admitted": False,
                "revisions": [
                    {
                        "revision": "v1",
                        "pdf": "corpus/doc-a.pdf",
                        "oracle": "oracle/doc-a.json",
                    }
                ],
            }
        ],
        "slice_counts": {"text.easy_control": 1},
    }
    if manifest_mutator:
        manifest_mutator(manifest)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _expect_error(tmp_path, match):
    with pytest.raises(CorpusError, match=match):
        load_corpus(tmp_path)


class TestFailClosed:
    def test_minimal_corpus_loads(self, tmp_path):
        corpus = load_corpus(_write_minimal_corpus(tmp_path))
        assert len(corpus.docs) == 1
        assert len(corpus.queries) == 1

    def test_bad_manifest_version(self, tmp_path):
        root = _write_minimal_corpus(
            tmp_path, manifest_mutator=lambda m: m.update({"manifest_version": "v0"})
        )
        _expect_error(root, "unsupported manifest_version")

    def test_missing_provenance(self, tmp_path):
        root = _write_minimal_corpus(
            tmp_path, manifest_mutator=lambda m: m.pop("provenance")
        )
        _expect_error(root, "missing provenance")

    def test_missing_pdf(self, tmp_path):
        def mutate(m):
            m["documents"][0]["revisions"][0]["pdf"] = "corpus/gone.pdf"

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "missing pdf file")

    def test_missing_oracle(self, tmp_path):
        def mutate(m):
            m["documents"][0]["revisions"][0]["oracle"] = "oracle/gone.json"

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "missing oracle file")

    def test_oracle_doc_id_mismatch(self, tmp_path):
        root = _write_minimal_corpus(tmp_path)
        oracle = json.loads((root / "oracle" / "doc-a.json").read_text())
        oracle["doc_id"] = "doc-b"
        (root / "oracle" / "doc-a.json").write_text(json.dumps(oracle))
        _expect_error(root, "oracle doc_id mismatch")

    def test_oracle_page_gap(self, tmp_path):
        root = _write_minimal_corpus(tmp_path)
        oracle = json.loads((root / "oracle" / "doc-a.json").read_text())
        oracle["pages"].append({"page_number": 3, "nodes": ["x"]})
        (root / "oracle" / "doc-a.json").write_text(json.dumps(oracle))
        _expect_error(root, "page numbers must be 1..N")

    def test_bad_doc_slice(self, tmp_path):
        def mutate(m):
            m["documents"][0]["slices"] = ["chart.hologram"]

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "bad slices")

    def test_duplicate_doc_id(self, tmp_path):
        def mutate(m):
            m["documents"].append(dict(m["documents"][0]))

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "duplicate doc_id")

    def test_two_live_revisions_rejected(self, tmp_path):
        def mutate(m):
            (tmp_path / "oracle" / "doc-a-v2.json").write_text(
                json.dumps(
                    {
                        "doc_id": "doc-a",
                        "revision": "v2",
                        "pages": [{"page_number": 1, "nodes": ["alpha two"]}],
                    }
                ),
                encoding="utf-8",
            )
            m["documents"][0]["revisions"].append(
                {
                    "revision": "v2",
                    "pdf": "corpus/doc-a.pdf",
                    "oracle": "oracle/doc-a-v2.json",
                }
            )

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "exactly one revision must be live")

    def test_superseded_by_target_missing(self, tmp_path):
        def mutate(m):
            (tmp_path / "oracle" / "doc-a-v2.json").write_text(
                json.dumps(
                    {
                        "doc_id": "doc-a",
                        "revision": "v2",
                        "pages": [{"page_number": 1, "nodes": ["alpha two"]}],
                    }
                ),
                encoding="utf-8",
            )
            m["documents"][0]["revisions"][0]["superseded_by"] = "v9"
            m["documents"][0]["revisions"].append(
                {
                    "revision": "v2",
                    "pdf": "corpus/doc-a.pdf",
                    "oracle": "oracle/doc-a-v2.json",
                }
            )

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "superseded_by target missing")

    def test_duplicate_query_id(self, tmp_path):
        root = _write_minimal_corpus(
            tmp_path, query_mutator=lambda qs: qs.append(dict(qs[0]))
        )
        _expect_error(root, "duplicate query_id")

    def test_unknown_query_slice(self, tmp_path):
        def mutate(qs):
            qs[0]["slice_tag"] = "smell.appearance"

        root = _write_minimal_corpus(tmp_path, query_mutator=mutate)
        _expect_error(root, "unknown query slice")

    def test_page_number_out_of_range(self, tmp_path):
        def mutate(qs):
            qs[0]["page_number"] = 9

        root = _write_minimal_corpus(tmp_path, query_mutator=mutate)
        _expect_error(root, "outside current revision")

    def test_no_delivery_requires_denied_and_empty_answer(self, tmp_path):
        def mutate(qs):
            qs[0]["expectation"] = "no_delivery"
            qs[0]["answer"] = "still here"

        root = _write_minimal_corpus(tmp_path, query_mutator=mutate)
        _expect_error(root, "no_delivery requires denied profile")

    def test_phase_rejected_outside_revision_doc(self, tmp_path):
        def mutate(qs):
            qs[0]["phase"] = "post_revision"

        root = _write_minimal_corpus(tmp_path, query_mutator=mutate)
        _expect_error(root, "phases are only valid")

    def test_gold_answer_must_normalize(self, tmp_path):
        def mutate(qs):
            qs[0]["answer_kind"] = "decimal"

        root = _write_minimal_corpus(tmp_path, query_mutator=mutate)
        _expect_error(root, "gold answer does not normalize")

    def test_slice_counts_mismatch(self, tmp_path):
        def mutate(m):
            m["slice_counts"] = {"text.easy_control": 5}

        root = _write_minimal_corpus(tmp_path, manifest_mutator=mutate)
        _expect_error(root, "slice_counts do not match")
