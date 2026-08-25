"""Repository-root pytest hooks.

Enables capacity-scaled parallelism for the default `python -m pytest`
invocation. Injection happens here rather than in `addopts` so that a
bare environment without pytest-xdist (the cross-platform conformance
matrix installs pytest only) still runs the suite serially instead of
failing on an unknown option.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

_PLAN_PATH = pathlib.Path(__file__).parent / "backend" / "tests" / "parallel_plan.py"


def _load_plan():
    spec = importlib.util.spec_from_file_location("marker_parallel_plan", _PLAN_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _should_apply_plan(config: pytest.Config, plan) -> bool:
    invocation = getattr(config, "invocation_params", None)
    args = getattr(invocation, "args", ())
    predicate = getattr(plan, "should_auto_parallelize", None)
    return predicate is None or predicate(args, os.environ)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Select workers before xdist converts its options into worker specs."""
    if not config.pluginmanager.hasplugin("xdist"):
        return
    if getattr(config.option, "numprocesses", None) is not None:
        return

    plan = _load_plan()
    if plan is None or not _should_apply_plan(config, plan):
        return
    workers = plan.resolve_worker_count(os.environ)
    if workers is None:
        return

    config.option.numprocesses = workers
    if getattr(config.option, "dist", "no") in (None, "no"):
        config.option.dist = plan.DEFAULT_DISTRIBUTION


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Keep config state consistent for tools inspecting pytest config."""
    if not config.pluginmanager.hasplugin("xdist"):
        return
    plan = _load_plan()
    if (
        plan is None
        or getattr(config.option, "numprocesses", None) is not None
        or not _should_apply_plan(config, plan)
    ):
        return
    workers = plan.resolve_worker_count(os.environ)
    if workers is None:
        return
    config.option.numprocesses = workers
    if getattr(config.option, "dist", "no") in (None, "no"):
        config.option.dist = plan.DEFAULT_DISTRIBUTION
