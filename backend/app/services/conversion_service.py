"""ConversionService — the universal conversion orchestrator.

Sits between ``TaskManager`` and the format-specific converters.  For
Phase 0, only ``MarkerPdfConverter`` is registered, so all files route
through the existing ``MarkerService``.  Future PRs register additional
converters without changing this module.

The service returns the legacy ``{text, extension, images, metadata}``
dict so ``_finalize_job`` is untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from app.conversion.converters.marker_pdf import MarkerPdfConverter
from app.conversion.converters.office_docx import OfficeDocxConverter
from app.conversion.converters.office_pptx import OfficePptxConverter
from app.conversion.registry import ConverterRegistry
from app.conversion.result import ConverterPlan
from app.conversion.router import ConversionRouter
from app.conversion.stream_info import StreamInfo

logger = logging.getLogger(__name__)


class ConversionService:
    """Orchestrates file conversion through the registry and router.

    Construction wires up the registry with all available converters.
    ``convert_file`` is the single entry point called by ``TaskManager``.
    """

    def __init__(self, marker_service: Any) -> None:
        self._marker_service = marker_service
        self._registry = ConverterRegistry()
        self._router = ConversionRouter()

        # Register Phase 0 converters.
        self._registry.register(MarkerPdfConverter(marker_service))
        self._registry.register(OfficeDocxConverter(marker_service))
        self._registry.register(OfficePptxConverter(marker_service))

    @property
    def registry(self) -> ConverterRegistry:
        """Expose registry for tests and capability queries."""
        return self._registry

    def _resolve_fallback(self, plan: ConverterPlan) -> ConverterPlan:
        """Fallback to marker_pdf if planned engine has no registered converter."""
        if not self._registry.has(plan.engine):
            return ConverterPlan(
                engine="marker_pdf",
                label=f"{plan.label} → Marker PDF (no converter registered)",
                confidence=min(plan.confidence, 0.5),
                reasons=plan.reasons + [
                    f"Engine '{plan.engine}' not registered; "
                    f"falling back to marker_pdf"
                ],
                needs_marker_models=True,
                needs_gpu=True,
                fallback_chain=[plan.engine, "marker_pdf"],
                warnings=plan.warnings + [
                    f"Converter for '{plan.engine}' not available in this build"
                ],
            )
        return plan

    def plan(self, filepath: str, config: dict[str, Any]) -> ConverterPlan:
        """Plan (but don't execute) a conversion — useful for the /plan API."""
        stream_info = StreamInfo.from_path(filepath)
        plan = self._router.plan(stream_info, config)
        return self._resolve_fallback(plan)

    def plan_by_metadata(self, filename: str, size: int, config: dict[str, Any]) -> ConverterPlan:
        """Plan a conversion based on filename and size without local filesystem storage."""
        import mimetypes
        from pathlib import Path

        p = Path(filename)
        extension = p.suffix.lower() if p.suffix else ""
        mime_type, _ = mimetypes.guess_type(p.name)
        if not mime_type:
            mime_type = "application/octet-stream"

        stream_info = StreamInfo(
            path=filename,
            extension=extension,
            mime_type=mime_type,
            size=size,
            sample=b"",
        )
        plan = self._router.plan(stream_info, config)
        return self._resolve_fallback(plan)


    def convert_file(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> dict[str, Any]:
        """Convert a file and return the legacy envelope dict.

        This is the drop-in replacement for ``marker_service.convert_file``
        that ``TaskManager._run_conversion`` calls.
        """
        plan = self.plan(filepath, config)

        converter = self._registry.get(plan.engine)
        if converter is None:
            # Should not happen after plan() fallback, but defensive.
            raise RuntimeError(
                f"No converter registered for engine '{plan.engine}' "
                f"(file: {filepath})"
            )

        logger.info(
            "Converting '%s' with engine=%s (confidence=%.2f)",
            filepath,
            plan.engine,
            plan.confidence,
        )

        result = converter.convert(filepath, config, device=device)

        # Inject the plan into metadata so job status/history can show it.
        result.metadata["engine"] = plan.to_dict()

        return result.to_legacy_envelope()
