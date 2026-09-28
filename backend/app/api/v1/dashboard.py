from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Dict, Any
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user
from app.models.auth import AuthenticatedUser
from app.services.reservations import get_tenant_properties

router = APIRouter()

def require_tenant(current_user: AuthenticatedUser) -> str:
    tenant_id = current_user.tenant_id
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=403, detail="Authenticated tenant required")
    return tenant_id


@router.get("/dashboard/properties")
async def get_dashboard_properties(current_user: AuthenticatedUser = Depends(get_current_user)):
    return await get_tenant_properties(require_tenant(current_user))


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    year: int = Query(..., ge=1, le=9998),
    month: int = Query(..., ge=1, le=12),
    current_user: AuthenticatedUser = Depends(get_current_user)
) -> Dict[str, Any]:

    tenant_id = require_tenant(current_user)
    revenue_data = await get_revenue_summary(property_id, tenant_id, year, month)
    # Assignment does not define half-cent ties; agreed display policy is ROUND_HALF_UP.
    total_revenue = Decimal(revenue_data['total']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": format(total_revenue, '.2f'),
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count'],
        "year": year,
        "month": month,
        "timezone": revenue_data['timezone'],
    }
