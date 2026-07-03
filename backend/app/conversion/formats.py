"""Canonical output format constants shared across API surfaces."""

from __future__ import annotations

from typing import Literal


OutputFormat = Literal["markdown", "json", "html", "chunks"]

OUTPUT_FORMATS: tuple[OutputFormat, ...] = ("markdown", "json", "html", "chunks")
OUTPUT_FORMAT_SET = frozenset(OUTPUT_FORMATS)
OUTPUT_FORMATS_DESCRIPTION = ", ".join(OUTPUT_FORMATS)
