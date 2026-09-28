import logging
from datetime import datetime, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.database_pool import db_pool

logger = logging.getLogger(__name__)


def month_bounds(year: int, month: int, property_timezone: str):
    """Convert the property's local half-open calendar month to UTC instants."""
    local_timezone = ZoneInfo(property_timezone)
    start = datetime(year, month, 1, tzinfo=local_timezone)
    end = (
        datetime(year + 1, 1, 1, tzinfo=local_timezone)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=local_timezone)
    )
    try:
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    except OverflowError as exc:
        raise HTTPException(status_code=422, detail="Reporting month is outside supported datetime range") from exc


async def get_tenant_properties(tenant_id: str):
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=403, detail="Authenticated tenant required")
    try:
        await db_pool.initialize()
        async with db_pool.get_session() as session:
            result = await session.execute(
                text("SELECT id, name, timezone FROM properties WHERE tenant_id = :tenant_id ORDER BY id"),
                {"tenant_id": tenant_id},
            )
            return [dict(row) for row in result.mappings()]
    except (SQLAlchemyError, OSError, TimeoutError, RuntimeError) as exc:
        logger.exception("Property database access failed")
        raise HTTPException(status_code=503, detail="Revenue database unavailable") from exc


async def calculate_monthly_revenue(
    property_id: str, tenant_id: str, year: int, month: int, db_session=None
) -> Dict[str, Any]:
    """Sum NUMERIC amounts exactly; the API rounds the final aggregate once."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=403, detail="Authenticated tenant required")
    try:
        if db_session is None:
            await db_pool.initialize()
            async with db_pool.get_session() as session:
                return await calculate_monthly_revenue(property_id, tenant_id, year, month, session)

        property_result = await db_session.execute(
            text("SELECT timezone FROM properties WHERE id = :property_id AND tenant_id = :tenant_id"),
            {"property_id": property_id, "tenant_id": tenant_id},
        )
        property_timezone = property_result.scalar_one_or_none()
        if property_timezone is None:
            raise HTTPException(status_code=404, detail="Property not found")

        start, end = month_bounds(year, month, property_timezone)
        result = await db_session.execute(
            text("""
                SELECT currency, SUM(total_amount) AS total_revenue, COUNT(*) AS reservation_count
                FROM reservations
                WHERE property_id = :property_id AND tenant_id = :tenant_id
                  AND check_in_date >= :start AND check_in_date < :end
                GROUP BY currency
            """),
            {"property_id": property_id, "tenant_id": tenant_id, "start": start, "end": end},
        )
        rows = result.mappings().all()
        if len(rows) > 1:
            raise HTTPException(status_code=422, detail="Cannot combine revenue in different currencies")
        if rows and not rows[0]["currency"]:
            raise HTTPException(status_code=422, detail="Reservation currency is missing")

        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "year": year,
            "month": month,
            "timezone": property_timezone,
            # asyncpg returns NUMERIC as Decimal. Strings preserve it through Redis/JSON.
            "total": str(rows[0]["total_revenue"]) if rows else "0.00",
            "currency": rows[0]["currency"] if rows else None,
            "count": rows[0]["reservation_count"] if rows else 0,
        }
    except (SQLAlchemyError, OSError, TimeoutError, RuntimeError) as exc:
        logger.exception("Revenue database access failed")
        raise HTTPException(status_code=503, detail="Revenue database unavailable") from exc
