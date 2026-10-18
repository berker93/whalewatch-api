"""``/v1/auth/*``, against a real Postgres: sessions, registration, and passwords.

What matters is what the frontend branches on: the code of each failure (a
wrong password is 403 ``invalid_credentials``, never a 401, which means only
that the session is gone), the cookie's flags, and that a password change or
reset signs out who it should and nobody else.
"""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.passwords import verify_password_sync
from app.accounts.sessions import digest, now
from app.api.rate_limit import Decision
from app.db.models import AppUser, SignupRequest, UserSession
from tests.conftest import make_settings
from tests.integration.accounts import FRONTEND, PASSWORD, Outbox, make_user, sign_in


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --- login and the cookie ---------------------------------------------------------


async def test_login_sets_an_httponly_lax_cookie_and_says_who(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/login",
        json={"email": "  Ada@Example.COM ", "password": PASSWORD},
        headers=FRONTEND,
    )

    assert response.status_code == 200
    assert response.json()["user"] == {"email": "ada@example.com"}
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("ww_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=" in cookie  # remembered by default
    assert "Secure" not in cookie  # only where deployed
    assert response.headers["cache-control"] == "no-store"
    # Only the token's hash is stored.
    token = cookie.split(";")[0].split("=", 1)[1]
    stored = await db_session.scalar(select(UserSession.token_hash))
    assert stored == digest(token) != token


async def test_a_session_not_remembered_ends_with_the_browser_and_in_12_hours(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/login",
        json={"email": "ada@example.com", "password": PASSWORD, "remember": False},
        headers=FRONTEND,
    )

    assert "Max-Age" not in response.headers["set-cookie"]
    expires = await db_session.scalar(select(UserSession.expires_at))
    assert expires is not None
    assert timedelta(hours=11) < expires - now() <= timedelta(hours=12)


@pytest.mark.parametrize(
    ("email", "password"),
    [("ada@example.com", "wrong password"), ("nobody@example.com", PASSWORD)],
    ids=["wrong password", "no such account"],
)
async def test_a_wrong_email_or_password_is_the_same_403(
    client: AsyncClient, db_session: AsyncSession, email: str, password: str
) -> None:
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/login", json={"email": email, "password": password}, headers=FRONTEND
    )

    assert _error(response) == (403, "invalid_credentials")
    assert "set-cookie" not in response.headers


async def test_the_session_endpoint_answers_null_signed_out_and_who_signed_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    assert (await client.get("/v1/auth/session")).json() == {"session": None}

    await sign_in(client)
    body = (await client.get("/v1/auth/session")).json()

    assert body["session"]["user"] == {"email": "ada@example.com"}


async def test_an_ended_session_is_session_expired_and_clears_the_cookie(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await db_session.execute(update(UserSession).values(expires_at=now() - timedelta(seconds=1)))

    response = await client.get("/v1/auth/session")

    assert _error(response) == (401, "session_expired")
    assert "Max-Age=0" in response.headers["set-cookie"]
    # A signed-in route says the same.
    assert _error(await client.get("/v1/account")) == (401, "unauthenticated")


async def test_no_cookie_on_a_signed_in_route_is_unauthenticated(client: AsyncClient) -> None:
    assert _error(await client.get("/v1/account")) == (401, "unauthenticated")


async def test_logout_ends_the_session_and_is_fine_twice(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    first = await client.post("/v1/auth/logout", headers=FRONTEND)
    second = await client.post("/v1/auth/logout", headers=FRONTEND)

    assert (first.status_code, second.status_code) == (204, 204)
    assert await db_session.scalar(select(func.count()).select_from(UserSession)) == 0
    assert (await client.get("/v1/auth/session")).json() == {"session": None}


async def test_a_session_cookie_is_secure_and_shared_where_deployed(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.settings = make_settings(
        environment="production", session_cookie_domain=".whalewatch.com"
    )
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/login",
        json={"email": "ada@example.com", "password": PASSWORD},
        headers=FRONTEND,
    )

    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie
    assert "Domain=.whalewatch.com" in cookie


# --- registration -----------------------------------------------------------------


async def test_while_signups_are_closed_registering_is_recorded_and_starts_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/v1/auth/register",
        json={"name": " Ada ", "email": "Ada@Example.com", "password": PASSWORD},
        headers=FRONTEND,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    assert "set-cookie" not in response.headers
    requests = await db_session.execute(select(SignupRequest.email, SignupRequest.name))
    assert [tuple(row) for row in requests] == [("ada@example.com", "Ada")]
    assert await db_session.scalar(select(func.count()).select_from(AppUser)) == 0


async def test_while_signups_are_open_registering_signs_in(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.settings = make_settings(signups_open=True)

    response = await client.post(
        "/v1/auth/register",
        json={
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "password": PASSWORD,
            "time_zone": "Europe/London",
        },
        headers=FRONTEND,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "authed"
    assert response.json()["session"]["user"] == {"email": "ada@example.com"}
    account = (await client.get("/v1/account")).json()
    assert account["time_zone"] == "Europe/London"
    assert account["member_since"] == account["password_changed_at"]


async def test_registering_a_taken_address_is_email_taken(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.settings = make_settings(signups_open=True)
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/register",
        json={"name": "Imposter", "email": "ADA@example.com", "password": PASSWORD},
        headers=FRONTEND,
    )

    assert _error(response) == (409, "email_taken")


@pytest.mark.parametrize(
    ("body", "code", "field"),
    [
        ({"name": " A ", "email": "ada@example.com", "password": PASSWORD}, "invalid_name", "name"),
        ({"name": "Ada", "email": "ada@example", "password": PASSWORD}, "invalid_email", "email"),
        (
            {"name": "Ada", "email": "ada@example.com", "password": "123456789"},
            "weak_password",
            "password",
        ),
    ],
)
async def test_a_refused_registration_names_its_field(
    client: AsyncClient, body: dict[str, str], code: str, field: str
) -> None:
    response = await client.post("/v1/auth/register", json=body, headers=FRONTEND)

    assert _error(response) == (422, code)
    assert [e["loc"] for e in response.json()["error"]["detail"]["errors"]] == [["body", field]]


async def test_ten_characters_is_enough_as_the_frontend_says(client: AsyncClient) -> None:
    """PASSWORD_MIN_LENGTH in whalewatch-web: the API's rule is the same or looser."""
    response = await client.post(
        "/v1/auth/register",
        json={"name": "Ada", "email": "ada@example.com", "password": "1234567890"},
        headers=FRONTEND,
    )

    assert response.status_code == 200


# --- changing the password --------------------------------------------------------


async def test_changing_the_password_signs_out_every_other_session(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await sign_in(other)

        response = await client.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "battery staple 1999"},
            headers=FRONTEND,
        )

        assert response.status_code == 204
        assert _error(await other.get("/v1/auth/session")) == (401, "session_expired")
    assert (await client.get("/v1/auth/session")).json()["session"] is not None
    account = (await client.get("/v1/account")).json()
    assert account["password_changed_at"] > account["member_since"]
    stored = await db_session.scalar(select(AppUser.password_hash).where(AppUser.id == user_id))
    assert stored is not None and verify_password_sync("battery staple 1999", stored)


@pytest.mark.parametrize(
    ("current", "new", "status", "code"),
    [
        ("wrong password", "battery staple 1999", 403, "invalid_credentials"),
        (PASSWORD, "short", 422, "weak_password"),
        (PASSWORD, PASSWORD, 422, "weak_password"),
    ],
    ids=["wrong current", "too short", "unchanged"],
)
async def test_a_refused_password_change_says_why(
    client: AsyncClient, db_session: AsyncSession, current: str, new: str, status: int, code: str
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await client.post(
        "/v1/auth/password",
        json={"current_password": current, "new_password": new},
        headers=FRONTEND,
    )

    assert _error(response) == (status, code)
    # The session survives a refusal.
    assert (await client.get("/v1/auth/session")).json()["session"] is not None


async def test_changing_the_password_signed_out_is_a_401(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "battery staple 1999"},
        headers=FRONTEND,
    )

    assert _error(response) == (401, "unauthenticated")


# --- resetting it -----------------------------------------------------------------


async def test_a_reset_link_sets_a_new_password_once_and_signs_everyone_out(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await sign_in(client)

    asked = await client.post(
        "/v1/auth/password-reset", json={"email": "ADA@example.com"}, headers=FRONTEND
    )
    token = outbox.token_for("ada@example.com")
    reset = await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "battery staple 1999"},
        headers=FRONTEND,
    )
    again = await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "another one 2000"},
        headers=FRONTEND,
    )

    assert (asked.status_code, reset.status_code) == (204, 204)
    assert _error(again) == (422, "invalid_token")
    assert _error(await client.get("/v1/auth/session")) == (401, "session_expired")
    await sign_in(client, password="battery staple 1999")


async def test_a_reset_for_no_account_answers_the_same_and_sends_nothing(
    client: AsyncClient, outbox: Outbox
) -> None:
    response = await client.post(
        "/v1/auth/password-reset", json={"email": "nobody@example.com"}, headers=FRONTEND
    )

    assert response.status_code == 204
    assert outbox.sent == []


async def test_a_weak_password_does_not_spend_the_reset_link(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await client.post(
        "/v1/auth/password-reset", json={"email": "ada@example.com"}, headers=FRONTEND
    )
    token = outbox.token_for("ada@example.com")

    weak = await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "short"},
        headers=FRONTEND,
    )
    good = await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "battery staple 1999"},
        headers=FRONTEND,
    )

    assert _error(weak) == (422, "weak_password")
    assert good.status_code == 204


# --- the stricter limit -----------------------------------------------------------


class _Refusing:
    limit = 10

    def __init__(self) -> None:
        self.clients: list[str] = []

    async def hit(self, client: str) -> Decision:
        self.clients.append(client)
        return Decision(admitted=False, remaining=0, retry_after=42)


async def test_sign_ins_are_limited_per_address(client: AsyncClient, app: FastAPI) -> None:
    app.state.auth_rate_limiter = limiter = _Refusing()

    response = await client.post(
        "/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}, headers=FRONTEND
    )

    assert _error(response) == (429, "rate_limited")
    assert response.headers["retry-after"] == "42"
    assert limiter.clients == ["127.0.0.1"]
    # Reads are not counted against it.
    assert (await client.get("/v1/auth/session")).status_code == 200
