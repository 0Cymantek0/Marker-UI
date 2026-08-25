"""Office fixture generation must be controller-owned under xdist."""

from __future__ import annotations

from types import SimpleNamespace

from tests import conftest


def test_controller_builds_office_fixtures_before_workers(monkeypatch):
    calls = []
    monkeypatch.setattr(conftest, "_build_office_fixtures", lambda: calls.append(True))

    conftest.pytest_configure(SimpleNamespace())
    conftest.pytest_configure(SimpleNamespace(workerinput={"workerid": "gw0"}))

    assert calls == [True]
