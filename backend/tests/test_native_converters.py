"""Tests for native non-Marker converters."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from openpyxl import Workbook

from app.conversion.converters.archive import ArchiveConverter
from app.conversion.converters.html import HtmlConverter
from app.conversion.converters.notebook import NotebookConverter
from app.conversion.converters.spreadsheet import SpreadsheetConverter
from app.conversion.converters.text_data import TextDataConverter
from app.conversion.converters.xml_rss import XmlRssConverter
from app.services.conversion_service import ConversionService


class _FakeMarkerService:
    def convert_file(self, filepath, options, device=None):
        return {"text": "marker fallback", "extension": "md", "images": {}, "metadata": {}}


def test_service_registers_every_advertised_native_engine() -> None:
    svc = ConversionService(_FakeMarkerService())

    for engine in [
        "archive",
        "html",
        "notebook",
        "spreadsheet",
        "text_data",
        "xml_rss",
    ]:
        assert svc.registry.has(engine)


def test_text_data_converter_turns_csv_into_markdown_table(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,score\nAda,10\nLinus,9\n", encoding="utf-8")

    result = TextDataConverter().convert(str(path), {})

    assert "| name | score |" in result.text
    assert "| Ada | 10 |" in result.text
    assert result.metadata["engine_detail"]["format"] == "csv"


def test_html_converter_drops_scripts_and_emits_markdown(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><title>Demo</title><script>bad()</script></head>"
        "<body><h1>Hello</h1><p><strong>World</strong></p></body></html>",
        encoding="utf-8",
    )

    result = HtmlConverter().convert(str(path), {})

    assert "# Hello" in result.text
    assert "**World**" in result.text
    assert "bad()" not in result.text


def test_xml_rss_converter_reads_feed_items(tmp_path: Path) -> None:
    path = tmp_path / "feed.rss"
    path.write_text(
        "<rss><channel><title>News</title><item><title>Item A</title>"
        "<link>https://example.com/a</link><description>Body</description>"
        "</item></channel></rss>",
        encoding="utf-8",
    )

    result = XmlRssConverter().convert(str(path), {})

    assert "# News" in result.text
    assert "## Item A" in result.text
    assert "https://example.com/a" in result.text


def test_notebook_converter_preserves_markdown_code_and_output(tmp_path: Path) -> None:
    path = tmp_path / "analysis.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["## Intro\n"]},
                    {
                        "cell_type": "code",
                        "source": ["print('ok')\n"],
                        "outputs": [{"text": ["ok\n"]}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = NotebookConverter().convert(str(path), {})

    assert "## Intro" in result.text
    assert "```python\nprint('ok')" in result.text
    assert "ok" in result.text


def test_spreadsheet_converter_reads_xlsx_sheets(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Scores"
    ws.append(["name", "score"])
    ws.append(["Ada", 10])
    wb.save(path)

    result = SpreadsheetConverter().convert(str(path), {})

    assert "## Sheet: Scores" in result.text
    assert "| name | score |" in result.text
    assert "| Ada | 10 |" in result.text


def test_archive_converter_lists_zip_without_extracting(tmp_path: Path) -> None:
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("notes/readme.txt", "hello archive")
        zf.writestr("../sneaky.txt", "bad path")

    result = ArchiveConverter().convert(str(path), {})

    assert "`notes/readme.txt`" in result.text
    assert "hello archive" in result.text
    assert "suspicious-name" in result.text
