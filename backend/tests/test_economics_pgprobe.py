"""Pure-function tests for the industrial economics pgprobe WAL delta.

No services are required: these exercise ``wal_delta`` only, asserting the
delta arithmetic and its negative-counter guard. The ``stats_reset`` string
field is explicitly excluded from the delta so a snapshot's reset marker
never influences a measurement.
"""

from app.eval.economics.pgprobe import wal_delta


def _snapshot(wal_records=100, wal_fpi=10, wal_bytes=2048, wal_buffers_full=2,
              wal_write=5, wal_sync=3, stats_reset="2026-01-01 00:00:00"):
    return {
        "wal_records": wal_records,
        "wal_fpi": wal_fpi,
        "wal_bytes": wal_bytes,
        "wal_buffers_full": wal_buffers_full,
        "wal_write": wal_write,
        "wal_sync": wal_sync,
        "stats_reset": stats_reset,
    }


def test_wal_delta_positive_deltas():
    before = _snapshot()
    after = _snapshot(
        wal_records=140, wal_fpi=14, wal_bytes=4096,
        wal_buffers_full=3, wal_write=8, wal_sync=4,
    )
    delta = wal_delta(before, after)
    assert delta == {
        "wal_records_delta": 40,
        "wal_fpi_delta": 4,
        "wal_bytes_delta": 2048,
        "wal_buffers_full_delta": 1,
        "wal_write_delta": 3,
        "wal_sync_delta": 1,
    }


def test_wal_delta_zero_deltas():
    snap = _snapshot()
    delta = wal_delta(snap, dict(snap))
    assert all(v == 0 for v in delta.values())


def test_wal_delta_ignores_stats_reset_string():
    before = _snapshot(stats_reset="2026-01-01 00:00:00")
    after = _snapshot(
        wal_records=200, wal_fpi=20, wal_bytes=8192,
        wal_buffers_full=4, wal_write=10, wal_sync=6,
        stats_reset="2026-06-01 12:00:00",
    )
    delta = wal_delta(before, after)
    assert delta["wal_records_delta"] == 100
    assert delta["wal_bytes_delta"] == 6144


def test_wal_delta_negative_raises_value_error():
    before = _snapshot(wal_bytes=4096, wal_records=140)
    after = _snapshot(wal_bytes=2048, wal_records=100)
    exc = None
    try:
        wal_delta(before, after)
    except ValueError as e:
        exc = e
    assert exc is not None
    assert "decreased inside the measurement window" in str(exc)


def test_wal_delta_each_key_guarded():
    # Only one counter decreases; wal_delta must still raise.
    before = _snapshot()
    after = _snapshot(wal_records=50, wal_fpi=10, wal_bytes=2048,
                      wal_buffers_full=2, wal_write=5, wal_sync=3)
    exc = None
    try:
        wal_delta(before, after)
    except ValueError as e:
        exc = e
    assert exc is not None
    assert "wal_records" in str(exc)
    assert "decreased inside the measurement window" in str(exc)
