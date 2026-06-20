"""Tests for the job transport boundary (envelope/event pickling + queue drain).

These run with NO GPU and NO real conversion. They lock the two invariants the
multi-worker design rests on:

* ``JobEnvelope`` / ``WorkerEvent`` survive a pickle round-trip unchanged (they
  cross a process boundary, so anything non-picklable here breaks spawn).
* ``QueueTransport.drain`` blocks for one event then sweeps the rest, and the
  stop sentinel ends iteration.
"""

from __future__ import annotations

import pickle
import queue

from app.services.job_transport import (
    JobEnvelope,
    QueueTransport,
    WorkerEvent,
    WorkerEventType,
)


class TestPickleRoundTrip:
    def test_job_envelope_round_trips(self):
        env = JobEnvelope(
            job_id="job-1",
            filepath="/tmp/doc.pdf",
            config={"output_format": "markdown", "use_llm": True},
            device_str="cuda:1",
        )
        back = pickle.loads(pickle.dumps(env))
        assert back == env
        assert back.device_str == "cuda:1"
        assert back.config["use_llm"] is True

    def test_worker_event_round_trips(self):
        ev = WorkerEvent(
            type=WorkerEventType.result,
            job_id="job-1",
            worker_id=2,
            payload={"text": "# Hi", "extension": "md"},
        )
        back = pickle.loads(pickle.dumps(ev))
        assert back == ev
        assert back.type is WorkerEventType.result
        assert back.payload["extension"] == "md"

    def test_event_defaults_keep_it_picklable(self):
        # A minimal progress event must not pull in any unpicklable default.
        ev = WorkerEvent(type=WorkerEventType.progress, job_id="j", percent=42, label="OCR")
        back = pickle.loads(pickle.dumps(ev))
        assert back.percent == 42
        assert back.label == "OCR"
        assert back.payload == {}


class TestQueueTransport:
    def test_emit_then_drain_yields_event(self):
        t = QueueTransport(queue.Queue())
        t.emit(WorkerEvent(type=WorkerEventType.progress, job_id="j", percent=10))
        events = list(t.drain(timeout=0.1))
        assert len(events) == 1
        assert events[0].percent == 10

    def test_drain_sweeps_all_pending_after_first_block(self):
        t = QueueTransport(queue.Queue())
        for i in range(3):
            t.emit(WorkerEvent(type=WorkerEventType.progress, job_id="j", percent=i))
        events = list(t.drain(timeout=0.1))
        assert [e.percent for e in events] == [0, 1, 2]

    def test_drain_times_out_empty(self):
        t = QueueTransport(queue.Queue())
        assert list(t.drain(timeout=0.05)) == []

    def test_stop_sentinel_ends_drain(self):
        t = QueueTransport(queue.Queue())
        t.stop()
        assert list(t.drain(timeout=0.1)) == []

    def test_stop_after_events_truncates(self):
        t = QueueTransport(queue.Queue())
        t.emit(WorkerEvent(type=WorkerEventType.progress, job_id="j", percent=1))
        t.stop()
        t.emit(WorkerEvent(type=WorkerEventType.progress, job_id="j", percent=2))
        # First drain yields the pre-stop event, then hits the sentinel.
        first = list(t.drain(timeout=0.1))
        assert [e.percent for e in first] == [1]
