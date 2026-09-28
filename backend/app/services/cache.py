import json
import redis.asyncio as redis
from typing import Dict, Any
import os
from fastapi import HTTPException

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

async def get_revenue_summary(property_id: str, tenant_id: str, year: int, month: int) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=403, detail="Authenticated tenant required")
    # New keys cannot read the old shared cache; JSON keeps identifier delimiters unambiguous.
    cache_key = "revenue:v2:" + json.dumps([tenant_id, property_id, year, month], separators=(",", ":"))
    
    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)
    
    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_monthly_revenue
    
    # Calculate revenue
    result = await calculate_monthly_revenue(property_id, tenant_id, year, month)
    
    # Cache the result for 5 minutes
    await redis_client.setex(cache_key, 300, json.dumps(result))
    
    return result
