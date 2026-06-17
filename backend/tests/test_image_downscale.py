"""Unit tests for VLM-crop downscaling (plan §8 cost lever)."""

from __future__ import annotations

from PIL import Image

from app.utils.image_downscale import downscale_to_max


def test_downscales_large_image_preserving_aspect():
    img = Image.new("RGB", (2000, 1000), color="white")
    out = downscale_to_max(img, max_px=768)
    assert max(out.size) == 768
    # Aspect ratio preserved (2:1).
    assert out.size == (768, 384)


def test_does_not_upscale_small_image():
    img = Image.new("RGB", (100, 80), color="white")
    out = downscale_to_max(img, max_px=768)
    assert out.size == (100, 80)  # unchanged, identity


def test_tall_image_capped_on_height():
    img = Image.new("RGB", (400, 1600), color="white")
    out = downscale_to_max(img, max_px=800)
    assert max(out.size) == 800
    assert out.size == (200, 800)


def test_none_returns_none():
    assert downscale_to_max(None, max_px=768) is None


def test_zero_cap_returns_input_untouched():
    img = Image.new("RGB", (500, 500), color="white")
    assert downscale_to_max(img, max_px=0) is img
