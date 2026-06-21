from fastapi import APIRouter
from app.models.schemas import CapabilitiesResponse
from app.conversion.dependencies import get_engine_status

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])

@router.get("", response_model=CapabilitiesResponse)
async def get_capabilities() -> CapabilitiesResponse:
    """Get status of all conversion engines and dependencies."""
    return CapabilitiesResponse(engines=get_engine_status())
