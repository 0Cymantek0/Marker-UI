from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import app.conversion.converters.liteparse_pdf as liteparse_pdf
from app.conversion.converters.liteparse_pdf import LiteParsePdfConverter


def _write_text_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawString(72, 720, "LiteParse page one clean digital text")
    c.drawString(72, 700, "Revenue 100 Cost 40")
    c.showPage()
    c.drawString(72, 720, "LiteParse page two should be filtered")
    c.showPage()
    c.save()


def test_liteparse_cli_uses_no_ocr_and_target_pages(tmp_path, monkeypatch) -> None:
    pdf_path = tmp_path / "clean.pdf"
    _write_text_pdf(pdf_path)
    seen: dict[str, list[str]] = {}

    monkeypatch.setattr(liteparse_pdf, "_find_lit_executable", lambda: "lit")

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = list(cmd)
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.write_text("cli output", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(liteparse_pdf.subprocess, "run", fake_run)

    result = LiteParsePdfConverter().convert(str(pdf_path), {"page_range": "1"})

    assert result.text == "cli output"
    assert "--no-ocr" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--target-pages") + 1] == "1"
    assert result.metadata["liteparse"]["ocr_enabled"] is False
    assert result.metadata["liteparse"]["execution_mode"] == "cli"


def test_liteparse_python_api_fallback_runs_real_clean_pdf(tmp_path, monkeypatch) -> None:
    pdf_path = tmp_path / "clean.pdf"
    _write_text_pdf(pdf_path)
    monkeypatch.setattr(liteparse_pdf, "_find_lit_executable", lambda: None)

    result = LiteParsePdfConverter().convert(str(pdf_path), {"page_range": "1"})

    assert "LiteParse page one clean digital text" in result.text
    assert "Revenue 100 Cost 40" in result.text
    assert "page two should be filtered" not in result.text
    assert result.metadata["liteparse"] == {
        "ocr_enabled": False,
        "image_mode": "off",
        "execution_mode": "python_api",
    }
