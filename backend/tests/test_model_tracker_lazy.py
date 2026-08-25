"""Model tracker lazy-construction regressions."""

from __future__ import annotations

import threading

from app.services import model_tracker


def test_tracker_proxy_constructs_one_singleton_on_first_use(monkeypatch):
    calls = []
    real_tracker = model_tracker.ModelTracker

    class RecordingTracker(real_tracker):
        def __init__(self):
            calls.append(True)
            super().__init__()

    monkeypatch.setattr(model_tracker, "ModelTracker", RecordingTracker)
    monkeypatch.setattr(model_tracker, "_tracker_instance", None)
    assert calls == []

    first = model_tracker.get_tracker()
    second = model_tracker.get_tracker()
    assert first is second
    assert len(calls) == 1
    monkeypatch.setattr(model_tracker, "_tracker_instance", None)


def test_tracker_proxy_is_safe_for_concurrent_first_access(monkeypatch):
    real_tracker = model_tracker.ModelTracker
    created = []

    class RecordingTracker(real_tracker):
        def __init__(self):
            created.append(True)
            super().__init__()

    monkeypatch.setattr(model_tracker, "ModelTracker", RecordingTracker)
    monkeypatch.setattr(model_tracker, "_tracker_instance", None)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(model_tracker.get_tracker()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 1
    assert len({id(item) for item in results}) == 1
    monkeypatch.setattr(model_tracker, "_tracker_instance", None)
