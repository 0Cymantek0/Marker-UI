"""Tests for ConverterRegistry — registration, priority ordering, lookup."""

from __future__ import annotations

from typing import Any

import pytest

from app.conversion.registry import BaseConverter, ConverterRegistry
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


class _StubConverter(BaseConverter):
    """Minimal concrete converter for registry tests."""

    def __init__(self, engine: str, priority: int = 0, extensions: frozenset[str] | None = None) -> None:
        self.engine_name = engine
        self.priority = priority
        self._extensions = extensions or frozenset()

    @property
    def supported_extensions(self) -> frozenset[str]:
        return self._extensions

    def convert(self, filepath: str, config: dict[str, Any]) -> UniversalConversionResult:
        return UniversalConversionResult(text="stub", extension="md")


class TestConverterRegistry:
    """Registry behavior: registration, priority, lookup, listing."""

    def test_register_and_get(self) -> None:
        """Registered converter is retrievable by engine name."""
        reg = ConverterRegistry()
        conv = _StubConverter("test_engine")
        reg.register(conv)

        assert reg.get("test_engine") is conv
        assert reg.has("test_engine")

    def test_get_unregistered_returns_none(self) -> None:
        """Unregistered engine returns None."""
        reg = ConverterRegistry()
        assert reg.get("nonexistent") is None
        assert not reg.has("nonexistent")

    def test_higher_priority_wins(self) -> None:
        """Higher priority converter replaces lower for same engine."""
        reg = ConverterRegistry()
        low = _StubConverter("engine_a", priority=10)
        high = _StubConverter("engine_a", priority=100)

        reg.register(low)
        reg.register(high)

        assert reg.get("engine_a") is high

    def test_lower_priority_does_not_replace(self) -> None:
        """Lower priority converter does NOT replace higher for same engine."""
        reg = ConverterRegistry()
        high = _StubConverter("engine_a", priority=100)
        low = _StubConverter("engine_a", priority=10)

        reg.register(high)
        reg.register(low)

        assert reg.get("engine_a") is high

    def test_equal_priority_replaces(self) -> None:
        """Equal priority converter does NOT replace (uses > not >=)."""
        reg = ConverterRegistry()
        first = _StubConverter("engine_a", priority=50)
        second = _StubConverter("engine_a", priority=50)

        reg.register(first)
        reg.register(second)

        # Equal priority: first wins (> not >=)
        assert reg.get("engine_a") is first

    def test_engine_names_sorted(self) -> None:
        """engine_names returns sorted list of registered engine names."""
        reg = ConverterRegistry()
        reg.register(_StubConverter("zebra"))
        reg.register(_StubConverter("alpha"))
        reg.register(_StubConverter("middle"))

        assert reg.engine_names == ["alpha", "middle", "zebra"]

    def test_multiple_engines_independent(self) -> None:
        """Different engine names coexist independently."""
        reg = ConverterRegistry()
        a = _StubConverter("engine_a", priority=10)
        b = _StubConverter("engine_b", priority=20)

        reg.register(a)
        reg.register(b)

        assert reg.get("engine_a") is a
        assert reg.get("engine_b") is b
        assert len(reg.engine_names) == 2

    def test_converters_list_tracks_all(self) -> None:
        """converters property includes all registered, even duplicates."""
        reg = ConverterRegistry()
        c1 = _StubConverter("engine_a", priority=10)
        c2 = _StubConverter("engine_a", priority=100)

        reg.register(c1)
        reg.register(c2)

        assert len(reg.converters) == 2
        assert c1 in reg.converters
        assert c2 in reg.converters

    def test_accepts_delegates_to_supported_extensions(self) -> None:
        """BaseConverter.accepts checks supported_extensions by default."""
        conv = _StubConverter("test", extensions=frozenset({".pdf", ".docx"}))
        pdf_info = StreamInfo(path="/f.pdf", extension=".pdf", mime_type="", size=0, sample=b"")
        txt_info = StreamInfo(path="/f.txt", extension=".txt", mime_type="", size=0, sample=b"")

        assert conv.accepts(pdf_info, {}) is True
        assert conv.accepts(txt_info, {}) is False
