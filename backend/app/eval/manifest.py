"""Corpus manifest format for deterministic evaluations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EVAL_MANIFEST_SCHEMA_VERSION = "marker.eval_manifest.v1"


@dataclass(frozen=True)
class EvalSample:
    sample_id: str
    golden_text: str
    candidate_text: str
    golden_table: Any = None
    candidate_table: Any = None
    routing: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalManifest:
    name: str
    samples: list[EvalSample]
    schema_version: str = EVAL_MANIFEST_SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)


def load_manifest(path: str | Path) -> EvalManifest:
    manifest_path = Path(path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_version = data.get("schema_version")
    if schema_version != EVAL_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported eval manifest schema_version {schema_version!r}; "
            f"expected {EVAL_MANIFEST_SCHEMA_VERSION}"
        )
    samples = [
        _load_sample(item, base_dir=manifest_path.parent)
        for item in data.get("samples") or []
        if isinstance(item, dict)
    ]
    if not samples:
        raise ValueError("Eval manifest must contain at least one sample")
    return EvalManifest(
        name=str(data.get("name") or manifest_path.stem),
        samples=samples,
        metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    )


def _load_sample(data: dict[str, Any], *, base_dir: Path) -> EvalSample:
    sample_id = str(data.get("sample_id") or "").strip()
    if not sample_id:
        raise ValueError("Eval sample missing sample_id")
    return EvalSample(
        sample_id=sample_id,
        golden_text=_text_value(data, "golden_text", "golden_path", base_dir=base_dir),
        candidate_text=_text_value(data, "candidate_text", "candidate_path", base_dir=base_dir),
        golden_table=data.get("golden_table"),
        candidate_table=data.get("candidate_table"),
        routing=data.get("routing") if isinstance(data.get("routing"), dict) else {},
        metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    )


def _text_value(data: dict[str, Any], text_key: str, path_key: str, *, base_dir: Path) -> str:
    if text_key in data:
        return str(data.get(text_key) or "")
    raw_path = data.get(path_key)
    if raw_path:
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = base_dir / path
        return path.read_text(encoding="utf-8")
    raise ValueError(f"Eval sample missing {text_key} or {path_key}")
