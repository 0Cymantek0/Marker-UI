"""Shared worker helpers for Hybrid OCR subprocesses."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

import requests

from app.hybrid_ocr.locality import require_local_endpoint


def main_for_engine(
    engine: str,
    endpoint_env: str,
    command_env: str,
    native_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    try:
        endpoint = os.environ.get(endpoint_env)
        command = os.environ.get(command_env)
        if endpoint:
            result = _call_local_endpoint(endpoint, request)
        elif command:
            result = _call_external_command(command, args.request)
        elif native_runner is not None:
            result = native_runner(request)
        else:
            raise RuntimeError(
                f"{engine} worker not configured. Set {endpoint_env} to a localhost service "
                f"or {command_env} to a local specialist command after running Hybrid OCR setup."
            )
        Path(args.response).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001 - worker boundary returns structured failure
        payload = {"results": [_failure(target, str(exc)) for target in request.get("targets", [])]}
        Path(args.response).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return 0


def _call_local_endpoint(endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
    require_local_endpoint(endpoint)
    payload = {
        "engine": request.get("engine"),
        "targets": [
            {
                **target,
                "image_base64": _read_image_b64(target.get("crop_path")),
            }
            for target in request.get("targets", [])
            if isinstance(target, dict)
        ],
    }
    response = requests.post(endpoint, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise RuntimeError("Specialist endpoint response must be a JSON object with results list.")
    return data


def _call_external_command(command: str, request_path: str) -> dict[str, Any]:
    response_path = Path(request_path).with_suffix(".worker-response.json")
    command_parts = shlex.split(command, posix=False)
    proc = subprocess.run(
        [*command_parts, "--request", request_path, "--response", str(response_path)],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Specialist command failed with exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")
    if not response_path.exists():
        raise RuntimeError("Specialist command did not write response file.")
    data = json.loads(response_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise RuntimeError("Specialist command response must be a JSON object with results list.")
    return data


def _read_image_b64(path: Any) -> str:
    if not path:
        return ""
    image_path = Path(str(path))
    if not image_path.exists():
        return ""
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def _failure(target: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "target_id": target.get("target_id"),
        "status": "failed",
        "text": "",
        "markdown": "",
        "html": "",
        "duration_ms": 0,
        "replacement_policy": "no_change",
        "error": message,
    }
