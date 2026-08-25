"""Repository hook regressions for zero-flag parallel activation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


_ROOT_CONFTEST = Path(__file__).parents[2] / "conftest.py"


def _load_root_hooks():
    spec = importlib.util.spec_from_file_location("marker_root_conftest_test", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(
    *,
    has_xdist: bool = True,
    numprocesses=None,
    dist: str = "no",
    invocation_args: tuple[str, ...] = (),
):
    pluginmanager = SimpleNamespace(
        hasplugin=lambda name: has_xdist and name == "xdist"
    )
    option = SimpleNamespace(numprocesses=numprocesses, dist=dist)
    invocation_params = SimpleNamespace(args=invocation_args)
    return SimpleNamespace(
        pluginmanager=pluginmanager,
        option=option,
        invocation_params=invocation_params,
    )


def test_cmdline_hook_enables_capacity_scaled_workers(monkeypatch):
    hooks = _load_root_hooks()
    config = _config()
    plan = SimpleNamespace(
        DEFAULT_DISTRIBUTION="loadscope",
        resolve_worker_count=lambda env: 6,
    )
    monkeypatch.setattr(hooks, "_load_plan", lambda: plan)

    hooks.pytest_cmdline_main(config)

    assert config.option.numprocesses == 6
    assert config.option.dist == "loadscope"


def test_cmdline_hook_preserves_explicit_xdist_options(monkeypatch):
    hooks = _load_root_hooks()
    config = _config(numprocesses=3, dist="loadfile")
    monkeypatch.setattr(
        hooks,
        "_load_plan",
        lambda: SimpleNamespace(
            DEFAULT_DISTRIBUTION="loadscope",
            resolve_worker_count=lambda env: 12,
        ),
    )

    hooks.pytest_cmdline_main(config)

    assert config.option.numprocesses == 3
    assert config.option.dist == "loadfile"


def test_cmdline_hook_stays_serial_without_xdist(monkeypatch):
    hooks = _load_root_hooks()
    config = _config(has_xdist=False)
    monkeypatch.setattr(
        hooks,
        "_load_plan",
        lambda: SimpleNamespace(
            DEFAULT_DISTRIBUTION="loadscope",
            resolve_worker_count=lambda env: 8,
        ),
    )

    hooks.pytest_cmdline_main(config)

    assert config.option.numprocesses is None
    assert config.option.dist == "no"


def test_cmdline_hook_honors_serial_resolution(monkeypatch):
    hooks = _load_root_hooks()
    config = _config()
    monkeypatch.setattr(
        hooks,
        "_load_plan",
        lambda: SimpleNamespace(
            DEFAULT_DISTRIBUTION="loadscope",
            resolve_worker_count=lambda env: None,
        ),
    )

    hooks.pytest_cmdline_main(config)

    assert config.option.numprocesses is None
    assert config.option.dist == "no"


def test_cmdline_hook_keeps_focused_file_runs_serial(monkeypatch):
    hooks = _load_root_hooks()
    config = _config(invocation_args=("backend/tests/test_parallel_plan.py", "-q"))
    monkeypatch.setattr(
        hooks,
        "_load_plan",
        lambda: SimpleNamespace(
            DEFAULT_DISTRIBUTION="loadscope",
            resolve_worker_count=lambda env: 12,
            should_auto_parallelize=lambda args, env: False,
        ),
    )

    hooks.pytest_cmdline_main(config)

    assert config.option.numprocesses is None
    assert config.option.dist == "no"


def test_cmdline_hook_allows_explicit_worker_override_for_focused_run(monkeypatch):
    hooks = _load_root_hooks()
    config = _config(invocation_args=("backend/tests/test_parallel_plan.py", "-q"))
    monkeypatch.setattr(
        hooks,
        "_load_plan",
        lambda: SimpleNamespace(
            DEFAULT_DISTRIBUTION="loadscope",
            resolve_worker_count=lambda env: 4,
            should_auto_parallelize=lambda args, env: True,
        ),
    )

    hooks.pytest_cmdline_main(config)

    assert config.option.numprocesses == 4
    assert config.option.dist == "loadscope"
