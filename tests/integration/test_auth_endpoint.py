"""``/v1/auth/*``, against a real Postgres: sessions, registration, and passwords.

What matters is what the frontend branches on: the code of each failure (a
wrong password is 403 ``invalid_credentials``, never a 401, which means only
that the session is gone), the cookie's flags, and that a password change or
reset signs out who it should and nobody else.

Sessions are in a fake Redis here (``session_redis``), one per test; the store's
own commands are tested against a real one in test_session_store.py.
"""

import json
from collections import Counter
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.passwords import DUMMY_HASH, hash_password_sync, verify_password_sync
from app.accounts.sessions import SESSION_KEY_PREFIX, digest, now
from app.api.auth import email_key
from app.api.rate_limit import Decision
from app.db.models import AppUser, SignupRequest
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, PASSWORD, Outbox, make_user, sign_in


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _sessions(redis: FakeRedis) -> dict[str, dict[str, object]]:
    """The live sessions, by key."""
    return {
        key: json.loads(value)
        for key, value in redis.values.items()
        if key.startswith(SESSION_KEY_PREFIX)
    }


# --- login and the cookie ---------------------------------------------------------


async def test_login_sets_an_httponly_lax_cookie_and_says_who(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/login",
        json={"email": "  Ada@Example.COM ", "password": PASSWORD},
        headers=FRONTEND,
    )

    assert response.status_code == 200
    assert response.json()["user"] == {"email": "ada@example.com", "email_verified": True}
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("ww_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=" in cookie  # remembered by default
    assert "Secure" not in cookie  # only where deployed
    assert response.headers["cache-control"] == "no-store"
    # Only the token's hash is stored, in Redis, set to expire with the session.
    token = cookie.split(";")[0].split("=", 1)[1]
    [key] = _sessions(session_redis)
    assert key == SESSION_KEY_PREFIX + digest(token)
    assert token not in json.dumps(session_redis.values)
    assert timedelta(days=29) < timedelta(seconds=session_redis.ttls[key]) <= timedelta(days=30)


async def test_a_session_not_remembered_ends_with_the_browser_and_in_12_hours(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    await make_user(db_session)

    response = await client.post(
        "/v1/auth/login",
        json={"email": "ada@example.com", "password": PASSWORD, "remember": False},
        headers=FRONTEND,
    )

    assert "Max-Age" not in response.headers["set-cookie"]
    [key] = _sessions(session_redis)
    assert timedelta(hours=11) < timedelta(seconds=session_redis.ttls[key]) <= timedelta(hours=12)


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

    assert body["session"]["user"] == {"email": "ada@example.com", "email_verified": True}


async def test_an_ended_session_is_session_expired_and_clears_the_cookie(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    await make_user(db_session)
    await sign_in(client)
    # Expired, as Redis would have it in the second before it drops the key.
    [(key, stored)] = _sessions(session_redis).items()
    stored["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    session_redis.values[key] = json.dumps(stored)

    response = await client.get("/v1/auth/session")

    assert _error(response) == (401, "session_expired")
    assert "Max-Age=0" in response.headers["set-cookie"]
    # A signed-in route says the same.
    assert _error(await client.get("/v1/account")) == (401, "unauthenticated")


async def test_no_cookie_on_a_signed_in_route_is_unauthenticated(client: AsyncClient) -> None:
    assert _error(await client.get("/v1/account")) == (401, "unauthenticated")


async def test_logout_ends_the_session_at_once_and_is_fine_twice(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis, app: FastAPI
) -> None:
    await make_user(db_session)
    await sign_in(client)
    cookie = client.cookies["ww_session"]

    first = await client.post("/v1/auth/logout", headers=FRONTEND)
    second = await client.post("/v1/auth/logout", headers=FRONTEND)

    assert (first.status_code, second.status_code) == (204, 204)
    assert _sessions(session_redis) == {}
    assert session_redis.sets == {}
    assert (await client.get("/v1/auth/session")).json() == {"session": None}
    # A copy of the cookie, kept by someone else, is refused from now on.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies={"ww_session": cookie}
    ) as copied:
        assert _error(await copied.get("/v1/me")) == (401, "session_expired")


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
        json={
            "name": " Ada ",
            "email": "Ada@Example.com",
            "password": PASSWORD,
            "accept_terms": True,
        },
        headers=FRONTEND,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    assert "set-cookie" not in response.headers
    requests = await db_session.execute(select(SignupRequest.email, SignupRequest.name))
    assert [tuple(row) for row in requests] == [("Ada@Example.com", "Ada")]
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
            "accept_terms": True,
        },
        headers=FRONTEND,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "authed"
    assert response.json()["session"]["user"] == {
        "email": "ada@example.com",
        "email_verified": False,
    }
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
        json={
            "name": "Imposter",
            "email": "ADA@example.com",
            "password": PASSWORD,
            "accept_terms": True,
        },
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
        json={
            "name": "Ada",
            "email": "ada@example.com",
            "password": "1234567890",
            "accept_terms": True,
        },
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


# --- the attempt limit ------------------------------------------------------------


class _Counting:
    """The attempt limiter, counting in a dict and never forgetting: what is
    under test is which keys a route counts, not the window, which
    test_rate_limit.py runs against a real Redis."""

    limit = 5
    window_ms = 15 * 60 * 1000

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    async def hit(self, key: str) -> Decision:
        if self.counts[key] >= self.limit:
            return Decision(admitted=False, remaining=0, retry_after=600)
        self.counts[key] += 1
        return Decision(admitted=True, remaining=self.limit - self.counts[key], retry_after=0)


def _from(app: FastAPI, address: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=(address, 50000)), base_url="http://test"
    )


async def _login(client: AsyncClient, email: str, password: str = "wrong password") -> Response:
    return await client.post(
        "/v1/auth/login", json={"email": email, "password": password}, headers=FRONTEND
    )


async def test_five_logins_from_one_address_then_429_whichever_email(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.attempt_limiter = _Counting()
    await make_user(db_session)

    tries = [await _login(client, f"user{n}@example.com") for n in range(5)]
    refused = await _login(client, "ada@example.com", PASSWORD)

    assert [r.status_code for r in tries] == [403] * 5
    assert _error(refused) == (429, "rate_limited")
    assert refused.headers["retry-after"] == "600"
    assert "set-cookie" not in refused.headers
    # Reads are not counted against it.
    assert (await client.get("/v1/auth/session")).status_code == 200


async def test_five_logins_for_one_email_then_429_from_any_address(
    db_session: AsyncSession, app: FastAPI, client: AsyncClient
) -> None:
    """Many addresses against one account get no further than one."""
    app.state.attempt_limiter = _Counting()
    await make_user(db_session)

    for n in range(5):
        async with _from(app, f"203.0.113.{n}") as other:
            assert (await _login(other, "ada@example.com")).status_code == 403
    async with _from(app, "198.51.100.1") as fresh:
        refused = await _login(fresh, "ADA@example.com", PASSWORD)

    assert _error(refused) == (429, "rate_limited")


async def test_an_address_with_no_account_is_counted_the_same(
    client: AsyncClient, app: FastAPI
) -> None:
    """Or the 429 would say which addresses have accounts."""
    app.state.attempt_limiter = limiter = _Counting()

    await _login(client, "nobody@example.com")

    assert limiter.counts == Counter(
        {"login:ip:127.0.0.1": 1, f"login:email:{email_key('nobody@example.com')}": 1}
    )
    # Hashed, and without case, as the citext column compares.
    assert email_key(" Nobody@Example.com") == email_key("nobody@example.com")
    assert "nobody" not in json.dumps(list(limiter.counts))


async def test_each_action_has_its_own_budget(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI, outbox: Outbox
) -> None:
    """Five mistyped passwords still leave a reset to ask for."""
    app.state.attempt_limiter = _Counting()
    await make_user(db_session)
    for _ in range(5):
        await _login(client, "ada@example.com")

    reset = await client.post(
        "/v1/auth/password-reset", json={"email": "ada@example.com"}, headers=FRONTEND
    )

    assert reset.status_code == 204
    assert [m.to for m in outbox.sent] == ["ada@example.com"]


async def test_resets_and_registrations_are_limited_too(client: AsyncClient, app: FastAPI) -> None:
    app.state.attempt_limiter = _Counting()
    app.state.settings = make_settings(signups_open=True)

    resets = [
        await client.post(
            "/v1/auth/password-reset", json={"email": "ada@example.com"}, headers=FRONTEND
        )
        for _ in range(6)
    ]
    registrations = [
        await client.post(
            "/v1/auth/register",
            json={
                "name": "Ada",
                "email": f"ada{n}@example.com",
                "password": PASSWORD,
                "accept_terms": True,
            },
            headers=FRONTEND,
        )
        for n in range(6)
    ]

    assert [r.status_code for r in resets] == [204] * 5 + [429]
    assert [r.status_code for r in registrations] == [200] * 5 + [429]


async def test_a_refused_field_is_not_a_registration_attempt(
    client: AsyncClient, app: FastAPI
) -> None:
    """The form checks the same rules; a typo should not spend the budget."""
    app.state.attempt_limiter = limiter = _Counting()

    await client.post(
        "/v1/auth/register",
        json={"name": "Ada", "email": "ada@example.com", "password": "short", "accept_terms": True},
        headers=FRONTEND,
    )

    assert limiter.counts == Counter()


async def test_a_redis_that_cannot_count_lets_attempts_through(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    class _Down:
        limit, window_ms = 5, 900_000

        async def hit(self, key: str) -> Decision:
            await FakeRedis(down=True).get(key)
            raise AssertionError("unreachable")

    app.state.attempt_limiter = _Down()
    await make_user(db_session)

    assert (await _login(client, "ada@example.com", PASSWORD)).status_code == 200


class _Refusing:
    limit = 10

    def __init__(self) -> None:
        self.clients: list[str] = []

    async def hit(self, client: str) -> Decision:
        self.clients.append(client)
        return Decision(admitted=False, remaining=0, retry_after=42)


async def test_the_emailed_tokens_are_limited_per_address(
    client: AsyncClient, app: FastAPI
) -> None:
    app.state.auth_rate_limiter = limiter = _Refusing()

    response = await client.post(
        "/v1/auth/verify-email", json={"token": "anything"}, headers=FRONTEND
    )

    assert _error(response) == (429, "rate_limited")
    assert response.headers["retry-after"] == "42"
    assert limiter.clients == ["127.0.0.1"]


# --- a login that says nothing about who has an account ---------------------------


async def test_a_login_for_no_account_does_the_same_argon2_work(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timing-safe: one verification against the dummy hash, whose parameters
    are every real hash's, so the 403 takes as long as a wrong password's."""
    await make_user(db_session)
    await make_user(db_session, "gone@example.com")
    await db_session.execute(
        update(AppUser).where(AppUser.email == "gone@example.com").values(deleted_at=now())
    )
    verified: list[str] = []

    async def spy(password: str, stored: str) -> bool:
        verified.append(stored)
        return verify_password_sync(password, stored)

    monkeypatch.setattr("app.api.routers.auth.verify_password", spy)

    wrong = await _login(client, "ada@example.com")
    nobody = await _login(client, "nobody@example.com")
    gone = await _login(client, "gone@example.com", PASSWORD)

    assert _error(wrong) == _error(nobody) == _error(gone) == (403, "invalid_credentials")
    assert (
        wrong.json()["error"]["message"]
        == nobody.json()["error"]["message"]
        == gone.json()["error"]["message"]
    )
    assert len(verified) == 3
    assert verified[0] != DUMMY_HASH
    assert verified[1] == verified[2] == DUMMY_HASH
    assert DUMMY_HASH.split("$")[:4] == verified[0].split("$")[:4]


# --- one account per address, whatever its case ------------------------------------


async def test_an_address_is_one_account_whatever_its_case(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    await make_user(db_session, "Berk@Example.com")

    login = await _login(client, "berk@EXAMPLE.com", PASSWORD)

    assert login.status_code == 200
    # Kept as typed.
    assert login.json()["user"]["email"] == "Berk@Example.com"
    app.state.settings = make_settings(signups_open=True)
    again = await client.post(
        "/v1/auth/register",
        json={
            "name": "Berk",
            "email": "berk@example.com",
            "password": PASSWORD,
            "accept_terms": True,
        },
        headers=FRONTEND,
    )
    assert _error(again) == (409, "email_taken")


async def test_the_database_refuses_a_second_account_by_case(db_session: AsyncSession) -> None:
    await make_user(db_session, "berk@example.com")

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_user(db_session, "BERK@example.com")


async def test_a_deleted_account_signs_nobody_in_and_frees_its_address(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)
    await db_session.execute(update(AppUser).where(AppUser.id == user_id).values(deleted_at=now()))

    assert _error(await client.get("/v1/me")) == (401, "session_expired")
    assert _error(await _login(client, "ada@example.com", PASSWORD)) == (
        403,
        "invalid_credentials",
    )
    await db_session.execute(
        insert(AppUser).values(
            email="ada@example.com",
            name="Ada Again",
            password_hash=hash_password_sync(PASSWORD),
            time_zone="UTC",
        )
    )
    assert (await _login(client, "ada@example.com", PASSWORD)).status_code == 200


# --- when Redis cannot be reached ---------------------------------------------------


async def test_without_redis_a_signed_in_route_is_503_never_401(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    """A 401 would have the frontend sign the user out over an outage."""
    await make_user(db_session)
    await sign_in(client)
    session_redis.down = True

    response = await client.get("/v1/me")

    assert _error(response) == (503, "unavailable")
    assert response.headers["retry-after"] == "5"
    assert _error(await _login(client, "ada@example.com", PASSWORD)) == (503, "unavailable")


async def test_without_redis_a_route_that_works_signed_out_still_works(
    client: AsyncClient, db_session: AsyncSession, session_redis: FakeRedis
) -> None:
    await make_user(db_session)
    await sign_in(client)
    session_redis.down = True

    response = await client.get("/v1/timeline", params={"from": "2026-01-01", "to": "2026-01-31"})

    assert response.status_code == 200


# --- last seen ----------------------------------------------------------------------


async def test_a_signed_in_request_marks_the_account_seen_every_few_minutes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)
    seen_at_login = await db_session.scalar(
        select(AppUser.last_seen_at).where(AppUser.id == user_id)
    )
    assert seen_at_login is not None

    await client.get("/v1/me")
    unchanged = await db_session.scalar(select(AppUser.last_seen_at).where(AppUser.id == user_id))
    await db_session.execute(
        update(AppUser)
        .where(AppUser.id == user_id)
        .values(last_seen_at=now() - timedelta(minutes=6))
    )
    db_session.expire_all()
    await client.get("/v1/me")
    moved = await db_session.scalar(select(AppUser.last_seen_at).where(AppUser.id == user_id))

    assert unchanged == seen_at_login
    assert moved is not None and now() - moved < timedelta(seconds=5)


# --- email verification -------------------------------------------------------------


async def _register(client: AsyncClient, app: FastAPI, email: str = "ada@example.com") -> Response:
    app.state.settings = make_settings(signups_open=True)
    response = await client.post(
        "/v1/auth/register",
        json={"name": "Ada Lovelace", "email": email, "password": PASSWORD, "accept_terms": True},
        headers=FRONTEND,
    )
    assert response.status_code == 200, response.text
    return response


async def _follow(client: AsyncClient) -> Response:
    return await client.put(
        "/v1/watchlist/items",
        json={"ref": {"kind": "investor", "slug": "nobody"}},
        headers=FRONTEND,
    )


async def test_a_new_account_reads_but_saves_nothing_until_verified(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI, outbox: Outbox
) -> None:
    await _register(client, app)

    [message] = outbox.sent
    assert message.to == "ada@example.com"
    assert "http://app.localhost:5173/verify-email?token=" in message.body
    assert (await client.get("/v1/watchlist")).status_code == 200
    assert _error(await _follow(client)) == (403, "email_unverified")
    assert _error(
        await client.request(
            "DELETE",
            "/v1/watchlist/items",
            json={"ref": {"kind": "investor", "slug": "nobody"}},
            headers=FRONTEND,
        )
    ) == (403, "email_unverified")

    verified = await client.post(
        "/v1/auth/verify-email",
        json={"token": outbox.token_for("ada@example.com")},
        headers=FRONTEND,
    )

    assert verified.status_code == 204
    # Past the gate: an unknown investor is the watchlist's own 404.
    assert _error(await _follow(client)) == (404, "not_found")
    assert (await client.get("/v1/auth/session")).json()["session"]["user"]["email_verified"]


async def test_a_verification_link_works_once(
    client: AsyncClient, app: FastAPI, outbox: Outbox
) -> None:
    await _register(client, app)
    token = outbox.token_for("ada@example.com")

    first = await client.post("/v1/auth/verify-email", json={"token": token}, headers=FRONTEND)
    second = await client.post("/v1/auth/verify-email", json={"token": token}, headers=FRONTEND)

    assert first.status_code == 204
    assert _error(second) == (422, "invalid_token")


async def test_a_new_link_replaces_the_last_and_none_is_sent_once_verified(
    client: AsyncClient, app: FastAPI, outbox: Outbox
) -> None:
    await _register(client, app)
    old = outbox.token_for("ada@example.com")
    outbox.sent.clear()

    resent = await client.post("/v1/auth/verify-email/resend", headers=FRONTEND)
    new = outbox.token_for("ada@example.com")

    assert resent.status_code == 204
    assert _error(
        await client.post("/v1/auth/verify-email", json={"token": old}, headers=FRONTEND)
    ) == (422, "invalid_token")
    assert (
        await client.post("/v1/auth/verify-email", json={"token": new}, headers=FRONTEND)
    ).status_code == 204
    outbox.sent.clear()
    assert (await client.post("/v1/auth/verify-email/resend", headers=FRONTEND)).status_code == 204
    assert outbox.sent == []


async def test_a_reset_link_verifies_the_address_it_reached(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    user_id = await make_user(db_session, verified=False)
    await client.post(
        "/v1/auth/password-reset", json={"email": "ada@example.com"}, headers=FRONTEND
    )

    await client.post(
        "/v1/auth/password-reset/confirm",
        json={"token": outbox.token_for("ada@example.com"), "new_password": "battery staple 1999"},
        headers=FRONTEND,
    )

    db_session.expire_all()
    verified_at = await db_session.scalar(
        select(AppUser.email_verified_at).where(AppUser.id == user_id)
    )
    assert verified_at is not None


async def test_an_unverified_account_still_signs_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, verified=False)

    body = await sign_in(client)

    assert body["user"] == {"email": "ada@example.com", "email_verified": False}
