"""GLM-OCR local worker wrapper.

This wrapper does not call GLM MaaS/cloud. It delegates to a configured
localhost service or local command that owns the actual GLM-OCR runtime.
"""

from __future__ import annotations

from app.hybrid_ocr.workers.common import main_for_engine
from app.hybrid_ocr.workers.transformers_native import run_glm_transformers


def main() -> int:
    return main_for_engine(
        "GLM-OCR",
        "MARKER_GLM_OCR_ENDPOINT",
        "MARKER_GLM_OCR_COMMAND",
        native_runner=run_glm_transformers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
