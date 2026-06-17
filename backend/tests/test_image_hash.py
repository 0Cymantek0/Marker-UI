"""Unit tests for the aHash dedup helper (plan §8a)."""

from __future__ import annotations

from PIL import Image

from app.utils.image_hash import average_hash, hamming_distance


def test_identical_images_hash_equal():
    a = Image.new("RGB", (40, 40), color=(120, 30, 200))
    b = Image.new("RGB", (40, 40), color=(120, 30, 200))
    assert average_hash(a) == average_hash(b)
    assert hamming_distance(average_hash(a), average_hash(b)) == 0


def test_hash_is_16_hex_chars_for_8x8():
    h = average_hash(Image.new("L", (8, 8), color=128))
    assert isinstance(h, str)
    assert len(h) == 16
    int(h, 16)  # parses as hex


def test_different_images_differ():
    # A half-black/half-white image vs a uniform one -> different fingerprints.
    split = Image.new("L", (16, 16), color=0)
    for x in range(16):
        for y in range(8):
            split.putpixel((x, y), 255)
    uniform = Image.new("L", (16, 16), color=128)
    assert average_hash(split) != average_hash(uniform)


def test_hash_none_on_bad_input():
    assert average_hash(None) is None


def test_hamming_distance_mismatched_length_is_sentinel():
    assert hamming_distance("ff", "ffff") == 9999
    assert hamming_distance("", "ff") == 9999


def test_hamming_distance_counts_bits():
    # 0x0 vs 0xF -> 4 differing bits.
    assert hamming_distance("0", "f") == 4
