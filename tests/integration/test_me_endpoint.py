"""``GET /v1/me``: the user, their plan and their entitlements, in one call."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.accounts import make_user, sign_in


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
    assert body["entitlements"] == {"save": True, "watchlist_max_items": 500}


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
