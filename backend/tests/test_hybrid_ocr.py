from __future__ import annotations

from pathlib import Path

import pytest

from app.hybrid_ocr.collector import collect_targets
from app.hybrid_ocr.orchestrator import HybridOcrOrchestrator
from app.hybrid_ocr.router import route_target
from app.hybrid_ocr.contracts import HybridEngine, TargetKind
from app.hybrid_ocr.validators import validate_formula, validate_table, validate_text


class Block:
    def __init__(self, block_type: str, text: str = "", confidence: float | None = None):
        self.block_type = block_type
        self.text = text
        self.confidence = confidence
        self.children: list[object] = []


class Page:
    def __init__(self, children: list[object]):
        self.children = children


class Document:
    def __init__(self, pages: list[Page]):
        self.pages = pages


def test_collect_targets_dedupes_and_classifies_specialist_blocks(tmp_path: Path):
    table = Block("Table", "| A | B |")
    duplicate_table = Block("Table", "| A | B |")
    formula = Block("Equation", "x^2")
    degraded = Block("Text", "bad scan", confidence=0.2)
    document = Document([Page([table, duplicate_table, formula, degraded])])

    targets = collect_targets(document, filepath="doc.pdf", job_dir=tmp_path)

    assert [target.target_kind for target in targets] == [
        TargetKind.TABLE,
        TargetKind.FORMULA,
        TargetKind.DEGRADED_TEXT,
    ]
    assert len({target.fingerprint for target in targets}) == 3


def test_router_keeps_specialists_internal():
    table_target = collect_targets(
        Document([Page([Block("Table", "| A | B |")])]),
        filepath="doc.pdf",
        job_dir=Path("."),
    )[0]
    degraded_target = collect_targets(
        Document([Page([Block("Text", "faint", confidence=0.1)])]),
        filepath="doc.pdf",
        job_dir=Path("."),
    )[0]

    assert route_target(table_target)[:2] == [HybridEngine.GLM_OCR, HybridEngine.PADDLEOCR_VL]
    assert route_target(degraded_target)[:2] == [HybridEngine.PADDLEOCR_VL, HybridEngine.GLM_OCR]


def test_validators_reject_hidden_bad_outputs():
    assert validate_text("good extracted text", baseline_text="good").accepted
    assert not validate_text("bad bad bad bad bad bad bad").accepted
    assert validate_table("| A | B |\n|---|---|\n| 1 | 2 |").accepted
    assert not validate_table("This is prose, not a table").accepted
    assert validate_formula(r"\frac{x}{y} + z").accepted
    assert not validate_formula(r"\frac{x}{y").accepted


def test_orchestrator_accepts_valid_mock_result_and_replaces_block(monkeypatch):
    monkeypatch.setenv("MARKER_GLM_PYTHON", "python")
    table = Block("Table", "| old | value |")
    document = Document([Page([table])])

    out_doc, meta = HybridOcrOrchestrator().refine(
        document=document,
        filepath="doc.pdf",
        options={
            "ocr_engine": "hybrid_ocr",
            "hybrid_ocr_mock_results": {
                "p1_table_01": {
                    "engine": "glm_ocr",
                    "markdown": "| New | Value |\n|---|---|\n| 7 | 9 |",
                    "replacement_policy": "replace_block",
                }
            },
        },
    )

    assert out_doc is document
    assert table.text == "| New | Value |\n|---|---|\n| 7 | 9 |"
    assert meta["local_only"] is True
    assert meta["targets_total"] == 1
    assert meta["specialist_results"]["accepted"] == 1
    assert meta["specialist_results"]["replacements"] == 1
    assert meta["engines_used"] == ["glm_ocr"]


def test_orchestrator_missing_specialists_fails_soft_without_cloud(monkeypatch):
    monkeypatch.delenv("MARKER_GLM_PYTHON", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_PYTHON", raising=False)
    table = Block("Table", "| old | value |")
    document = Document([Page([table])])

    _doc, meta = HybridOcrOrchestrator().refine(
        document=document,
        filepath="doc.pdf",
        options={"ocr_engine": "hybrid_ocr"},
    )

    assert table.text == "| old | value |"
    assert meta["specialist_results"]["skipped_missing_engine"] == 1
    assert meta["engines_used"] == []
    assert any("Surya baseline kept" in warning for warning in meta["warnings"])


def test_orchestrator_can_require_specialists(monkeypatch):
    monkeypatch.delenv("MARKER_GLM_PYTHON", raising=False)
    table = Block("Table", "| old | value |")
    document = Document([Page([table])])

    with pytest.raises(RuntimeError, match="required but unavailable"):
        HybridOcrOrchestrator().refine(
            document=document,
            filepath="doc.pdf",
            options={"ocr_engine": "hybrid_ocr", "hybrid_ocr_require_specialists": True},
        )


def test_hybrid_package_has_no_vlm_service_dependency():
    package_root = Path(__file__).resolve().parents[1] / "app" / "hybrid_ocr"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in package_root.rglob("*.py"))
    assert "app.services.vlm_service" not in combined
    assert "VLMService" not in combined
