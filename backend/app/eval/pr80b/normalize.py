"""Task-level value normalization for the PR80B benchmark.

The implementation lives in production
(:mod:`app.extraction.normalization`) so the benchmark's declared task
conventions and the hybrid corroboration proof instrument can never
drift apart. This module re-exports the single authority unchanged.
"""

from __future__ import annotations

from app.extraction.normalization import (  # noqa: F401  (re-export shim)
    NormResult,
    normalize_by_type,
    normalize_currency,
    normalize_date,
    normalize_decimal,
    normalize_integer,
    normalize_string,
)

__all__ = [
    "NormResult",
    "normalize_by_type",
    "normalize_currency",
    "normalize_date",
    "normalize_decimal",
    "normalize_integer",
    "normalize_string",
]
