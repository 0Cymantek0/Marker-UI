from __future__ import annotations

from pathlib import Path
import sys

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


def test_orchestrator_runs_configured_glm_worker_and_replaces_block(monkeypatch, tmp_path: Path):
    model_dir = tmp_path / "glm-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    worker = tmp_path / "glm_worker.py"
    worker.write_text(
        """
import json
import sys
from pathlib import Path

request_path = Path(sys.argv[sys.argv.index('--request') + 1])
response_path = Path(sys.argv[sys.argv.index('--response') + 1])
request = json.loads(request_path.read_text(encoding='utf-8'))
response = {
    'results': [
        {
            'target_id': request['targets'][0]['target_id'],
            'status': 'ok',
            'markdown': '| New | Value |\\n|---|---|\\n| 7 | 9 |',
            'replacement_policy': 'replace_block',
        }
    ]
}
response_path.write_text(json.dumps(response), encoding='utf-8')
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKER_GLM_OCR_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("MARKER_GLM_OCR_COMMAND", f"{sys.executable} {worker}")
    table = Block("Table", "| old | value |")
    document = Document([Page([table])])

    out_doc, meta = HybridOcrOrchestrator().refine(
        document=document,
        filepath="doc.pdf",
        options={"ocr_engine": "hybrid_ocr"},
    )

    assert out_doc is document
    assert table.text == "| New | Value |\n|---|---|\n| 7 | 9 |"
    assert meta["local_only"] is True
    assert meta["targets_total"] == 1
    assert meta["specialist_results"]["accepted"] == 1
    assert meta["specialist_results"]["replacements"] == 1
    assert meta["engines_used"] == ["glm_ocr"]


def test_orchestrator_missing_specialists_fails_soft_without_cloud(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MARKER_GLM_PYTHON", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_COMMAND", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_MODEL_DIR", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_PYTHON", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_COMMAND", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_MODEL_DIR", raising=False)
    monkeypatch.setenv("MARKER_HYBRID_OCR_MODEL_ROOT", str(tmp_path / "empty-model-root"))
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


def test_orchestrator_can_require_specialists(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MARKER_GLM_PYTHON", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_COMMAND", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_MODEL_DIR", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_COMMAND", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_MODEL_DIR", raising=False)
    monkeypatch.setenv("MARKER_HYBRID_OCR_MODEL_ROOT", str(tmp_path / "empty-model-root"))
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


def test_hybrid_setup_status_reports_model_presence(monkeypatch, tmp_path: Path):
    from app.hybrid_ocr.setup import hybrid_setup_status

    model_dir = tmp_path / "paddle-model"
    model_dir.mkdir()
    (model_dir / "README.md").write_text("model", encoding="utf-8")
    monkeypatch.setenv("MARKER_PADDLE_OCR_VL_MODEL_DIR", str(model_dir))

    status = hybrid_setup_status()

    assert status["engines"]["paddleocr_vl"]["model_present"] is True
    assert status["engines"]["paddleocr_vl"]["model_dir"] == str(model_dir)


def test_capability_detects_native_transformers_runtime(monkeypatch, tmp_path: Path):
    from app.hybrid_ocr.capability import detect_capabilities

    glm_dir = tmp_path / "glm_ocr"
    glm_dir.mkdir()
    (glm_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MARKER_HYBRID_OCR_MODEL_ROOT", str(tmp_path))
    monkeypatch.setenv("MARKER_HYBRID_OCR_ENABLE_NATIVE_TRANSFORMERS", "true")
    monkeypatch.delenv("MARKER_GLM_OCR_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_GLM_OCR_COMMAND", raising=False)

    caps = detect_capabilities()

    assert HybridEngine.GLM_OCR in caps.available


def test_capability_detects_paddle_native_runtime_without_extra_env(monkeypatch, tmp_path: Path):
    from app.hybrid_ocr.capability import detect_capabilities

    paddle_dir = tmp_path / "paddleocr_vl"
    paddle_dir.mkdir()
    (paddle_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MARKER_HYBRID_OCR_MODEL_ROOT", str(tmp_path))
    monkeypatch.delenv("MARKER_HYBRID_OCR_ENABLE_NATIVE_TRANSFORMERS", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_ENDPOINT", raising=False)
    monkeypatch.delenv("MARKER_PADDLE_OCR_VL_COMMAND", raising=False)

    caps = detect_capabilities()

    assert HybridEngine.PADDLEOCR_VL in caps.available


def test_download_model_snapshot_uses_huggingface_snapshot(monkeypatch, tmp_path: Path):
    from app.hybrid_ocr import setup as hybrid_setup

    calls: list[dict[str, str]] = []

    def fake_snapshot_download(**kwargs):
        calls.append(kwargs)
        Path(kwargs["local_dir"]).mkdir(parents=True, exist_ok=True)
        Path(kwargs["local_dir"], "model.bin").write_bytes(b"1")

    monkeypatch.setenv("MARKER_HYBRID_OCR_MODEL_ROOT", str(tmp_path))
    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot_download)

    result = hybrid_setup.download_model_snapshot("glm_ocr")

    assert result["status"] == "downloaded"
    assert calls[0]["repo_id"] == "zai-org/GLM-OCR"
    assert Path(result["path"], "model.bin").exists()


def test_image_ocr_engine_uses_hybrid_specialist(monkeypatch):
    from PIL import Image

    from app.hybrid_ocr.contracts import HybridEngine
    from app.hybrid_ocr.contracts import HybridResult, ReplacementPolicy
    from app.hybrid_ocr.validators import validate_text
    from app.services import ocr_engine as ocr_engine_mod

    class DummyFallback:
        def __init__(self, **kwargs):
            pass

        @property
        def available(self):
            return False

        def recognize(self, image):
            raise AssertionError("Surya fallback should not run when Paddle is available")

    class Caps:
        available = {HybridEngine.PADDLEOCR_VL}

        def is_available(self, engine):
            return engine in self.available

    calls = []

    def fake_worker(engine, targets, timeout_s):
        calls.append((engine, targets, timeout_s))
        return [
            HybridResult(
                target_id=targets[0].target_id,
                engine=engine,
                status="ok",
                output_kind=targets[0].target_kind,
                text="hybrid text",
                markdown="hybrid text",
                html="",
                json_payload={},
                confidence=0.9,
                duration_ms=12,
                validation=validate_text("hybrid text"),
                replacement_policy=ReplacementPolicy.REPLACE_BLOCK,
            )
        ]

    monkeypatch.setattr(ocr_engine_mod, "SuryaOCREngine", DummyFallback)
    monkeypatch.setattr("app.hybrid_ocr.capability.detect_capabilities", lambda: Caps())
    monkeypatch.setattr("app.hybrid_ocr.adapters.run_specialist_worker", fake_worker)

    engine = ocr_engine_mod.build_ocr_engine("hybrid_ocr")
    result = engine.recognize(Image.new("RGB", (64, 32), "white"))

    assert result.text == "hybrid text"
    assert result.details["engine"] == "paddleocr_vl"
    assert calls[0][0] == HybridEngine.PADDLEOCR_VL
