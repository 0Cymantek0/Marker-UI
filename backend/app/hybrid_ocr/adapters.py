"""Subprocess adapters for local Hybrid OCR specialists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.hybrid_ocr.contracts import (
    HybridEngine,
    HybridResult,
    HybridTarget,
    ReplacementPolicy,
)
from app.hybrid_ocr.validators import validate_for_kind


def run_specialist_worker(
    engine: HybridEngine,
    targets: list[HybridTarget],
    *,
    timeout_s: float,
) -> list[HybridResult]:
    if engine == HybridEngine.SURYA or not targets:
        return []
    worker_python = _worker_python(engine)
    module = {
        HybridEngine.GLM_OCR: "app.hybrid_ocr.workers.glm_worker",
        HybridEngine.PADDLEOCR_VL: "app.hybrid_ocr.workers.paddle_worker",
    }[engine]
    with tempfile.TemporaryDirectory(prefix=f"marker-{engine.value}-") as temp_dir:
        request_path = Path(temp_dir) / "request.json"
        response_path = Path(temp_dir) / "response.json"
        request_path.write_text(
            json.dumps({"engine": engine.value, "targets": [_target_payload(target) for target in targets]}, ensure_ascii=False),
            encoding="utf-8",
        )
        started = time.perf_counter()
        env = os.environ.copy()
        backend_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = backend_root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [worker_python, "-m", module, "--request", str(request_path), "--response", str(response_path)],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            return [
                _failed_result(
                    target,
                    engine,
                    f"Worker failed with exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}",
                    int((time.perf_counter() - started) * 1000),
                )
                for target in targets
            ]
        if not response_path.exists():
            return [
                _failed_result(target, engine, "Worker did not write response.json", int((time.perf_counter() - started) * 1000))
                for target in targets
            ]
        payload = json.loads(response_path.read_text(encoding="utf-8"))
        return _coerce_worker_results(payload, targets, engine)


def _worker_python(engine: HybridEngine) -> str:
    if engine == HybridEngine.GLM_OCR:
        return os.environ.get("MARKER_GLM_PYTHON") or sys.executable
    if engine == HybridEngine.PADDLEOCR_VL:
        return os.environ.get("MARKER_PADDLE_PYTHON") or sys.executable
    return sys.executable


def _target_payload(target: HybridTarget) -> dict[str, Any]:
    return {
        "target_id": target.target_id,
        "page": target.page_number,
        "kind": target.target_kind.value,
        "block_type": target.block_type,
        "crop_path": target.crop_path,
        "baseline_text": target.baseline_text,
        "baseline_html": target.baseline_html,
        "bbox": target.bbox,
        "route_hints": target.route_hints,
    }


def _coerce_worker_results(payload: dict[str, Any], targets: list[HybridTarget], engine: HybridEngine) -> list[HybridResult]:
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        return [_failed_result(target, engine, "Worker response missing results list", 0) for target in targets]
    by_target = {target.target_id: target for target in targets}
    out: list[HybridResult] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        target = by_target.get(str(raw.get("target_id") or ""))
        if target is None:
            continue
        text = str(raw.get("text") or "")
        markdown = str(raw.get("markdown") or text)
        validation = validate_for_kind(target.target_kind, text, markdown, baseline_text=target.baseline_text)
        status = str(raw.get("status") or "ok")
        policy = raw.get("replacement_policy") or ReplacementPolicy.REPLACE_BLOCK
        out.append(
            HybridResult(
                target_id=target.target_id,
                engine=engine,
                status=status,
                output_kind=target.target_kind,
                text=text,
                markdown=markdown,
                html=str(raw.get("html") or ""),
                json_payload=raw.get("json_payload") if isinstance(raw.get("json_payload"), dict) else {},
                confidence=raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None,
                duration_ms=int(raw.get("duration_ms") or 0),
                validation=validation,
                replacement_policy=ReplacementPolicy(policy),
                warnings=list(raw.get("warnings") or []),
                error=raw.get("error"),
            )
        )
    missing = set(by_target) - {result.target_id for result in out}
    out.extend(_failed_result(by_target[target_id], engine, "Worker omitted target result", 0) for target_id in missing)
    return out


def _failed_result(target: HybridTarget, engine: HybridEngine, error: str, duration_ms: int) -> HybridResult:
    validation = validate_for_kind(target.target_kind, "", "", baseline_text=target.baseline_text)
    return HybridResult(
        target_id=target.target_id,
        engine=engine,
        status="failed",
        output_kind=target.target_kind,
        text="",
        markdown="",
        html="",
        json_payload={},
        confidence=None,
        duration_ms=duration_ms,
        validation=validation,
        replacement_policy=ReplacementPolicy.NO_CHANGE,
        warnings=[],
        error=error,
    )
