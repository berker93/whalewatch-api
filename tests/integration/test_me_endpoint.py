"""``GET /v1/me``: the user, their plan and their entitlements, in one call."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.accounts import make_user, sign_in, subscribe


async def test_me_is_the_user_their_plan_and_what_it_allows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, "Ada@Example.com", time_zone="Europe/London")
    await sign_in(client, "ada@example.com")

    response = await client.get("/v1/me")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    user = body["user"]
    assert {k: user[k] for k in ("email", "name", "time_zone", "email_verified")} == {
        "email": "Ada@Example.com",
        "name": "Ada Lovelace",
        "time_zone": "Europe/London",
        "email_verified": True,
    }
    assert user["email_verified_at"] is not None
    assert user["created_at"] is not None
    assert user["last_seen_at"] is not None
    assert body["plan"] == {"id": "free", "name": "Free"}
    assert body["entitlements"] == {
        "save": True,
        "history_quarters": 4,
        "insider_months": 6,
        "watchlist_items": 10,
        "screener_saved_filters": 3,
        "api_calls_month": 0,
        "mcp_calls_month": 0,
        "export_enabled": False,
        "alerts_instant": False,
        "mcp_enabled": False,
    }


async def test_a_subscriber_sees_their_plan_and_its_terms(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    await subscribe(
        db_session, user_id, "pro", grandfathered="api_hobbyist", overrides={"history_quarters": 40}
    )
    await sign_in(client)

    body = (await client.get("/v1/me")).json()

    # Billed for Pro; on the API Hobbyist terms promised; 40 quarters granted alone.
    assert body["plan"] == {"id": "pro", "name": "Pro"}
    assert body["entitlements"]["history_quarters"] == 40
    assert body["entitlements"]["api_calls_month"] == 10_000
    assert body["entitlements"]["mcp_enabled"] is True
    assert body["entitlements"]["watchlist_items"] is None


async def test_an_unverified_account_is_entitled_to_read_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, verified=False)
    await sign_in(client)

    body = (await client.get("/v1/me")).json()

    assert body["user"]["email_verified"] is False
    assert body["user"]["email_verified_at"] is None
    assert body["entitlements"]["save"] is False


async def test_me_signed_out_is_a_401(client: AsyncClient) -> None:
    response = await client.get("/v1/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
