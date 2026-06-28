"""Local Hybrid OCR refinement package.

This package must stay local-only. Do not import VLM services or cloud OCR
clients here.
"""

from app.hybrid_ocr.orchestrator import HybridOcrOrchestrator

__all__ = ["HybridOcrOrchestrator"]
