from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from app.conversion.converters.archive import ArchiveConverter


def _write_zip(path: Path, entries: dict[str, bytes], *, compression: int = ZIP_STORED) -> None:
    with ZipFile(path, "w", compression=compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _base_config(**overrides):
    config = {
        "archive_recursive": True,
        "archive_max_files": 100,
        "archive_max_child_bytes": 1024 * 1024,
        "archive_max_depth": 2,
        "archive_max_converted_children": 25,
        "archive_max_total_uncompressed_bytes": 20 * 1024 * 1024,
        "archive_max_compression_ratio": 100.0,
    }
    config.update(overrides)
    return config


def test_nested_archives_share_total_uncompressed_budget(tmp_path: Path) -> None:
    first_inner = tmp_path / "first.zip"
    second_inner = tmp_path / "second.zip"
    _write_zip(first_inner, {"one.txt": b"one " * 10})
    _write_zip(second_inner, {"two.txt": b"two " * 10})

    first_bytes = first_inner.read_bytes()
    second_bytes = second_inner.read_bytes()
    outer = tmp_path / "outer.zip"
    _write_zip(outer, {"first.zip": first_bytes, "second.zip": second_bytes})

    max_total = len(first_bytes) + 40 + len(second_bytes) - 1
    result = ArchiveConverter().convert(
        str(outer),
        _base_config(archive_max_total_uncompressed_bytes=max_total),
    )

    manifest = result.metadata["engine_detail"]["manifest"]
    first_entry = next(item for item in manifest if item["path"] == "first.zip")
    second_entry = next(item for item in manifest if item["path"] == "second.zip")

    assert first_entry["action"] == "converted"
    assert second_entry["action"] == "skipped"
    assert second_entry["reason"] == "archive total uncompressed byte budget reached"
    assert "one one one" in result.text
    assert "two two two" not in result.text


def test_archive_skips_high_compression_ratio_child_before_reading(tmp_path: Path) -> None:
    archive = tmp_path / "bombish.zip"
    _write_zip(archive, {"repeated.txt": b"a" * 4096}, compression=ZIP_DEFLATED)

    result = ArchiveConverter().convert(
        str(archive),
        _base_config(archive_max_compression_ratio=2.0),
    )

    manifest = result.metadata["engine_detail"]["manifest"]
    entry = next(item for item in manifest if item["path"] == "repeated.txt")
    assert entry["action"] == "skipped"
    assert entry["reason"] == "archive child compression ratio exceeds limit"
