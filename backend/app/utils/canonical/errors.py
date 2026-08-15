"""Error type for the canonical identity layer.

Every rejection in this package raises :class:`CanonicalValueError` so
callers get one explicit, catchable failure instead of opportunistic
stringification of values that must never enter an identity preimage.
"""

from __future__ import annotations


class CanonicalValueError(ValueError):
    """An identity-bearing value violated the canonicalization contract."""
