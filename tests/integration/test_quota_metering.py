"""TIER-5, against a real Postgres and a real Redis: API keys and their monthly quotas.

The meter is a Lua script, and Lua run by a fake would test the fake, so the
counters here are a Redis container's (one for the module, flushed per test).
Everything else is the integration suite's own: the test's transaction, the
fake Redis the cache and sessions use, and the outbox in place of mail.

Most tests put Ada on API Hobbyist with a quota of 5 calls a month, so 80% is
the fourth call and the sixth is refused.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.accounts.api_keys import MAX_API_KEYS
from app.accounts.usage import (
    DIRTY_KEY,
    FLUSH_LOCK_KEY,
    FLUSHING_KEY,
    QuotaPeriod,
    UsageMeter,
    claim_warning,
    counter_key,
    flush,
    flush_quietly,
    quota_period,
)
from app.api.entitlements import entitlement_context
from app.api.rate_limit import SlidingWindowLimiter
from app.db.models import ApiKey, AppUser, Subscription, UsageCounter
from app.main import create_app
from tests.integration.accounts import FRONTEND, Outbox, make_user, sign_in, subscribe
from tests.integration.test_rate_limit import REDIS_IMAGE

#: A cheap metered read: answers on an empty database.
METERED_PATH = "/v1/meta/periods"


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
def meter(app: FastAPI, client: AsyncClient, redis: Redis) -> UsageMeter:
    """The app's meter, on the real Redis, as the lifespan builds it."""
    meter = UsageMeter(redis)
    app.state.meter = meter
    return meter


@pytest.fixture
def factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Sessions for the flush, inside the test's transaction like every other."""
    return async_sessionmaker(
        bind=db_session.bind,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )


async def keyed(
    client: AsyncClient,
    db_session: AsyncSession,
    *,
    email: str = "ada@example.com",
    plan: str = "api_hobbyist",
    calls: int | None = 5,
) -> str:
    """An account on ``plan``, ``calls`` a month when given, and a key it made
    signed in. The client is signed out after, as a program calling with the
    key would be."""
    user_id = await make_user(db_session, email)
    await subscribe(
        db_session,
        user_id,
        plan,
        overrides={"api_calls_month": calls} if calls is not None else None,
    )
    await sign_in(client, email)
    response = await client.post("/v1/api-keys", json={"name": "laptop"}, headers=FRONTEND)
    assert response.status_code == 201, response.text
    client.cookies.clear()
    key: str = response.json()["key"]
    return key


def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


async def ada(db_session: AsyncSession) -> AppUser:
    user = await db_session.scalar(select(AppUser).where(AppUser.email == "ada@example.com"))
    assert user is not None
    return user


def this_month(user: AppUser, subscription_start: datetime | None = None) -> QuotaPeriod:
    return quota_period(subscription_start or user.created_at, datetime.now(UTC))


# --- counting -------------------------------------------------------------------------


async def test_each_keyed_request_spends_one_and_says_what_is_left(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    key = await keyed(client, db_session)

    answers = [await client.get(METERED_PATH, headers=bearer(key)) for _ in range(3)]

    assert [r.status_code for r in answers] == [200, 200, 200]
    assert [r.headers["x-ratelimit-limit"] for r in answers] == ["5", "5", "5"]
    assert [r.headers["x-ratelimit-remaining"] for r in answers] == ["4", "3", "2"]
    subscription = await db_session.scalar(select(Subscription.created_at))
    assert subscription is not None
    period = this_month(await ada(db_session), subscription)
    assert answers[0].headers["x-ratelimit-reset"] == str(int(period.resets_at.timestamp()))
    # Its answer is its account's: no shared cache keeps it.
    assert answers[0].headers["cache-control"].startswith("private")
    assert "Authorization" in answers[0].headers["vary"]


async def test_a_spent_month_is_a_429_until_it_resets_and_counts_nothing_more(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter, redis: Redis
) -> None:
    key = await keyed(client, db_session)
    for _ in range(5):
        assert (await client.get(METERED_PATH, headers=bearer(key))).status_code == 200

    refused = [await client.get(METERED_PATH, headers=bearer(key)) for _ in range(3)]

    assert {r.status_code for r in refused} == {429}
    response = refused[0]
    error = response.json()["error"]
    assert error["code"] == "quota_exceeded"
    assert error["detail"]["limit"] == "api_calls_month"
    assert error["detail"]["allowed"] == 5
    assert error["detail"]["used"] == 5
    # The cheapest with more than 5: the plan's own 10,000, above Ada's override.
    assert error["detail"]["plan"]["slug"] == "api_hobbyist"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.headers["x-ratelimit-limit"] == "5"
    reset = int(response.headers["x-ratelimit-reset"])
    assert datetime.fromisoformat(error["detail"]["resets_at"]).timestamp() == reset
    assert 0 < int(response.headers["retry-after"]) <= 31 * 24 * 3600
    # The refusals spent nothing.
    user = await ada(db_session)
    [counter] = await redis.keys(
        counter_key(user.id, "api_calls_month", this_month(user))[:-10] + "*"
    )
    assert await redis.get(counter) == "5"


async def test_requests_without_a_key_spend_nothing_and_keep_the_minutes_headers(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, meter: UsageMeter, redis: Redis
) -> None:
    key = await keyed(client, db_session)
    app.state.rate_limiter = SlidingWindowLimiter(redis, 60)

    anonymous = await client.get(METERED_PATH)
    with_key = await client.get(METERED_PATH, headers=bearer(key))

    # The minute's, for everyone else.
    assert anonymous.headers["x-ratelimit-limit"] == "60"
    assert "x-ratelimit-reset" not in anonymous.headers
    # The month's, for a key: what its program budgets by.
    assert with_key.headers["x-ratelimit-limit"] == "5"
    assert with_key.headers["x-ratelimit-remaining"] == "4"
    assert await redis.scard(DIRTY_KEY) == 1  # type: ignore[misc]


async def test_mcp_calls_are_counted_apart_from_api_calls(
    db_session: AsyncSession, meter: UsageMeter
) -> None:
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))

    for _ in range(3):
        await meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)
    mcp = await meter.spend(db_session, user_id, "mcp_calls_month", limit=10, period=period)

    assert mcp.used == 1
    assert await meter.used(
        db_session, user_id, ["api_calls_month", "mcp_calls_month"], period
    ) == {
        "api_calls_month": 3,
        "mcp_calls_month": 1,
    }


async def test_no_limit_is_counted_and_never_refused(
    db_session: AsyncSession, meter: UsageMeter
) -> None:
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))

    spent = [
        await meter.spend(db_session, user_id, "api_calls_month", limit=None, period=period)
        for _ in range(3)
    ]

    assert [s.admitted for s in spent] == [True, True, True]
    assert spent[-1].used == 3
    assert spent[-1].remaining is None
    assert not any(s.warn for s in spent)


async def test_requests_at_once_never_pass_the_quota(
    db_session: AsyncSession, meter: UsageMeter
) -> None:
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    # The first seeds the counter from Postgres; the rest race on Redis alone.
    await meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)

    spent = await asyncio.gather(
        *(
            meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)
            for _ in range(30)
        )
    )

    assert sum(s.admitted for s in spent) == 9
    assert sum(s.warn for s in spent) == 1


async def test_a_counter_redis_lost_carries_on_from_the_last_flush(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    key = await keyed(client, db_session)
    user = await ada(db_session)
    started = await db_session.scalar(select(Subscription.created_at))
    db_session.add(
        UsageCounter(
            user_id=user.id, key="api_calls_month", period=this_month(user, started).start, count=4
        )
    )
    await db_session.flush()

    last, refused = [await client.get(METERED_PATH, headers=bearer(key)) for _ in range(2)]

    assert last.headers["x-ratelimit-remaining"] == "0"
    assert refused.status_code == 429


async def test_the_month_resets_on_the_subscriptions_anniversary(
    client: AsyncClient,
    db_session: AsyncSession,
    meter: UsageMeter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscribed on 14 September: the 13th of October is September's month,
    the 14th a new one, whatever the calendar month."""
    key = await keyed(client, db_session)
    await db_session.execute(
        update(Subscription).values(created_at=datetime(2026, 9, 14, 15, 30, tzinfo=UTC))
    )

    def on(day: datetime) -> None:
        monkeypatch.setattr("app.api.metering.now", lambda: day)

    on(datetime(2026, 10, 13, 23, 59, tzinfo=UTC))
    for _ in range(5):
        await client.get(METERED_PATH, headers=bearer(key))
    spent = await client.get(METERED_PATH, headers=bearer(key))
    on(datetime(2026, 10, 14, 0, 0, tzinfo=UTC))
    fresh = await client.get(METERED_PATH, headers=bearer(key))

    assert spent.status_code == 429
    assert spent.headers["x-ratelimit-reset"] == str(
        int(datetime(2026, 10, 14, tzinfo=UTC).timestamp())
    )
    assert fresh.status_code == 200
    assert fresh.headers["x-ratelimit-remaining"] == "4"
    assert fresh.headers["x-ratelimit-reset"] == str(
        int(datetime(2026, 11, 14, tzinfo=UTC).timestamp())
    )


# --- the 80% email --------------------------------------------------------------------


async def test_the_eighty_percent_email_goes_once_a_month(
    client: AsyncClient,
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    outbox: Outbox,
) -> None:
    key = await keyed(client, db_session)
    outbox.sent.clear()  # the verification mail is not this test's

    for _ in range(3):
        await client.get(METERED_PATH, headers=bearer(key))
    assert outbox.sent == []
    await client.get(METERED_PATH, headers=bearer(key))  # the fourth of 5: 80%
    assert len(outbox.sent) == 1
    for _ in range(4):  # the fifth, and three refused
        await client.get(METERED_PATH, headers=bearer(key))

    [mail] = outbox.sent
    assert mail.to == "ada@example.com"
    assert "80%" in mail.subject
    assert "4 of the 5 API calls" in mail.body
    user = await ada(db_session)
    warned = await db_session.scalar(
        select(UsageCounter.warned_at).where(UsageCounter.user_id == user.id)
    )
    assert warned is not None

    # Redis forgets everything; the month's row still says it was sent.
    await redis.flushdb()
    for _ in range(5):
        await client.get(METERED_PATH, headers=bearer(key))
    assert len(outbox.sent) == 1


async def test_of_two_claims_one_sends(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))

    first = await claim_warning(db_session, user_id, "api_calls_month", period, 8)
    second = await claim_warning(db_session, user_id, "api_calls_month", period, 9)
    other_quota = await claim_warning(db_session, user_id, "mcp_calls_month", period, 8)
    next_month = await claim_warning(
        db_session,
        user_id,
        "api_calls_month",
        QuotaPeriod(start=date(2026, 11, 14), end=date(2026, 12, 14)),
        8,
    )

    assert (first, second, other_quota, next_month) == (True, False, True, True)


# --- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "Bearer ww_not_a_key_anyone_has",
        "Bearer something-else",
        "Bearer",
        "Basic YWRhOnB3",
    ],
)
async def test_a_key_that_is_not_one_is_a_401(
    client: AsyncClient, meter: UsageMeter, header: str
) -> None:
    response = await client.get(METERED_PATH, headers={"authorization": header})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_a_revoked_key_stops_at_once(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    key = await keyed(client, db_session)
    assert (await client.get(METERED_PATH, headers=bearer(key))).status_code == 200
    await sign_in(client)
    [listed] = (await client.get("/v1/api-keys")).json()["data"]

    revoked = await client.delete(f"/v1/api-keys/{listed['id']}", headers=FRONTEND)
    again = await client.delete(f"/v1/api-keys/{listed['id']}", headers=FRONTEND)
    client.cookies.clear()

    assert (revoked.status_code, again.status_code) == (204, 204)
    assert (await client.get(METERED_PATH, headers=bearer(key))).status_code == 401


async def test_a_plan_without_api_calls_is_a_402_naming_one_with_them(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    """Made on API Hobbyist, then the subscription ended: Free has no calls."""
    key = await keyed(client, db_session)
    await db_session.execute(update(Subscription).values(status="canceled"))

    response = await client.get(METERED_PATH, headers=bearer(key))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "plan_limit"
    assert error["detail"]["limit"] == "api_calls_month"
    assert (error["detail"]["allowed"], error["detail"]["needed"]) == (0, 1)
    assert error["detail"]["plan"]["slug"] == "api_hobbyist"


async def test_a_key_signs_nobody_in(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    key = await keyed(client, db_session)

    for path in ("/v1/me", "/v1/api-keys", "/v1/watchlist"):
        response = await client.get(path, headers=bearer(key))
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthenticated", path


async def test_a_key_reads_with_its_plans_window(
    app: FastAPI,
    client: AsyncClient,
    db_session: AsyncSession,
    meter: UsageMeter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On 25 October 2026 Free's four quarters start at 2025Q4; API
    Hobbyist's twenty, at 2021Q4."""
    key = await keyed(client, db_session)
    monkeypatch.setattr("app.api.entitlements.utc_today", lambda: date(2026, 10, 25))
    app.dependency_overrides.pop(entitlement_context, None)
    old = {"period": "2023Q4"}

    anonymous = await client.get("/v1/market/top-holdings", params=old)
    with_key = await client.get("/v1/market/top-holdings", params=old, headers=bearer(key))

    assert anonymous.status_code == 402
    assert with_key.status_code != 402


async def test_deleting_the_account_revokes_its_keys(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    key = await keyed(client, db_session)
    await sign_in(client)

    deleted = await client.request(
        "DELETE", "/v1/account", json={"password": "correct horse 42"}, headers=FRONTEND
    )

    assert deleted.status_code == 204, deleted.text
    assert (await client.get(METERED_PATH, headers=bearer(key))).status_code == 401
    assert await db_session.scalar(select(ApiKey.revoked_at)) is not None


# --- the keys -------------------------------------------------------------------------


async def test_a_key_is_shown_once_and_listed_by_its_prefix(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "api_hobbyist")
    await sign_in(client)

    made = (await client.post("/v1/api-keys", json={"name": " nightly "}, headers=FRONTEND)).json()
    listed = (await client.get("/v1/api-keys")).json()["data"]

    assert made["key"].startswith("ww_")
    assert made["prefix"] == made["key"][:11]
    assert made["name"] == "nightly"
    assert listed == [{k: v for k, v in made.items() if k != "key"}]
    stored = await db_session.scalar(select(ApiKey.token_hash))
    assert stored is not None and made["key"] not in stored


async def test_free_makes_no_key(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await client.post("/v1/api-keys", json={"name": "laptop"}, headers=FRONTEND)

    assert response.status_code == 402
    assert response.json()["error"]["detail"]["plan"]["slug"] == "api_hobbyist"


async def test_an_unverified_account_makes_no_key(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session, verified=False)
    await subscribe(db_session, user_id, "api_hobbyist")
    await sign_in(client)

    response = await client.post("/v1/api-keys", json={"name": "laptop"}, headers=FRONTEND)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_unverified"


async def test_at_most_ten_keys_work_at_once(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "api_hobbyist")
    await sign_in(client)
    made = [
        await client.post("/v1/api-keys", json={"name": f"k{n}"}, headers=FRONTEND)
        for n in range(MAX_API_KEYS)
    ]

    over = await client.post("/v1/api-keys", json={"name": "one more"}, headers=FRONTEND)
    await client.delete(f"/v1/api-keys/{made[0].json()['id']}", headers=FRONTEND)
    after_revoking = await client.post("/v1/api-keys", json={"name": "one more"}, headers=FRONTEND)

    assert {r.status_code for r in made} == {201}
    assert over.status_code == 422
    assert after_revoking.status_code == 201
    assert len((await client.get("/v1/api-keys")).json()["data"]) == MAX_API_KEYS


# --- the account page -----------------------------------------------------------------


async def test_the_account_page_shows_the_month_before_the_wall(
    client: AsyncClient, db_session: AsyncSession, meter: UsageMeter
) -> None:
    key = await keyed(client, db_session)
    for _ in range(3):
        await client.get(METERED_PATH, headers=bearer(key))
    await sign_in(client)

    usage = (await client.get("/v1/me")).json()["usage"]

    started = await db_session.scalar(select(Subscription.created_at))
    period = this_month(await ada(db_session), started)
    assert usage["api_calls_month"] == {
        "used": 3,
        "limit": 5,
        "remaining": 2,
        "period_start": period.start.isoformat(),
        "resets_at": period.resets_at.isoformat().replace("+00:00", "Z"),
    }
    assert usage["mcp_calls_month"]["used"] == 0
    assert usage["mcp_calls_month"]["limit"] == 10_000


async def test_free_counts_its_months_from_the_account(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    usage = (await client.get("/v1/me")).json()["usage"]["api_calls_month"]

    period = this_month(await ada(db_session))
    assert (usage["used"], usage["limit"], usage["remaining"]) == (0, 0, 0)
    assert usage["period_start"] == period.start.isoformat()


# --- the flush ------------------------------------------------------------------------


async def _counted(db_session: AsyncSession) -> dict[tuple[int, str, date], int]:
    rows = await db_session.execute(
        select(UsageCounter.user_id, UsageCounter.key, UsageCounter.period, UsageCounter.count)
    )
    return {(user_id, key, period): count for user_id, key, period, count in rows}


async def test_a_flush_writes_each_changed_counter(
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ada_id = await make_user(db_session)
    bob_id = await make_user(db_session, "bob@example.com")
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    for _ in range(3):
        await meter.spend(db_session, ada_id, "api_calls_month", limit=10, period=period)
    await meter.spend(db_session, bob_id, "mcp_calls_month", limit=10, period=period)

    written = await flush(redis, factory)
    nothing_changed = await flush(redis, factory)
    await meter.spend(db_session, ada_id, "api_calls_month", limit=10, period=period)
    one_changed = await flush(redis, factory)

    assert (written, nothing_changed, one_changed) == (2, 0, 1)
    assert await _counted(db_session) == {
        (ada_id, "api_calls_month", period.start): 4,
        (bob_id, "mcp_calls_month", period.start): 1,
    }
    assert not await redis.exists(DIRTY_KEY, FLUSHING_KEY, FLUSH_LOCK_KEY)


async def test_a_flush_never_takes_a_count_back(
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Redis behind the row (restored from an old snapshot, say) leaves the row as it is."""
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    await meter.spend(db_session, user_id, "api_calls_month", limit=None, period=period)
    db_session.add(
        UsageCounter(user_id=user_id, key="api_calls_month", period=period.start, count=50)
    )
    await db_session.flush()

    await flush(redis, factory)

    assert await _counted(db_session) == {(user_id, "api_calls_month", period.start): 50}


async def test_a_failed_flush_leaves_its_counters_for_the_next(
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    await meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)

    def broken() -> Any:
        raise OSError("connection refused")

    await flush_quietly(redis, broken)  # type: ignore[arg-type]
    await meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)
    await flush_quietly(redis, factory)

    assert await _counted(db_session) == {(user_id, "api_calls_month", period.start): 2}
    assert not await redis.exists(FLUSH_LOCK_KEY)


async def test_one_flush_at_a_time(
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    await meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)
    await redis.set(FLUSH_LOCK_KEY, "another process", ex=60)

    assert await flush(redis, factory) == 0
    assert await redis.get(FLUSH_LOCK_KEY) == "another process"
    await redis.delete(FLUSH_LOCK_KEY)
    assert await flush(redis, factory) == 1


async def test_a_flush_skips_an_account_erased_meanwhile(
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    gone = await make_user(db_session, "gone@example.com")
    kept = await make_user(db_session)
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    for user_id in (gone, kept):
        await meter.spend(db_session, user_id, "api_calls_month", limit=10, period=period)
    await db_session.delete(await db_session.get(AppUser, gone))
    await db_session.flush()

    await flush(redis, factory)

    assert await _counted(db_session) == {(kept, "api_calls_month", period.start): 1}


# --- what the account holds -----------------------------------------------------------


async def test_the_export_lists_the_keys_and_the_months(
    client: AsyncClient,
    db_session: AsyncSession,
    meter: UsageMeter,
    redis: Redis,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await keyed(client, db_session)
    await client.get(METERED_PATH, headers=bearer(key))
    await flush(redis, factory)
    await sign_in(client)

    export = (await client.get("/v1/me/export")).json()

    [exported_key] = export["api_keys"]
    assert exported_key["name"] == "laptop"
    assert exported_key["prefix"] == key[:11]
    assert exported_key["revoked_at"] is None
    assert key not in str(export)
    [month] = export["usage"]
    assert (month["key"], month["count"], month["warned_at"]) == ("api_calls_month", 1, None)


async def test_the_process_flushes_as_it_exits(
    settings: Any, migrated_engine: Any, redis_url: str
) -> None:
    """The lifespan's own pools, as in production: a call counted while the
    app serves is in Postgres once it has stopped, long before the next
    periodic flush. Committed for real, so the account is deleted after."""
    app = create_app(
        settings.model_copy(update={"redis_url": redis_url, "usage_flush_seconds": 3600})
    )
    async with migrated_engine.begin() as connection:
        user_id = await connection.scalar(
            insert(AppUser)
            .values(email="exit@example.com", name="Exit Test", password_hash="x", time_zone="UTC")
            .returning(AppUser.id)
        )
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    try:
        async with app.router.lifespan_context(app), app.state.session_factory() as session:
            await app.state.meter.spend(
                session, user_id, "api_calls_month", limit=10, period=period
            )
        async with migrated_engine.connect() as connection:
            count = await connection.scalar(
                select(UsageCounter.count).where(UsageCounter.user_id == user_id)
            )
        assert count == 1
    finally:
        async with migrated_engine.begin() as connection:
            await connection.execute(delete(AppUser).where(AppUser.id == user_id))
