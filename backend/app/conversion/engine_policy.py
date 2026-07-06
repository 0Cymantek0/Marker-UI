"""Shared validation for explicit conversion engine overrides."""

from __future__ import annotations

from typing import Any

from app.conversion.formats import INPUT_FORMATS
from app.errors import UsageError


ENGINE_COMPATIBLE_EXTENSIONS = {
    engine: frozenset(
        ext
        for spec in INPUT_FORMATS
        if spec.engine == engine
        for ext in spec.extensions
    )
    for engine in sorted({spec.engine for spec in INPUT_FORMATS})
}
ENGINE_COMPATIBLE_EXTENSIONS["liteparse_pdf"] = frozenset({".pdf"})


def validate_engine_override(config: dict[str, Any], suffix: str) -> None:
    """Reject unknown or incompatible engine overrides.

    ``auto`` is a UI sentinel, not a backend engine. Removing it keeps callers
    that submit the sentinel compatible with the normal automatic router.
    """

    engine = str(config.get("engine_override") or "").strip()
    if not engine:
        return
    if engine == "auto":
        config.pop("engine_override", None)
        return

    extension = suffix.lower()
    compatible = ENGINE_COMPATIBLE_EXTENSIONS.get(engine)
    if compatible is None:
        raise UsageError(
            f"Unknown engine_override '{engine}'.",
            details={
                "engine_override": engine,
                "known_engines": sorted(ENGINE_COMPATIBLE_EXTENSIONS),
            },
        )
    if extension not in compatible:
        raise UsageError(
            f"engine_override '{engine}' is incompatible with extension '{extension or '<none>'}'.",
            details={
                "engine_override": engine,
                "extension": extension,
                "compatible_extensions": sorted(compatible),
            },
        )
