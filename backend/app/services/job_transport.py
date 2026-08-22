"""Job transport boundary for multi-worker conversion scaling.

This module defines the ONLY two things that cross a process (or, later, a
network) boundary between the parent orchestrator and a conversion worker:

* ``JobEnvelope`` — everything a worker needs to run one conversion. It must be
  picklable, so it carries plain data only: ids, strings, and a config dict. No
  ``MarkerService``, no converter, no DB session ever crosses the boundary.
* ``WorkerEvent`` — a tagged status/progress/log/result message flowing back
  from a worker to the parent.

``JobTransport`` is the abstract submit/stream/result interface. Phase 1 ships
``QueueTransport`` over ``multiprocessing.Queue``. A later multi-node phase can
implement the same interface over Redis/HTTP with zero call-site changes, which
is the whole reason this seam exists.
"""

from __future__ import annotations

import multiprocessing as mp
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# Messages that cross the boundary
# ---------------------------------------------------------------------------


@dataclass
class JobEnvelope:
    """The complete, picklable description of one conversion job.

    ``filepath`` is treated as an opaque resource locator string. Today it is a
    local path; a future object-store phase makes it an object key without
    changing this signature.
    """

    job_id: str
    filepath: str
    config: dict[str, Any]
    device_str: str = "cpu"


class WorkerEventType(str, Enum):
    """Kind of a worker -> parent message."""

    progress = "progress"
    log = "log"
    status = "status"
    result = "result"
    error = "error"
    heartbeat = "heartbeat"
    # PR69 runtime capacity: structured admission/residency observations
    # (cold load, warm reuse, queue reason, admission decision, OOM
    # containment feedback) so callers can see WHY work waits and what it
    # cost, instead of everything being generic "processing".
    runtime = "runtime"


@dataclass
class WorkerEvent:
    """A single message emitted by a worker about a job.

    Only the fields relevant to ``type`` are populated; the rest stay at their
    defaults so the whole object stays trivially picklable.
    """

    type: WorkerEventType
    job_id: str
    worker_id: int = -1

    # progress
    percent: int = 0
    label: str = ""

    # log
    message: str = ""
    levelname: str = "INFO"

    # status
    status_text: str = ""
    pid: Optional[int] = None

    # result / error
    payload: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""

    # runtime (PR69): the structured observation payload. Phase marker in
    # runtime["phase"]; see gpu_worker._emit_runtime for the schema.
    runtime: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Transport interface + queue implementation
# ---------------------------------------------------------------------------


class JobTransport(ABC):
    """Submit jobs to workers and stream their events back.

    The parent owns one transport instance. Workers receive only the event
    sink (e.g. the queue) via their initializer, never the transport object.
    """

    @abstractmethod
    def emit(self, event: WorkerEvent) -> None:
        """Push one event from a worker toward the parent (worker side)."""

    @abstractmethod
    def drain(self, timeout: float | None = None) -> Iterator[WorkerEvent]:
        """Yield available events on the parent side, blocking up to *timeout*."""

    @abstractmethod
    def close(self) -> None:
        """Release any underlying resources."""


# A sentinel pushed onto the queue to unblock and stop a blocking drain loop.
_STOP = "__job_transport_stop__"


class QueueTransport(JobTransport):
    """``multiprocessing.Queue`` backed transport for single-node multi-GPU.

    ``emit`` runs in worker processes; ``drain`` runs in the parent's drain
    thread. The same underlying queue object is shared by passing it to the
    pool initializer.
    """

    def __init__(self, queue: Any | None = None) -> None:
        # Accept an injected queue (tests pass a plain ``queue.Queue``); default
        # to a real mp queue for cross-process use.
        self._queue = queue if queue is not None else mp.Queue()

    @property
    def queue(self) -> Any:
        """The raw queue to hand to a worker pool initializer."""
        return self._queue

    def emit(self, event: WorkerEvent) -> None:
        self._queue.put(event)

    def drain(self, timeout: float | None = None) -> Iterator[WorkerEvent]:
        """Block for one event (up to *timeout*), then yield everything queued.

        Yields nothing on timeout. A ``_STOP`` sentinel ends iteration so the
        parent's drain loop can shut down cleanly.
        """
        import queue as _q

        try:
            first = self._queue.get(timeout=timeout) if timeout is not None else self._queue.get()
        except _q.Empty:
            return
        if first == _STOP:
            return
        yield first
        # Drain whatever else is immediately available without blocking.
        while True:
            try:
                item = self._queue.get_nowait()
            except _q.Empty:
                break
            if item == _STOP:
                break
            yield item

    def stop(self) -> None:
        """Push the stop sentinel so a blocked ``drain`` returns."""
        self._queue.put(_STOP)

    def close(self) -> None:
        close = getattr(self._queue, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
