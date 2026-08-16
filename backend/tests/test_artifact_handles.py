"""ArtifactHandle data-plane contract tests (PR68A).

Covers the observable contract of the local handle seam: exact-byte round
trips across every encoding, hostile/malformed metadata rejection,
tamper/truncation/missing fail-closed behavior, lifecycle (consume,
sweep, duplicate delivery), worker payload staging with graceful inline
fallback, and parent-side strict resolution.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any

import pytest

from app.services import artifact_handles as ah
from app.services.artifact_handles import (
    ArtifactCorruptError,
    ArtifactHandleStore,
    ArtifactHandleValidationError,
    ArtifactMissingError,
    HandleRef,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_store(tmp_path: Path, **kwargs: Any) -> ArtifactHandleStore:
    return ArtifactHandleStore(tmp_path / "handles", **kwargs)


def stage_bytes(store: ArtifactHandleStore, data: bytes, *, job_id: str = "job-1") -> HandleRef:
    return store.stage(
        data, slot=("result", "text"), kind="text", encoding="raw", job_id=job_id
    )


def big_text(n: int, seed: str = "x") -> str:
    return (seed * (n // len(seed) + 1))[:n]


def pil_image(size: int = 64):
    from PIL import Image

    return Image.new("RGB", (size, size), color=(120, 40, 200))


def sample_result(*, text: str = "small text", image_bytes: bytes = b"\x89PNG small", with_pil: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text": text,
        "extension": "md",
        "images": {"p1.png": image_bytes},
        "metadata": {"page_count": 3},
        "assets": [
            {"name": "sheets/Sheet1.csv", "media_type": "text/csv", "data": b"a,b\n1,2\n", "pil": None}
        ],
    }
    if with_pil:
        result["images"]["p2.png"] = pil_image()
    return result


# ---------------------------------------------------------------------------
# store contract: identity, round trip, verification
# ---------------------------------------------------------------------------


class TestStoreContract:
    def test_round_trip_empty_tiny_large_raw(self, tmp_path):
        store = make_store(tmp_path)
        for data in (b"", b"\x00", os.urandom(1 << 20)):
            ref = stage_bytes(store, data)
            assert store.resolve(ref) == data

    def test_name_is_uuid_and_unique_per_stage(self, tmp_path):
        store = make_store(tmp_path)
        ref1 = stage_bytes(store, b"same bytes")
        ref2 = stage_bytes(store, b"same bytes")
        assert ref1.name != ref2.name  # no dedup: consumption can never race another job
        assert ref1.sha256 == ref2.sha256

    def test_stage_rejects_non_bytes(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ArtifactHandleValidationError):
            store.stage("not bytes", slot=("result", "text"), kind="text", encoding="raw", job_id="j")

    def test_resolve_missing_blob_fails_closed(self, tmp_path):
        store = make_store(tmp_path)
        ref = stage_bytes(store, b"data")
        (store._blobs_dir / f"{ref.name}.bin").unlink()
        with pytest.raises(ArtifactMissingError):
            store.resolve(ref)
        assert store.missing_rejects == 1

    def test_resolve_truncated_blob_detected(self, tmp_path):
        store = make_store(tmp_path)
        ref = stage_bytes(store, os.urandom(4096))
        path = store._blobs_dir / f"{ref.name}.bin"
        path.write_bytes(path.read_bytes()[:100])
        with pytest.raises(ArtifactCorruptError):
            store.resolve(ref)
        assert store.corrupt_rejects == 1

    def test_resolve_tampered_same_length_detected(self, tmp_path):
        store = make_store(tmp_path)
        data = os.urandom(4096)
        ref = stage_bytes(store, data)
        path = store._blobs_dir / f"{ref.name}.bin"
        tampered = bytearray(data)
        tampered[0] ^= 0xFF
        path.write_bytes(bytes(tampered))
        with pytest.raises(ArtifactCorruptError):
            store.resolve(ref)

    def test_resolve_length_claim_mismatch_detected(self, tmp_path):
        store = make_store(tmp_path)
        data = os.urandom(2048)
        ref = stage_bytes(store, data)
        lying = HandleRef(
            slot=ref.slot, kind=ref.kind, encoding=ref.encoding, name=ref.name,
            length=ref.length + 1, sha256=ref.sha256, job_id=ref.job_id,
        )
        with pytest.raises(ArtifactCorruptError):
            store.resolve(lying)

    def test_resolve_oversize_claim_rejected_before_read(self, tmp_path):
        store = make_store(tmp_path, max_read_bytes=1024)
        ref = stage_bytes(store, os.urandom(4096))
        with pytest.raises(ArtifactHandleValidationError):
            store.resolve(ref)

    @pytest.mark.parametrize(
        "bad_name",
        ["../escape", "..\\escape", "/abs/path", "sub/dir", "UPPERCASE", "z" * 32, "", "12345", None, 5],
    )
    def test_hostile_names_rejected(self, tmp_path, bad_name):
        store = make_store(tmp_path)
        with pytest.raises(ArtifactHandleValidationError):
            store._path_for(bad_name)
        ref = HandleRef(
            slot=("result", "text"), kind="text", encoding="raw", name="0" * 32,
            length=1, sha256="0" * 64, job_id="j",
        )
        object.__setattr__(ref, "name", bad_name)  # simulate hostile wire claim
        with pytest.raises(ArtifactHandleValidationError):
            store.resolve(ref)

    def test_consume_unlinks_and_second_fails_closed(self, tmp_path):
        store = make_store(tmp_path)
        ref = stage_bytes(store, b"payload")
        assert store.consume(ref) == b"payload"
        assert store.count_blobs() == 0
        with pytest.raises(ArtifactMissingError):
            store.consume(ref)  # duplicate delivery converges: missing, never wrong bytes

    def test_unlink_failure_is_observable_not_silent(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)
        ref = stage_bytes(store, b"payload")
        path = store._blobs_dir / f"{ref.name}.bin"

        real_unlink = Path.unlink

        def failing_unlink(self: Path, *args: Any, **kwargs: Any):
            if self == path:
                raise OSError("locked")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        assert store.consume(ref) == b"payload"
        assert store.failed_unlinks == 1

    def test_fsync_profile_still_verifies(self, tmp_path):
        store = make_store(tmp_path, fsync=True)
        data = os.urandom(70000)
        ref = stage_bytes(store, data)
        assert store.resolve(ref) == data

    def test_stage_short_write_refuses_honestly(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)
        real_open = open

        def truncating_open(file: Any, *args: Any, **kwargs: Any):
            handle = real_open(file, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "")
            if "b" in mode and "x" in mode:
                real_write = handle.write

                def short_write(data: bytes) -> int:
                    return real_write(data[: max(0, len(data) - 10)])

                handle.write = short_write  # type: ignore[method-assign]
            return handle

        monkeypatch.setattr("builtins.open", truncating_open)
        with pytest.raises(OSError):
            store.stage(b"x" * 4096, slot=("result", "text"), kind="text", encoding="raw", job_id="j")
        assert store.count_blobs() == 0  # partial file removed


# ---------------------------------------------------------------------------
# reclamation
# ---------------------------------------------------------------------------


class TestSweep:
    def _age(self, store: ArtifactHandleStore, ref: HandleRef, seconds: float) -> None:
        path = store._blobs_dir / f"{ref.name}.bin"
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_sweep_removes_only_old_blobs(self, tmp_path):
        store = make_store(tmp_path)
        young = stage_bytes(store, b"young")
        old = stage_bytes(store, b"old")
        self._age(store, old, 7200)
        removed = store.sweep(older_than_seconds=3600)
        removed_names = {p.stem for p in removed}
        assert removed_names == {old.name}
        assert store.count_blobs() == 1
        assert (store._blobs_dir / f"{young.name}.bin").is_file()

    def test_sweep_on_empty_root_is_safe(self, tmp_path):
        store = make_store(tmp_path / "nothing-here")
        assert store.sweep(older_than_seconds=0) == []
        assert store.count_blobs() == 0


# ---------------------------------------------------------------------------
# wire envelope validation
# ---------------------------------------------------------------------------


class TestHandleWireValidation:
    def _wire(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "slot": ["result", "text"],
            "kind": "text",
            "encoding": "utf8",
            "name": "a" * 32,
            "length": 3,
            "sha256": "b" * 64,
            "job_id": "job-1",
        }
        base.update(overrides)
        return base

    @pytest.mark.parametrize(
        "overrides",
        [
            {"kind": "unknown"},
            {"encoding": "unknown"},
            {"name": "../bad"},
            {"name": 7},
            {"length": -1},
            {"length": "3"},
            {"sha256": "XYZ"},
            {"job_id": ""},
            {"slot": []},
            {"slot": ["a", 1.5]},
            {"slot": "result-text"},
        ],
    )
    def test_malformed_handles_rejected(self, overrides):
        with pytest.raises(ArtifactHandleValidationError):
            HandleRef.from_wire(self._wire(**overrides))

    def test_non_dict_handle_rejected(self):
        with pytest.raises(ArtifactHandleValidationError):
            HandleRef.from_wire(["not", "a", "dict"])

    def test_wire_round_trip(self):
        ref = HandleRef(
            slot=("formats_payload", "html", "assets", 2, "data"),
            kind="asset_data", encoding="raw", name="c" * 32,
            length=99, sha256="d" * 64, job_id="job-9",
        )
        assert HandleRef.from_wire(ref.to_wire()) == ref


# ---------------------------------------------------------------------------
# worker-side staging
# ---------------------------------------------------------------------------


class TestStageWorkerPayload:
    def test_small_payload_unchanged_inline(self, tmp_path):
        store = make_store(tmp_path)
        payload = {"result": sample_result(), "formats_payload": {}}
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1")
        assert wire is payload  # same object: nothing staged, no envelope
        assert store.staged_count == 0

    def test_disabled_returns_payload_unchanged(self, tmp_path):
        store = make_store(tmp_path)
        payload = {"result": sample_result(text=big_text(600_000)), "formats_payload": {}}
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1", enabled=False)
        assert wire is payload
        assert store.staged_count == 0

    def test_large_text_staged_with_utf8(self, tmp_path):
        store = make_store(tmp_path)
        text = big_text(600_000, seed="markdown ")
        payload = {"result": sample_result(text=text), "formats_payload": {}}
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1")
        assert ah.is_handle_envelope(wire)
        env = wire[ah.HANDLE_WIRE_KEY]
        assert env["version"] == ah.HANDLE_VERSION
        (handle,) = env["handles"]
        assert handle["kind"] == "text" and handle["encoding"] == "utf8"
        assert handle["slot"] == ["result", "text"]
        assert "text" not in env["inline"]["result"]  # big bytes left the control message
        assert env["inline"]["result"]["metadata"] == {"page_count": 3}

    def test_large_bytes_image_and_asset_staged_raw(self, tmp_path):
        store = make_store(tmp_path)
        img = os.urandom(400_000)
        asset_data = os.urandom(300_000)
        result = sample_result(image_bytes=img)
        result["assets"][0]["data"] = asset_data
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        handles = wire[ah.HANDLE_WIRE_KEY]["handles"]
        by_slot = {tuple(h["slot"]): h for h in handles}
        assert by_slot[("result", "images", "p1.png")]["encoding"] == "raw"
        assert by_slot[("result", "assets", 0, "data")]["encoding"] == "raw"
        inline_result = wire[ah.HANDLE_WIRE_KEY]["inline"]["result"]
        assert "p1.png" not in inline_result["images"]
        assert "data" not in inline_result["assets"][0]  # leaf extracted, skeleton intact
        assert inline_result["assets"][0]["name"] == "sheets/Sheet1.csv"

    def test_large_pil_image_staged_as_pickle(self, tmp_path):
        store = make_store(tmp_path)
        big_pil = pil_image(600)  # 600*600*3 raw raster > inline limit once pickled
        result = sample_result()
        result["images"]["huge.png"] = big_pil
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        handles = wire[ah.HANDLE_WIRE_KEY]["handles"]
        by_slot = {tuple(h["slot"]): h for h in handles}
        assert by_slot[("result", "images", "huge.png")]["kind"] == "image_pil"
        assert by_slot[("result", "images", "huge.png")]["encoding"] == "pickle"

    def test_multi_format_envelope_walked(self, tmp_path):
        store = make_store(tmp_path)
        payload = {
            "result": sample_result(text=big_text(500_000)),
            "formats_payload": {
                "html": sample_result(text=big_text(400_000, seed="<p>html</p>"), with_pil=False),
            },
        }
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1")
        slots = {tuple(h["slot"]) for h in wire[ah.HANDLE_WIRE_KEY]["handles"]}
        assert ("result", "text") in slots
        assert ("formats_payload", "html", "text") in slots

    def test_single_format_payload_without_envelope_walked(self, tmp_path):
        store = make_store(tmp_path)
        payload = sample_result(text=big_text(500_000))
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1")
        assert ah.is_handle_envelope(wire)
        (handle,) = wire[ah.HANDLE_WIRE_KEY]["handles"]
        assert handle["slot"] == ["text"]

    def test_surrogate_text_falls_back_to_pickle_encoding(self, tmp_path):
        store = make_store(tmp_path)
        text = "lead" + "\ud800" + big_text(400_000)  # lone surrogate breaks utf-8
        payload = sample_result(text=text)
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1")
        (handle,) = wire[ah.HANDLE_WIRE_KEY]["handles"]
        assert handle["encoding"] == "pickle"

    def test_staging_failure_degrades_to_inline(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)
        text = big_text(600_000)
        payload = sample_result(text=text)
        calls = {"n": 0}

        real_stage = ArtifactHandleStore.stage

        def failing_stage(self: ArtifactHandleStore, data: bytes, **kwargs: Any):
            calls["n"] += 1
            raise OSError("disk full")

        monkeypatch.setattr(ArtifactHandleStore, "stage", failing_stage)
        wire = ah.stage_worker_payload(payload, store=store, job_id="j1")
        assert wire is payload  # zero handles staged -> classic inline contract
        assert calls["n"] >= 1

    def test_partial_staging_failure_keeps_earlier_handles(self, tmp_path, monkeypatch):
        store = make_store(tmp_path)
        img = os.urandom(400_000)
        result = sample_result(text=big_text(600_000), image_bytes=img)
        result["assets"][0]["data"] = os.urandom(400_000)
        real_stage = ArtifactHandleStore.stage
        calls = {"n": 0}

        def flaky_stage(self: ArtifactHandleStore, data: bytes, **kwargs: Any):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("disk full mid-job")
            return real_stage(self, data, **kwargs)

        monkeypatch.setattr(ArtifactHandleStore, "stage", flaky_stage)
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        env = wire[ah.HANDLE_WIRE_KEY]
        assert len(env["handles"]) == 1  # first candidate made it out as a handle
        # The staged field left the control message; the failing candidates
        # stay inline — mixed degradation is a valid, truthful envelope.
        assert "text" not in env["inline"]["result"]
        assert env["inline"]["result"]["images"]["p1.png"] == img
        assert len(env["inline"]["result"]["assets"][0]["data"]) == 400_000


# ---------------------------------------------------------------------------
# parent-side strict resolution
# ---------------------------------------------------------------------------


class TestResolveWorkerPayload:
    def _round_trip(self, tmp_path, payload: dict[str, Any], job_id: str = "j1") -> dict[str, Any]:
        store = make_store(tmp_path)
        wire = ah.stage_worker_payload(payload, store=store, job_id=job_id)
        return ah.resolve_worker_payload(wire, store=store, job_id=job_id)

    def test_rebuild_matches_original_semantics(self, tmp_path):
        text = big_text(600_000)
        img = os.urandom(400_000)
        big_pil = pil_image(500)
        result = sample_result(text=text, image_bytes=img)
        result["images"]["huge.png"] = big_pil
        result["assets"][0]["data"] = os.urandom(300_000)
        original = dict(result)
        original_images = dict(result["images"])
        rebuilt = self._round_trip(tmp_path, {"result": result})
        assert rebuilt["result"]["text"] == text
        assert rebuilt["result"]["images"]["p1.png"] == img
        assert rebuilt["result"]["images"]["huge.png"].tobytes() == big_pil.tobytes()
        assert rebuilt["result"]["images"]["huge.png"].mode == big_pil.mode
        assert rebuilt["result"]["images"]["huge.png"].size == big_pil.size
        assert rebuilt["result"]["assets"][0]["data"] == result["assets"][0]["data"]
        assert rebuilt["result"]["metadata"] == original["metadata"]
        assert set(rebuilt["result"]["images"]) == set(original_images)

    def test_multi_format_rebuild(self, tmp_path):
        payload = {
            "result": sample_result(text=big_text(500_000)),
            "formats_payload": {
                "html": sample_result(text=big_text(400_000, seed="<p>x</p>"), with_pil=False),
            },
        }
        rebuilt = self._round_trip(tmp_path, payload)
        assert len(rebuilt["result"]["text"]) == 500_000
        assert len(rebuilt["formats_payload"]["html"]["text"]) == 400_000

    def test_resolution_consumes_all_blobs(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        result["images"]["big.png"] = os.urandom(400_000)
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        ah.resolve_worker_payload(wire, store=store, job_id="j1")
        assert store.count_blobs() == 0

    def test_inline_payload_passes_through_untouched(self, tmp_path):
        store = make_store(tmp_path)
        payload = {"result": sample_result()}
        assert ah.resolve_worker_payload(payload, store=store, job_id="j1") is payload

    def test_unknown_version_rejected(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        wire[ah.HANDLE_WIRE_KEY]["version"] = 99
        with pytest.raises(ArtifactHandleValidationError):
            ah.resolve_worker_payload(wire, store=store, job_id="j1")

    def test_cross_job_handle_rejected(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="job-a")
        with pytest.raises(ArtifactHandleValidationError, match="cross-job"):
            ah.resolve_worker_payload(wire, store=store, job_id="job-b")

    def test_tampered_inline_structure_rejected(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        del wire[ah.HANDLE_WIRE_KEY]["inline"]["result"]  # skeleton gone
        with pytest.raises(ArtifactHandleValidationError):
            ah.resolve_worker_payload(wire, store=store, job_id="j1")

    def test_duplicate_resolution_fails_closed_and_leaves_no_residue(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        first = ah.resolve_worker_payload(wire, store=store, job_id="j1")
        assert first["result"]["text"] == result["text"]
        with pytest.raises(ArtifactMissingError):
            ah.resolve_worker_payload(wire, store=store, job_id="j1")
        assert store.count_blobs() == 0

    def test_deleted_backing_fails_resolution(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        store.sweep(older_than_seconds=0)
        with pytest.raises(ArtifactMissingError):
            ah.resolve_worker_payload(wire, store=store, job_id="j1")

    def test_envelope_must_be_dict_shape(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ArtifactHandleValidationError):
            ah.resolve_worker_payload(
                {ah.HANDLE_WIRE_KEY: {"version": 1, "inline": [], "handles": []}},
                store=store,
                job_id="j1",
            )


# ---------------------------------------------------------------------------
# adversarial lifecycle: crash windows, concurrency, leaks (WP-D)
# ---------------------------------------------------------------------------


class TestLifecycleHardening:
    def test_producer_crash_before_delivery_leaves_sweepable_orphans(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        # Worker dies here: the event never crosses the boundary.
        assert store.count_blobs() == 1
        removed = store.sweep(older_than_seconds=0)
        assert len(removed) == 1
        assert store.count_blobs() == 0

    def test_consumer_crash_before_consume_leaves_sweepable_orphans(self, tmp_path):
        store = make_store(tmp_path)
        result = sample_result(text=big_text(600_000))
        wire = ah.stage_worker_payload({"result": result}, store=store, job_id="j1")
        # Parent receives the event but dies before resolution; restart sweeps.
        store.sweep(older_than_seconds=3600)  # young blob must survive a cautious sweep
        assert store.count_blobs() == 1
        store.sweep(older_than_seconds=0)
        assert store.count_blobs() == 0

    def test_concurrent_distinct_payloads_never_cross_wire(self, tmp_path):
        import threading

        store = make_store(tmp_path)
        secrets = {f"job-{i}": os.urandom(300_000 + i) for i in range(8)}
        wires: dict[str, Any] = {}
        locks = threading.Lock()

        def producer(job_id: str, blob: bytes) -> None:
            result = sample_result(text=blob.decode("latin-1"))
            wire = ah.stage_worker_payload({"result": result}, store=store, job_id=job_id)
            with locks:
                wires[job_id] = wire

        threads = [threading.Thread(target=producer, args=(jid, blob)) for jid, blob in secrets.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        def consumer(job_id: str) -> None:
            rebuilt = ah.resolve_worker_payload(wires[job_id], store=store, job_id=job_id)
            assert rebuilt["result"]["text"].encode("latin-1") == secrets[job_id]

        threads = [threading.Thread(target=consumer, args=(jid,)) for jid in secrets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert store.count_blobs() == 0

    def test_repeated_cycles_do_not_leak_blobs(self, tmp_path):
        store = make_store(tmp_path)
        for cycle in range(25):
            result = sample_result(text=big_text(300_000, seed=f"cycle{cycle} "))
            wire = ah.stage_worker_payload({"result": result}, store=store, job_id=f"j{cycle}")
            rebuilt = ah.resolve_worker_payload(wire, store=store, job_id=f"j{cycle}")
            assert rebuilt["result"]["text"] == result["text"]
        assert store.count_blobs() == 0
        assert store.staged_count == 25
        assert store.resolved_count == 25

    def test_slow_consumer_with_more_results_completing(self, tmp_path):
        store = make_store(tmp_path)
        wires = []
        for i in range(5):
            result = sample_result(text=big_text(300_000, seed=f"slow{i} "))
            wires.append(ah.stage_worker_payload({"result": result}, store=store, job_id=f"j{i}"))
        # Producer finished all results; consumer has not touched any yet.
        assert store.count_blobs() == 5  # bounded by produced results, producer never blocked
        time.sleep(0.05)
        for i, wire in enumerate(wires):
            rebuilt = ah.resolve_worker_payload(wire, store=store, job_id=f"j{i}")
            assert rebuilt["result"]["text"].startswith(f"slow{i}")
        assert store.count_blobs() == 0


# ---------------------------------------------------------------------------
# real cross-process handoff (one producer process, one consumer)
# ---------------------------------------------------------------------------


def _child_handoff(queue: Any, root: str) -> None:
    """Spawn-side producer: build a big result, stage it, emit the wire event."""
    import sys

    sys_path_fix = Path(__file__).resolve().parent.parent
    if str(sys_path_fix) not in sys.path:
        sys.path.insert(0, str(sys_path_fix))
    from app.services import artifact_handles as ah_mod
    from app.services.job_transport import WorkerEvent, WorkerEventType

    store = ArtifactHandleStore(root)
    text = big_text(900_000, seed="cross-process ")
    img = os.urandom(500_000)
    result = sample_result(text=text, image_bytes=img, with_pil=True)
    wire = ah_mod.stage_worker_payload({"result": result}, store=store, job_id="proc-job")
    queue.put(
        WorkerEvent(
            type=WorkerEventType.result,
            job_id="proc-job",
            worker_id=0,
            payload=wire,
        )
    )


class TestRealProcessHandoff:
    def test_handle_created_in_one_process_consumed_in_another(self, tmp_path):
        root = str(tmp_path / "handles")
        queue: Any = mp.Queue()
        child = mp.Process(target=_child_handoff, args=(queue, root), daemon=True)
        child.start()
        event = queue.get(timeout=60)
        child.join(timeout=30)
        assert not child.is_alive()

        assert event.type.value == "result"
        wire = event.payload
        assert ah.is_handle_envelope(wire)
        handles = wire[ah.HANDLE_WIRE_KEY]["handles"]
        assert len(handles) >= 2  # text + bytes image at least (PIL may also exceed)

        store = ArtifactHandleStore(root)
        rebuilt = ah.resolve_worker_payload(wire, store=store, job_id="proc-job")
        assert rebuilt["result"]["text"].startswith("cross-process ")
        assert len(rebuilt["result"]["text"]) == 900_000
        assert len(rebuilt["result"]["images"]["p1.png"]) == 500_000
        assert rebuilt["result"]["images"]["p2.png"].size == (64, 64)  # inline PIL survived too
        assert store.count_blobs() == 0
