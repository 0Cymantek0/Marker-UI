"""Perceptual image hashing for dedup (plan §8a).

A document often repeats the same figure — a logo in every header, a recurring
diagram, the same chart on a summary page. Sending each copy to the VLM is pure
waste. This module computes a cheap **average hash** (aHash) so identical (and
near-identical) images collapse to one extraction that is then fanned back out
to every duplicate block.

Pure ``PIL`` + ``numpy`` — no new dependency. The hash is a 64-bit fingerprint
rendered as a 16-char hex string. :func:`hamming_distance` lets callers tune how
near "near-identical" is; the processor uses distance ``0`` (exact aHash match)
by default to avoid ever collapsing two genuinely different figures.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_HASH_SIDE = 8  # 8x8 -> 64-bit hash.


def average_hash(image: Any, hash_side: int = _HASH_SIDE) -> str | None:
    """Compute the aHash of a PIL image as a hex string. None on failure.

    Steps: convert to grayscale, resize to ``hash_side x hash_side``, threshold
    each pixel against the mean, pack the bits big-endian into hex.
    """
    if image is None:
        return None
    try:
        import numpy as np

        gray = image.convert("L").resize(
            (hash_side, hash_side), _resample_lanczos()
        )
        arr = np.asarray(gray, dtype="float64")
        if arr.size == 0:
            return None
        mean = arr.mean()
        bits = (arr > mean).flatten()
        value = 0
        for bit in bits:
            value = (value << 1) | int(bool(bit))
        width = (hash_side * hash_side + 3) // 4
        return format(value, f"0{width}x")
    except Exception as exc:  # noqa: BLE001 — dedup is best-effort, never fatal
        logger.debug("average_hash failed: %r", exc)
        return None


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Number of differing bits between two equal-length hex hashes.

    Returns a large sentinel (``9999``) when the inputs are malformed or differ
    in length, so a caller comparing against a small threshold treats them as
    "not a match".
    """
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return 9999
    try:
        return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")
    except ValueError:
        return 9999


def _resample_lanczos() -> Any:
    """Return the LANCZOS resample filter across Pillow versions."""
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS  # Pillow < 9.1 fallback.
