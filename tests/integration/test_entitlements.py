"""Entitlements as rows: the seeded plans, the tables' checks, and the grant a session keeps.

The resolution order and the catalog's rules on their own are in
tests/test_entitlements.py.
"""

import json
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import entitlements
from app.accounts.entitlements import (
    FREE,
    MAX_WATCHLIST_ITEMS,
    Grant,
    can,
    grant_for,
    limit,
    load_catalog,
)
from app.accounts.sessions import SESSION_KEY_PREFIX, SessionStore, now
from app.db.models import (
    ENTITLEMENT_KEYS,
    FLAG_KEYS,
    LIMIT_KEYS,
    Filer,
    Plan,
    PlanEntitlement,
)
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, make_user, sign_in, subscribe

#: The launch plans, as agreed. The API plans' prices and quotas are
#: placeholders until they are settled.
AGREED: dict[str, dict[str, Any]] = {
    "free": {
        "history_quarters": 4,
        "insider_months": 6,
        "watchlist_items": 10,
        "screener_saved_filters": 3,
        "api_calls_month": 0,
        "export_enabled": False,
        "alerts_instant": False,
        "mcp_enabled": False,
    },
    "pro": {
        "history_quarters": 20,
        "insider_months": None,
        "watchlist_items": None,
        "screener_saved_filters": None,
        "api_calls_month": 0,
        "export_enabled": True,
        "alerts_instant": True,
        "mcp_enabled": False,
    },
    "api_hobbyist": {
        "history_quarters": 20,
        "insider_months": None,
        "watchlist_items": None,
        "screener_saved_filters": None,
        "api_calls_month": 10_000,
        "export_enabled": True,
        "alerts_instant": True,
        "mcp_enabled": True,
    },
    "api_pro": {
        "history_quarters": 20,
        "insider_months": None,
        "watchlist_items": None,
        "screener_saved_filters": None,
        "api_calls_month": 100_000,
        "export_enabled": True,
        "alerts_instant": True,
        "mcp_enabled": True,
    },
}


def _as(plan: str) -> Any:
    class Subscriber:
        grant = Grant(plan=plan, grandfathered=None, overrides={}, ends_at=None)

    return Subscriber()


def _resolved(who: Any) -> dict[str, Any]:
    return {
        **{key: limit(who, key) for key in LIMIT_KEYS},
        **{key: can(who, key) for key in FLAG_KEYS},
    }


# --- the seed -----------------------------------------------------------------------


async def test_the_seed_is_the_four_launch_plans_at_their_prices(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        select(Plan.slug, Plan.price_cents, Plan.currency, Plan.interval, Plan.is_public).order_by(
            Plan.id
        )
    )

    assert [tuple(row) for row in rows] == [
        ("free", 0, "USD", None, True),
        ("pro", 1400, "USD", "month", True),
        ("api_hobbyist", 2900, "USD", "month", True),
        ("api_pro", 9900, "USD", "month", True),
    ]


async def test_every_key_resolves_for_every_plan(db_session: AsyncSession) -> None:
    entitlements.install(await load_catalog(db_session))
    try:
        assert set(entitlements.current().plans) == set(AGREED)
        for slug, agreed in AGREED.items():
            assert set(agreed) == set(ENTITLEMENT_KEYS)
            assert _resolved(_as(slug)) == agreed, slug
    finally:
        entitlements.install(None)


async def test_every_key_resolves_signed_out_and_is_free(db_session: AsyncSession) -> None:
    entitlements.install(await load_catalog(db_session))
    try:
        assert _resolved(None) == AGREED[FREE]
        assert entitlements.plan_of(None).slug == FREE
    finally:
        entitlements.install(None)


async def test_a_limit_changes_with_an_update_not_a_deploy(db_session: AsyncSession) -> None:
    await db_session.execute(
        update(PlanEntitlement)
        .where(
            PlanEntitlement.plan_id == select(Plan.id).where(Plan.slug == FREE).scalar_subquery(),
            PlanEntitlement.key == "watchlist_items",
        )
        .values(value=25)
    )
    entitlements.install(await load_catalog(db_session))
    try:
        assert limit(None, "watchlist_items") == 25
    finally:
        entitlements.install(None)


# --- the tables' checks -----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("watchlist_items", "-1"),
        ("watchlist_items", "2.5"),
        ("watchlist_items", '"10"'),
        ("watchlist_items", "true"),
        ("export_enabled", "1"),
        ("export_enabled", "null"),
        ("teleport", "true"),
    ],
)
async def test_postgres_refuses_a_value_that_does_not_fit_its_key(
    db_session: AsyncSession, key: str, value: str
) -> None:
    with pytest.raises(IntegrityError, match="ck_plan_entitlement_"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE plan_entitlement SET key = :key, value = CAST(:value AS jsonb) "
                    "WHERE plan_id = (SELECT id FROM plan WHERE slug = 'pro') "
                    "AND key = 'watchlist_items'"
                ),
                {"key": key, "value": value},
            )


async def test_a_user_has_one_live_subscription_at_most(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", status="canceled")
    await subscribe(db_session, user_id, "pro")

    with pytest.raises(IntegrityError, match="uq_subscription_user_live"):
        async with db_session.begin_nested():
            await subscribe(db_session, user_id, "api_pro", status="trialing")


# --- one user's grant ---------------------------------------------------------------


async def test_a_live_subscription_is_the_grant(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", status="canceled")
    await subscribe(
        db_session,
        user_id,
        "pro",
        status="past_due",
        grandfathered="api_hobbyist",
        overrides={"watchlist_items": 50, "mcp_enabled": False},
    )

    assert await grant_for(db_session, user_id) == Grant(
        plan="pro",
        grandfathered="api_hobbyist",
        overrides={"watchlist_items": 50, "mcp_enabled": False},
        ends_at=None,
    )


@pytest.mark.parametrize("status", ["canceled", "unpaid", "incomplete", "paused"])
async def test_a_subscription_not_live_is_no_grant(db_session: AsyncSession, status: str) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", status=status)

    assert await grant_for(db_session, user_id) is None


async def test_a_subscription_past_its_cancel_at_is_no_grant(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", cancel_at=now() - timedelta(minutes=1))

    assert await grant_for(db_session, user_id) is None


# --- the grant on the session ---------------------------------------------------------


def _stored(redis: FakeRedis) -> dict[str, Any]:
    [key] = [key for key in redis.values if key.startswith(SESSION_KEY_PREFIX)]
    stored: dict[str, Any] = json.loads(redis.values[key])
    return stored


async def _plan(client: AsyncClient) -> str:
    response = await client.get("/v1/me")
    assert response.status_code == 200, response.text
    plan: str = response.json()["plan"]["id"]
    return plan


async def test_the_session_keeps_the_grant_rather_than_reading_it_each_request(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)
    assert await _plan(client) == "free"
    assert _stored(session_redis)["granted_at"] is not None

    # Subscribed behind the session's back: it keeps what it read.
    await subscribe(db_session, user_id, "pro")
    assert await _plan(client) == "free"

    # Code that changes a subscription has every session read it again.
    await SessionStore(session_redis).forget_grants(user_id)  # type: ignore[arg-type]
    assert await _plan(client) == "pro"
    assert _stored(session_redis)["grant"]["plan"] == "pro"


async def test_a_stale_grant_is_read_again(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)
    assert await _plan(client) == "free"
    await subscribe(db_session, user_id, "pro")

    [key] = [key for key in session_redis.values if key.startswith(SESSION_KEY_PREFIX)]
    stored = json.loads(session_redis.values[key])
    stale = now() - entitlements.GRANT_EVERY - timedelta(seconds=1)
    session_redis.values[key] = json.dumps({**stored, "granted_at": stale.isoformat()})
    ttl = session_redis.ttls[key]

    assert await _plan(client) == "pro"
    # Rewritten in place: the session ends when it was set to.
    assert session_redis.ttls[key] == ttl


async def test_a_session_from_before_grants_reads_one(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "api_pro")
    await sign_in(client)
    [key] = [key for key in session_redis.values if key.startswith(SESSION_KEY_PREFIX)]
    stored = json.loads(session_redis.values[key])
    session_redis.values[key] = json.dumps(
        {k: stored[k] for k in ("user_id", "remember", "expires_at")}
    )

    assert await _plan(client) == "api_pro"


# --- the watchlist, as the first thing gated ---------------------------------------------


async def _follow_many(client: AsyncClient, session: AsyncSession, n: int) -> list[int]:
    slugs = [f"filer-{i:02}" for i in range(n)]
    await session.execute(insert(Filer), [{"name": slug.upper(), "slug": slug} for slug in slugs])
    statuses = []
    for slug in slugs:
        response = await client.put(
            "/v1/watchlist/items",
            json={"ref": {"kind": "investor", "slug": slug}},
            headers=FRONTEND,
        )
        statuses.append(response.status_code)
    return statuses


async def test_free_follows_ten_things(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    await sign_in(client)

    statuses = await _follow_many(client, db_session, 11)

    assert statuses == [200] * 10 + [422]


async def test_a_plan_of_no_limit_follows_past_free(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro")
    await sign_in(client)

    assert await _follow_many(client, db_session, 11) == [200] * 11
    assert limit(_as("pro"), "watchlist_items") is None
    assert MAX_WATCHLIST_ITEMS == 500
