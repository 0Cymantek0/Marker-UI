from fastapi import APIRouter
from app.conversion.formats import (
    INPUT_FORMATS,
    MARKER_MULTI_FORMAT_EXTENSIONS,
    OUTPUT_FORMATS,
    renderable_output_formats_for_extensions,
)
from app.models.schemas import CapabilitiesResponse, InputFormatCapability
from app.conversion.dependencies import get_engine_status

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])

@router.get("", response_model=CapabilitiesResponse)
async def get_capabilities() -> CapabilitiesResponse:
    """Get status of all conversion engines and dependencies."""
    return CapabilitiesResponse(
        engines=get_engine_status(),
        output_formats=list(OUTPUT_FORMATS),
        marker_multi_format_extensions=sorted(MARKER_MULTI_FORMAT_EXTENSIONS),
        input_formats=[
            InputFormatCapability(
                extensions=list(spec.extensions),
                engine=spec.engine,
                label=spec.label,
                category=spec.category,
                needs_marker_models=spec.needs_marker_models,
                needs_gpu=spec.needs_gpu,
                upload_allowed=spec.upload_allowed,
                url_allowed=spec.url_allowed,
                output_formats=renderable_output_formats_for_extensions(spec.extensions),
            )
            for spec in INPUT_FORMATS
        ],
    )
