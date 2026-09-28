"""Revenue regressions; opt in to rollback-only PostgreSQL tests with
RUN_REVENUE_INTEGRATION_TESTS=1. Run from backend with unittest discovery.
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import dashboard
from app.config import settings
from app.core.database_pool import DatabasePool
from app.models.auth import AuthenticatedUser
from app.services import cache, reservations


def authenticated_user(tenant_id="tenant-a"):
    return AuthenticatedUser(
        id="revenue-test-user", email="test@propertyflow.com", permissions=[],
        cities=[], is_admin=False, tenant_id=tenant_id,
    )


def revenue_data(total="2250.000", currency="USD", **overrides):
    result = {
        "property_id": "prop-001", "tenant_id": "tenant-a", "year": 2024,
        "month": 3, "timezone": "Europe/Paris", "total": total,
        "currency": currency, "count": 4,
    }
    result.update(overrides)
    return result


def dashboard_app(user=None):
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/v1")
    app.dependency_overrides[dashboard.get_current_user] = lambda: user or authenticated_user()
    return app


class MemoryRedis:
    """A cache double that exercises JSON round trips without a Redis server."""

    def __init__(self):
        self.values = {}
        self.get = AsyncMock(side_effect=self.values.get)
        self.setex = AsyncMock(side_effect=self.store)

    async def store(self, key, ttl, value):
        self.values[key] = value.encode()


class MonthBoundsTests(unittest.TestCase):
    def test_paris_march_spans_dst_and_includes_february_utc_boundary(self):
        start, end = reservations.month_bounds(2024, 3, "Europe/Paris")
        self.assertEqual(start, datetime(2024, 2, 29, 23, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2024, 3, 31, 22, tzinfo=timezone.utc))
        seeded_check_in = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)
        self.assertLessEqual(start, seeded_check_in)
        self.assertLess(seeded_check_in, end)

    def test_new_york_march_uses_both_local_offsets(self):
        start, end = reservations.month_bounds(2024, 3, "America/New_York")
        self.assertEqual(start, datetime(2024, 3, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2024, 4, 1, 4, tzinfo=timezone.utc))

    def test_december_rolls_into_next_year(self):
        start, end = reservations.month_bounds(2024, 12, "Europe/Paris")
        self.assertEqual(start, datetime(2024, 11, 30, 23, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2024, 12, 31, 23, tzinfo=timezone.utc))

    def test_local_month_outside_utc_datetime_range_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            reservations.month_bounds(1, 1, "Europe/Paris")
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(error.exception.detail, "Reporting month is outside supported datetime range")


class RevenueCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis = MemoryRedis()
        self.redis_patch = patch.object(cache, "redis_client", self.redis)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)

    async def test_every_result_dimension_has_an_independent_warm_cache_entry(self):
        async def calculate(property_id, tenant_id, year, month):
            return revenue_data(
                property_id=property_id, tenant_id=tenant_id, year=year, month=month,
                total=f"{year}.{month:03d}",
            )

        requests = [
            ("prop-001", "tenant-a", 2024, 3),
            ("prop-001", "tenant-b", 2024, 3),
            ("prop-002", "tenant-a", 2024, 3),
            ("prop-001", "tenant-a", 2025, 3),
            ("prop-001", "tenant-a", 2024, 4),
        ]
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock(side_effect=calculate)) as service:
            cold = [await cache.get_revenue_summary(*request) for request in requests]
            warm = [await cache.get_revenue_summary(*request) for request in reversed(requests)]
        self.assertEqual(warm, list(reversed(cold)))
        self.assertEqual(service.await_count, len(requests))
        self.assertEqual(len(self.redis.values), len(requests))
        self.assertTrue(all(isinstance(item["total"], str) for item in warm))

    async def test_tenants_remain_isolated_in_both_request_orders(self):
        for order in (("tenant-a", "tenant-b"), ("tenant-b", "tenant-a")):
            with self.subTest(order=order):
                self.redis.values.clear()

                async def calculate(property_id, tenant_id, year, month):
                    return revenue_data(
                        tenant_id=tenant_id,
                        total="2250.000" if tenant_id == "tenant-a" else "0.00",
                    )

                with patch.object(reservations, "calculate_monthly_revenue", AsyncMock(side_effect=calculate)) as service:
                    for tenant in (*order, *order):
                        result = await cache.get_revenue_summary("prop-001", tenant, 2024, 3)
                        self.assertEqual(result["tenant_id"], tenant)
                        self.assertEqual(result["total"], "2250.000" if tenant == "tenant-a" else "0.00")
                self.assertEqual(service.await_count, 2)

    async def test_previous_shared_key_cannot_supply_another_tenants_result(self):
        self.redis.values["revenue:prop-001"] = json.dumps(revenue_data()).encode()
        ocean = revenue_data(total="0.00", tenant_id="tenant-b", count=0)
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock(return_value=ocean)) as service:
            result = await cache.get_revenue_summary("prop-001", "tenant-b", 2024, 3)
        self.assertEqual(result, ocean)
        service.assert_awaited_once_with("prop-001", "tenant-b", 2024, 3)

    async def test_identifier_delimiters_cannot_make_different_tenants_share_an_entry(self):
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock(return_value=revenue_data())) as service:
            await cache.get_revenue_summary("c", "a:b", 2024, 3)
            await cache.get_revenue_summary("b:c", "a", 2024, 3)
        self.assertEqual(service.await_count, 2)
        self.assertEqual(len(self.redis.values), 2)

    async def test_invalid_tenant_never_reads_cache_or_calculates_revenue(self):
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock()) as service:
            for tenant in (None, "", " ", 1, [], {}):
                with self.subTest(tenant=tenant), self.assertRaises(HTTPException) as error:
                    await cache.get_revenue_summary("prop-001", tenant, 2024, 3)
                self.assertEqual(error.exception.status_code, 403)
        self.redis.get.assert_not_awaited()
        self.redis.setex.assert_not_awaited()
        service.assert_not_awaited()

    async def test_redis_read_failure_propagates_without_fabricated_revenue(self):
        self.redis.get.side_effect = RedisError("cache unavailable")
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock()) as service:
            with self.assertRaises(RedisError):
                await cache.get_revenue_summary("prop-001", "tenant-a", 2024, 3)
        service.assert_not_awaited()
        self.redis.setex.assert_not_awaited()

    async def test_redis_write_failure_propagates_without_success_response(self):
        self.redis.setex.side_effect = RedisError("cache unavailable")
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock(return_value=revenue_data())):
            with self.assertRaises(RedisError):
                await cache.get_revenue_summary("prop-001", "tenant-a", 2024, 3)
        self.assertEqual(self.redis.values, {})

    async def test_database_service_failure_is_not_cached(self):
        with patch.object(reservations, "calculate_monthly_revenue", AsyncMock(
            side_effect=HTTPException(status_code=503, detail="Revenue database unavailable")
        )):
            with self.assertRaises(HTTPException) as error:
                await cache.get_revenue_summary("prop-001", "tenant-a", 2024, 3)
        self.assertEqual(error.exception.status_code, 503)
        self.redis.setex.assert_not_awaited()


class RevenueServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_tenant_never_initializes_database(self):
        with patch.object(reservations.db_pool, "initialize", AsyncMock()) as initialize:
            for tenant in (None, "", " ", 1, [], {}):
                for operation in (
                    lambda: reservations.calculate_monthly_revenue("prop-001", tenant, 2024, 3),
                    lambda: reservations.get_tenant_properties(tenant),
                ):
                    with self.subTest(tenant=tenant), self.assertRaises(HTTPException) as error:
                        await operation()
                    self.assertEqual(error.exception.status_code, 403)
        initialize.assert_not_awaited()

    async def test_database_query_failure_returns_explicit_service_error(self):
        for failure in (SQLAlchemyError("query failed"), ConnectionRefusedError("db offline"), TimeoutError("db timeout")):
            session = SimpleNamespace(execute=AsyncMock(side_effect=failure))
            with self.subTest(failure=type(failure).__name__), self.assertLogs(reservations.logger, level="ERROR"):
                with self.assertRaises(HTTPException) as error:
                    await reservations.calculate_monthly_revenue("prop-001", "tenant-a", 2024, 3, session)
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(error.exception.detail, "Revenue database unavailable")

    async def test_pool_failure_returns_explicit_service_error_for_revenue_and_properties(self):
        with patch.object(reservations.db_pool, "initialize", AsyncMock(side_effect=RuntimeError("pool unavailable"))):
            for operation in (
                lambda: reservations.calculate_monthly_revenue("prop-001", "tenant-a", 2024, 3),
                lambda: reservations.get_tenant_properties("tenant-a"),
            ):
                with self.assertLogs(reservations.logger, level="ERROR"), self.assertRaises(HTTPException) as error:
                    await operation()
                self.assertEqual(error.exception.status_code, 503)


class DatabasePoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_uses_configured_database_url_and_reuses_async_session_factory(self):
        pool = DatabasePool()
        self.addAsyncCleanup(pool.close)
        with patch.object(settings, "database_url", "postgresql://test_user:test_password@configured-db:5444/configured_database"):
            await pool.initialize()
            engine = pool.engine
            session_factory = pool.session_factory
            await pool.initialize()
        self.assertIs(pool.engine, engine)
        self.assertIs(pool.session_factory, session_factory)
        self.assertEqual(engine.url.host, "configured-db")
        self.assertEqual(engine.url.port, 5444)
        self.assertEqual(engine.url.database, "configured_database")
        self.assertEqual(engine.url.username, "test_user")
        self.assertEqual(engine.url.drivername, "postgresql+asyncpg")
        async with pool.get_session() as session:
            self.assertIsInstance(session, AsyncSession)
        await pool.close()
        self.assertIsNone(pool.engine)
        self.assertIsNone(pool.session_factory)


class DashboardMoneyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dashboard_app())
        self.addCleanup(self.client.close)

    def test_api_rounds_aggregate_half_up_once_and_returns_two_decimal_strings(self):
        for raw, displayed in (("1.005", "1.01"), ("1.004", "1.00"), ("1000.000", "1000.00"), ("0", "0.00")):
            with self.subTest(total=raw), patch.object(dashboard, "get_revenue_summary", AsyncMock(return_value=revenue_data(raw))):
                response = self.client.get("/api/v1/dashboard/summary?property_id=prop-001&year=2024&month=3")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total_revenue"], displayed)
            self.assertIsInstance(response.json()["total_revenue"], str)

    def test_api_preserves_currency_and_authenticated_tenant(self):
        with patch.object(dashboard, "get_revenue_summary", AsyncMock(return_value=revenue_data("1.005", currency="EUR"))) as service:
            response = self.client.get(
                "/api/v1/dashboard/summary?property_id=prop-001&year=2024&month=3&tenant_id=tenant-b",
                headers={"X-Tenant-ID": "tenant-b"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["currency"], "EUR")
        self.assertEqual(response.json()["total_revenue"], "1.01")
        service.assert_awaited_once_with("prop-001", "tenant-a", 2024, 3)

    def test_required_period_validation_prevents_revenue_access(self):
        with patch.object(dashboard, "get_revenue_summary", AsyncMock()) as service:
            for period in ("", "&year=2024", "&month=3", "&year=2024&month=13", "&year=0&month=3"):
                with self.subTest(period=period):
                    response = self.client.get("/api/v1/dashboard/summary?property_id=prop-001" + period)
                    self.assertEqual(response.status_code, 422)
        service.assert_not_awaited()

    def test_missing_authenticated_tenant_fails_before_service_access(self):
        with TestClient(dashboard_app(authenticated_user(None))) as client, \
                patch.object(dashboard, "get_revenue_summary", AsyncMock()) as revenue, \
                patch.object(dashboard, "get_tenant_properties", AsyncMock()) as properties:
            self.assertEqual(client.get("/api/v1/dashboard/summary?property_id=prop-001&year=2024&month=3").status_code, 403)
            self.assertEqual(client.get("/api/v1/dashboard/properties").status_code, 403)
        revenue.assert_not_awaited()
        properties.assert_not_awaited()

    def test_database_service_error_contains_no_financial_fallback(self):
        with patch.object(dashboard, "get_revenue_summary", AsyncMock(
            side_effect=HTTPException(status_code=503, detail="Revenue database unavailable")
        )):
            response = self.client.get("/api/v1/dashboard/summary?property_id=prop-001&year=2024&month=3")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Revenue database unavailable"})


@unittest.skipUnless(os.getenv("RUN_REVENUE_INTEGRATION_TESTS") == "1", "Requires seeded PostgreSQL; set RUN_REVENUE_INTEGRATION_TESTS=1")
class RevenueDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Each test owns its engine/event loop and one external transaction.
        self.engine = create_async_engine(make_url(settings.database_url).set(drivername="postgresql+asyncpg"))
        self.addAsyncCleanup(self.engine.dispose)
        self.connection = await self.engine.connect()
        self.addAsyncCleanup(self.connection.close)
        self.transaction = await self.connection.begin()
        self.addAsyncCleanup(self.transaction.rollback)
        self.session = AsyncSession(bind=self.connection, expire_on_commit=False)
        self.addAsyncCleanup(self.session.close)

    async def fixture_property(self, property_timezone="Europe/Paris"):
        suffix = uuid4().hex
        tenant = "revenue-test-tenant-" + suffix
        property_id = "revenue-test-property-" + suffix
        await self.session.execute(text("INSERT INTO tenants (id, name) VALUES (:id, :name)"), {"id": tenant, "name": "Rollback-only revenue test"})
        await self.session.execute(
            text("INSERT INTO properties (id, tenant_id, name, timezone) VALUES (:id, :tenant, :name, :timezone)"),
            {"id": property_id, "tenant": tenant, "name": "Rollback-only property", "timezone": property_timezone},
        )
        return property_id, tenant

    async def add_reservation(self, property_id, tenant, check_in, amount, currency="USD"):
        await self.session.execute(
            text("""
                INSERT INTO reservations (id, property_id, tenant_id, check_in_date, check_out_date, total_amount, currency)
                VALUES (:id, :property, :tenant, :check_in, :check_out, :amount, :currency)
            """),
            {"id": "revenue-test-" + uuid4().hex, "property": property_id, "tenant": tenant,
             "check_in": check_in, "check_out": check_in + timedelta(days=1),
             "amount": Decimal(amount), "currency": currency},
        )

    async def test_seeded_march_sunset_and_ocean_are_scoped_to_own_tenant(self):
        sunset = await reservations.calculate_monthly_revenue("prop-001", "tenant-a", 2024, 3, self.session)
        ocean = await reservations.calculate_monthly_revenue("prop-001", "tenant-b", 2024, 3, self.session)
        self.assertEqual(Decimal(sunset["total"]), Decimal("2250.000"))
        self.assertEqual(sunset["count"], 4)
        self.assertEqual(sunset["timezone"], "Europe/Paris")
        self.assertEqual(sunset["currency"], "USD")
        self.assertEqual(Decimal(ocean["total"]), Decimal("0.00"))
        self.assertEqual(ocean["count"], 0)
        self.assertEqual(ocean["timezone"], "America/New_York")
        self.assertIsNone(ocean["currency"])

    async def test_seeded_timezone_boundary_reservation_belongs_to_march_locally(self):
        result = await self.session.execute(text("""
            SELECT r.check_in_date, r.check_in_date AT TIME ZONE p.timezone AS local_check_in
            FROM reservations r JOIN properties p ON p.id = r.property_id AND p.tenant_id = r.tenant_id
            WHERE r.id = 'res-tz-1'
        """))
        row = result.mappings().one()
        self.assertEqual(row["check_in_date"], datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc))
        self.assertEqual(row["local_check_in"], datetime(2024, 3, 1, 0, 30))

    async def test_other_tenant_property_and_unknown_tenant_fail_closed(self):
        for property_id, tenant in (("prop-002", "tenant-b"), ("prop-004", "tenant-a"), ("prop-001", "unknown-tenant")):
            with self.subTest(property_id=property_id, tenant=tenant), self.assertRaises(HTTPException) as error:
                await reservations.calculate_monthly_revenue(property_id, tenant, 2024, 3, self.session)
            self.assertEqual(error.exception.status_code, 404)

    async def test_property_list_contains_only_authenticated_tenants_seeded_properties(self):
        with patch.object(reservations.db_pool, "initialize", AsyncMock()), \
                patch.object(reservations.db_pool, "get_session", return_value=self.session):
            sunset = await reservations.get_tenant_properties("tenant-a")
            ocean = await reservations.get_tenant_properties("tenant-b")
        self.assertEqual([item["id"] for item in sunset], ["prop-001", "prop-002", "prop-003"])
        self.assertEqual([item["id"] for item in ocean], ["prop-001", "prop-004", "prop-005"])
        self.assertEqual(sunset[0]["name"], "Beach House Alpha")
        self.assertEqual(ocean[0]["name"], "Mountain Lodge Beta")

    async def test_month_start_is_inclusive_and_next_month_start_is_exclusive(self):
        for property_timezone in ("Europe/Paris", "America/New_York"):
            with self.subTest(timezone=property_timezone):
                property_id, tenant = await self.fixture_property(property_timezone)
                start, end = reservations.month_bounds(2024, 3, property_timezone)
                for instant, amount in (
                    (start - timedelta(microseconds=1), "100.000"),
                    (start, "7.000"),
                    (end - timedelta(microseconds=1), "11.000"),
                    (end, "200.000"),
                ):
                    await self.add_reservation(property_id, tenant, instant, amount)
                result = await reservations.calculate_monthly_revenue(property_id, tenant, 2024, 3, self.session)
                self.assertEqual(Decimal(result["total"]), Decimal("18.000"))
                self.assertEqual(result["count"], 2)

    async def test_exact_numeric_amounts_are_aggregated_before_display_rounding(self):
        property_id, tenant = await self.fixture_property()
        for amount in ("333.333", "333.333", "333.334"):
            await self.add_reservation(property_id, tenant, datetime(2024, 3, 15, tzinfo=timezone.utc), amount)
        result = await reservations.calculate_monthly_revenue(property_id, tenant, 2024, 3, self.session)
        self.assertEqual(result["total"], "1000.000")
        self.assertEqual(result["count"], 3)
        with TestClient(dashboard_app(authenticated_user(tenant))) as client, \
                patch.object(dashboard, "get_revenue_summary", AsyncMock(return_value=result)):
            response = client.get(f"/api/v1/dashboard/summary?property_id={property_id}&year=2024&month=3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_revenue"], "1000.00")

    async def test_subcent_eur_sum_is_preserved_then_rounded_half_up_once(self):
        property_id, tenant = await self.fixture_property()
        for _ in range(3):
            await self.add_reservation(property_id, tenant, datetime(2024, 3, 15, tzinfo=timezone.utc), "0.335", "EUR")
        result = await reservations.calculate_monthly_revenue(property_id, tenant, 2024, 3, self.session)
        self.assertEqual(result["total"], "1.005")
        self.assertEqual(result["currency"], "EUR")
        with TestClient(dashboard_app(authenticated_user(tenant))) as client, \
                patch.object(dashboard, "get_revenue_summary", AsyncMock(return_value=result)):
            response = client.get(f"/api/v1/dashboard/summary?property_id={property_id}&year=2024&month=3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_revenue"], "1.01")
        self.assertIsInstance(response.json()["total_revenue"], str)
        self.assertEqual(response.json()["currency"], "EUR")

    async def test_mixed_currencies_produce_explicit_error(self):
        property_id, tenant = await self.fixture_property()
        for currency in ("USD", "EUR"):
            await self.add_reservation(property_id, tenant, datetime(2024, 3, 15, tzinfo=timezone.utc), "10.000", currency)
        with self.assertRaises(HTTPException) as error:
            await reservations.calculate_monthly_revenue(property_id, tenant, 2024, 3, self.session)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(error.exception.detail, "Cannot combine revenue in different currencies")

    async def test_missing_currency_does_not_silently_label_money_usd(self):
        property_id, tenant = await self.fixture_property()
        await self.add_reservation(property_id, tenant, datetime(2024, 3, 15, tzinfo=timezone.utc), "10.000", None)
        with self.assertRaises(HTTPException) as error:
            await reservations.calculate_monthly_revenue(property_id, tenant, 2024, 3, self.session)
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
