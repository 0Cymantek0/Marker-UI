"""PR68A data-plane characterization benchmark.

Reproducible comparison of the lanes that can move a process-worker
conversion result from a spawned worker to the parent:

* ``queue_inline``       — pickle the whole result through
  ``multiprocessing.Queue`` (the pre-PR68A mechanism, the baseline);
* ``file_handle``        — stage eligible large fields in an
  ``ArtifactHandleStore`` and resolve them in the parent (ephemeral,
  no-fsync profile);
* ``file_handle_fsync``  — same store with the fsync durability profile;
* ``shared_memory``      — ``multiprocessing.shared_memory`` fast lane
  (experiment only; promotion decided by this benchmark).

Protocol per repetition: the parent spawns ONE child process, ships the
raw source payload to it over a control queue (not measured), the child
produces the handoff on the measured lane, and the parent records the
time until the bytes are back in memory AND verified. Child CPU time,
serialized control-message size, leftover artifacts and cleanup cost are
recorded alongside.

Usage (from backend/):

    python scripts/artifact_dataplane_benchmark.py \
        --lanes queue_inline,file_handle,shared_memory \
        --sizes 262144,4194304,33554432 --reps 7 --shape real \
        --children 2 --out report.json

The report is a JSON document safe to commit: it records platform,
Python version, CPU count and timings only — never local paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import pickle
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

LANES = ("queue_inline", "file_handle", "file_handle_fsync", "shared_memory")
#: Fields the file-handle lanes treat as eligible large data (mirrors
#: ``app.services.artifact_handles`` policy).
INLINE_LIMIT = 256 * 1024


def _rand_bytes(n: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return rng.randbytes(n)


def build_real_payload(total_bytes: int, *, seed: int = 1408) -> dict[str, Any]:
    """Deterministic conversion-result envelope of roughly ``total_bytes``.

    Shape mirrors ``{result, formats_payload}`` worker envelopes: text,
    PIL image, raw-bytes image, metadata and an asset with byte data.
    """
    from PIL import Image

    text_len = max(1, int(total_bytes * 0.35))
    img_len = max(1, int(total_bytes * 0.40))
    asset_len = max(1, total_bytes - text_len - img_len)

    text = _rand_bytes(text_len, seed).decode("latin-1")
    raw_image = _rand_bytes(img_len, seed + 1)
    side = 768
    pil_image = Image.frombytes("RGB", (side, side), _rand_bytes(side * side * 3, seed + 2))

    result = {
        "text": text,
        "extension": "md",
        "images": {
            "page_1.png": pil_image,
            "page_2.png": raw_image,
        },
        "metadata": {"benchmark": True, "seed": seed},
        "assets": [
            {
                "name": "sheets/Sheet1.csv",
                "media_type": "text/csv",
                "data": _rand_bytes(asset_len, seed + 3),
                "pil": None,
            }
        ],
    }
    formats = {
        "html": {
            "text": f"<html><body>{text[: text_len // 2]}</body></html>",
            "extension": "html",
            "images": {"page_2.png": raw_image},
            "metadata": {},
            "assets": [],
        }
    }
    return {"result": result, "formats_payload": formats}


def _payload_bytes_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_snapshot(payload)).hexdigest()


def _canonical_snapshot(payload: Any) -> bytes:
    """Byte snapshot used for end-to-end verification of a rebuilt payload.

    Dict order is normalized because the handle rebuild re-inserts staged
    fields at the end of their container; the logical payload is equal.
    """
    if isinstance(payload, dict):
        items = sorted(payload.items(), key=lambda kv: str(kv[0]))
        return pickle.dumps({k: _canonical_snapshot(v) for k, v in items}, protocol=4)
    if isinstance(payload, (list, tuple)):
        return pickle.dumps([_canonical_snapshot(v) for v in payload], protocol=4)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8", "surrogatepass")
    if payload.__class__.__name__ == "PngImagePlugin.PngImageFile" or hasattr(payload, "tobytes"):
        return pickle.dumps(payload, protocol=4)
    return pickle.dumps(payload, protocol=4)


# ---------------------------------------------------------------------------
# Child side
# ---------------------------------------------------------------------------


def _child_main(task_q: Any, data_q: Any, stats_q: Any, cons_q: Any, root: str) -> None:
    """Produce handoffs on requested lanes until told to stop."""
    while True:
        msg = task_q.get()
        if msg[0] == "stop":
            return
        lane, payload = msg
        try:
            stats = _child_run_lane(lane, payload, root)
            stats_q.put(("ok", stats))
        except Exception as exc:  # noqa: BLE001 - report and keep the loop alive
            # Unblock the parent's receive first, then deliver the failure.
            data_q.put(None)
            stats_q.put(("error", repr(exc)))


def _child_run_lane(lane: str, payload: Any, root: str) -> dict[str, Any]:
    stats: dict[str, Any] = {"lane": lane}
    t0 = time.perf_counter()
    cpu0 = time.process_time()
    if lane == "queue_inline":
        import app.services.job_transport as jt

        event = jt.WorkerEvent(
            type=jt.WorkerEventType.result,
            job_id="bench-job",
            worker_id=0,
            payload=payload,
        )
        stats["emit_pickle_bytes"] = len(pickle.dumps(event, protocol=pickle.HIGHEST_PROTOCOL))
        data_q_put = _data_q()
        data_q_put.put(event)
    elif lane in ("file_handle", "file_handle_fsync"):
        from app.services.artifact_handles import ArtifactHandleStore, stage_worker_payload

        store = ArtifactHandleStore(Path(root), fsync=(lane == "file_handle_fsync"))
        wire = stage_worker_payload(payload, store=store, job_id="bench-job")
        import app.services.job_transport as jt

        event = jt.WorkerEvent(
            type=jt.WorkerEventType.result,
            job_id="bench-job",
            worker_id=0,
            payload=wire,
        )
        stats["emit_pickle_bytes"] = len(pickle.dumps(event, protocol=pickle.HIGHEST_PROTOCOL))
        stats["handles"] = len(wire["artifact_handles"]["handles"])
        _data_q().put(event)
    elif lane == "shared_memory":
        from multiprocessing import shared_memory

        blob = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        shm = shared_memory.SharedMemory(create=True, size=len(blob))
        try:
            shm.buf[: len(blob)] = blob
            stats["emit_pickle_bytes"] = len(blob)
            _data_q().put(
                {
                    "name": shm.name,
                    "size": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
            # Parent acks after it copied the bytes; then the child may unlink.
            _cons_q().get()
        finally:
            shm.close()
            shm.unlink()
    else:
        raise ValueError(f"unknown lane: {lane}")
    stats["emit_cpu_ms"] = (time.process_time() - cpu0) * 1000.0
    stats["emit_wall_ms"] = (time.perf_counter() - t0) * 1000.0
    return stats


_DATA_Q: Any = None
_CONS_Q: Any = None
_STATS_Q: Any = None


def _data_q() -> Any:
    global _DATA_Q
    return _DATA_Q


def _cons_q() -> Any:
    global _CONS_Q
    return _CONS_Q


def _child_entry(task_q: Any, data_q: Any, stats_q: Any, cons_q: Any, root: str) -> None:
    global _DATA_Q, _CONS_Q
    _DATA_Q = data_q
    _CONS_Q = cons_q
    _child_main(task_q, data_q, stats_q, cons_q, root)


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------


def _parent_receive(lane: str, root: str, source_digest: str) -> tuple[dict[str, Any], float]:
    """Block for one handoff, verify it, return (stats, handoff_ms)."""
    t0 = time.perf_counter()
    data_q = _DATA_Q
    if lane == "queue_inline":
        event = data_q.get()
        if event is None:
            status, err = _STATS_Q.get()
            raise RuntimeError(f"child failed on lane {lane}: {err}")
        rebuilt = event.payload
    elif lane in ("file_handle", "file_handle_fsync"):
        from app.services.artifact_handles import ArtifactHandleStore, resolve_worker_payload

        event = data_q.get()
        if event is None:
            status, err = _STATS_Q.get()
            raise RuntimeError(f"child failed on lane {lane}: {err}")
        rebuilt = resolve_worker_payload(event.payload, store=ArtifactHandleStore(Path(root)), job_id="bench-job")
    elif lane == "shared_memory":
        from multiprocessing import shared_memory

        meta = data_q.get()
        shm = shared_memory.SharedMemory(name=meta["name"])
        try:
            blob = bytes(shm.buf[: meta["size"]])
        finally:
            shm.close()
        _CONS_Q.put(True)
        if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
            raise RuntimeError("shared-memory digest mismatch")
        rebuilt = pickle.loads(blob)
    else:
        raise ValueError(f"unknown lane: {lane}")
    handoff_ms = (time.perf_counter() - t0) * 1000.0
    digest = hashlib.sha256(_canonical_snapshot(rebuilt)).hexdigest()
    if digest != source_digest:
        raise RuntimeError(f"lane {lane}: rebuilt payload digest mismatch")
    return {}, handoff_ms


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def run_benchmark(
    lanes: list[str],
    sizes: list[int],
    reps: int,
    shape: str,
    children: int = 1,
) -> dict[str, Any]:
    scratch = tempfile.mkdtemp(prefix="marker_pr68a_bench_")
    root = os.path.join(scratch, "handles")
    # Shared data/stats/cons queues model the real topology: N workers, one
    # parent drain point. Only the per-child task queue is private, so the
    # shm-lane consume ack reaches whichever child is waiting.
    data_q: Any = mp.Queue()
    stats_q: Any = mp.Queue()
    cons_q: Any = mp.Queue()
    global _DATA_Q
    _DATA_Q = data_q
    global _STATS_Q
    _STATS_Q = stats_q
    global _CONS_Q
    _CONS_Q = cons_q

    child_procs: list[dict[str, Any]] = []
    for _ in range(children):
        task_q: Any = mp.Queue()
        child = mp.Process(
            target=_child_entry, args=(task_q, data_q, stats_q, cons_q, root), daemon=True
        )
        child.start()
        child_procs.append({"task_q": task_q, "child": child})

    report: dict[str, Any] = {
        "schema": "marker.pr68a.dataplane.benchmark.v1",
        "env": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "machine": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
            "cpu_count": os.cpu_count(),
            "start_method": mp.get_start_method(),
            "shape": shape,
            "inline_limit": INLINE_LIMIT,
        },
        "params": {"lanes": lanes, "sizes": sizes, "reps": reps, "children": children},
        "results": [],
    }

    try:
        for size in sizes:
            if shape == "real":
                payload = build_real_payload(size)
            else:
                payload = {"result": {"text": _rand_bytes(size, 7).decode("latin-1"), "images": {}, "metadata": {}, "assets": []}, "formats_payload": {}}
            source_digest = _payload_bytes_digest(payload)
            for lane in lanes:
                for rep in range(reps):
                    print(
                        f"bench: lane={lane} size={size} rep={rep} children={children} submitting",
                        file=sys.stderr,
                        flush=True,
                    )
                    t0 = time.perf_counter()
                    # Fan the same rep out to every child, then receive them
                    # all through the one shared data queue.
                    for spec in child_procs:
                        spec["task_q"].put((lane, payload))
                    for _ in range(children):
                        _parent_receive(lane, root, source_digest)
                    batch_ms = (time.perf_counter() - t0) * 1000.0
                    rep_stats: dict[str, Any] = {}
                    for _ in range(children):
                        status, child_stats = stats_q.get()
                        if status != "ok":
                            raise RuntimeError(f"child failed on lane {lane}: {child_stats}")
                        if not rep_stats:
                            rep_stats = {
                                k: (round(v, 3) if isinstance(v, float) else v)
                                for k, v in child_stats.items()
                                if k not in ("lane",)
                            }
                    print(f"bench: lane={lane} size={size} rep={rep} received", file=sys.stderr, flush=True)
                    row = {
                        "lane": lane,
                        "size_bytes": size,
                        "rep": rep,
                        "children": children,
                        "batch_ms": round(batch_ms, 3),
                        "per_result_ms": round(batch_ms / children, 3),
                        **rep_stats,
                    }
                    report["results"].append(row)
    finally:
        for spec in child_procs:
            spec["task_q"].put(("stop", None))
            spec["child"].join(timeout=15)
            if spec["child"].is_alive():
                spec["child"].terminate()
                spec["child"].join(timeout=5)
        queues = [data_q, stats_q, cons_q] + [spec["task_q"] for spec in child_procs]
        for q in queues:
            q.close()
            q.join_thread()

    summary: dict[str, Any] = {}
    for lane in lanes:
        lane_rows = [r for r in report["results"] if r["lane"] == lane]
        for size in sizes:
            size_rows = [r for r in lane_rows if r["size_bytes"] == size]
            if not size_rows:
                continue
            per_result = [r["per_result_ms"] for r in size_rows]
            summary.setdefault(lane, {})[str(size)] = {
                "per_result_p50_ms": round(statistics.median(per_result), 3),
                "per_result_p95_ms": round(_pct(per_result, 0.95), 3),
                "emit_pickle_bytes_median": statistics.median(
                    [r.get("emit_pickle_bytes", 0) for r in size_rows]
                ),
            }
    report["summary"] = summary

    # Leftover artifacts after the run: the reclamation story in numbers.
    leftover = 0
    handles_dir = Path(root) / "blobs"
    if handles_dir.is_dir():
        leftover = sum(1 for _ in handles_dir.iterdir())
    report["leftover_artifacts"] = leftover
    report["scratch_root_basename"] = Path(scratch).name
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lanes",
        default="queue_inline",
        help=f"comma-separated subset of {LANES}",
    )
    parser.add_argument("--sizes", default="262144,4194304,33554432", help="comma-separated byte sizes")
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument("--shape", choices=("real", "flat"), default="real")
    parser.add_argument(
        "--children",
        type=int,
        default=1,
        help="concurrent producer processes sharing one parent data queue",
    )
    parser.add_argument("--out", default=None, help="optional path to write the JSON report")
    args = parser.parse_args()

    lanes = [lane.strip() for lane in args.lanes.split(",") if lane.strip()]
    unknown = [lane for lane in lanes if lane not in LANES]
    if unknown:
        print(f"unknown lanes: {unknown}; valid: {LANES}", file=sys.stderr)
        return 2
    if "file_handle" in lanes or "file_handle_fsync" in lanes:
        try:
            import app.services.artifact_handles  # noqa: F401
        except ImportError as exc:
            print(f"file_handle lanes require the artifact_handles module: {exc}", file=sys.stderr)
            return 2
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    report = run_benchmark(lanes, sizes, args.reps, args.shape, children=max(1, args.children))
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"report written to {out_path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
