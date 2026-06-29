"""PaddleOCR-VL local worker wrapper."""

from __future__ import annotations

from app.hybrid_ocr.workers.common import main_for_engine
from app.hybrid_ocr.workers.transformers_native import run_paddle_transformers


def main() -> int:
    return main_for_engine(
        "PaddleOCR-VL",
        "MARKER_PADDLE_OCR_VL_ENDPOINT",
        "MARKER_PADDLE_OCR_VL_COMMAND",
        native_runner=run_paddle_transformers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
