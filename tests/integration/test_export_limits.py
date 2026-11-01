"""EXP-3, against a real Postgres and a real Redis: who may export, and how much.

The hour's limit and the month's count are Lua run by Redis, so they get a
Redis container, as the quota meter's tests do (one for the module, flushed
per test). The plan's flag is :mod:`tests.integration.test_exports_endpoint`'s.

Most tests put Ada on Pro with an hour of 3 exports, rather than the 20 the
setting gives, so the fourth is refused.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.accounts.usage import UsageMeter, flush
from app.api.entitlements import entitlement_context
from app.api.rate_limit import EXPORT_KEY_PREFIX, SlidingWindowLimiter
from app.db.models import UsageCounter
from tests.integration.accounts import FRONTEND, make_user, sign_in, subscribe
from tests.integration.test_exports_endpoint import _two_quarters
from tests.integration.test_rate_limit import REDIS_IMAGE

#: Two rows: BERKSHIRE's Q2, ALPHA and CHARLIE.
PORTFOLIO = "/v1/investors/berkshire/portfolio.csv"

#: The hour's exports in these tests.
HOURLY = 3


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    )
    with container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def limits(app: FastAPI, client: AsyncClient, redis: Redis) -> UsageMeter:
    """The app's meter and export limiter, on the real Redis, as the lifespan
    builds them but for the hour's number."""
    app.state.export_limiter = SlidingWindowLimiter(
        redis, HOURLY, 60 * 60, prefix=EXPORT_KEY_PREFIX
    )
    meter = UsageMeter(redis)
    app.state.meter = meter
    return meter


async def pro(
    client: AsyncClient,
    db_session: AsyncSession,
    email: str = "ada@example.com",
    **overrides: Any,
) -> int:
    """An account on Pro, with ``overrides`` of its terms, signed in."""
    user_id = await make_user(db_session, email)
    await subscribe(db_session, user_id, "pro", overrides=overrides or None)
    await sign_in(client, email)
    return user_id


async def _exports(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/v1/me")
    assert response.status_code == 200, response.text
    usage: dict[str, Any] = response.json()["usage"]["exports_month"]
    return usage


# --- the hour -------------------------------------------------------------------------


async def test_the_hours_exports_are_taken_then_a_429_says_when_to_retry(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    await _two_quarters(db_session)
    await pro(client, db_session)

    taken = [
        (await client.get(path)).status_code
        for path in (PORTFOLIO, "/v1/flows.xlsx", "/v1/investors/berkshire/activity.csv")
    ]
    refused = await client.get(PORTFOLIO)

    assert taken == [200] * HOURLY
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/json")
    error = refused.json()["error"]
    assert error["code"] == "rate_limited"
    assert f"at most {HOURLY} an hour" in error["message"]
    assert 3500 < int(refused.headers["retry-after"]) <= 3600
    # The refused one is not an export.
    assert (await _exports(client))["used"] == HOURLY


async def test_the_hour_is_each_accounts_own(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    await _two_quarters(db_session)
    await pro(client, db_session)
    for _ in range(HOURLY):
        assert (await client.get(PORTFOLIO)).status_code == 200
    assert (await client.get(PORTFOLIO)).status_code == 429

    client.cookies.clear()
    await pro(client, db_session, "bob@example.com")
    assert (await client.get(PORTFOLIO)).status_code == 200


async def test_a_refused_export_spends_none_of_the_hour(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    await _two_quarters(db_session)
    await pro(client, db_session)
    for _ in range(HOURLY + 2):
        assert (await client.get("/v1/investors/nobody/portfolio.csv")).status_code == 404
        assert (await client.get("/v1/flows.csv?sector=Energy")).status_code == 422

    for _ in range(HOURLY):
        assert (await client.get(PORTFOLIO)).status_code == 200
    assert (await _exports(client))["used"] == HOURLY


# --- the month, on the account page ---------------------------------------------------


async def test_each_export_is_counted_for_the_account_page_and_kept(
    client: AsyncClient,
    db_session: AsyncSession,
    limits: UsageMeter,
    redis: Redis,
) -> None:
    await _two_quarters(db_session)
    user_id = await pro(client, db_session)
    before = await _exports(client)

    assert (await client.get(PORTFOLIO)).status_code == 200
    assert (await client.get("/v1/flows.xlsx")).status_code == 200
    after = await _exports(client)
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    await flush(redis, factory)
    kept = await db_session.scalar(
        select(UsageCounter.count).where(
            UsageCounter.user_id == user_id, UsageCounter.key == "exports_month"
        )
    )

    assert (before["used"], before["limit"], before["remaining"]) == (0, None, None)
    assert (after["used"], after["limit"]) == (2, None)
    assert after["period_start"] == before["period_start"]
    assert kept == 2


async def test_a_monthly_limit_set_by_update_is_a_quota_429(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    await _two_quarters(db_session)
    await pro(client, db_session, exports_month=1)

    assert (await client.get(PORTFOLIO)).status_code == 200
    refused = await client.get(PORTFOLIO)

    assert refused.status_code == 429
    error = refused.json()["error"]
    assert error["code"] == "quota_exceeded"
    assert (error["detail"]["limit"], error["detail"]["allowed"]) == ("exports_month", 1)
    # Pro has no monthly limit: the plan that lifts it.
    assert error["detail"]["plan"]["slug"] == "pro"
    assert "x-ratelimit-limit" not in refused.headers
    assert (await _exports(client))["used"] == 1


async def test_a_keyed_export_spends_a_call_and_an_export_and_says_the_calls(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    app.dependency_overrides.pop(entitlement_context)
    await _two_quarters(db_session)
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "api_hobbyist", overrides={"api_calls_month": 10})
    await sign_in(client)
    made = await client.post("/v1/api-keys", json={"name": "laptop"}, headers=FRONTEND)
    assert made.status_code == 201, made.text

    # The insider feed: the fixtures' 13Fs are 2024's, before the real
    # window, but an empty feed is still a file.
    exported = await client.get(
        "/v1/insiders/transactions.csv",
        headers={"authorization": f"Bearer {made.json()['key']}"},
    )

    assert exported.status_code == 200, exported.text
    assert exported.headers["x-ratelimit-limit"] == "10"
    assert exported.headers["x-ratelimit-remaining"] == "9"
    assert (await _exports(client))["used"] == 1


# --- the plan's rows ------------------------------------------------------------------


async def test_a_table_over_the_plans_rows_is_refused_before_the_file(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter, redis: Redis
) -> None:
    await _two_quarters(db_session)
    user_id = await pro(client, db_session, export_rows=1)

    refused = await client.get(PORTFOLIO)
    workbook = await client.get(PORTFOLIO.replace(".csv", ".xlsx"))

    assert refused.status_code == workbook.status_code == 402
    assert refused.headers["content-type"].startswith("application/json")
    error = refused.json()["error"]
    assert error["code"] == "plan_limit"
    detail = error["detail"]
    assert (detail["limit"], detail["allowed"], detail["needed"]) == ("export_rows", 1, 2)
    assert detail["plan"]["slug"] == "pro"
    assert "this table has 2" in error["message"]
    # Neither is an export: not counted, and none of the hour spent.
    assert (await _exports(client))["used"] == 0
    assert not await redis.exists(f"{EXPORT_KEY_PREFIX}user:{user_id}")


async def test_a_table_of_exactly_the_plans_rows_is_exported(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    await _two_quarters(db_session)
    await pro(client, db_session, export_rows=2)

    response = await client.get(PORTFOLIO)

    assert response.status_code == 200, response.text
    rows = [line for line in response.text.splitlines() if line and not line.startswith(("#", "﻿#"))]
    assert len(rows) == 1 + 2


async def test_free_is_offered_the_plan_that_exports_not_refused_silently(
    client: AsyncClient, db_session: AsyncSession, limits: UsageMeter
) -> None:
    await _two_quarters(db_session)
    await make_user(db_session)
    await sign_in(client)

    response = await client.get(PORTFOLIO)

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["detail"]["limit"] == "export_enabled"
    assert error["detail"]["plan"]["slug"] == "pro"
    assert error["message"] == "Exporting is not on your plan. Pro does."
    me = (await client.get("/v1/me")).json()
    assert me["entitlements"]["export_enabled"] is False
    assert me["usage"]["exports_month"]["limit"] == 0
