"""Compatibility tests verifying displacement facade, package exports, and cycle absence."""

from __future__ import annotations

import importlib
import sys


def test_displacement_facade_and_package_compatibility_exports():
    """Verify all 62 public symbols are identical between facade and package."""
    import app.eval.accountability.displacement as pkg
    import app.eval.accountability.displacement_decision as facade

    assert hasattr(pkg, "__all__"), "displacement package must define __all__"
    assert hasattr(facade, "__all__"), (
        "displacement_decision facade must define __all__"
    )
    assert pkg.__all__ == facade.__all__, "__all__ must be identical"

    assert len(pkg.__all__) == 62, (
        f"Expected 62 public exports, found {len(pkg.__all__)}"
    )

    for sym in pkg.__all__:
        pkg_val = getattr(pkg, sym)
        facade_val = getattr(facade, sym)
        assert pkg_val is facade_val, f"Export {sym!r} object identity mismatch"


def test_displacement_accountability_package_reexports():
    """Verify all displacement symbols are re-exported by app.eval.accountability."""
    import app.eval.accountability as acc
    import app.eval.accountability.displacement as pkg

    for sym in pkg.__all__:
        assert hasattr(acc, sym), f"Symbol {sym!r} missing from app.eval.accountability"
        assert getattr(acc, sym) is getattr(pkg, sym), (
            f"Symbol {sym!r} object mismatch in accountability"
        )
        assert sym in acc.__all__, f"Symbol {sym!r} missing from accountability.__all__"


def test_displacement_import_cycle_absence():
    """Verify no circular import issues when importing submodules in any order."""
    modules_to_test = [
        "app.eval.accountability.displacement.contracts",
        "app.eval.accountability.displacement.validation",
        "app.eval.accountability.displacement.engine",
        "app.eval.accountability.displacement.pr80b_adapter",
        "app.eval.accountability.displacement",
        "app.eval.accountability.displacement_decision",
        "app.eval.accountability",
    ]

    for mod_name in modules_to_test:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Test fresh imports in various orders
    mod_val = importlib.import_module("app.eval.accountability.displacement.validation")
    assert mod_val is not None

    mod_eng = importlib.import_module("app.eval.accountability.displacement.engine")
    assert mod_eng is not None

    mod_pr80b = importlib.import_module(
        "app.eval.accountability.displacement.pr80b_adapter"
    )
    assert mod_pr80b is not None

    mod_contracts = importlib.import_module(
        "app.eval.accountability.displacement.contracts"
    )
    assert mod_contracts is not None

    mod_pkg = importlib.import_module("app.eval.accountability.displacement")
    assert mod_pkg is not None

    mod_facade = importlib.import_module(
        "app.eval.accountability.displacement_decision"
    )
    assert mod_facade is not None
