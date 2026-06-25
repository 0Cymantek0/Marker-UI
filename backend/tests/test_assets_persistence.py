"""UCM-004.4: UniversalConversionResult.assets must persist end-to-end.

Earlier converters returned non-image Asset sidecars (e.g. spreadsheet CSV
exports), but to_legacy_envelope() dropped them and the finalizer ignored
the field, so assets silently disappeared. These tests pin the envelope
contract and the synchronous convert_document asset-writing path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversion.result import Asset, UniversalConversionResult, asset_to_dict


def test_legacy_envelope_carries_assets_list():
    asset = Asset(name="sheets/Sheet1.csv", media_type="text/csv", data=b"a,b\n1,2\n")
    result = UniversalConversionResult(
        text="# doc",
        extension="md",
        assets=[asset],
    )

    envelope = result.to_legacy_envelope()

    assert envelope["text"] == "# doc"
    assert isinstance(envelope["assets"], list)
    assert envelope["assets"][0] == asset_to_dict(asset)
    assert envelope["assets"][0]["name"] == "sheets/Sheet1.csv"
    assert envelope["assets"][0]["data"] == b"a,b\n1,2\n"
    assert envelope["assets"][0]["media_type"] == "text/csv"


def test_asset_to_dict_preserves_bytes_and_marks_missing_data_as_none():
    with_bytes = Asset(name="a.csv", media_type="text/csv", data=b"x")
    without = Asset(name="b.csv", media_type="text/csv", data=None)

    assert asset_to_dict(with_bytes)["data"] == b"x"
    assert asset_to_dict(without)["data"] is None


@pytest.mark.asyncio
async def test_convert_document_writes_bytes_assets_to_disk(tmp_path: Path):
    """The synchronous convert path must persist bytes-backed assets alongside output."""
    from app.agent_api import AgentConversionOptions, convert_document

    source = tmp_path / "data.tsv"
    source.write_text("name\tscore\nalpha\t1\n", encoding="utf-8")

    def fake_convert_file(self, filepath, config, device=None):  # noqa: ANN001
        result = UniversalConversionResult(
            text="| name | score |\n| --- | --- |\n| alpha | 1 |",
            extension="md",
            metadata={"engine": {"engine": "text_data"}},
            assets=[
                Asset(name="export/rows.csv", media_type="text/csv", data=b"name,score\nalpha,1\n"),
            ],
        )
        return result.to_legacy_envelope()

    from app.services import conversion_service

    original = conversion_service.ConversionService.convert_file
    conversion_service.ConversionService.convert_file = fake_convert_file  # type: ignore[assignment]
    try:
        out = await convert_document(
            local_file_path=str(source),
            output_dir=str(tmp_path / "out"),
            max_chars=5000,
            options=AgentConversionOptions(output_format="markdown"),
        )
    finally:
        conversion_service.ConversionService.convert_file = original  # type: ignore[assignment]

    asset_paths = out["output"]["asset_paths"]
    assert asset_paths, "bytes-backed assets must be written and returned"
    csv_path = next(Path(p) for p in asset_paths if p.endswith("export/rows.csv") or p.endswith("export\\rows.csv"))
    assert csv_path.read_bytes() == b"name,score\nalpha,1\n"
