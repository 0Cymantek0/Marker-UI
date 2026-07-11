"""Converter registry — BaseConverter ABC and ConverterRegistry.

Every format-specific converter inherits ``BaseConverter`` and registers
itself with a ``ConverterRegistry``.  The registry selects the best
converter for a given engine name, respecting priority ordering.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.conversion.native_requirements import NativeRequirement
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo

logger = logging.getLogger(__name__)


class BaseConverter(ABC):
    """Interface every format converter implements.

    Subclasses declare which engines they handle (``engine_name``), whether
    they need marker models, and their priority.  The registry calls
    ``accepts`` to confirm, then ``convert``.
    """

    # Unique engine identifier — matches ``ConverterPlan.engine``.
    engine_name: str = ""

    # Higher priority wins when multiple converters claim the same engine.
    priority: int = 0

    # Whether this converter loads Marker/Surya models.
    requires_marker_models: bool = False

    # Whether this converter needs GPU.
    requires_gpu: bool = False

    # Native system binaries the converter needs at runtime (e.g. ffmpeg).
    # Empty tuple means no native deps. Declared at class level so the
    # capability endpoint can introspect without instantiating the converter.
    native_requirements: tuple[NativeRequirement, ...] = ()

    def runtime_ready(self) -> bool:
        """True when every ``native_requirements`` entry is present and version-OK."""
        return all(req.resolve()["available"] for req in self.native_requirements)

    def missing_requirements(self) -> list[dict[str, Any]]:
        """Structured list of native deps that are missing or wrong-version."""
        return [
            req.resolve()
            for req in self.native_requirements
            if not req.resolve()["available"]
        ]

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        """Quick check: can this converter handle the file?

        The default implementation checks the extension against
        ``supported_extensions``.  Subclasses may override for richer sniffing
        (e.g. magic bytes).

        Must be cheap — no heavy imports, no full file reads.
        """
        return stream_info.extension in self.supported_extensions

    @property
    def supported_extensions(self) -> frozenset[str]:
        """Extensions this converter handles (with dot, lower-cased)."""
        return frozenset()

    @abstractmethod
    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        """Run the conversion and return a structured result."""
        ...


class ConverterRegistry:
    """Holds registered converters and selects by engine name.

    Converters are registered at startup.  Selection is by engine name
    (from the router's ``ConverterPlan``), breaking ties by priority.
    """

    def __init__(self) -> None:
        self._converters: dict[str, BaseConverter] = {}
        self._all: list[BaseConverter] = []

    def register(self, converter: BaseConverter) -> None:
        """Register a converter.  Higher priority replaces lower for same engine."""
        existing = self._converters.get(converter.engine_name)
        if existing is None or converter.priority > existing.priority:
            self._converters[converter.engine_name] = converter
        self._all.append(converter)
        logger.debug(
            "Registered converter %s (engine=%s, priority=%d)",
            type(converter).__name__,
            converter.engine_name,
            converter.priority,
        )

    def get(self, engine_name: str) -> BaseConverter | None:
        """Return the highest-priority converter for *engine_name*, or None."""
        return self._converters.get(engine_name)

    def unregister(self, engine_name: str) -> None:
        """Unregister a converter for testing fallback scenarios."""
        if engine_name in self._converters:
            self._converters.pop(engine_name)
        self._all = [c for c in self._all if c.engine_name != engine_name]

    def has(self, engine_name: str) -> bool:
        """True if a converter is registered for *engine_name*."""
        return engine_name in self._converters

    @property
    def engine_names(self) -> list[str]:
        """All registered engine names, sorted alphabetically."""
        return sorted(self._converters.keys())

    @property
    def converters(self) -> list[BaseConverter]:
        """All registered converters (may include duplicates for same engine)."""
        return list(self._all)
