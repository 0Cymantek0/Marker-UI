"""Image downscaling for VLM cost control (plan §8).

The single largest non-obvious lever on cloud VLM spend is **input image
resolution**: every provider bills vision input by tiling the image, so a
full-resolution page crop can cost an order of magnitude more tokens than the
same crop scaled to fit a small tile band — with no measurable accuracy loss
for the chart/diagram/photo understanding job (it is *understanding*, not
pixel-exact OCR; the OCR path runs locally on the full-resolution crop).

This module shrinks a crop so its longest side fits ``max_px`` while preserving
aspect ratio. Images already within the cap are returned unchanged so we never
*upscale* (which would add cost and invent detail).

Pure ``PIL`` — no new dependency. Never raises: any failure returns the input
image untouched so a downscale problem never aborts a conversion.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def downscale_to_max(image: Any, max_px: int = 768) -> Any:
    """Return ``image`` scaled so its longest side is at most ``max_px``.

    Aspect ratio is preserved. Images already within the cap are returned
    unchanged (no upscaling). Returns the input untouched on any failure.
    """
    if image is None or max_px <= 0:
        return image
    try:
        size = getattr(image, "size", None)
        if not size or len(size) != 2:
            return image
        width, height = int(size[0]), int(size[1])
        longest = max(width, height)
        if longest <= max_px:
            return image

        scale = max_px / float(longest)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        resized = image.resize((new_w, new_h), _resample_lanczos())
        logger.debug(
            "downscale_to_max: %dx%d -> %dx%d (cap=%d)",
            width,
            height,
            new_w,
            new_h,
            max_px,
        )
        return resized
    except Exception as exc:  # noqa: BLE001 — downscale is best-effort
        logger.debug("downscale_to_max failed (%r); using original", exc)
        return image


def _resample_lanczos() -> Any:
    """Return the LANCZOS resample filter across Pillow versions."""
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS  # Pillow < 9.1 fallback.
