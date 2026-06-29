"""Native Transformers inference for downloaded Hybrid OCR snapshots."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PIL import Image

from app.hybrid_ocr.setup import expected_model_dir


def run_glm_transformers(request: dict[str, Any]) -> dict[str, Any]:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model_dir = expected_model_dir("glm_ocr")
    processor = AutoProcessor.from_pretrained(str(model_dir), local_files_only=True)
    model = AutoModelForImageTextToText.from_pretrained(
        str(model_dir),
        torch_dtype="auto",
        device_map="auto",
        local_files_only=True,
    )
    results = [_run_glm_target(processor, model, target) for target in request.get("targets", [])]
    return {"results": results}


def run_paddle_transformers(request: dict[str, Any]) -> dict[str, Any]:
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    _patch_transformers_masking_compat()
    model_dir = expected_model_dir("paddleocr_vl")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    processor = AutoProcessor.from_pretrained(str(model_dir), trust_remote_code=True, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir),
        trust_remote_code=True,
        torch_dtype=dtype,
        local_files_only=True,
    ).to(device).eval()
    results = [_run_paddle_target(processor, model, target, device) for target in request.get("targets", [])]
    return {"results": results}


def _patch_transformers_masking_compat() -> None:
    import inspect
    import sys
    import transformers.masking_utils as masking_utils

    original = masking_utils.create_causal_mask
    if "inputs_embeds" in inspect.signature(original).parameters:
        return

    def compat_create_causal_mask(*args: Any, **kwargs: Any) -> Any:
        if "inputs_embeds" in kwargs and "input_embeds" not in kwargs:
            kwargs["input_embeds"] = kwargs.pop("inputs_embeds")
        return original(*args, **kwargs)

    masking_utils.create_causal_mask = compat_create_causal_mask
    for module in list(sys.modules.values()):
        if getattr(module, "create_causal_mask", None) is original:
            setattr(module, "create_causal_mask", compat_create_causal_mask)


def _run_glm_target(processor: Any, model: Any, target: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    prompt = _prompt_for_kind(str(target.get("kind") or "text"), glm=True)
    crop_path = str(target.get("crop_path") or "")
    if not Path(crop_path).exists():
        return _failure(target, "crop_path missing")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "url": crop_path},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    ).to(model.device)
    inputs.pop("token_type_ids", None)
    generated_ids = model.generate(**inputs, max_new_tokens=2048, do_sample=False)
    output = processor.decode(generated_ids[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True)
    return _success(target, output, int((time.perf_counter() - started) * 1000))


def _run_paddle_target(processor: Any, model: Any, target: dict[str, Any], device: str) -> dict[str, Any]:
    started = time.perf_counter()
    prompt = _prompt_for_kind(str(target.get("kind") or "text"), glm=False)
    crop_path = str(target.get("crop_path") or "")
    if not Path(crop_path).exists():
        return _failure(target, "crop_path missing")
    image = Image.open(crop_path).convert("RGB")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    ).to(device)
    output_ids = model.generate(**inputs, max_new_tokens=2048, do_sample=False)
    output = processor.batch_decode(output_ids, skip_special_tokens=True)[0]
    return _success(target, output, int((time.perf_counter() - started) * 1000))


def _prompt_for_kind(kind: str, *, glm: bool) -> str:
    if kind == "table":
        return "Table Recognition:"
    if kind == "formula":
        return "Formula Recognition:"
    return "Text Recognition:" if glm else "OCR:"


def _success(target: dict[str, Any], output: str, duration_ms: int) -> dict[str, Any]:
    return {
        "target_id": target.get("target_id"),
        "status": "ok",
        "text": output,
        "markdown": output,
        "duration_ms": duration_ms,
        "replacement_policy": "replace_block",
    }


def _failure(target: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "target_id": target.get("target_id"),
        "status": "failed",
        "text": "",
        "markdown": "",
        "duration_ms": 0,
        "replacement_policy": "no_change",
        "error": message,
    }
