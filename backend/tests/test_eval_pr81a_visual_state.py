"""PR81A visual store, embedders, and partitioned index tests.

Matrix letter X (visual state): render-cache identity and reuse,
admission gating, single-flight generation, honest failures, retention
economics, deterministic fake embeddings, generation identity binding,
bounded search, and the high-assurance physical-partition property.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.eval.pr81a.embeddings import HashEmbedder
from app.eval.pr81a.visual_index import (
    VisualIndex,
    VisualIndexError,
    VisualPageEntry,
    VisualQueryBudget,
    visual_generation_identity,
)
from app.eval.pr81a.visual_store import (
    NotAdmittedError,
    PageRenderStore,
    RenderState,
    VisualRenderError,
    render_key_for,
)


def _entry(doc_id: str, page: int, *, blob: str | None = None, domain: str = "general") -> VisualPageEntry:
    return VisualPageEntry(
        doc_id=doc_id,
        page_number=page,
        page_index=page - 1,
        blob_key=blob or f"sha256:{doc_id}-r1-p{page}",
        revision="v1",
        domain=domain,
        source_ref=f"src.{doc_id}",
    )


class TestRenderStoreIdentity:
    def test_key_binds_revision_page_and_render_state(self):
        base = render_key_for("sha256:aaa", 0, RenderState())
        assert base == render_key_for("sha256:aaa", 0, RenderState())
        assert base != render_key_for("sha256:bbb", 0, RenderState())
        assert base != render_key_for("sha256:aaa", 1, RenderState())
        assert base != render_key_for("sha256:aaa", 0, RenderState(scale=3.0))

    def test_cached_render_reused_not_recomputed(self, tmp_path):
        calls = {"n": 0}

        def renderer(pdf_path: Path, page_index: int, scale: float) -> bytes:
            calls["n"] += 1
            return b"fake-png-" + str(page_index).encode()

        store = PageRenderStore(tmp_path, renderer=renderer)
        first = store.render("sha256:x", 0, Path("whatever.pdf"), admitted=True)
        second = store.render("sha256:x", 0, Path("whatever.pdf"), admitted=True)
        assert first.from_cache is False
        assert second.from_cache is True
        assert calls["n"] == 1
        assert first.byte_length == second.byte_length
        assert store.stats()["cache_hits"] == 1
        assert store.stats()["rendered"] == 1

    def test_revision_change_cannot_reuse_old_render(self, tmp_path):
        store = PageRenderStore(tmp_path, renderer=lambda p, i, s: b"png")
        old = store.render("sha256:old", 0, Path("a.pdf"), admitted=True)
        new = store.render("sha256:new", 0, Path("b.pdf"), admitted=True)
        assert old.render_key != new.render_key
        assert store.stats()["cached_entries"] == 2


class TestRenderStoreAdmission:
    def test_not_admitted_refused_and_counted(self, tmp_path):
        store = PageRenderStore(tmp_path, renderer=lambda p, i, s: b"png")
        with pytest.raises(NotAdmittedError):
            store.render("sha256:no", 0, Path("a.pdf"), admitted=False)
        assert store.stats()["not_admitted"] == 1
        assert store.stats()["cached_entries"] == 0

    def test_peek_generates_nothing(self, tmp_path):
        store = PageRenderStore(tmp_path, renderer=lambda p, i, s: b"png")
        assert store.peek("sha256:ghost", 0) is None
        assert store.stats()["rendered"] == 0


class TestRenderStoreFailures:
    def test_failed_render_leaves_nothing_queryable(self, tmp_path):
        def boom(pdf_path, page_index, scale):
            raise RuntimeError("renderer exploded")

        store = PageRenderStore(tmp_path, renderer=boom)
        with pytest.raises(VisualRenderError, match="renderer exploded"):
            store.render("sha256:fail", 0, Path("a.pdf"), admitted=True)
        assert store.stats()["failures"] == 1
        assert store.peek("sha256:fail", 0) is None
        assert store.stats()["cached_entries"] == 0

    def test_out_of_range_page_is_explicit(self, tmp_path):
        import pypdfium2 as pdfium
        import io as _io

        from reportlab.pdfgen import canvas as _canvas

        buf = _io.BytesIO()
        c = _canvas.Canvas(buf)
        c.drawString(72, 720, "one page")
        c.showPage()
        c.save()
        pdf_path = tmp_path / "one.pdf"
        pdf_path.write_bytes(buf.getvalue())
        store = PageRenderStore(tmp_path / "store")
        with pytest.raises(VisualRenderError, match="out of range"):
            store.render("sha256:one", 5, pdf_path, admitted=True)
        assert store.peek("sha256:one", 5) is None

    def test_retry_after_failure_recovers(self, tmp_path):
        state = {"fail": True}

        def flaky(pdf_path, page_index, scale):
            if state["fail"]:
                raise RuntimeError("transient")
            return b"recovered"

        store = PageRenderStore(tmp_path, renderer=flaky)
        with pytest.raises(VisualRenderError):
            store.render("sha256:flaky", 0, Path("a.pdf"), admitted=True)
        state["fail"] = False
        result = store.render("sha256:flaky", 0, Path("a.pdf"), admitted=True)
        assert result.from_cache is False
        assert result.byte_length == len(b"recovered")


class TestRenderStoreConcurrency:
    def test_single_flight_one_generation(self, tmp_path):
        calls = {"n": 0}
        lock = threading.Lock()

        def slow(pdf_path, page_index, scale):
            with lock:
                calls["n"] += 1
            time.sleep(0.15)
            return b"slow-png"

        store = PageRenderStore(tmp_path, renderer=slow)
        results = []
        errors = []

        def worker():
            try:
                results.append(store.render("sha256:c", 0, Path("a.pdf"), admitted=True))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert calls["n"] == 1
        assert all(r.byte_length == len(b"slow-png") for r in results)


class TestRenderStoreRetention:
    def test_prune_reclaims_orphaned_revision(self, tmp_path):
        store = PageRenderStore(tmp_path, renderer=lambda p, i, s: b"png-old-revision")
        old = store.render("sha256:rev1", 0, Path("a.pdf"), admitted=True)
        live = store.render("sha256:rev2", 0, Path("b.pdf"), admitted=True)
        report = store.prune({live.render_key})
        assert report["removed_entries"] == 1
        assert report["reclaimed_bytes"] == old.byte_length
        assert store.peek("sha256:rev1", 0) is None
        assert store.peek("sha256:rev2", 0) is not None

    def test_real_render_bytes_deterministic(self, tmp_path):
        import io as _io

        import pypdfium2 as pdfium
        from reportlab.pdfgen import canvas as _canvas

        buf = _io.BytesIO()
        c = _canvas.Canvas(buf)
        c.drawString(72, 720, "determinism target")
        c.showPage()
        c.save()
        pdf_path = tmp_path / "d.pdf"
        pdf_path.write_bytes(buf.getvalue())
        store_a = PageRenderStore(tmp_path / "a")
        store_b = PageRenderStore(tmp_path / "b")
        a = store_a.render("sha256:d", 0, pdf_path, admitted=True)
        b = store_b.render("sha256:d", 0, pdf_path, admitted=True)
        assert a.render_key == b.render_key
        assert a.path.read_bytes() == b.path.read_bytes()
        assert a.byte_length > 1000  # a real PNG, not a stub


class TestHashEmbedder:
    def test_deterministic_and_unit_norm(self):
        embedder = HashEmbedder(dim=32)
        v1 = embedder.embed_image(b"png")
        v2 = embedder.embed_image(b"png")
        assert np.array_equal(v1, v2)
        assert abs(float(np.linalg.norm(v1)) - 1.0) < 1e-5
        assert embedder.embed_image(b"png") @ embedder.embed_text("png") < 0.999

    def test_distinct_inputs_decorrelate(self):
        embedder = HashEmbedder(dim=64)
        a = embedder.embed_image(b"page-one")
        b = embedder.embed_image(b"page-two")
        assert abs(float(a @ b)) < 0.5


class TestVisualGenerationIdentity:
    def test_identity_binds_member_set(self):
        entries = [_entry("doc-a", 1), _entry("doc-a", 2)]
        same = visual_generation_identity(
            workspace_id="ws", embedder_identity="hash:64", entries=entries
        )
        assert same == visual_generation_identity(
            workspace_id="ws", embedder_identity="hash:64", entries=entries
        )
        changed = visual_generation_identity(
            workspace_id="ws", embedder_identity="hash:64", entries=entries[:1]
        )
        assert same != changed

    def test_partition_splits_identity(self):
        entries = [_entry("doc-a", 1)]
        shared = visual_generation_identity(
            workspace_id="ws", embedder_identity="hash:64", entries=entries
        )
        partitioned = visual_generation_identity(
            workspace_id="ws", embedder_identity="hash:64", entries=entries, partition_key="abc"
        )
        assert shared != partitioned

    def test_embedder_change_splits_identity(self):
        entries = [_entry("doc-a", 1)]
        assert (
            visual_generation_identity(workspace_id="ws", embedder_identity="a", entries=entries)
            != visual_generation_identity(
                workspace_id="ws", embedder_identity="b", entries=entries
            )
        )


class TestVisualIndex:
    def _index(self, embedder: HashEmbedder) -> VisualIndex:
        pages = [
            (_entry("doc-a", 1), b"png-a1"),
            (_entry("doc-a", 2), b"png-a2"),
            (_entry("doc-b", 1, domain="restricted"), b"png-b1"),
        ]
        return VisualIndex.build(workspace_id="ws", embedder=embedder, pages=pages)

    def test_build_orders_and_shapes(self):
        index = self._index(HashEmbedder())
        assert [e.doc_id for e in index.entries] == ["doc-a", "doc-a", "doc-b"]
        assert index.matrix.shape == (3, HashEmbedder().dim)

    def test_search_ranked_and_bounded(self):
        embedder = HashEmbedder()
        index = self._index(embedder)
        result = index.search(embedder.embed_text("png-a1"), budget=VisualQueryBudget(top_k=2))
        assert result.hits[0].doc_id == "doc-a" and result.hits[0].page_number == 1
        assert len(result.hits) == 2
        assert result.hits[0].rank == 1
        assert result.pages_scored == 3

    def test_candidate_filter_excludes_before_competition(self):
        embedder = HashEmbedder()
        index = self._index(embedder)
        result = index.search(
            embedder.embed_text("png-b1"),
            candidate_filter=lambda entry: entry.domain != "restricted",
        )
        assert all(h.doc_id != "doc-b" for h in result.hits)
        assert result.pages_scored == 2  # forbidden page never scored

    def test_high_assurance_partition_is_physical(self):
        embedder = HashEmbedder()
        pages = [
            (_entry("doc-a", 1), b"png-a1"),
            (_entry("doc-b", 1, domain="restricted"), b"png-b1"),
        ]
        shared = VisualIndex.build(workspace_id="ws", embedder=embedder, pages=pages)
        partition = VisualIndex.build_high_assurance(
            workspace_id="ws", embedder=embedder, pages=pages, allowed_domains=["general"]
        )
        assert shared.generation_id != partition.generation_id
        assert len(partition.entries) == 1
        assert partition.matrix.shape[0] == 1
        # a query for the forbidden page cannot surface it: the vector is
        # not in the array at all
        result = partition.search(embedder.embed_text("png-b1"))
        assert all(h.doc_id != "doc-b" for h in result.hits)

    def test_partition_from_saved_index_matches_scratch_build(self, tmp_path):
        embedder = HashEmbedder()
        pages = [
            (_entry("doc-a", 1), b"png-a1"),
            (_entry("doc-c", 2), b"png-c2"),
            (_entry("doc-b", 1, domain="restricted"), b"png-b1"),
        ]
        shared = VisualIndex.build(workspace_id="ws", embedder=embedder, pages=pages)
        scratch = VisualIndex.build_high_assurance(
            workspace_id="ws", embedder=embedder, pages=pages, allowed_domains=["general"]
        )
        path = tmp_path / "full.npz"
        shared.save(path)
        replayed = VisualIndex.load(path)
        derived = VisualIndex.partition_from(replayed, ["general"])
        assert derived.generation_id == scratch.generation_id
        assert np.array_equal(derived.matrix, scratch.matrix)
        assert [e.doc_id for e in derived.entries] == [e.doc_id for e in scratch.entries]

    def test_forbidden_cannot_influence_rank_in_partition(self):
        embedder = HashEmbedder()
        allowed_pages = [(_entry("doc-a", 1), b"png-a1"), (_entry("doc-c", 1), b"png-c1")]
        base = VisualIndex.build(workspace_id="ws", embedder=embedder, pages=allowed_pages)
        with_forbidden = VisualIndex.build_high_assurance(
            workspace_id="ws",
            embedder=embedder,
            pages=allowed_pages + [(_entry("doc-b", 1, domain="restricted"), b"png-b1")],
            allowed_domains=["general"],
        )
        q = embedder.embed_text("png-a1")
        base_hits = base.search(q, budget=VisualQueryBudget(top_k=2)).hits
        part_hits = with_forbidden.search(q, budget=VisualQueryBudget(top_k=2)).hits
        assert [(h.doc_id, h.page_number, h.score) for h in base_hits] == [
            (h.doc_id, h.page_number, h.score) for h in part_hits
        ]

    def test_budget_validation(self):
        with pytest.raises(VisualIndexError):
            VisualQueryBudget(top_k=0)
        with pytest.raises(VisualIndexError):
            VisualQueryBudget(max_pages_scored=99999)

    def test_max_pages_scored_caps_work(self):
        embedder = HashEmbedder(dim=8)
        pages = [(_entry(f"doc-{i:02d}", 1), f"png-{i}".encode()) for i in range(20)]
        index = VisualIndex.build(workspace_id="ws", embedder=embedder, pages=pages)
        result = index.search(
            embedder.embed_text("png-1"), budget=VisualQueryBudget(top_k=5, max_pages_scored=3)
        )
        assert result.pages_scored == 3
        assert len(result.hits) <= 3

    def test_empty_index_explicit_absence(self):
        index = VisualIndex.build(workspace_id="ws", embedder=HashEmbedder(), pages=[])
        result = index.search(np.zeros(8, dtype=np.float32))
        assert result.hits == ()
        assert result.pages_scored == 0

    def test_duplicate_page_rejected(self):
        embedder = HashEmbedder()
        pages = [(_entry("doc-a", 1), b"p"), (_entry("doc-a", 1), b"p2")]
        with pytest.raises(VisualIndexError, match="duplicate page entry"):
            VisualIndex.build(workspace_id="ws", embedder=embedder, pages=pages)

    def test_save_load_roundtrip_preserves_identity(self, tmp_path):
        embedder = HashEmbedder()
        index = self._index(embedder)
        path = tmp_path / "vi.npz"
        index.save(path)
        loaded = VisualIndex.load(path)
        assert loaded.generation_id == index.generation_id
        assert loaded.matrix.shape == index.matrix.shape
        assert np.array_equal(loaded.matrix, index.matrix)
        assert [e.doc_id for e in loaded.entries] == [e.doc_id for e in index.entries]

    def test_load_rejects_wrong_schema(self, tmp_path):
        import json

        path = tmp_path / "bad.npz"
        np.savez(
            path,
            matrix=np.zeros((1, 4), dtype=np.float32),
            meta=np.frombuffer(
                json.dumps({"schema": "bogus.v0"}).encode("utf-8"), dtype=np.uint8
            ),
        )
        with pytest.raises(VisualIndexError, match="unsupported visual index schema"):
            VisualIndex.load(path)

    def test_load_rejects_tampered_identity(self, tmp_path):
        import json

        embedder = HashEmbedder()
        index = self._index(embedder)
        path = tmp_path / "t.npz"
        index.save(path)
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(data["meta"].tobytes().decode("utf-8"))
            matrix = data["matrix"]
        meta["generation_id"] = "sha256:" + "0" * 64
        np.savez(
            path,
            matrix=matrix,
            meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8),
        )
        with pytest.raises(VisualIndexError, match="generation identity mismatch"):
            VisualIndex.load(path)
